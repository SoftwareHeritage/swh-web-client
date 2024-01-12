# Copyright (C) 2021  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

import hashlib

API_DATA_STATIC = {
    "post": {
        "origin/save/git/url/https://gitlab.org/gazelle/itest/": r"""
      {
        "visit_type": "git",
        "origin_url": "https://gitlab.org/gazelle/itest",
        "save_request_date": "2021-04-20T11:34:38.752929+00:00",
        "save_request_status": "accepted",
        "save_task_status": "not yet scheduled",
        "visit_date": null
      }
        """,
        "origin/save/git/url/https://git.renater.fr/anonscm/git/6po/6po.git/": r"""
      {
        "visit_type": "git",
        "origin_url": "https://git.renater.fr/anonscm/git/6po/6po.git",
        "save_request_date": "2021-04-20T11:34:40.115226+00:00",
        "save_request_status": "accepted",
        "save_task_status": "not yet scheduled",
        "visit_date": null
      }
        """,
        "origin/save/git/url/https://github.com/colobot/colobot/": r"""
      {
        "visit_type": "git",
        "origin_url": "https://github.com/colobot/colobot",
        "save_request_date": "2021-04-20T11:40:47.667492+00:00",
        "save_request_status": "accepted",
        "save_task_status": "not yet scheduled",
        "visit_date": null
      }
        """,
        "vault/flat/swh:1:dir:977fc4b98c0e85816348cebd3b12026407c368b6/": r"""
      {
        "fetch_url":
          "https://archive.softwareheritage.org/api/1/vault/flat/swh:1:dir:977fc4b98c0e85816348cebd3b12026407c368b6/raw/",
        "id": 415999462,
        "progress_message": "Processing...",
        "status": "pending",
        "swhid": "swh:1:dir:977fc4b98c0e85816348cebd3b12026407c368b6"
      }
        """,
    },
    "get": {
        "vault/flat/swh:1:dir:977fc4b98c0e85816348cebd3b12026407c368b6/": r"""
      {
        "fetch_url":
          "https://archive.softwareheritage.org/api/1/vault/flat/swh:1:dir:977fc4b98c0e85816348cebd3b12026407c368b6/raw/",
        "id": 415999462,
        "progress_message": "Processing...",
        "status": "pending",
        "swhid": "swh:1:dir:977fc4b98c0e85816348cebd3b12026407c368b6"
      }
        """,
        "vault/flat/swh:1:dir:977fc4b98c0e85816348cebd3b12026407c368b6/raw": r"""
      "OCTET_STREAM_MOCK"
        """,
    },
}

KNOWN_SWHIDS = set()
for id_type in ("cnt", "dir", "rev", "rel", "snp"):
    for x in range(10000):
        h = hashlib.md5(b"%d" % x).hexdigest()
        KNOWN_SWHIDS.add(f"swh:1:{id_type}:{h}{h[:8]}")
