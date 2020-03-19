# Copyright (C) 2020  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

from dateutil.parser import parse as parse_date

from swh.model.identifiers import parse_persistent_identifier as parse_pid


def test_get_content(web_api_client, web_api_mock):
    pid = parse_pid("swh:1:cnt:fe95a46679d128ff167b7c55df5d02356c5a1ae1")
    obj = web_api_client.get(pid)

    assert obj["length"] == 151810
    for key in ("length", "status", "checksums", "data_url"):
        assert key in obj
    assert obj["checksums"]["sha1_git"] == str(pid).split(":")[3]
    assert obj["checksums"]["sha1"] == \
        "dc2830a9e72f23c1dfebef4413003221baa5fb62"

    assert obj == web_api_client.content(pid)


def test_get_directory(web_api_client, web_api_mock):
    pid = parse_pid("swh:1:dir:977fc4b98c0e85816348cebd3b12026407c368b6")
    obj = web_api_client.get(pid)

    assert len(obj) == 35  # number of directory entries
    assert all(map(lambda entry: entry["dir_id"] == pid, obj))
    dir_entry = obj[0]
    assert dir_entry["type"] == "file"
    assert dir_entry["target"] == parse_pid(
        "swh:1:cnt:58471109208922c9ee8c4b06135725f03ed16814")
    assert dir_entry["name"] == ".bzrignore"
    assert dir_entry["length"] == 582

    assert obj == web_api_client.directory(pid)


def test_get_release(web_api_client, web_api_mock):
    pid = parse_pid("swh:1:rel:b9db10d00835e9a43e2eebef2db1d04d4ae82342")
    obj = web_api_client.get(pid)

    assert obj["id"] == pid
    assert obj["author"]["fullname"] == "Paul Tagliamonte <tag@pault.ag>"
    assert obj["author"]["name"] == "Paul Tagliamonte"
    assert obj["date"] == parse_date("2013-07-06T19:34:11-04:00")
    assert obj["name"] == "0.9.9"
    assert obj["target_type"] == "revision"
    assert obj["target"] == parse_pid(
        "swh:1:rev:e005cb773c769436709ca6a1d625dc784dbc1636")
    assert not obj["synthetic"]

    assert obj == web_api_client.release(pid)


def test_get_revision(web_api_client, web_api_mock):
    pid = parse_pid("swh:1:rev:aafb16d69fd30ff58afdd69036a26047f3aebdc6")
    obj = web_api_client.get(pid)

    assert obj["id"] == pid
    for role in ("author", "committer"):
        assert obj[role]["fullname"] == \
            "Nicolas Dandrimont <nicolas.dandrimont@crans.org>"
        assert obj[role]["name"] == "Nicolas Dandrimont"
    timestamp = parse_date("2014-08-18T18:18:25+02:00")
    assert obj["date"] == timestamp
    assert obj["committer_date"] == timestamp
    assert obj["message"].startswith("Merge branch")
    assert obj["merge"]
    assert len(obj["parents"]) == 2
    assert obj["parents"][0]["id"] == parse_pid(
        "swh:1:rev:26307d261279861c2d9c9eca3bb38519f951bea4")
    assert obj["parents"][1]["id"] == parse_pid(
        "swh:1:rev:37fc9e08d0c4b71807a4f1ecb06112e78d91c283")

    assert obj == web_api_client.revision(pid)


def test_get_snapshot(web_api_client, web_api_mock):
    # small snapshot, the one from Web API doc
    pid = parse_pid("swh:1:snp:6a3a2cf0b2b90ce7ae1cf0a221ed68035b686f5a")
    obj = web_api_client.get(pid)

    assert len(obj) == 4
    assert obj["refs/heads/master"]["target_type"] == "revision"
    assert obj["refs/heads/master"]["target"] == parse_pid(
        "swh:1:rev:83c20a6a63a7ebc1a549d367bc07a61b926cecf3")
    assert obj["refs/tags/dpkt-1.7"]["target_type"] == "revision"
    assert obj["refs/tags/dpkt-1.7"]["target"] == parse_pid(
        "swh:1:rev:0c9dbfbc0974ec8ac1d8253aa1092366a03633a8")


def test_iter_snapshot(web_api_client, web_api_mock):
    # large snapshot from the Linux kernel, usually spanning two pages
    pid = parse_pid("swh:1:snp:cabcc7d7bf639bbe1cc3b41989e1806618dd5764")
    obj = web_api_client.snapshot(pid)

    snp = {}
    for partial in obj:
        snp.update(partial)

    assert len(snp) == 1391
