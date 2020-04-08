# Copyright (C) 2020  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

from getpass import getpass
import json

import click
from click.core import Context

from swh.web.client.auth import OpenIDConnectSession

CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])


def _output_json(obj):
    print(json.dumps(obj, indent=4, sort_keys=True))


@click.group(name="auth", context_settings=CONTEXT_SETTINGS)
@click.option(
    "--oidc-server-url",
    "oidc_server_url",
    default="https://auth.softwareheritage.org/auth/",
    help=(
        "URL of OpenID Connect server (default to "
        '"https://auth.softwareheritage.org/auth/")'
    ),
)
@click.option(
    "--realm-name",
    "realm_name",
    default="SoftwareHeritage",
    help=(
        "Name of the OpenID Connect authentication realm "
        '(default to "SoftwareHeritage")'
    ),
)
@click.option(
    "--client-id",
    "client_id",
    default="swh-web",
    help=("OpenID Connect client identifier in the realm " '(default to "swh-web")'),
)
@click.pass_context
def auth(ctx: Context, oidc_server_url: str, realm_name: str, client_id: str):
    """
    Authenticate Software Heritage users with OpenID Connect.

    This CLI tool eases the retrieval of bearer tokens to authenticate
    a user querying the Software Heritage Web API.
    """
    ctx.ensure_object(dict)
    ctx.obj["oidc_session"] = OpenIDConnectSession(
        oidc_server_url, realm_name, client_id
    )


@auth.command("login")
@click.argument("username")
@click.pass_context
def login(ctx: Context, username: str):
    """
    Login and create new offline OpenID Connect session.

    Login with USERNAME, create a new OpenID Connect session and get
    access and refresh tokens.

    User will be prompted for his password and tokens will be printed in
    JSON format to standard output.

    When its access token has expired, user can request a new one using the
    session-refresh command of that CLI tool without having to authenticate
    using a password again.

    The created OpenID Connect session is an offline one so the provided
    refresh token has a much longer expiration time than classical OIDC
    sessions (usually several dozens of days).
    """
    password = getpass()

    oidc_profile = ctx.obj["oidc_session"].login(username, password)
    _output_json(oidc_profile)


@auth.command("refresh")
@click.argument("refresh_token")
@click.pass_context
def refresh(ctx: Context, refresh_token: str):
    """
    Refresh an offline OpenID Connect session.

    Get a new access token from REFRESH_TOKEN when previous one expired.

    New access token will be printed in JSON format to standard output.
    """
    oidc_profile = ctx.obj["oidc_session"].refresh(refresh_token)
    if "access_token" in oidc_profile:
        _output_json(oidc_profile["access_token"])
    else:
        # print oidc error
        _output_json(oidc_profile)


@auth.command("logout")
@click.argument("refresh_token")
@click.pass_context
def logout(ctx: Context, refresh_token: str):
    """
    Logout from an offline OpenID Connect session.

    Use REFRESH_TOKEN to logout from an offline OpenID Connect session.

    Access and refresh tokens are no more usable after that operation.
    """
    ctx.obj["oidc_session"].logout(refresh_token)
    print("Successfully logged out from OpenID Connect session")
