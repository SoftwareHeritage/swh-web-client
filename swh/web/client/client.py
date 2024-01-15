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
import itertools
import logging
import threading
import time
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple, Union
from urllib.parse import urlparse

import dateutil.parser
import requests
import requests.status_codes

from swh.model.hashutil import hash_to_bytes, hash_to_hex
from swh.model.swhids import CoreSWHID, ObjectType
from swh.web.client.cli import DEFAULT_CONFIG

logger = logging.getLogger(__name__)

SWHIDish = Union[CoreSWHID, str]


CONTENT = "content"
DIRECTORY = "directory"
REVISION = "revision"
RELEASE = "release"
SNAPSHOT = "snapshot"
ORIGIN_VISIT = "origin_visit"
ORIGIN = "origin"


def _get_object_id_hex(swhidish: SWHIDish) -> str:
    """Parse string or SWHID and return the hex value of the object_id"""
    if isinstance(swhidish, str):
        swhid = CoreSWHID.from_string(swhidish)
    else:
        swhid = swhidish

    return hash_to_hex(swhid.object_id)


def typify_json(data: Any, obj_type: str) -> Any:
    """Type API responses using pythonic types where appropriate

    The following conversions are performed:

    - identifiers are converted from strings to SWHID instances
    - timestamps are converted from strings to datetime.datetime objects

    """

    def to_swhid(object_type: Union[str, ObjectType], s: Any) -> CoreSWHID:
        if isinstance(object_type, str):
            parsed_object_type = ObjectType[object_type.upper()]
        else:
            parsed_object_type = object_type
        return CoreSWHID(object_type=parsed_object_type, object_id=hash_to_bytes(s))

    def to_date(date: str) -> datetime:
        return dateutil.parser.parse(date)

    def to_optional_date(date: Optional[str]) -> Optional[datetime]:
        return None if date is None else to_date(date)

    # The date attribute is optional for Revision and Release object

    def obj_type_of_entry_type(s):
        if s == "file":
            return ObjectType.CONTENT
        elif s == "dir":
            return ObjectType.DIRECTORY
        elif s == "rev":
            return ObjectType.REVISION
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
            data["snapshot"] = to_swhid("snapshot", data["snapshot"])
    else:
        raise ValueError(f"invalid object type: {obj_type}")

    return data


def _parse_limit_header(response) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """parse the X-RateLimit Headers if any

    return a `(Limit, Remaining, Reset)` tuple

    Limit:     containing the requests quota in the time window;
    Remaining: containing the remaining requests quota in the current window;
    Reset:     date of the current windows reset, as UTC second.
    """
    limit = response.headers.get("X-RateLimit-Limit")
    if limit is not None:
        limit = int(limit)
    remaining = response.headers.get("X-RateLimit-Remaining")
    if remaining is not None:
        remaining = int(remaining)
    reset = response.headers.get("X-RateLimit-Reset")
    if reset is not None:
        reset = int(reset)
    return (limit, remaining, reset)


class _RateLimitInfo:
    """Object that hold rate limit information and can compute delay

    >>> reset = 150
    >>> ### test replacement logic
    >>> # some old information to replace
    >>> old = _RateLimitInfo(42, 44, 1000, 800, reset)
    >>> # some information with the same date but lower budget
    >>> new = _RateLimitInfo(42, 44, 1000, 700, reset)
    >>> # some information with a later window
    >>> newer = _RateLimitInfo(42, 44, 1000, 100, reset * 2)
    >>> # the old one is replaced by a lower budget
    >>> assert new.replacing(old)
    >>> # the later window replace the older window
    >>> assert newer.replacing(old)
    >>> assert newer.replacing(new)
    >>> ### test delay logic
    >>> # if the budget is fully available we expect no delay
    >>> full = _RateLimitInfo(42, 42, 1000, 1000, reset)
    >>> # whatever the remaining windows is
    >>> assert full.pratical_delay(50) == 0
    >>> assert full.pratical_delay(100) == 0
    >>> # if the budget is half consumed we expect a delay
    >>> half = _RateLimitInfo(42, 42, 1000, 500, reset)
    >>> delay_100w = half.pratical_delay(50)
    >>> assert 0 < delay_100w < half.MAX_SLOW_FACTOR
    >>> # smaller windows means smaller delay
    >>> delay_50w = half.pratical_delay(100)
    >>> assert 0 < delay_50w < half.MAX_SLOW_FACTOR
    >>> assert (delay_100w / 2.1) < delay_50w < (delay_100w / 1.9)
    """

    # see usage code for documentation
    SLOW_DOWN_RATIO = 0.9
    MIN_SLOW_FACTOR = 0.1
    MAX_SLOW_FACTOR = 10
    SLOW_POWER = 4

    def __init__(self, start, end, limit, remaining, reset):
        self.start = start
        self.end = end
        self.limit = limit
        self.remaining = remaining
        self.reset_date = reset

        self.remaining_ratio = self.remaining / self.limit

    def __repr__(self):
        r = "<RateLimitInfo start=%s, end=%s, budget=%d/%d, reset_date=%d>"
        r %= (self.start, self.end, self.remaining, self.limit, self.reset_date)
        return r

    def replacing(self, other):
        if other.reset_date != self.reset_date:
            # the one with a later reset date is likely more up to date.
            return other.reset_date < self.reset_date

        # new info are strictly newer than existing one
        if other.end < self.start:
            return True

        # new info are strictly older than existing one
        if other.end < self.start:
            return False

        # information overlap, keep the stricter one
        return self.remaining_ratio < other.remaining_ratio

    def theoretical_delay(self, current_date):
        """theoretical necessary delay between request until the end of the windows

        If request are issued at this interval, they will match the request
        rate limit from the server.

        Value is return in second"""
        timeframe = self.reset_date - current_date

        if timeframe <= 0:
            # the reset date is passed, no rate limiting to apply
            return 0

        return timeframe / self.remaining

    def pratical_delay(self, current_date):
        """return current appropriate request delay in second

        how much we should wait before each issuing a new request if we want
        to avoid depleting the budget early
        That logic is not very elaborate for now and has various limitation. For example:
        - the delay should be dynamic and take in account the age the information,
        - we don't account for potential multiple thread
        """
        if self.remaining <= 0:
            # no more credit, we can just wait.
            #
            # We wait a bit more since reaching zero budget is a failure in
            # itself.
            delay = self.reset_date - current_date
            delay *= 1.1
            return delay

        delay = self.theoretical_delay(current_date)
        # We do not introduce delay for the initial part of the budget.
        #
        # We do not want to slow down a small burst of request.
        # that ratio is controlled by SLOW_DOWN_RATIO.
        if delay <= 0 or self.remaining_ratio > self.SLOW_DOWN_RATIO:
            return 0

        # The remaining budget start to be limited, we are going to delay
        # request to match a rate that allow us to keep issuing requests until
        # the windows is reset
        #
        # The lower the budget, the longer we delay request, waiting more than
        # what is strictly necessary. This is intended to help cope with other
        # Client chipping at the same budget.
        #
        # when SLOW_DOWN_RATIO is reached, such factor will be
        # MIN_SLOW_FACTOR at the start and MAX_SLOW_FACTOR at the end. That
        # factor will evolve using a xʸ curve. Where y is SLOW_POWER.
        assert 0 <= self.SLOW_DOWN_RATIO <= 1
        assert self.MIN_SLOW_FACTOR < self.MAX_SLOW_FACTOR
        assert 0 < self.SLOW_POWER
        start = self.MIN_SLOW_FACTOR ** (1 / self.SLOW_POWER)
        end = self.MAX_SLOW_FACTOR ** (1 / self.SLOW_POWER)
        used_up = 1 - (self.remaining_ratio / self.SLOW_DOWN_RATIO)
        x = start + ((end - start) * used_up)
        factor = x**self.SLOW_POWER

        if factor <= 0:
            # let us avoid negative delay in case MIN_SLOW_FACTOR allows for it.
            return 0

        return delay * factor


# The maximum amount of SWHID that one can request in a single `known` request
KNOWN_QUERY_LIMIT = 1000


def _get_known_chunk(swhids):
    """slice a list of `swhids` into smaller list of size KNOWN_QUERY_LIMIT"""
    for i in range(0, len(swhids), KNOWN_QUERY_LIMIT):
        yield swhids[i : i + KNOWN_QUERY_LIMIT]


MAX_RETRY = 10

DEFAULT_RETRY_REASONS = {
    requests.status_codes.codes.TOO_MANY_REQUESTS,
}


class WebAPIClient:
    """Client for the Software Heritage archive Web API, see :swh_web:`api/`"""

    def __init__(
        self,
        api_url: str = DEFAULT_CONFIG["api_url"],
        bearer_token: Optional[str] = DEFAULT_CONFIG["bearer_token"],
        request_retry=MAX_RETRY,
        retry_status=DEFAULT_RETRY_REASONS,
    ):
        """Create a client for the Software Heritage Web API

        See: :swh_web:`api/`

        Args:
            api_url: base URL for API calls
            bearer_token: optional bearer token to do authenticated API calls
        """
        api_url = api_url.rstrip("/")
        u = urlparse(api_url)

        self.api_url = api_url
        self.api_path = u.path
        self.bearer_token = bearer_token
        self._max_retry = request_retry
        self._retry_status = retry_status

        self._getters: Dict[ObjectType, Callable[[SWHIDish, bool], Any]] = {
            ObjectType.CONTENT: self.content,
            ObjectType.DIRECTORY: self.directory,
            ObjectType.RELEASE: self.release,
            ObjectType.REVISION: self.revision,
            ObjectType.SNAPSHOT: self._get_snapshot,
        }
        # assume we will do multiple call and keep the connection alive
        self._session = requests.Session()

        self._rate_limit_lock = threading.Lock()
        self._latest_request_date = 0
        self._latest_rate_limit_info = None

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

        headers = {}
        if self.bearer_token is not None:
            headers = {"Authorization": f"Bearer {self.bearer_token}"}

        if http_method not in ("get", "post", "head"):
            raise ValueError(f"unsupported HTTP method: {http_method}")

        return self._retryable_call(http_method, url, headers, req_args)

    def _retryable_call(self, http_method, url, headers, req_args):
        assert http_method in ("get", "post", "head"), http_method

        retry = self._max_retry
        delay = 0.1
        while retry > 0:
            retry -= 1
            r = self._one_call(http_method, url, headers, req_args)
            if r.status_code not in self._retry_status:
                r.raise_for_status()
                break
            if logger.isEnabledFor(logging.DEBUG):
                msg = f"HTTP RETRY {http_method} {url} delay={delay} remaining-tries={retry}"
                logger.debug(msg)
            time.sleep(delay)
            delay *= 2
        return r

    def _one_call(self, http_method, url, headers, req_args):
        """call on request and update rate limit info if available"""
        assert http_method in ("get", "post", "head"), http_method
        is_dbg = logger.isEnabledFor(logging.DEBUG)
        rate_limit = self._latest_rate_limit_info
        if rate_limit is not None:
            delay = rate_limit.pratical_delay(time.time())
            if delay > 0:
                time.sleep(delay)
        if is_dbg:
            dbg_msg = f"HTTP CALL {http_method} {url}"
            if rate_limit is not None:
                rate_dbg = f" latest-rate-limit-info=%r delay={delay}"
                rate_dbg %= rate_limit
                dbg_msg += rate_dbg
            logger.debug(dbg_msg)
        start = time.monotonic()
        with self._rate_limit_lock:
            if start > self._latest_request_date:
                self._latest_request_date = start
        if http_method == "get":
            r = self._session.get(url, **req_args, headers=headers)
        elif http_method == "post":
            r = self._session.post(url, **req_args, headers=headers)
        elif http_method == "head":
            r = self._session.head(url, **req_args, headers=headers)
        end = time.monotonic()
        with self._rate_limit_lock:
            if end > self._latest_request_date:
                self._latest_request_date = end

        if is_dbg:
            dbg_msg = f"HTTP REPLY {r.status_code} {http_method} {url}"

        rate_limit_header = _parse_limit_header(r)
        if None not in rate_limit_header:
            new = _RateLimitInfo(start, end, *rate_limit_header)
            if is_dbg:
                dbg_msg += " rate-limit-info=%r" % new
            with self._rate_limit_lock:
                existing = self._latest_rate_limit_info
                if existing is None or new.replacing(existing):
                    # no pre-existing data
                    self._latest_rate_limit_info = new
        if is_dbg:
            logger.debug(dbg_msg)
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
        if isinstance(swhid, str):
            obj_type = CoreSWHID.from_string(swhid).object_type
        else:
            obj_type = swhid.object_type
        return self._getters[obj_type](swhid, typify)

    def iter(
        self, swhid: SWHIDish, typify: bool = True, **req_args
    ) -> Iterator[Dict[str, Any]]:
        """Stream over the information about an object of any kind

        Streaming variant of get()

        """
        if isinstance(swhid, str):
            obj_type = CoreSWHID.from_string(swhid).object_type
        else:
            obj_type = swhid.object_type
        if obj_type == ObjectType.SNAPSHOT:
            yield from self.snapshot(swhid, typify)
        elif obj_type == ObjectType.REVISION:
            yield from [self.revision(swhid, typify)]
        elif obj_type == ObjectType.RELEASE:
            yield from [self.release(swhid, typify)]
        elif obj_type == ObjectType.DIRECTORY:
            yield from self.directory(swhid, typify)
        elif obj_type == ObjectType.CONTENT:
            yield from [self.content(swhid, typify)]
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
            f"content/sha1_git:{_get_object_id_hex(swhid)}/", **req_args
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
        json = self._call(f"directory/{_get_object_id_hex(swhid)}/", **req_args).json()
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
        json = self._call(f"revision/{_get_object_id_hex(swhid)}/", **req_args).json()
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
        json = self._call(f"release/{_get_object_id_hex(swhid)}/", **req_args).json()
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
        query = f"snapshot/{_get_object_id_hex(swhid)}/"

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

    def last_visit(self, origin: str, typify: bool = True) -> Dict[str, Any]:
        """Return the last visit of an origin.

        Args:
            origin: the URL of a software origin
            typify: if True, convert return value to pythonic types wherever
                possible, otherwise return raw JSON types (default: True)

        Returns:
            The last visit for that origin

        Raises:
            requests.HTTPError: if HTTP request fails

        """
        query = f"origin/{origin}/visit/latest/"
        r = self._call(query, http_method="get")
        visit = r.json()
        return typify_json(visit, ORIGIN_VISIT) if typify else visit

    def known(
        self, swhids: Iterable[SWHIDish], **req_args
    ) -> Dict[CoreSWHID, Dict[Any, Any]]:
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
        all_swh_ids = list(swhids)
        chunks = _get_known_chunk(all_swh_ids)
        all_results = []
        for c in chunks:
            ids = list(map(str, c))
            r = self._call("known/", http_method="post", json=ids, **req_args)
            all_results.append(r.json())
        results = itertools.chain.from_iterable(e.items() for e in all_results)
        return {CoreSWHID.from_string(k): v for k, v in results}

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
                f"content/sha1_git:{_get_object_id_hex(swhid)}/",
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
                f"directory/{_get_object_id_hex(swhid)}/",
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
                f"revision/{_get_object_id_hex(swhid)}/",
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
                f"release/{_get_object_id_hex(swhid)}/",
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
                f"snapshot/{_get_object_id_hex(swhid)}/",
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
            self._call(
                f"origin/{origin}/get/",
                http_method="head",
                **req_args,
            )
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
            f"content/sha1_git:{_get_object_id_hex(swhid)}/raw/",
            stream=True,
            **req_args,
        )
        r.raise_for_status()

        yield from r.iter_content(chunk_size=None, decode_unicode=False)

    def origin_search(
        self,
        query: str,
        limit: Optional[int] = None,
        with_visit: bool = False,
        **req_args,
    ) -> Iterator[Dict[str, Any]]:
        """List origin search results

        Args:
            query: search keywords
            limit: the maximum number of found origins to return
            with_visit: if true, only return origins with at least one visit

        Returns:
            an iterator over search results

        Raises:
            requests.HTTPError: if HTTP request fails

        """

        params = []
        if limit is not None:
            params.append(("limit", limit))
        if with_visit:
            params.append(("with_visit", True))

        done = False
        nb_returned = 0
        q = f"origin/search/{query}/"
        while not done:
            r = self._call(q, params=params, **req_args)
            json = r.json()
            if limit and nb_returned + len(json) > limit:
                json = json[: limit - nb_returned]

            nb_returned += len(json)
            yield from json

            if limit and nb_returned == limit:
                done = True

            if "next" in r.links and "url" in r.links["next"]:
                params = []
                q = r.links["next"]["url"]
            else:
                done = True

    def origin_save(self, visit_type: str, origin: str) -> Dict:
        """Save code now query for the origin with visit_type.

        Args:
            visit_type: Type of the visit
            origin: the origin to save

        Returns:
            The resulting dict of the visit saved

        Raises:
            requests.HTTPError: if HTTP request fails

        """
        q = f"origin/save/{visit_type}/url/{origin}/"
        r = self._call(q, http_method="post")
        return r.json()

    def get_origin(self, swhid: CoreSWHID) -> Optional[Any]:
        """Walk the compressed graph to discover the origin of a given swhid

        This method exist for the swh-scanner and is likely to change
        significantly and/or be replaced, we do not recommend using it.
        """
        key = str(swhid)
        q = (
            f"graph/randomwalk/{key}/ori/"
            f"?direction=backward&limit=-1&resolve_origins=true"
        )
        with self._call(q, http_method="get") as r:
            return r.text

    def cooking_request(
        self, bundle_type: str, swhid: SWHIDish, email: Optional[str] = None, **req_args
    ) -> Dict[str, Any]:
        """Request a cooking of a bundle

        Args:
            bundle_type: Type of the bundle
            swhid: object persistent identifier
            email: e-mail to notify when the archive is ready
            req_args: extra keyword arguments for requests.post()


        Returns:
            an object containing the following keys:
                fetch_url (string): the url from which to download the archive
                progress_message (string): message describing the cooking task progress
                id (number): the cooking task id
                status (string): the cooking task status (new/pending/done/failed)
                swhid (string): the identifier of the object to cook

        Raises:
            requests.HTTPError: if HTTP request fails

        """
        q = f"vault/{bundle_type}/{swhid}/"
        r = self._call(
            q,
            http_method="post",
            json={"email": email},
            **req_args,
        )
        r.raise_for_status()
        return r.json()

    def cooking_check(
        self, bundle_type: str, swhid: SWHIDish, **req_args
    ) -> Dict[str, Any]:
        """Check the status of a cooking task

        Args:
            bundle_type: Type of the bundle
            swhid: object persistent identifier
            req_args: extra keyword arguments for requests.get()


        Returns:
            an object containing the following keys:
                fetch_url (string): the url from which to download the archive
                progress_message (string): message describing the cooking task progress
                id (number): the cooking task id
                status (string): the cooking task status (new/pending/done/failed)
                swhid (string): the identifier of the object to cook

        Raises:
            requests.HTTPError: if HTTP request fails

        """
        q = f"vault/{bundle_type}/{swhid}/"
        r = self._call(
            q,
            http_method="get",
            **req_args,
        )
        r.raise_for_status()
        return r.json()

    def cooking_fetch(
        self, bundle_type: str, swhid: SWHIDish, **req_args
    ) -> requests.models.Response:
        """Fetch the archive of a cooking task

        Args:
            bundle_type: Type of the bundle
            swhid: object persistent identifier
            req_args: extra keyword arguments for requests.get()


        Returns:
            a requests.models.Response object containing a stream of the archive

        Raises:
            requests.HTTPError: if HTTP request fails

        """
        q = f"vault/{bundle_type}/{swhid}/raw"
        r = self._call(
            q,
            http_method="get",
            stream=True,
            **req_args,
        )
        r.raise_for_status()
        return r
