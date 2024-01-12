# Copyright (C) 2020-2021  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

import os

import pytest
import yaml

from swh.web.client.client import KNOWN_QUERY_LIMIT, WebAPIClient

from .api_data import API_DATA, API_URL
from .api_data_static import API_DATA_STATIC, KNOWN_SWHIDS


@pytest.fixture
def web_api_mock(requests_mock):
    # monkey patch URLs that require a special response headers
    for api_call, data in API_DATA.items():
        headers = {}
        if api_call == "snapshot/cabcc7d7bf639bbe1cc3b41989e1806618dd5764/":
            # to make the client init and follow pagination
            headers = {
                "Link": f'<{API_URL}/{api_call}?branches_count=1000&branches_from=refs/tags/v3.0-rc7>; rel="next"'  # NoQA: B950
            }
        elif (
            api_call
            == "origin/https://github.com/NixOS/nixpkgs/visits/?last_visit=50&per_page=10"  # NoQA: B950
        ):
            # to make the client follow pagination
            headers = {
                "Link": f'<{API_URL}/origin/https://github.com/NixOS/nixpkgs/visits/?last_visit=40&per_page=10>; rel="next"'  # NoQA: B950
            }
        requests_mock.get(f"{API_URL}/{api_call}", text=data, headers=headers)

    def known_callback(request, context):
        swhids = request.json()
        if len(swhids) > KNOWN_QUERY_LIMIT:
            raise RuntimeError("Too many swhids in the queries")
        return {swhid: {"known": swhid in KNOWN_SWHIDS} for swhid in swhids}

    requests_mock.register_uri("POST", f"{API_URL}/known/", json=known_callback)

    # Add some other post urls to mock
    for api_call, data in API_DATA_STATIC["post"].items():
        requests_mock.post(f"{API_URL}/{api_call}", text=data)

    # Add some other get urls to mock
    for api_call, data in API_DATA_STATIC["get"].items():
        requests_mock.get(f"{API_URL}/{api_call}", text=data)

    return requests_mock


@pytest.fixture
def web_api_client():
    # use the fake base API URL that matches API data
    return WebAPIClient(api_url=API_URL)


@pytest.fixture
def cli_global_config_dict():
    """Define a basic configuration yaml for the cli."""
    return {
        "api_url": API_URL,
        "bearer_token": None,
    }


@pytest.fixture
def cli_config_path(tmp_path, cli_global_config_dict, monkeypatch):
    """Write a global.yml file and writes it in the environment"""
    config_path = os.path.join(tmp_path, "global.yml")
    with open(config_path, "w") as f:
        f.write(yaml.dump(cli_global_config_dict))
    monkeypatch.setenv("SWH_CONFIG_FILE", config_path)

    return config_path
