# Copyright (C) 2020  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

# WARNING: do not import unnecessary things here to keep cli startup time under
# control
import click
from click.core import Context

from swh.core.cli import swh as swh_cli_group

CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])


@swh_cli_group.group(name="auth", context_settings=CONTEXT_SETTINGS)
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

    This CLI tool eases the retrieval of a bearer token to authenticate
    a user querying the Software Heritage Web API.
    """
    from swh.web.client.auth import OpenIDConnectSession

    ctx.ensure_object(dict)
    ctx.obj["oidc_session"] = OpenIDConnectSession(
        oidc_server_url, realm_name, client_id
    )


@auth.command("generate-token")
@click.argument("username")
@click.pass_context
def generate_token(ctx: Context, username: str):
    """
    Generate a new bearer token for Web API authentication.

    Login with USERNAME, create a new OpenID Connect session and get
    bearer token.

    User will be prompted for his password and token will be printed
    to standard output.

    The created OpenID Connect session is an offline one so the provided
    token has a much longer expiration time than classical OIDC
    sessions (usually several dozens of days).
    """
    from getpass import getpass

    password = getpass()

    oidc_info = ctx.obj["oidc_session"].login(username, password)
    if "refresh_token" in oidc_info:
        print(oidc_info["refresh_token"])
    else:
        print(oidc_info)


@auth.command("login", deprecated=True)
@click.argument("username")
@click.pass_context
def login(ctx: Context, username: str):
    """
    Alias for 'generate-token'
    """
    ctx.forward(generate_token)


@auth.command("revoke-token")
@click.argument("token")
@click.pass_context
def revoke_token(ctx: Context, token: str):
    """
    Revoke a bearer token used for Web API authentication.

    Use TOKEN to logout from an offline OpenID Connect session.

    The token is definitely revoked after that operation.
    """
    ctx.obj["oidc_session"].logout(token)
    print("Token successfully revoked.")


@auth.command("logout", deprecated=True)
@click.argument("token")
@click.pass_context
def logout(ctx: Context, token: str):
    """
    Alias for 'revoke-token'
    """
    ctx.forward(revoke_token)
