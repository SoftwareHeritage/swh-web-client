# Copyright (C) 2020  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

from typing import Any, Dict
from urllib.parse import urljoin

import requests

SWH_OIDC_SERVER_URL = "https://auth.softwareheritage.org/auth/"
SWH_REALM_NAME = "SoftwareHeritage"
SWH_WEB_CLIENT_ID = "swh-web"


class AuthenticationError(Exception):
    """Authentication related error.

    Example: A bearer token has been revoked.

    """

    pass


class OpenIDConnectSession:
    """
    Simple class wrapping requests sent to an OpenID Connect server.

    Args:
        oidc_server_url: URL of OpenID Connect server
        realm_name: name of the OpenID Connect authentication realm
        client_id: OpenID Connect client identifier in the realm
    """

    def __init__(
        self,
        oidc_server_url: str = SWH_OIDC_SERVER_URL,
        realm_name: str = SWH_REALM_NAME,
        client_id: str = SWH_WEB_CLIENT_ID,
    ):
        realm_url = urljoin(oidc_server_url, f"realms/{realm_name}/")
        self.client_id = client_id
        self.token_url = urljoin(realm_url, "protocol/openid-connect/token/")
        self.logout_url = urljoin(realm_url, "protocol/openid-connect/logout/")

    def login(self, username: str, password: str) -> Dict[str, Any]:
        """
        Login and create new offline OpenID Connect session.

        Args:
            username: an existing username in the realm
            password: password associated to username

        Returns:
            The OpenID Connect session info
        """
        return requests.post(
            url=self.token_url,
            data={
                "grant_type": "password",
                "client_id": self.client_id,
                "scope": "openid offline_access",
                "username": username,
                "password": password,
            },
        ).json()

    def logout(self, token: str):
        """
        Logout from an offline OpenID Connect session and invalidate
        previously emitted tokens.

        Args:
            token: a bearer token retrieved after login
        """
        requests.post(
            url=self.logout_url,
            data={
                "client_id": self.client_id,
                "scope": "openid",
                "refresh_token": token,
            },
        )
