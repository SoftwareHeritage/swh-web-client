# Copyright (C) 2020  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

import pytest

from swh.web.client.client import WebAPIClient

from .api_data import API_DATA, API_URL


@pytest.fixture
def web_api_mock(requests_mock):
    # monkey patch URLs that require a special response headers
    for api_call, data in API_DATA.items():
        headers = {}
        if api_call == "snapshot/cabcc7d7bf639bbe1cc3b41989e1806618dd5764/":
            # to make the client init and follow pagination
            headers = {
                "Link": f'<{API_URL}/{api_call}?branches_count=1000&branches_from=refs/tags/v3.0-rc7>; rel="next"'  # NoQA: E501
            }
        elif (
            api_call
            == "origin/https://github.com/NixOS/nixpkgs/visits/?last_visit=50&per_page=10"  # NoQA: E501
        ):
            # to make the client follow pagination
            headers = {
                "Link": f'<{API_URL}/origin/https://github.com/NixOS/nixpkgs/visits/?last_visit=40&per_page=10>; rel="next"'  # NoQA: E501
            }
        requests_mock.get(f"{API_URL}/{api_call}", text=data, headers=headers)
    return requests_mock


@pytest.fixture
def web_api_client():
    # use the fake base API URL that matches API data
    return WebAPIClient(api_url=API_URL)
