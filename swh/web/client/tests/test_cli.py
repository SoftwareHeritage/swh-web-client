# Copyright (C) 2020  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

from click.testing import CliRunner

from swh.web.client.cli import auth

runner = CliRunner()

oidc_profile = {
    "access_token": "some-access-token",
    "expires_in": 600,
    "refresh_expires_in": 0,
    "refresh_token": "some-refresh-token",
    "token_type": "bearer",
    "session_state": "some-state",
    "scope": "openid email profile offline_access",
}


def test_auth_login(mocker):
    mock_getpass = mocker.patch("getpass.getpass")
    mock_getpass.return_value = "password"
    mock_oidc_session = mocker.patch("swh.web.client.auth.OpenIDConnectSession")
    mock_login = mock_oidc_session.return_value.login
    mock_login.return_value = oidc_profile

    result = runner.invoke(auth, ["login", "username"], input="password\n")
    assert result.exit_code == 0
    assert result.output[:-1] == oidc_profile["refresh_token"]

    mock_login.side_effect = Exception("Auth error")

    result = runner.invoke(auth, ["login", "username"], input="password\n")
    assert result.exit_code == 1


def test_auth_logout(mocker):

    mock_oidc_session = mocker.patch("swh.web.client.auth.OpenIDConnectSession")
    mock_logout = mock_oidc_session.return_value.logout

    result = runner.invoke(auth, ["logout", oidc_profile["refresh_token"]])
    assert result.exit_code == 0

    mock_logout.side_effect = Exception("Auth error")
    result = runner.invoke(auth, ["logout", oidc_profile["refresh_token"]])
    assert result.exit_code == 1
