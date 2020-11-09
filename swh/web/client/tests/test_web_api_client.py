# Copyright (C) 2020  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

import json

from dateutil.parser import parse as parse_date

from swh.model.identifiers import REVISION, parse_swhid
from swh.web.client.client import typify_json

from .api_data import API_DATA


def test_get_content(web_api_client, web_api_mock):
    swhid = parse_swhid("swh:1:cnt:fe95a46679d128ff167b7c55df5d02356c5a1ae1")
    obj = web_api_client.get(swhid)

    assert obj["length"] == 151810
    for key in ("length", "status", "checksums", "data_url"):
        assert key in obj
    assert obj["checksums"]["sha1_git"] == str(swhid).split(":")[3]
    assert obj["checksums"]["sha1"] == "dc2830a9e72f23c1dfebef4413003221baa5fb62"

    assert obj == web_api_client.content(swhid)


def test_get_directory(web_api_client, web_api_mock):
    swhid = parse_swhid("swh:1:dir:977fc4b98c0e85816348cebd3b12026407c368b6")
    obj = web_api_client.get(swhid)

    assert len(obj) == 35  # number of directory entries
    assert all(map(lambda entry: entry["dir_id"] == swhid, obj))
    dir_entry = obj[0]
    assert dir_entry["type"] == "file"
    assert dir_entry["target"] == parse_swhid(
        "swh:1:cnt:58471109208922c9ee8c4b06135725f03ed16814"
    )
    assert dir_entry["name"] == ".bzrignore"
    assert dir_entry["length"] == 582

    assert obj == web_api_client.directory(swhid)


def test_get_release(web_api_client, web_api_mock):
    swhid = parse_swhid("swh:1:rel:b9db10d00835e9a43e2eebef2db1d04d4ae82342")
    obj = web_api_client.get(swhid)

    assert obj["id"] == swhid
    assert obj["author"]["fullname"] == "Paul Tagliamonte <tag@pault.ag>"
    assert obj["author"]["name"] == "Paul Tagliamonte"
    assert obj["date"] == parse_date("2013-07-06T19:34:11-04:00")
    assert obj["name"] == "0.9.9"
    assert obj["target_type"] == "revision"
    assert obj["target"] == parse_swhid(
        "swh:1:rev:e005cb773c769436709ca6a1d625dc784dbc1636"
    )
    assert not obj["synthetic"]

    assert obj == web_api_client.release(swhid)


def test_get_revision(web_api_client, web_api_mock):
    swhid = parse_swhid("swh:1:rev:aafb16d69fd30ff58afdd69036a26047f3aebdc6")
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
    assert obj["parents"][0]["id"] == parse_swhid(
        "swh:1:rev:26307d261279861c2d9c9eca3bb38519f951bea4"
    )
    assert obj["parents"][1]["id"] == parse_swhid(
        "swh:1:rev:37fc9e08d0c4b71807a4f1ecb06112e78d91c283"
    )

    assert obj == web_api_client.revision(swhid)


def test_get_snapshot(web_api_client, web_api_mock):
    # small snapshot, the one from Web API doc
    swhid = parse_swhid("swh:1:snp:6a3a2cf0b2b90ce7ae1cf0a221ed68035b686f5a")
    obj = web_api_client.get(swhid)

    assert len(obj) == 4
    assert obj["refs/heads/master"]["target_type"] == "revision"
    assert obj["refs/heads/master"]["target"] == parse_swhid(
        "swh:1:rev:83c20a6a63a7ebc1a549d367bc07a61b926cecf3"
    )
    assert obj["refs/tags/dpkt-1.7"]["target_type"] == "revision"
    assert obj["refs/tags/dpkt-1.7"]["target"] == parse_swhid(
        "swh:1:rev:0c9dbfbc0974ec8ac1d8253aa1092366a03633a8"
    )


def test_iter_snapshot(web_api_client, web_api_mock):
    # large snapshot from the Linux kernel, usually spanning two pages
    swhid = parse_swhid("swh:1:snp:cabcc7d7bf639bbe1cc3b41989e1806618dd5764")
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

    swhid = parse_swhid(f"swh:1:rel:{rel_id}")
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
    assert visits[7]["snapshot"] == parse_swhid(snapshot_swhid)


def test_known(web_api_client, web_api_mock):
    # full list of SWHIDs for which we mock a {known: True} answer
    known_swhids = [
        "swh:1:cnt:fe95a46679d128ff167b7c55df5d02356c5a1ae1",
        "swh:1:dir:977fc4b98c0e85816348cebd3b12026407c368b6",
        "swh:1:rev:aafb16d69fd30ff58afdd69036a26047f3aebdc6",
        "swh:1:rel:208f61cc7a5dbc9879ae6e5c2f95891e270f09ef",
        "swh:1:snp:6a3a2cf0b2b90ce7ae1cf0a221ed68035b686f5a",
    ]
    bogus_swhids = [s[:20] + "c0ffee" + s[26:] for s in known_swhids]
    all_swhids = known_swhids + bogus_swhids

    known_res = web_api_client.known(all_swhids)

    assert {str(k) for k in known_res} == set(all_swhids)
    for swhid, info in known_res.items():
        assert info["known"] == (str(swhid) in known_swhids)


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
    revision_typed = typify_json(revision_data, REVISION)
    pid = "swh:1:rev:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert revision_typed["id"] == parse_swhid(pid)
    assert revision_typed["date"] is None
