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
import heapq
import itertools
import logging
import queue
import threading
import time
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple, Union
from urllib.parse import urlparse
import weakref

import attr
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


# how many nanoseconds is one second:
#
# We use nanoseconds for the time arythmetic because it does not suffer from
# the same issue as floats with precision and drifting.
_1_SECOND = 1_000_000_000


class _RateLimitInfo:
    """Object that holds rate limit information and can compute delay

    The rate limiting information always come from an http request.

    It holds the following attributes:

    start:         date of the request start (seconds since epoch)
    end:           date of the request end (seconds since epoch)
    limit:         maximum number of requests in the rate limit window
    remaining:     number of remaining requestss
    reset_date:    date of rate limit window reset (second since epoch)

    reset_date_ns: date of rate limit window reset (nanoseconds since epoch)
    wait_ns:       ideal number of nanoseconds to wait between each request.
    free_token:    the number of token available initially
                   (see the `setup_free_token` method for details)

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
    >>> # with a full budget
    >>> full = _RateLimitInfo(42, 50, 1000, 1000, reset)
    >>> assert (0.0999 * _1_SECOND) < full.wait_ns < (0.1001 * _1_SECOND)
    >>> # with a half full budget
    >>> half = _RateLimitInfo(42, 50, 1000, 500, reset)
    >>> assert (0.1999 * _1_SECOND) < half.wait_ns < (0.2001 * _1_SECOND)
    >>> # with an empty budget
    >>> empty = _RateLimitInfo(42, 50, 1000, 0, reset)
    >>> assert (99.001 * _1_SECOND) < empty.wait_ns < (100.001 * _1_SECOND)
    """

    # amount of the available budget that we should give for free
    INITIAL_FREE_TOKEN_RATIO = 0.1
    # under which budget should we stop giving away free tokens
    FREE_TOKENS_CUTOFF_RATIO = 0.25

    def __init__(
        self,
        start: float,
        end: float,
        limit: int,
        remaining: int,
        reset: float,
    ):
        self.start = start
        self.end = end
        self.limit = limit
        self.remaining = remaining
        self.reset_date = reset

        self.free_token = 0

    def __repr__(self) -> str:
        r = "<RateLimitInfo start=%s, end=%s, budget=%d/%d, reset_date=%d>"
        r %= (self.start, self.end, self.remaining, self.limit, self.reset_date)
        return r

    @property
    def reset_date_ns(self) -> int:
        reset_date_ns = int(self.reset_date * _1_SECOND)
        # cache the value for future call
        self.__dict__["reset_date_ns"] = reset_date_ns
        return reset_date_ns

    @property
    def wait_ns(self) -> int:
        duration = (self.reset_date - self.end) * _1_SECOND
        limited = self.remaining
        limited -= self.free_token
        if limited <= 0:
            wait_ns = duration
        else:
            wait_ns = duration // limited
        wait_ns = int(wait_ns)
        wait_ns = max(wait_ns, 1)  # make sure wait_ns > 0
        # cache the value for future call
        self.__dict__["wait_ns"] = wait_ns
        return wait_ns

    def replacing(self, other: "_RateLimitInfo") -> bool:
        """return True if `self` as "better" information that "other"o

        The two criteria to decide something is better are:
        - `self` is about a later rate limiting windows than `other`
        - `self` is about the same window but requires a significantly longer
                 wait time.
        """
        if other.reset_date != self.reset_date:
            # the one with a later reset date is likely more up to date.
            return other.reset_date < self.reset_date

        # replace is significantly stricter
        return other.wait_ns < (self.wait_ns / 0.9)

    def setup_free_token(self) -> None:
        """setup the count of free token, adjusting the wait time

        This is a simple way to provide "progressive" rate limiting, that allow
        fast-paced initial request while slowing larger batch of request.

        This is achieved by initially granting a fraction of the initial budget
        without delay. The remaining budget is then paced on the remaining
        time.

        Example:

            We start with:
            - remaining request 100
            - remaining window: 60 seconds
            - free budget: 10%

            So `setup_free_token` results in:
            - 10 token available,
            - 90 requests paced over 60s, i.e. one request every ⅔ second.

            This give use a basic "progressive" rate in practice:
            - issuing   1 request  requires  0.00 seconds (0.00s / request)
            - issuing  10 requests requires  0.00 seconds (0.00s / request)
            - issuing  11 requests requires  0.66 seconds (0.06s / request)
            - issuing  15 requests requires  3.33 seconds (0.22s / request)
            - issuing  20 requests requires  6.66 seconds (0.33s / request)
            - issuing  50 requests requires 26.66 seconds (0.53s / request)
            - issuing 100 requests requires 60.00 seconds (0.60s / request)

        The progressive effect is still "less" good that having a fancy curve
        for the token generation, this is currently "good enough" for a
        fraction of the complexity.
        """
        if self.remaining / self.limit <= self.FREE_TOKENS_CUTOFF_RATIO:
            # we don't grant free token if the budget is running low as this is
            # the sign that many request are being issued in general.
            self.free_token = 0
        else:
            free = self.remaining * self.INITIAL_FREE_TOKEN_RATIO
            self.free_token = int(free)
        # clear `wait_ns` cache
        self.__dict__.pop("wait_ns", None)


@attr.s(slots=True, order=True)
class _RateLimitEvent:
    """Represent a date at which a rate limiting action is needed for a client

    This is used by _RateLimitEnforcer to schedule actions.
    """

    # when is this event due, (from time.monotonic_ns())
    date = attr.ib(type=int)
    # the rate limit this try to enforce
    rate_limit = attr.ib(type=_RateLimitInfo)
    # the client it applies too
    client_ref = attr.ib(type=weakref.ref)


_ALL_CLIENT_TYPE = weakref.WeakKeyDictionary["WebAPIClient", _RateLimitInfo]


# a pair of Semaphore: `(available, waiting)`
#
# `available`:
#   hold one token for each request we can currently make while respecting the
#   rate limit. It is filled in the background by the _RateLimitEnforcer`
#   thread.
# `waiting`:
#   hold one token for each request currently trying to acquire a "available"
#   token. These tokens are added and removed by the code doing the request.
#   (and ultimately by the `_RateLimitEnforcer` thread through
#   WebAPIClient._clear_rate_limit_tokens)
_RateLimitTokens = Tuple[threading.Semaphore, threading.Semaphore]


class _RateLimitEnforcer:
    """process rate limiting information into rate limiting token

    This object is meant to be run forever in a daemon thread. That daemon
    thread is started by the `_RateLimitEnforcer._get_limiter` class method when
    needed.

    The WebAPIClient send the rate limiting information they receive from the
    server into the `feed` Queue they get from that same `_get_limiter` class
    method, using the `new_info` class method.

    This function process these rate limiting information and slowly issue
    "available request" token to a `threading.Semaphore` instance at the
    appropriate rate.  These "available request" token are consumed by request,
    practically reducing the rate of requests.
    """

    _queue: Optional[queue.Queue] = None
    _limiter: Optional["_RateLimitEnforcer"] = None
    _limiter_thread = None  # not really needed, but lets keep it around.
    _limiter_lock = threading.Lock()

    @classmethod
    def new_info(cls, client: "WebAPIClient", info: _RateLimitInfo) -> None:
        """pass new _RateLimitInfo from client to the _RateLimitEnforcer"""
        feed = cls._get_limiter()
        feed.put((client, info))

    @classmethod
    def current_rate_limit_delay(cls, client: "WebAPIClient") -> float:
        """return the current rate limit delay for this Client (in second)"""
        limiter = cls._limiter
        if limiter is None:
            return 0.0
        rate_limit = limiter._all_clients.get(client)
        if rate_limit is None:
            return 0.0
        wait_ns = rate_limit.wait_ns
        if wait_ns == 1:
            return 0.0
        return max(rate_limit.wait_ns / _1_SECOND, 0.0)

    @classmethod
    def _get_limiter(cls) -> queue.Queue:
        """return the current queue that gather rate limit information

        That function will initialize that Queue and the associated daemon thread
        the first time it is called.
        """
        if cls._queue is None:
            with cls._limiter_lock:
                if cls._queue is None:
                    cls._queue = queue.Queue()
                    cls._limiter = cls(cls._queue)
                    cls._limiter_thread = threading.Thread(
                        target=cls._limiter._run_forever,
                        name=f"{__name__}._RateLimitEnforcer.run",
                        daemon=True,
                    )
                    cls._limiter_thread.start()
        return cls._queue

    def __init__(self, feed: queue.Queue):
        self._feed: queue.Queue = feed
        # a heap of _RateLimitInfo
        #
        # contains a date-ordered list of the futur _RateLimitInfo to proceed.
        #
        self._events: list[_RateLimitEvent] = []
        # a mapping for most up-to-date information for each WebAPIClient
        self._all_clients: _ALL_CLIENT_TYPE = weakref.WeakKeyDictionary()

    def _run_forever(self):
        """main entry points loop forever.

        Proceed new incoming information from Client and managing the
        _RateLimitTokens of the associated client.

        This must be run in a daemonized thread.
        """
        while True:
            self._consume_ready_events()
            self._process_infos()

    def _add_event(self, event: _RateLimitEvent) -> None:
        """schedule a new event"""
        heapq.heappush(self._events, event)

    def _consume_ready_events(self) -> None:
        """Consume Rate Limit event that are ready

        This find all events whose time is up, and process them.
        """

        current = time.monotonic_ns()
        for client, this_event in self._next_events(current):
            client._add_one_rate_limit_token()

            rate_limit = this_event.rate_limit

            # schedule the next event
            if rate_limit.reset_date_ns >= current:
                this_event.date += rate_limit.wait_ns
                self._add_event(this_event)
            else:
                # The windows closed. we should not reschedule an event.
                # The first request in the new window will rearm the logic.
                self._all_clients.pop(client, None)
                client._clear_rate_limit_tokens()

    def _next_events(
        self,
        current: int,
    ) -> Iterator[Tuple["WebAPIClient", _RateLimitEvent]]:
        """iterate over the (client, event) pair that is both ready and valid

        Readiness is computed compared to "current".

        return None if no such events exists.
        """
        while self._events and self._events[0].date <= current:
            event = heapq.heappop(self._events)

            # determine if that event is still valid
            client = event.client_ref()
            if client is None:
                # that client is no longer active
                continue
            latest_rate_limit = self._all_clients.get(client)
            if latest_rate_limit is None:
                # that client is no longer active (narrow race)
                continue
            if latest_rate_limit is not event.rate_limit:
                # that event was superseded by a more recent one, lets ignore it
                continue
            yield (client, event)

    def _process_infos(self):
        """process incoming _RateLimitInfo

        This process all available _RateLimitInfo, then wait until the next
        _RateLimitEvent is due for newer _RateLimitInfo.

        So if no new _RateLimitInfo come, this is equivalent to a sleep until
        the next _RateLimitEvent.
        """
        for current, client, rate_limit in self._next_infos():
            old = self._all_clients.get(client)
            if old is None or rate_limit.replacing(old):
                # We lets consider the time between the generation of this
                # limit server side and its processing negligible
                current = time.monotonic_ns()
                event = _RateLimitEvent(
                    date=current + rate_limit.wait_ns,
                    client_ref=weakref.ref(client),
                    rate_limit=rate_limit,
                )
                self._all_clients[client] = rate_limit
                if old is None:
                    # If this is the initial requests, we give the user a small
                    # free budget
                    #
                    # We do not do this when renewing the windows because we
                    # assume that if some rate limit information was still in
                    # place from the previous windows, the connection is
                    # somewhat heavily used.
                    rate_limit.setup_free_token()
                if old is None or old.reset_date != rate_limit.reset_date:
                    client._refresh_rate_limit_tokens(rate_limit.free_token)
                self._add_event(event)

    def _next_infos(
        self,
    ) -> Iterator[Tuple[int, "WebAPIClient", _RateLimitInfo]]:
        """iterate over the available (client, _RateLimitInfo) pairs

        If no new information are currently available, this wait until then
        wait until the next _RateLimitEvent is due. If no new information
        arrive during that time. The iteration is over.
        """
        while True:
            current = time.monotonic_ns()
            wait_ns = _1_SECOND  # default wait if there is nothing to do
            if self._events:
                wait_ns = self._events[0].date - current
            try:
                # passing timeout 0, or negative timeout will create issue, so
                # we set the minimum to one nano second.
                wait_ns = max(wait_ns, 1)
                wait = wait_ns / _1_SECOND
                client, rate_limit = self._feed.get(timeout=wait)
            except queue.Empty:
                # No external information received.
                #
                # However we reached the next items in `self._events` and need
                # to process them.
                break
            else:
                yield (current, client, rate_limit)


def _free_existing_request(tokens: _RateLimitTokens) -> None:
    r"""Internal Rate Limiting Method. Do not call directly.

    Unlock all waiting requests for a _RateLimitTokens. This is used when
    the rate limit windows for which the tokens applies concludes.

    The _RateLimitTokens consist of two Semaphores, "available" and
    "waiting".

    When this method no other code should be able to get access to that
    token.  Some client might still hold a reference to it, but the
    `_RateLimitEnforcer` will no longer add "available" tokens to it.

    The "waiting" semaphore contains one token for each request currently
    waiting for an available token. The goal is to unlock all of them. So
    add one "available" token for each "waiting-token" we can find. We
    acquire them before issuing the "available-token" otherwise the Thread
    doing request will concurrently acquire their "waiting-token" too,
    making our count wrong.

    /!\ This is an internal method related to rate-limiting management.  /!\
    /!\ It should only be called by the `_RateLimitEnforcer` function. /!\
    """
    available, waiting = tokens
    need_unlock = 0
    while waiting.acquire(blocking=False):
        need_unlock += 1
    for r in range(need_unlock):
        available.release()


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
        use_rate_limit: bool = True,
    ):
        """Create a client for the Software Heritage Web API

        See: :swh_web:`api/`

        Args:
            api_url: base URL for API calls
            bearer_token: optional bearer token to do authenticated API calls
            use_rate_limit: enable or disable request pacing according to
                            server rate limit information.

        With rate limiting enabled (the default), the client will adjust its
        request rate if the server provides Rate limiting headers.

        The rate limiting will pace out the available requests evenly in the
        rate limit windows. (except for a small initial budget as explained
        below)

        For example, if there is 600 request remaining for a windows that reset
        in 5 minutes (300 second), a request will be issuable every 0.5
        seconds.

        This pace will be enforced overall, allowing for period of inactivity
        between faster spike.

        For example (using the same number as above):

        - A client that tries to issue requests continuously will have to wait
          0.5 second between each requests.

        - A client that did not issue requests for 1 minutes (60 seconds) will
          be able to issue 120 requests right away (60 / 0.5) before having to
          wait 0.5 second between requests.

        The above is true regardless of the number of threads using the same
        WebAPIClient.

        In practice, to avoid slowing down small application doing few
        requests, 10% of the available budget is available immediately, the other
        90% of the requests being spread out over the rate limit window.o

        This initial "immediate" budget is only granted if at least 25% of the
        total request budget is available.
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

        self._use_rate_limit: bool = use_rate_limit
        self._rate_tokens: Optional[_RateLimitTokens] = None

    @property
    def rate_limit_delay(self):
        """current rate limit delay in second"""
        return _RateLimitEnforcer.current_rate_limit_delay(self)

    def _add_one_rate_limit_token(self) -> None:
        r"""Internal Rate Limiting Method. Do not call directly.

        This method is called when one extra request can be issued.

        /!\ This is an internal method related to rate-limiting management.  /!\
        /!\ It should only be called by the `_RateLimitEnforcer` function.   /!\
        """
        assert self._rate_tokens is not None
        self._rate_tokens[0].release()

    def _clear_rate_limit_tokens(self) -> None:
        r"""Internal Rate Limiting Method. Do not call directly.

        This is called used when a rate limit window conclude as we reached its
        end date.

        At that point we disable rate limiting and free any waiting requests.

        In some case, information about a new windows will be received before
        we detect this windows expiration and `_refresh_rate_limit_tokens` will
        be called instead.

        /!\ This is an internal method related to rate-limiting management.  /!\
        /!\ It should only be called by the `_RateLimitEnforcer` function.   /!\
        """
        tokens = self._rate_tokens
        self._rate_tokens = None
        if tokens is not None:
            _free_existing_request(tokens)

    def _refresh_rate_limit_tokens(self, free_token=0) -> None:
        r"""Internal Rate Limiting Method. Do not call directly.

        Setup a new Rate limit Windows by resetting the rate limit Semaphores.
        This is used when a RateLimitInfo for a newer windows is received.

        When a rate limit windows conclude, there is two possibles situations:

        1) There is no waiting request: the number of available token in ≥ 0.
        2) There is waiting request: the number of available token is 0.

        In the case (1) We need to discard this available tokens. A new Rate
        limiting window will start, and it need a blank slate. The request we
        did not do early are irrelevant for that new windows.

        For example, let says rate limit a window s 1 minute long with 100
        request. If the client issued only one request in each of the past two
        minutes, it used only 2 request out of a 200 total budget. However
        server side the budget reset for each windows, so at the start of the
        new windows, the client can only issue 100 request over the next
        minute. If we preserved token from only window to the next, at the
        point the client would have 198 available token already (+ 100 to
        accumulate over the next minute) a number totally disconnected from the
        server state.

        So, we have to reset the Semaphore token for each window.


        However, some request might still be waiting for token on the Semaphore
        of the old Windows. If we do nothing they would be stuck forever. We
        could do some fancy logic to transfer these waiting requests to the new
        semaphore, but is is significantly simpler to just unlock them. They'll
        consume some of the budget of the new windows, and the rate limiting
        will adjust to the remaining budget.

        If the number of waiting request is exceed (or even it close to) the
        total budget of the next windows, this means request are being made at
        an unreasonable parallelism level and there will be troubles anyways.

        /!\ This is an internal method related to rate-limiting management.  /!\
        /!\ It should only be called by the `_RateLimitEnforcer` function.   /!\
        """
        if not self._use_rate_limit:
            return
        tokens = self._rate_tokens
        self._rate_tokens = (
            threading.Semaphore(),  # available request
            threading.Semaphore(),  # waiting request
        )
        for i in range(free_token):
            self._rate_tokens[0].release()
        if tokens is not None:
            _free_existing_request(tokens)

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
                msg = (
                    f"HTTP RETRY {http_method} {url}"
                    f" delay={delay:.6f} remaining-tries={retry}"
                )
                logger.debug(msg)
            time.sleep(delay)
            delay *= 2
        return r

    def _one_call(self, http_method, url, headers, req_args):
        """call on request and update rate limit info if available"""
        assert http_method in ("get", "post", "head"), http_method
        is_dbg = logger.isEnabledFor(logging.DEBUG)
        delay = 0
        pre_grab = time.monotonic()
        tokens = self._rate_tokens
        if tokens is not None:
            available, waiting = tokens
            # signal we wait for a token, to ensure a refresh of the rate_token
            # does not leave us hanging forever.
            #
            # See `_free_existing_request(…)` for details.
            waiting.release()
            try:
                # If the `rate_token` tuple changed since we read it, this means
                # the tokens Semaphore where refreshed, and it might have
                # happened before our "waiting-token" was registered. Therefore
                # we cannot 100% rely on the "waiting-token" to ensure we will
                # eventually get a "available-request-token" available for us.
                #
                # We ignore the rate limiting logic in that case. The race is
                # narrow enough that it is unlikely to create issue in
                # practice.
                #
                # If the `rate_token` tuple did not change, we are certain the
                # "waiting-token" will be taken in account in the case a
                # refresh happends while wait for an "available-request-token".
                if tokens is self._rate_tokens:
                    # respect the rate limit enforced globally
                    #
                    # the `available` Semaphore is filled by the code in
                    # _RateLimitEnforcer
                    available.acquire()
            finally:
                # signal we no longer need to be saved from infinite hang
                #
                # We do a non-blocking acquire, because if the _RateLimitTokens
                # is being discarded, the `_RateLimitEnforcer` might have
                # acquired *our* "waiting-token" in the process of unlocking
                # this thread.
                waiting.acquire(blocking=False)
            delay = time.monotonic() - pre_grab
        if is_dbg:
            dbg_msg = f"HTTP CALL {http_method} {url}"
            if delay:
                dbg_msg += f" delay={delay:.6f}"
            logger.debug(dbg_msg)
        start = time.time()
        if http_method == "get":
            r = self._session.get(url, **req_args, headers=headers)
        elif http_method == "post":
            r = self._session.post(url, **req_args, headers=headers)
        elif http_method == "head":
            r = self._session.head(url, **req_args, headers=headers)
        end = time.time()

        if is_dbg:
            dbg_msg = f"HTTP REPLY {r.status_code} {http_method} {url}"

        rate_limit_header = _parse_limit_header(r)
        if None not in rate_limit_header:
            new = _RateLimitInfo(start, end, *rate_limit_header)
            if is_dbg:
                dbg_msg += " rate-limit-info=%r" % new
            _RateLimitEnforcer.new_info(self, new)
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
