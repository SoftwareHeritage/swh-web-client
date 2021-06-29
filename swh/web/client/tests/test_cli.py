# Copyright (C) 2020-2021  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

import json
import os

from click.testing import CliRunner

from swh.web.client.cli import auth_cli, auth_generate_token, auth_revoke_token, web

runner = CliRunner()


def test_auth_generate_token(mocker):
    forward_context = mocker.patch("swh.web.client.cli._forward_context")
    runner.invoke(web, ["auth", "generate-token", "username"])
    assert forward_context.call_count == 2
    ctx = forward_context.call_args_list[0][0][0]
    ctx2 = forward_context.call_args_list[1][0][0]
    forward_context.assert_has_calls(
        [
            mocker.call(ctx, auth_cli, client_id="swh-web"),
            mocker.call(ctx2, auth_generate_token, username="username"),
        ]
    )


def test_auth_revoke_token(mocker):
    forward_context = mocker.patch("swh.web.client.cli._forward_context")
    runner.invoke(web, ["auth", "revoke-token", "token"])
    assert forward_context.call_count == 2
    ctx = forward_context.call_args_list[0][0][0]
    ctx2 = forward_context.call_args_list[1][0][0]
    forward_context.assert_has_calls(
        [
            mocker.call(ctx, auth_cli, client_id="swh-web"),
            mocker.call(ctx2, auth_revoke_token, token="token"),
        ]
    )


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
