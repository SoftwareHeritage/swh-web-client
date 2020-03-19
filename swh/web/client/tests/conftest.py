# Copyright (C) 2020  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

import pytest

from .api_data import API_URL, API_DATA
from swh.web.client import WebAPIClient


@pytest.fixture
def web_api_mock(requests_mock):
    for api_call, data in API_DATA.items():
        headers = {}
        if api_call == "snapshot/cabcc7d7bf639bbe1cc3b41989e1806618dd5764/":
            # monkey patch the only URL that require a special response headers
            # (to make the client insit and follow pagination)
            headers = {
                "Link":
                f"<{API_URL}/{api_call}?branches_count=1000&branches_from=refs/tags/v3.0-rc7>; rel=\"next\""  # NoQA: E501
            }
        requests_mock.get(f"{API_URL}/{api_call}", text=data, headers=headers)


@pytest.fixture
def web_api_client():
    # use the fake base API URL that matches API data
    return WebAPIClient(api_url=API_URL)
