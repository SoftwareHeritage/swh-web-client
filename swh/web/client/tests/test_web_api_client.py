# Copyright (C) 2020-2021  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

import json
import random
import time

from dateutil.parser import parse as parse_date
import pytest
from requests.exceptions import HTTPError

from swh.model.hashutil import hash_to_hex
from swh.model.swhids import CoreSWHID
from swh.web.client.client import KNOWN_QUERY_LIMIT, typify_json

from .api_data import API_DATA, API_URL
from .api_data_static import KNOWN_SWHIDS


def test_get_content(web_api_client, web_api_mock):
    swhid = CoreSWHID.from_string("swh:1:cnt:fe95a46679d128ff167b7c55df5d02356c5a1ae1")
    obj = web_api_client.get(swhid)

    assert obj["length"] == 151810
    for key in ("length", "status", "checksums", "data_url"):
        assert key in obj
    assert obj["checksums"]["sha1_git"] == str(swhid).split(":")[3]
    assert obj["checksums"]["sha1"] == "dc2830a9e72f23c1dfebef4413003221baa5fb62"

    assert obj == web_api_client.content(swhid)


def limit_call(count=1):
    """build a function that will match a limited number of time"""

    # return rate limit info only once
    limited_uses = [None] * count

    def limited_matcher(*args, **kwargs):
        ret = not limited_uses
        if ret:
            limited_uses.pop()
        return ret

    return limited_matcher


def test_get_retry(web_api_client, web_api_mock):
    swhid = CoreSWHID.from_string("swh:1:cnt:fe95a46679d128ff167b7c55df5d02356c5a1ae1")
    url = f"{API_URL}/content/sha1_git:{hash_to_hex(swhid.object_id)}/"

    # return 429 only three time
    web_api_mock.get(url, status_code=429, additional_matcher=limit_call(3))

    # the call should work and return an object,
    # the 429 automatically result in a retry after some delay
    obj = web_api_client.content(swhid)
    assert obj["checksums"]["sha1_git"] == str(swhid).split(":")[3]

    web_api_mock.get(url, status_code=405)

    # the call should fail
    with pytest.raises(HTTPError):
        web_api_client.content(swhid)


def rate_headers(remaining: int, limit: int, reset_date: int):
    return {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(reset_date),
    }


def _smoke_rate_limit(web_api_client, web_api_mock, events):
    """Run a simple smoke test scenario for Rate Limiting

    The `events` is a list of tuple representing requests::

        (prior_sleep, remaining budget, total_budget_windows, window_end)

    note: Since this use actual timing, this is subject to false positive. But
    the goal here is to have basic smoke testing of the logic.
    """
    swhid = CoreSWHID.from_string("swh:1:cnt:fe95a46679d128ff167b7c55df5d02356c5a1ae1")
    content_key = f"content/sha1_git:{hash_to_hex(swhid.object_id)}/"
    url = f"{API_URL}/{content_key}"

    replies = []
    for __, remaining, total, reset_date in events:
        replies.append(
            {
                "headers": rate_headers(remaining, total, reset_date),
                "text": API_DATA[content_key],
            }
        )
    web_api_mock.register_uri(
        "GET",
        url,
        replies,
    )
    for prior_sleep, __, __, __ in events:
        time.sleep(prior_sleep)
        web_api_client.content(swhid)


def test_get_rate_limit_basic(web_api_client, web_api_mock):
    """Smoke test for multiple queries with rate limit header

    note: This test using actual timing, so it is subject to False positive.
    """
    now = int(time.time())
    _smoke_rate_limit(
        web_api_client,
        web_api_mock,
        [
            (0, 999, 1000, now + 10),
            # sleep 0.1 second to give a change to the background thread to
            # process the info. It might still fails on vey loaded systems
            (0.1, 998, 1000, now + 10),
        ],
    )


def test_get_rate_multi_windows(web_api_client, web_api_mock):
    """Smoke test for queries over multiple Rate limit Windows

    note: This test using actual timing, so it is subject to False positive.
    """
    now = int(time.time())
    _smoke_rate_limit(
        web_api_client,
        web_api_mock,
        [
            (0, 999, 1000, now + 1),
            # sleep 2 second to let the previous windows close
            (2, 999, 1000, now + 10),
        ],
    )


def test_get_rate_stricter_info(web_api_client, web_api_mock):
    """Smoke test situation where the rate limiting have to reasses its delay"""
    now = int(time.time())
    _smoke_rate_limit(
        web_api_client,
        web_api_mock,
        [
            (0, 999, 1000, now + 1),
            # sleep 0.1 second to give a change to the background thread to
            # process the info. It might still fails onv ery loaded systems
            (0.1, 100, 1000, now + 10),
            # sleep 0.1 second to give a change to the background thread to
            # process the info. It might still fails onv ery loaded systems
            (0.1, 99, 1000, now + 10),
            # Then do multiple close request that should get rate limited
            (0, 98, 1000, now + 10),
            (0, 97, 1000, now + 10),
        ],
    )


def test_get_rate_free_stuck_request(web_api_client, web_api_mock):
    """Smoke test situation where the rate limiting have to reasses its"""
    now = int(time.time())
    _smoke_rate_limit(
        web_api_client,
        web_api_mock,
        [
            (0, 2, 1000, now + 1),
            (0.1, 1, 1000, now + 1),
            (0.1, 0, 1000, now + 1),
            # this request should get stuck until the end of the windows
            (0.1, 9999, 1000, now + 20),
            # end everything should be fine afterward.
            (0.1, 9998, 1000, now + 20),
        ],
    )


def test_get_rate_out_or_order_information(web_api_client, web_api_mock):
    """Smoke test situation where the rate limit info arrive out of order

    This simulate multiple thread sending information without specific ordering.
    """
    start = time.time()
    # Give it 10 remaining request per second in the next 1200 second
    # With 10% of the budget left
    #
    # This use large number to reduce the impact of other delay on the processing
    total_time = 1200
    remaining_time = 1200 // 10
    rate_per_sec = 10
    total = total_time * rate_per_sec
    remaining = rate_per_sec * remaining_time
    window_end = int(start) + remaining_time
    _smoke_rate_limit(
        web_api_client,
        web_api_mock,
        [
            (0, remaining, total, window_end),
            # remaining doing request
            (0.1, remaining + 1, total, window_end),
            (0, remaining + 8, total, window_end),
            (0, remaining + 10, total, window_end),
            (0, remaining + 7, total, window_end),
            (0, remaining + 3, total, window_end),
            (0, remaining + 6, total, window_end),
            (0, remaining + 4, total, window_end),
            (0, remaining + 5, total, window_end),
            (0, remaining + 2, total, window_end),
            (0, remaining + 9, total, window_end),
            # Give an answer with a smaller budget, but given than some time
            # should have passed, it should not alter the pace.
            (0, remaining - 1, total, window_end),
            (0, remaining + 11, total, window_end),
            (0, remaining + 12, total, window_end),
            (0, remaining + 13, total, window_end),
            (0, remaining + 14, total, window_end),
            (0, remaining + 15, total, window_end),
            (0, remaining + 16, total, window_end),
            (0, remaining + 17, total, window_end),
            (0, remaining + 18, total, window_end),
            (0, remaining + 19, total, window_end),
        ],
    )
    # Lets be wild and actually test we rate limited here.
    #
    # Request should have a 0.1 second delay in average, so it should have
    # taken 2 second to run these 20 requests. However the time frame is quite
    #
    # small, and various overhead means the rate limiter needs some time to
    # pick up the information and start limiting request, making the timing of
    # the rate limit a bit off.
    #
    # So we assert on a longer value to take this in account.
    end = time.time()
    duration = end - start
    assert duration >= 1.5
    assert 0 < web_api_client.rate_limit_delay


def test_get_directory(web_api_client, web_api_mock):
    swhid = CoreSWHID.from_string("swh:1:dir:977fc4b98c0e85816348cebd3b12026407c368b6")
    obj = web_api_client.get(swhid)

    assert len(obj) == 35  # number of directory entries
    assert all(map(lambda entry: entry["dir_id"] == swhid, obj))
    dir_entry = obj[0]
    assert dir_entry["type"] == "file"
    assert dir_entry["target"] == CoreSWHID.from_string(
        "swh:1:cnt:58471109208922c9ee8c4b06135725f03ed16814"
    )
    assert dir_entry["name"] == ".bzrignore"
    assert dir_entry["length"] == 582

    assert obj == web_api_client.directory(swhid)


def test_get_release(web_api_client, web_api_mock):
    swhid = CoreSWHID.from_string("swh:1:rel:b9db10d00835e9a43e2eebef2db1d04d4ae82342")
    obj = web_api_client.get(swhid)

    assert obj["id"] == swhid
    assert obj["author"]["fullname"] == "Paul Tagliamonte <tag@pault.ag>"
    assert obj["author"]["name"] == "Paul Tagliamonte"
    assert obj["date"] == parse_date("2013-07-06T19:34:11-04:00")
    assert obj["name"] == "0.9.9"
    assert obj["target_type"] == "revision"
    assert obj["target"] == CoreSWHID.from_string(
        "swh:1:rev:e005cb773c769436709ca6a1d625dc784dbc1636"
    )
    assert not obj["synthetic"]

    assert obj == web_api_client.release(swhid)


def test_get_revision(web_api_client, web_api_mock):
    swhid = CoreSWHID.from_string("swh:1:rev:aafb16d69fd30ff58afdd69036a26047f3aebdc6")
    obj = web_api_client.get(swhid)

    assert obj["id"] == swhid
    for role in ("author", "committer"):
        assert (
            obj[role]["fullname"] == "Nicolas Dandrimont <nicolas.dandrimont@crans.org>"
        )
        assert obj[role]["name"] == "Nicolas Dandrimont"
    timestamp = parse_date("2014-08-18T18:18:25+02:00")
    assert obj["date"] == timestamp
    assert obj["committer_date"] == timestamp
    assert obj["message"].startswith("Merge branch")
    assert obj["merge"]
    assert len(obj["parents"]) == 2
    assert obj["parents"][0]["id"] == CoreSWHID.from_string(
        "swh:1:rev:26307d261279861c2d9c9eca3bb38519f951bea4"
    )
    assert obj["parents"][1]["id"] == CoreSWHID.from_string(
        "swh:1:rev:37fc9e08d0c4b71807a4f1ecb06112e78d91c283"
    )

    assert obj == web_api_client.revision(swhid)


def test_get_snapshot(web_api_client, web_api_mock):
    # small snapshot, the one from Web API doc
    swhid = CoreSWHID.from_string("swh:1:snp:6a3a2cf0b2b90ce7ae1cf0a221ed68035b686f5a")
    obj = web_api_client.get(swhid)

    assert len(obj) == 4
    assert obj["refs/heads/master"]["target_type"] == "revision"
    assert obj["refs/heads/master"]["target"] == CoreSWHID.from_string(
        "swh:1:rev:83c20a6a63a7ebc1a549d367bc07a61b926cecf3"
    )
    assert obj["refs/tags/dpkt-1.7"]["target_type"] == "revision"
    assert obj["refs/tags/dpkt-1.7"]["target"] == CoreSWHID.from_string(
        "swh:1:rev:0c9dbfbc0974ec8ac1d8253aa1092366a03633a8"
    )


def test_iter_snapshot(web_api_client, web_api_mock):
    # large snapshot from the Linux kernel, usually spanning two pages
    swhid = CoreSWHID.from_string("swh:1:snp:cabcc7d7bf639bbe1cc3b41989e1806618dd5764")
    obj = web_api_client.snapshot(swhid)

    snp = {}
    for partial in obj:
        snp.update(partial)

    assert len(snp) == 1391


def test_authentication(web_api_client, web_api_mock):
    rel_id = "b9db10d00835e9a43e2eebef2db1d04d4ae82342"
    url = f"{web_api_client.api_url}/release/{rel_id}/"

    refresh_token = "user-refresh-token"

    web_api_client.bearer_token = refresh_token

    swhid = CoreSWHID.from_string(f"swh:1:rel:{rel_id}")
    web_api_client.get(swhid)

    sent_request = web_api_mock._adapter.last_request

    assert sent_request.url == url
    assert "Authorization" in sent_request.headers

    assert sent_request.headers["Authorization"] == f"Bearer {refresh_token}"


def test_get_visits(web_api_client, web_api_mock):
    obj = web_api_client.visits(
        "https://github.com/NixOS/nixpkgs", last_visit=50, per_page=10
    )
    visits = [v for v in obj]
    assert len(visits) == 20

    timestamp = parse_date("2018-07-31 04:34:23.298931+00:00")
    assert visits[0]["date"] == timestamp

    assert visits[0]["snapshot"] is None
    snapshot_swhid = "swh:1:snp:456550ea74af4e2eecaa406629efaaf0b9b5f976"
    assert visits[7]["snapshot"] == CoreSWHID.from_string(snapshot_swhid)


def test_get_last_visit(web_api_client, web_api_mock):
    visit = web_api_client.last_visit("https://github.com/NixOS/nixpkgs")
    assert visit is not None

    timestamp = parse_date("2021-09-02 20:20:31.231786+00:00")
    assert visit["date"] == timestamp

    snapshot_swhid = "swh:1:snp:6e1fe7858066ff1a6905080ac6503a3a12b84f59"
    assert visit["snapshot"] == CoreSWHID.from_string(snapshot_swhid)


def test_origin_search(web_api_client, web_api_mock):
    limited_results = list(web_api_client.origin_search("python", limit=5))
    assert len(limited_results) == 5

    results = list(web_api_client.origin_search("foo bar baz qux", with_visit=True))
    actual_urls = [r["url"] for r in results]
    actual_visits = [r["origin_visits_url"] for r in results]
    # Check *some* of the URLS since the search could return more results in the future
    expected = [
        (
            "https://github.com/foo-bar-baz-qux/mygithubpage",
            "https://archive.softwareheritage.org/api/1/origin/https://github.com/foo-bar-baz-qux/mygithubpage/visits/",  # NoQA: B950
        ),
        (
            "https://www.npmjs.com/package/foo-bar-baz-qux",
            "https://archive.softwareheritage.org/api/1/origin/https://www.npmjs.com/package/foo-bar-baz-qux/visits/",  # NoQA: B950
        ),
        (
            "https://bitbucket.org/foobarbazqux/rp.git",
            "https://archive.softwareheritage.org/api/1/origin/https://bitbucket.org/foobarbazqux/rp.git/visits/",  # NoQA: B950
        ),
    ]
    for url, visit in expected:
        assert url in actual_urls
        assert visit in actual_visits


@pytest.mark.parametrize(
    "visit_type,origin",
    [
        ("git", "https://gitlab.org/gazelle/itest"),
        ("git", "https://git.renater.fr/anonscm/git/6po/6po.git"),
        ("git", "https://github.com/colobot/colobot"),
    ],
)
def test_origin_save(visit_type, origin, web_api_client, web_api_mock):
    """Post save code now is allowed from the client."""
    save_request = web_api_client.origin_save(visit_type, origin)

    assert save_request is not None
    assert save_request["save_request_status"] == "accepted"
    assert save_request["visit_date"] is None


def _query_known(web_api_client, query_size):
    known_swhids = sorted(KNOWN_SWHIDS, key=lambda x: x[::-1])[:query_size]
    bogus_swhids = [s[:20] + "c0ffee" + s[26:] for s in known_swhids]
    all_swhids = known_swhids + bogus_swhids
    random.shuffle(all_swhids)

    known_res = web_api_client.known(all_swhids)

    assert {str(k) for k in known_res} == set(all_swhids)
    for swhid, info in known_res.items():
        assert info["known"] == (str(swhid) in KNOWN_SWHIDS)


def test_known_small(web_api_client, web_api_mock):
    """check a query that is smaller than the limit"""
    query_size = KNOWN_QUERY_LIMIT // 10
    _query_known(web_api_client, query_size)


def test_known_large(web_api_client, web_api_mock):
    """check a query that is higher than the limit"""
    query_size = KNOWN_QUERY_LIMIT * 3
    _query_known(web_api_client, query_size)


def test_get_json(web_api_client, web_api_mock):
    swhids = [
        "swh:1:cnt:fe95a46679d128ff167b7c55df5d02356c5a1ae1",
        "swh:1:dir:977fc4b98c0e85816348cebd3b12026407c368b6",
        "swh:1:rel:b9db10d00835e9a43e2eebef2db1d04d4ae82342",
        "swh:1:rev:aafb16d69fd30ff58afdd69036a26047f3aebdc6",
        "swh:1:snp:6a3a2cf0b2b90ce7ae1cf0a221ed68035b686f5a",
    ]

    for swhid in swhids:
        actual = web_api_client.get(swhid, typify=False)
        expected = None
        # Fetch raw JSON data from the generated API_DATA
        for url, data in API_DATA.items():
            object_id = swhid[len("swh:1:XXX:") :]
            if object_id in url:
                expected = json.loads(data)
                # Special case: snapshots response differs slightly from the Web API
                if swhid.startswith("swh:1:snp:"):
                    expected = expected["branches"]
                break

        assert actual == expected


def test_typify_json_minimal_revision():
    revision_data = {
        "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "directory": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "date": None,
        "committer_date": None,
        "parents": [],
    }
    revision_typed = typify_json(revision_data, "revision")
    pid = "swh:1:rev:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert revision_typed["id"] == CoreSWHID.from_string(pid)
    assert revision_typed["date"] is None


@pytest.mark.parametrize(
    "swhid",
    [
        "swh:1:cnt:fe95a46679d128ff167b7c55df5d02356c5a1ae1",
        "swh:1:dir:977fc4b98c0e85816348cebd3b12026407c368b6",
        "swh:1:rev:aafb16d69fd30ff58afdd69036a26047f3aebdc6",
        "swh:1:rel:b9db10d00835e9a43e2eebef2db1d04d4ae82342",
        "swh:1:snp:6a3a2cf0b2b90ce7ae1cf0a221ed68035b686f5a",
    ],
)
@pytest.mark.parametrize("swhid_type", ["str", "CoreSWHID"])
@pytest.mark.parametrize("typify", [True, False])
def test_iter(web_api_client, web_api_mock, swhid, swhid_type, typify):
    # full list of SWHIDs for which we mock a {known: True} answer
    swhids = [
        "swh:1:cnt:fe95a46679d128ff167b7c55df5d02356c5a1ae1",
        "swh:1:dir:977fc4b98c0e85816348cebd3b12026407c368b6",
        "swh:1:rev:aafb16d69fd30ff58afdd69036a26047f3aebdc6",
        "swh:1:rel:b9db10d00835e9a43e2eebef2db1d04d4ae82342",
        "swh:1:snp:6a3a2cf0b2b90ce7ae1cf0a221ed68035b686f5a",
    ]
    for swhid in swhids:
        if swhid_type == "CoreSWHID":
            assert list(
                web_api_client.iter(CoreSWHID.from_string(swhid), typify=typify)
            )
        else:
            assert list(web_api_client.iter(swhid, typify=typify))


def test_cooking_request(web_api_client, web_api_mock):
    dir_swhid = "swh:1:dir:977fc4b98c0e85816348cebd3b12026407c368b6"
    obj = web_api_client.cooking_request("flat", dir_swhid)
    assert obj["fetch_url"] == (
        "https://archive.softwareheritage.org/api/1/vault/flat/" + dir_swhid + "/raw/"
    )
    assert obj["status"] == "pending"
    assert obj["swhid"] == dir_swhid
    assert obj["id"] == 415999462
    assert obj["progress_message"] == "Processing..."


def test_cooking_check(web_api_client, web_api_mock):
    dir_swhid = "swh:1:dir:977fc4b98c0e85816348cebd3b12026407c368b6"
    obj = web_api_client.cooking_check("flat", dir_swhid)
    assert obj["fetch_url"] == (
        "https://archive.softwareheritage.org/api/1/vault/flat/" + dir_swhid + "/raw/"
    )
    assert obj["status"] == "pending"
    assert obj["swhid"] == dir_swhid
    assert obj["id"] == 415999462
    assert obj["progress_message"] == "Processing..."


def test_cooking_fetch(web_api_client, web_api_mock):
    dir_swhid = "swh:1:dir:977fc4b98c0e85816348cebd3b12026407c368b6"
    obj = web_api_client.cooking_fetch("flat", dir_swhid)
    assert obj.content.find(b"OCTET_STREAM_MOCK") != -1
