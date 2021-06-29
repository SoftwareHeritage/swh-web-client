# Copyright (C) 2020-2021  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

import os
from typing import Any, Dict, List

# WARNING: do not import unnecessary things here to keep cli startup time under
# control
import click
from click.core import Context

from swh.auth.cli import auth as auth_cli
from swh.auth.cli import generate_token as auth_generate_token
from swh.auth.cli import revoke_token as auth_revoke_token
from swh.core.cli import swh as swh_cli_group

CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])

# TODO (T1410): All generic config code should reside in swh.core.config
DEFAULT_CONFIG_PATH = os.environ.get(
    "SWH_CONFIG_FILE", os.path.join(click.get_app_dir("swh"), "global.yml")
)

DEFAULT_CONFIG: Dict[str, Any] = {
    "api_url": "https://archive.softwareheritage.org/api/1",
    "bearer_token": None,
}


@swh_cli_group.group(name="web", context_settings=CONTEXT_SETTINGS)
@click.option(
    "-C",
    "--config-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    help=f"Configuration file (default: {DEFAULT_CONFIG_PATH})",
)
@click.pass_context
def web(ctx: Context, config_file: str):
    """Software Heritage web client"""

    import logging

    from swh.core import config
    from swh.web.client.client import WebAPIClient

    if not config_file:
        config_file = DEFAULT_CONFIG_PATH

    try:
        conf = config.read_raw_config(config.config_basepath(config_file))
        if not conf:
            raise ValueError(f"Cannot parse configuration file: {config_file}")

        # TODO: Determine what the following conditional is for
        if config_file == DEFAULT_CONFIG_PATH:
            try:
                conf = conf["swh"]["web"]["client"]
            except KeyError:
                pass

        # recursive merge not done by config.read
        conf = config.merge_configs(DEFAULT_CONFIG, conf)
    except Exception:
        logging.warning(
            "Using default configuration (cannot load custom one)", exc_info=True
        )
        conf = DEFAULT_CONFIG

    ctx.ensure_object(dict)
    ctx.obj["client"] = WebAPIClient(conf["api_url"], conf["bearer_token"])


@web.command(name="search")
@click.argument(
    "query", required=True, nargs=-1, metavar="KEYWORD...",
)
@click.option(
    "--limit",
    "limit",
    type=int,
    default=10,
    show_default=True,
    help="maximum number of results to show",
)
@click.option(
    "--only-visited",
    is_flag=True,
    show_default=True,
    help="if true, only return origins with at least one visit by Software heritage",
)
@click.option(
    "--url-encode/--no-url-encode",
    default=False,
    show_default=True,
    help="if true, escape origin URLs in results with percent encoding (RFC 3986)",
)
@click.pass_context
def search(
    ctx: Context, query: List[str], limit: int, only_visited: bool, url_encode: bool,
):
    """Search a query (as a list of keywords) into the Software Heritage
    archive.

    The search results are printed to CSV format, one result per line, using a
    tabulation as the field delimiter.
    """

    import logging
    import sys
    import urllib.parse

    import requests

    client = ctx.obj["client"]
    keywords = " ".join(query)
    try:
        results = client.origin_search(keywords, limit, only_visited)
        for result in results:
            if url_encode:
                result["url"] = urllib.parse.quote_plus(result["url"])

            print("\t".join(result.values()))
    except requests.HTTPError as err:
        logging.error("Could not retrieve search results: %s", err)
    except (BrokenPipeError, IOError):
        # Get rid of the BrokenPipeError message
        sys.stderr.close()


@web.group(name="save", context_settings=CONTEXT_SETTINGS)
@click.pass_context
def savecodenow(ctx: Context,):
    """Subcommand to interact from the cli with the save code now feature

    """
    pass


@savecodenow.command("submit-request")
@click.option("--delimiter", "-d", default=",")
@click.pass_context
def submit_request(ctx, delimiter: str) -> None:
    """Submit new save code now request through cli pipe. The expected format of the request
    if one csv row ``<visit_type>,<origin>``.

    Example:

        cat list-origins | swh web save submit-request

        echo svn;https://svn-url\ngit;https://git-url | swh web save \
            submit-request --delimiter ';'

    Prints:
        The output of save code now requests as json output.

    """
    import json
    import logging
    import sys

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    client = ctx.obj["client"]

    processed_origins = []
    for origin in sys.stdin:
        visit_type, origin = origin.rstrip().split(delimiter)

        try:
            saved_origin = client.origin_save(visit_type, origin)
            logging.info("Submitted origin (%s, %s)", visit_type, origin)
            processed_origins.append(saved_origin)
        except Exception as e:
            logging.warning(
                "Issue for origin (%s, %s)\n%s", origin, visit_type, e,
            )
    logging.debug("Origin saved: %s", len(processed_origins))
    print(json.dumps(processed_origins))


def _forward_context(ctx: Context, *args, **kwargs):
    ctx.forward(*args, **kwargs)


@web.group(name="auth", context_settings=CONTEXT_SETTINGS, deprecated=True)
@click.pass_context
def auth(ctx: Context):
    """
    Authenticate Software Heritage users with OpenID Connect.

    This CLI tool eases the retrieval of a bearer token to authenticate
    a user querying the Software Heritage Web API.

    That command group is deprecated, use ``swh auth`` instead.
    """
    _forward_context(ctx, auth_cli, client_id="swh-web")


@auth.command("generate-token", deprecated=True)
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
    _forward_context(ctx, auth_generate_token, username=username)


@auth.command("revoke-token", deprecated=True)
@click.argument("token")
@click.pass_context
def revoke_token(ctx: Context, token: str):
    """
    Revoke a bearer token used for Web API authentication.

    Use TOKEN to logout from an offline OpenID Connect session.

    The token is definitely revoked after that operation.
    """
    _forward_context(ctx, auth_revoke_token, token=token)
