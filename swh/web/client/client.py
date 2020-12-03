# Copyright (C) 2019-2020  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

"""Python client for the Software Heritage Web API

Light wrapper around requests for the archive API, taking care of data
conversions and pagination.

.. code-block:: python

   from swh.web.client.client import WebAPIClient
   cli = WebAPIClient()

   # retrieve any archived object via its SWHID
   cli.get('swh:1:rev:aafb16d69fd30ff58afdd69036a26047f3aebdc6')

   # same, but for specific object types
   cli.revision('swh:1:rev:aafb16d69fd30ff58afdd69036a26047f3aebdc6')

   # get() always retrieve entire objects, following pagination
   # WARNING: this might *not* be what you want for large objects
   cli.get('swh:1:snp:6a3a2cf0b2b90ce7ae1cf0a221ed68035b686f5a')

   # type-specific methods support explicit iteration through pages
   next(cli.snapshot('swh:1:snp:cabcc7d7bf639bbe1cc3b41989e1806618dd5764'))

"""

from datetime import datetime
from typing import Any, Callable, Dict, Iterator, List, Optional, Union
from urllib.parse import urlparse

import dateutil.parser
import requests

from swh.model.identifiers import (
    CONTENT,
    DIRECTORY,
    RELEASE,
    REVISION,
    SNAPSHOT,
    SWHID,
    parse_swhid,
)

SWHIDish = Union[SWHID, str]

ORIGIN_VISIT = "origin_visit"


def _get_swhid(swhidish: SWHIDish) -> SWHID:
    """Parse string to SWHID if needed"""
    if isinstance(swhidish, str):
        return parse_swhid(swhidish)
    else:
        return swhidish


def typify_json(data: Any, obj_type: str) -> Any:
    """Type API responses using pythonic types where appropriate

    The following conversions are performed:

    - identifiers are converted from strings to SWHID instances
    - timestamps are converted from strings to datetime.datetime objects

    """

    def to_swhid(object_type: str, s: Any) -> SWHID:
        return SWHID(object_type=object_type, object_id=s)

    def to_date(date: str) -> datetime:
        return dateutil.parser.parse(date)

    def to_optional_date(date: Optional[str]) -> Optional[datetime]:
        return None if date is None else to_date(date)

    # The date attribute is optional for Revision and Release object

    def obj_type_of_entry_type(s):
        if s == "file":
            return CONTENT
        elif s == "dir":
            return DIRECTORY
        elif s == "rev":
            return REVISION
        else:
            raise ValueError(f"invalid directory entry type: {s}")

    if obj_type == SNAPSHOT:
        for name, target in data.items():
            if target["target_type"] != "alias":
                # alias targets do not point to objects via SWHIDs; others do
                target["target"] = to_swhid(target["target_type"], target["target"])
    elif obj_type == REVISION:
        data["id"] = to_swhid(obj_type, data["id"])
        data["directory"] = to_swhid(DIRECTORY, data["directory"])
        for key in ("date", "committer_date"):
            data[key] = to_optional_date(data[key])
        for parent in data["parents"]:
            parent["id"] = to_swhid(REVISION, parent["id"])
    elif obj_type == RELEASE:
        data["id"] = to_swhid(obj_type, data["id"])
        data["date"] = to_optional_date(data["date"])
        data["target"] = to_swhid(data["target_type"], data["target"])
    elif obj_type == DIRECTORY:
        dir_swhid = None
        for entry in data:
            dir_swhid = dir_swhid or to_swhid(obj_type, entry["dir_id"])
            entry["dir_id"] = dir_swhid
            entry["target"] = to_swhid(
                obj_type_of_entry_type(entry["type"]), entry["target"]
            )
    elif obj_type == CONTENT:
        pass  # nothing to do for contents
    elif obj_type == ORIGIN_VISIT:
        data["date"] = to_date(data["date"])
        if data["snapshot"] is not None:
            data["snapshot"] = to_swhid(SNAPSHOT, data["snapshot"])
    else:
        raise ValueError(f"invalid object type: {obj_type}")

    return data


class WebAPIClient:
    """Client for the Software Heritage archive Web API, see

    https://archive.softwareheritage.org/api/

    """

    def __init__(
        self,
        api_url: str = "https://archive.softwareheritage.org/api/1",
        bearer_token: Optional[str] = None,
    ):
        """Create a client for the Software Heritage Web API

        See: https://archive.softwareheritage.org/api/

        Args:
            api_url: base URL for API calls (default:
                "https://archive.softwareheritage.org/api/1")
            bearer_token: optional bearer token to do authenticated API calls
        """
        api_url = api_url.rstrip("/")
        u = urlparse(api_url)

        self.api_url = api_url
        self.api_path = u.path
        self.bearer_token = bearer_token

        self._getters: Dict[str, Callable[[SWHIDish, bool], Any]] = {
            CONTENT: self.content,
            DIRECTORY: self.directory,
            RELEASE: self.release,
            REVISION: self.revision,
            SNAPSHOT: self._get_snapshot,
        }

    def _call(
        self, query: str, http_method: str = "get", **req_args
    ) -> requests.models.Response:
        """Dispatcher for archive API invocation

        Args:
            query: API method to be invoked, rooted at api_url
            http_method: HTTP method to be invoked, one of: 'get', 'head'
            req_args: extra keyword arguments for requests.get()/.head()

        Raises:
            requests.HTTPError: if HTTP request fails and http_method is 'get'

        """
        url = None
        if urlparse(query).scheme:  # absolute URL
            url = query
        else:  # relative URL; prepend base API URL
            url = "/".join([self.api_url, query])
        r = None

        headers = {}
        if self.bearer_token is not None:
            headers = {"Authorization": f"Bearer {self.bearer_token}"}

        if http_method == "get":
            r = requests.get(url, **req_args, headers=headers)
            r.raise_for_status()
        elif http_method == "post":
            r = requests.post(url, **req_args, headers=headers)
            r.raise_for_status()
        elif http_method == "head":
            r = requests.head(url, **req_args, headers=headers)
        else:
            raise ValueError(f"unsupported HTTP method: {http_method}")

        return r

    def _get_snapshot(self, swhid: SWHIDish, typify: bool = True) -> Dict[str, Any]:
        """Analogous to self.snapshot(), but zipping through partial snapshots,
        merging them together before returning

        """
        snapshot = {}
        for snp in self.snapshot(swhid, typify):
            snapshot.update(snp)

        return snapshot

    def get(self, swhid: SWHIDish, typify: bool = True, **req_args) -> Any:
        """Retrieve information about an object of any kind

        Dispatcher method over the more specific methods content(),
        directory(), etc.

        Note that this method will buffer the entire output in case of long,
        iterable output (e.g., for snapshot()), see the iter() method for
        streaming.

        """

        swhid_ = _get_swhid(swhid)
        return self._getters[swhid_.object_type](swhid_, typify)

    def iter(
        self, swhid: SWHIDish, typify: bool = True, **req_args
    ) -> Iterator[Dict[str, Any]]:
        """Stream over the information about an object of any kind

        Streaming variant of get()

        """
        swhid_ = _get_swhid(swhid)
        obj_type = swhid_.object_type
        if obj_type == SNAPSHOT:
            yield from self.snapshot(swhid_, typify)
        elif obj_type == REVISION:
            yield from [self.revision(swhid_, typify)]
        elif obj_type == RELEASE:
            yield from [self.release(swhid_, typify)]
        elif obj_type == DIRECTORY:
            yield from self.directory(swhid_, typify)
        elif obj_type == CONTENT:
            yield from [self.content(swhid_, typify)]
        else:
            raise ValueError(f"invalid object type: {obj_type}")

    def content(
        self, swhid: SWHIDish, typify: bool = True, **req_args
    ) -> Dict[str, Any]:
        """Retrieve information about a content object

        Args:
            swhid: object persistent identifier
            typify: if True, convert return value to pythonic types wherever
                possible, otherwise return raw JSON types (default: True)
            req_args: extra keyword arguments for requests.get()

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        json = self._call(
            f"content/sha1_git:{_get_swhid(swhid).object_id}/", **req_args
        ).json()
        return typify_json(json, CONTENT) if typify else json

    def directory(
        self, swhid: SWHIDish, typify: bool = True, **req_args
    ) -> List[Dict[str, Any]]:
        """Retrieve information about a directory object

        Args:
            swhid: object persistent identifier
            typify: if True, convert return value to pythonic types wherever
                possible, otherwise return raw JSON types (default: True)
            req_args: extra keyword arguments for requests.get()

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        json = self._call(
            f"directory/{_get_swhid(swhid).object_id}/", **req_args
        ).json()
        return typify_json(json, DIRECTORY) if typify else json

    def revision(
        self, swhid: SWHIDish, typify: bool = True, **req_args
    ) -> Dict[str, Any]:
        """Retrieve information about a revision object

        Args:
            swhid: object persistent identifier
            typify: if True, convert return value to pythonic types wherever
                possible, otherwise return raw JSON types (default: True)
            req_args: extra keyword arguments for requests.get()

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        json = self._call(f"revision/{_get_swhid(swhid).object_id}/", **req_args).json()
        return typify_json(json, REVISION) if typify else json

    def release(
        self, swhid: SWHIDish, typify: bool = True, **req_args
    ) -> Dict[str, Any]:
        """Retrieve information about a release object

        Args:
            swhid: object persistent identifier
            typify: if True, convert return value to pythonic types wherever
                possible, otherwise return raw JSON types (default: True)
            req_args: extra keyword arguments for requests.get()

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        json = self._call(f"release/{_get_swhid(swhid).object_id}/", **req_args).json()
        return typify_json(json, RELEASE) if typify else json

    def snapshot(
        self, swhid: SWHIDish, typify: bool = True, **req_args
    ) -> Iterator[Dict[str, Any]]:
        """Retrieve information about a snapshot object

        Args:
            swhid: object persistent identifier
            typify: if True, convert return value to pythonic types wherever
                possible, otherwise return raw JSON types (default: True)
            req_args: extra keyword arguments for requests.get()

        Returns:
            an iterator over partial snapshots (dictionaries mapping branch
            names to information about where they point to), each containing a
            subset of available branches

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        done = False
        r = None
        query = f"snapshot/{_get_swhid(swhid).object_id}/"

        while not done:
            r = self._call(query, http_method="get", **req_args)
            json = r.json()["branches"]
            yield typify_json(json, SNAPSHOT) if typify else json
            if "next" in r.links and "url" in r.links["next"]:
                query = r.links["next"]["url"]
            else:
                done = True

    def visits(
        self,
        origin: str,
        per_page: Optional[int] = None,
        last_visit: Optional[int] = None,
        typify: bool = True,
        **req_args,
    ) -> Iterator[Dict[str, Any]]:
        """List visits of an origin

        Args:
            origin: the URL of a software origin
            per_page: the number of visits to list
            last_visit: visit to start listing from
            typify: if True, convert return value to pythonic types wherever
                possible, otherwise return raw JSON types (default: True)
            req_args: extra keyword arguments for requests.get()

        Returns:
            an iterator over visits of the origin

        Raises:
            requests.HTTPError: if HTTP request fails

        """
        done = False
        r = None

        params = []
        if last_visit is not None:
            params.append(("last_visit", last_visit))
        if per_page is not None:
            params.append(("per_page", per_page))

        query = f"origin/{origin}/visits/"

        while not done:
            r = self._call(query, http_method="get", params=params, **req_args)
            yield from [typify_json(v, ORIGIN_VISIT) if typify else v for v in r.json()]
            if "next" in r.links and "url" in r.links["next"]:
                params = []
                query = r.links["next"]["url"]
            else:
                done = True

    def known(
        self, swhids: Iterator[SWHIDish], **req_args
    ) -> Dict[SWHID, Dict[Any, Any]]:
        """Verify the presence in the archive of several objects at once

        Args:
            swhids: SWHIDs of the objects to verify

        Returns:
            a dictionary mapping object SWHIDs to archive information about them; the
            dictionary includes a "known" key associated to a boolean value that is true
            if and only if the object is known to the archive

        Raises:
            requests.HTTPError: if HTTP request fails

        """
        r = self._call(
            "known/", http_method="post", json=list(map(str, swhids)), **req_args
        )
        return {parse_swhid(k): v for k, v in r.json().items()}

    def content_exists(self, swhid: SWHIDish, **req_args) -> bool:
        """Check if a content object exists in the archive

        Args:
            swhid: object persistent identifier
            req_args: extra keyword arguments for requests.head()

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        return bool(
            self._call(
                f"content/sha1_git:{_get_swhid(swhid).object_id}/",
                http_method="head",
                **req_args,
            )
        )

    def directory_exists(self, swhid: SWHIDish, **req_args) -> bool:
        """Check if a directory object exists in the archive

        Args:
            swhid: object persistent identifier
            req_args: extra keyword arguments for requests.head()

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        return bool(
            self._call(
                f"directory/{_get_swhid(swhid).object_id}/",
                http_method="head",
                **req_args,
            )
        )

    def revision_exists(self, swhid: SWHIDish, **req_args) -> bool:
        """Check if a revision object exists in the archive

        Args:
            swhid: object persistent identifier
            req_args: extra keyword arguments for requests.head()

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        return bool(
            self._call(
                f"revision/{_get_swhid(swhid).object_id}/",
                http_method="head",
                **req_args,
            )
        )

    def release_exists(self, swhid: SWHIDish, **req_args) -> bool:
        """Check if a release object exists in the archive

        Args:
            swhid: object persistent identifier
            req_args: extra keyword arguments for requests.head()

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        return bool(
            self._call(
                f"release/{_get_swhid(swhid).object_id}/",
                http_method="head",
                **req_args,
            )
        )

    def snapshot_exists(self, swhid: SWHIDish, **req_args) -> bool:
        """Check if a snapshot object exists in the archive

        Args:
            swhid: object persistent identifier
            req_args: extra keyword arguments for requests.head()

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        return bool(
            self._call(
                f"snapshot/{_get_swhid(swhid).object_id}/",
                http_method="head",
                **req_args,
            )
        )

    def origin_exists(self, origin: str, **req_args) -> bool:
        """Check if an origin object exists in the archive

        Args:
            origin: the URL of a software origin
            req_args: extra keyword arguments for requests.head()

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        return bool(
            self._call(f"origin/{origin}/get/", http_method="head", **req_args,)
        )

    def content_raw(self, swhid: SWHIDish, **req_args) -> Iterator[bytes]:
        """Iterate over the raw content of a content object

        Args:
            swhid: object persistent identifier
            req_args: extra keyword arguments for requests.get()

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        r = self._call(
            f"content/sha1_git:{_get_swhid(swhid).object_id}/raw/",
            stream=True,
            **req_args,
        )
        r.raise_for_status()

        yield from r.iter_content(chunk_size=None, decode_unicode=False)
