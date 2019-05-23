"""CILogon OAuthAuthenticator for JupyterHub

Uses OAuth 2.0 with cilogon.org (override with CILOGON_HOST)

Caveats:

- For user whitelist/admin purposes, username will be the ePPN by default.
  This is typically an email address and may not work as a Unix userid.
  Normalization may be required to turn the JupyterHub username into a Unix username.
- Default username_claim of ePPN does not work for all providers,
  e.g. generic OAuth such as Google.
  Use `c.CILogonOAuthenticator.username_claim = 'email'` to use
  email instead of ePPN as the JupyterHub username.
"""


import json
import os
import re

from tornado.auth import OAuth2Mixin
from tornado import gen, web

from tornado.httputil import url_concat
from tornado.httpclient import HTTPRequest, AsyncHTTPClient

from traitlets import Unicode, List, Bool, validate

from jupyterhub.auth import LocalAuthenticator

from .oauth2 import OAuthLoginHandler, OAuthenticator

CILOGON_HOST = os.environ.get('CILOGON_HOST') or 'cilogon.org'


class CILogonMixin(OAuth2Mixin):
    _OAUTH_AUTHORIZE_URL = "https://%s/authorize" % CILOGON_HOST
    _OAUTH_TOKEN_URL = "https://%s/oauth2/token" % CILOGON_HOST


class CILogonLoginHandler(OAuthLoginHandler, CILogonMixin):
    """See http://www.cilogon.org/oidc for general information."""

    def authorize_redirect(self, *args, **kwargs):
        """Add idp, skin to redirect params"""
        extra_params = kwargs.setdefault('extra_params', {})
        if self.authenticator.idp:
            extra_params["selected_idp"] = self.authenticator.idp
        if self.authenticator.skin:
            extra_params["skin"] = self.authenticator.skin

        return super().authorize_redirect(*args, **kwargs)


class CILogonOAuthenticator(OAuthenticator):
    login_service = "CILogon"

    client_id_env = 'CILOGON_CLIENT_ID'
    client_secret_env = 'CILOGON_CLIENT_SECRET'
    login_handler = CILogonLoginHandler

    scope = List(Unicode(), default_value=['openid', 'email', 'org.cilogon.userinfo'],
                 config=True,
                 help="""The OAuth scopes to request.

        See cilogon_scope.md for details.
        At least 'openid' is required.
        """,
                 )

    @validate('scope')
    def _validate_scope(self, proposal):
        """ensure openid is requested"""
        if 'openid' not in proposal.value:
            return ['openid'] + proposal.value
        return proposal.value

    idp_whitelist = List(
        config=True,
        help="""A list of IDP which can be stripped from the username after the @ sign.""",
    )
    strip_idp_domain = Bool(
        False,
        config=True,
        help="""Remove the IDP domain from the username. Note that only domains which
             appear in the `idp_whitelist` will be stripped.""",
    )
    idp = Unicode(
        config=True,
        help="""The `idp` attribute is the SAML Entity ID of the user's selected
            identity provider.

            See https://cilogon.org/include/idplist.xml for the list of identity
            providers supported by CILogon.
        """,
    )
    skin = Unicode(
        config=True,
        help="""The `skin` attribute is the name of the custom CILogon interface skin
            for your application.

            Contact help@cilogon.org to request a custom skin.
        """,
    )
    username_claim = Unicode(
        "eppn",
        config=True,
        help="""The claim in the userinfo response from which to get the JupyterHub username

            Examples include: eppn, email

            What keys are available will depend on the scopes requested.

            See http://www.cilogon.org/oidc for details.
        """,
    )

    additional_username_claims = List(
        config=True,
        help="""Additional claims to check if the username_claim fails.

        This is useful for linked identities where not all of them return
        the primary username_claim.
        """
    )

    @gen.coroutine
    def authenticate(self, handler, data=None):
        """We set up auth_state based on additional CILogon info if we
        receive it.
        """
        code = handler.get_argument("code")
        # TODO: Configure the curl_httpclient for tornado
        http_client = AsyncHTTPClient()

        # Exchange the OAuth code for a CILogon Access Token
        # See: http://www.cilogon.org/oidc
        headers = {
            "Accept": "application/json",
            "User-Agent": "JupyterHub",
        }

        params = dict(
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uri=self.oauth_callback_url,
            code=code,
            grant_type='authorization_code',
        )

        url = url_concat("https://%s/oauth2/token" % CILOGON_HOST, params)

        req = HTTPRequest(url,
                          headers=headers,
                          method="POST",
                          body=''
                          )

        resp = yield http_client.fetch(req)
        token_response = json.loads(resp.body.decode('utf8', 'replace'))
        access_token = token_response['access_token']
        self.log.info("Access token acquired.")
        # Determine who the logged in user is
        params = dict(access_token=access_token)
        req = HTTPRequest(url_concat("https://%s/oauth2/userinfo" %
                                     CILOGON_HOST, params),
                          headers=headers
                          )
        resp = yield http_client.fetch(req)
        resp_json = json.loads(resp.body.decode('utf8', 'replace'))

        claimlist = [self.username_claim]
        if self.additional_username_claims:
            claimlist.extend(self.additional_username_claims)

        for claim in claimlist:
            username = resp_json.get(claim)
            if username:
                break
        if not username:
            if len(claimlist) < 2:
                self.log.error("Username claim %s not found in response: %s",
                               self.username_claim, sorted(resp_json.keys())
                               )
            else:
                self.log.error("No username claim from %r in response: %s",
                               claimlist, sorted(resp_json.keys()))
            raise web.HTTPError(500, "Failed to get username from CILogon")

        if self.idp_whitelist:
            gotten_name, gotten_idp = username.split('@')
            if gotten_idp not in self.idp_whitelist:
                self.log.error(
                    "Trying to login from not whitelisted domain %s", gotten_idp)
                raise web.HTTPError(
                    500, "Trying to login from not whitelisted domain")
            if len(self.idp_whitelist) == 1 and self.strip_idp_domain:
                username = gotten_name
        userdict = {"name": username}
        # Now we set up auth_state
        userdict["auth_state"] = auth_state = {}
        # Save the token response and full CILogon reply in auth state
        # These can be used for user provisioning
        #  in the Lab/Notebook environment.
        auth_state['token_response'] = token_response
        # store the whole user model in auth_state.cilogon_user
        # keep access_token as well, in case anyone was relying on it
        auth_state['access_token'] = access_token
        auth_state['cilogon_user'] = resp_json
        return userdict


class LocalCILogonOAuthenticator(LocalAuthenticator, CILogonOAuthenticator):

    """A version that mixes in local system user creation"""
    pass

class LocalCILogonMapOAuthenticator(LocalCILogonOAuthenticator):

        mapfile = Unicode(
                "mapfile",
                config=True,
                help="""The location of the map file, containing mappings between OAuth usernames and local UNIX usernames.""",
        )

        full_names = Bool(
                False,
                config=True,
                help="""
                If you are including the full username string returned by CILogon in your mapfile, set this to True.
                Otherwise, the username string will be shortened by removing all text matching the regular expression
                defined in name_regex
                """,
        )

        name_regex = Unicode(
                '@.*$',
                config=True,
                help="""

                If full_names is False, the username string returned by CILogon will by shorteded by removing text matching
                this regular expression.
                """,
        )


        prev_map_mtime = 0
        username_map = dict()

        def normalize_username(self, username):
                """Overrides method from JupyterHub's Authenticator class.  Builds username_map from a map file.

                To allow updating the map without restarting the Hub, it keeps track of the file mtime.  If the
                map file has been modified, it will rebuild username_map

                The username is converted to lowercase and, if full_names is set to False, text matching name_regex
                will be removed.
                """

                mtime = os.stat(self.mapfile).st_mtime

                if mtime > self.prev_map_mtime:
                        self.prev_map_mtime = mtime
                        with open(self.mapfile, 'r') as m:
                                for line in m:
                                        names = line.split()
                                        self.username_map[names[0].lower()] = names[1].lower()

                if not self.full_names:
                        username = re.sub(self.name_regex, '', username.lower())
                else:
                        username = username.lower()
                username = self.username_map.get(username, username)
                return username
