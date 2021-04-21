# Copyright (C) 2020-2021  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

import json
import os

from click.testing import CliRunner

from swh.web.client.cli import auth, web

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


def test_auth_generate_token(mocker):
    mock_getpass = mocker.patch("getpass.getpass")
    mock_getpass.return_value = "password"
    mock_oidc_session = mocker.patch("swh.web.client.auth.OpenIDConnectSession")
    mock_login = mock_oidc_session.return_value.login
    mock_login.return_value = oidc_profile

    for command in ("generate-token", "login"):
        mock_login.side_effect = None
        result = runner.invoke(auth, [command, "username"], input="password\n")
        assert result.exit_code == 0
        assert oidc_profile["refresh_token"] in result.output

        mock_login.side_effect = Exception("Auth error")

        result = runner.invoke(auth, [command, "username"], input="password\n")
        assert result.exit_code == 1


def test_auth_revoke_token(mocker):

    mock_oidc_session = mocker.patch("swh.web.client.auth.OpenIDConnectSession")
    mock_logout = mock_oidc_session.return_value.logout

    for command in ("revoke-token", "logout"):
        mock_logout.side_effect = None
        result = runner.invoke(auth, [command, oidc_profile["refresh_token"]])
        assert result.exit_code == 0

        mock_logout.side_effect = Exception("Auth error")
        result = runner.invoke(auth, [command, oidc_profile["refresh_token"]])
        assert result.exit_code == 1


def test_save_code_now_through_cli(mocker, web_api_mock, tmp_path, cli_config_path):
    """Trigger save code now from the cli creates new save code now requests"""
    origins = [
        ("git", "https://gitlab.org/gazelle/itest"),
        ("git", "https://git.renater.fr/anonscm/git/6po/6po.git"),
        ("git", "https://github.com/colobot/colobot"),
        # this will be rejected
        ("tig", "invalid-and-refusing-to-save-this"),
    ]
    origins_csv = "\n".join(map(lambda t: ",".join(t), origins))
    origins_csv = f"{origins_csv}\n"

    temp_file = os.path.join(tmp_path, "tmp.csv")
    with open(temp_file, "w") as f:
        f.write(origins_csv)

    with open(temp_file, "r") as f:
        result = runner.invoke(
            web,
            ["--config-file", cli_config_path, "save", "submit-request"],
            input=f,
            catch_exceptions=False,
        )

    assert result.exit_code == 0, f"Unexpected output: {result.output}"
    actual_save_requests = json.loads(result.output.strip())
    assert len(actual_save_requests) == 3

    expected_save_requests = [
        {
            "origin_url": "https://gitlab.org/gazelle/itest",
            "save_request_date": "2021-04-20T11:34:38.752929+00:00",
            "save_request_status": "accepted",
            "save_task_status": "not yet scheduled",
            "visit_date": None,
            "visit_type": "git",
        },
        {
            "origin_url": "https://git.renater.fr/anonscm/git/6po/6po.git",
            "save_request_date": "2021-04-20T11:34:40.115226+00:00",
            "save_request_status": "accepted",
            "save_task_status": "not yet scheduled",
            "visit_date": None,
            "visit_type": "git",
        },
        {
            "origin_url": "https://github.com/colobot/colobot",
            "save_request_date": "2021-04-20T11:40:47.667492+00:00",
            "save_request_status": "accepted",
            "save_task_status": "not yet scheduled",
            "visit_date": None,
            "visit_type": "git",
        },
    ]
    for actual_save_request in actual_save_requests:
        assert actual_save_request in expected_save_requests
