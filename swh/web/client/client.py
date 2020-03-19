# Copyright (C) 2019-2020  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

"""Python client for the Software Heritage Web API

Light wrapper around requests for the archive API, taking care of data
conversions and pagination.

.. code-block:: python

   from swh.web.client import WebAPIClient
   cli = WebAPIClient()

   # retrieve any archived object via its PID
   cli.get('swh:1:rev:aafb16d69fd30ff58afdd69036a26047f3aebdc6')

   # same, but for specific object types
   cli.revision('swh:1:rev:aafb16d69fd30ff58afdd69036a26047f3aebdc6')

   # get() always retrieve entire objects, following pagination
   # WARNING: this might *not* be what you want for large objects
   cli.get('swh:1:snp:6a3a2cf0b2b90ce7ae1cf0a221ed68035b686f5a')

   # type-specific methods support explicit iteration through pages
   next(cli.snapshot('swh:1:snp:cabcc7d7bf639bbe1cc3b41989e1806618dd5764'))

"""

from typing import Any, Callable, Dict, Generator, List, Union
from urllib.parse import urlparse

import dateutil.parser
import requests

from swh.model.identifiers import \
    SNAPSHOT, REVISION, RELEASE, DIRECTORY, CONTENT
from swh.model.identifiers import PersistentId as PID
from swh.model.identifiers import parse_persistent_identifier as parse_pid


PIDish = Union[PID, str]


def _get_pid(pidish: PIDish) -> PID:
    """Parse string to PID if needed"""
    if isinstance(pidish, str):
        return parse_pid(pidish)
    else:
        return pidish


def typify(data: Any, obj_type: str) -> Any:
    """Type API responses using pythonic types where appropriate

    The following conversions are performed:

    - identifiers are converted from strings to PersistentId instances
    - timestamps are converted from strings to datetime.datetime objects

    """
    def to_pid(object_type, s):
        return PID(object_type=object_type, object_id=s)

    def to_date(s):
        return dateutil.parser.parse(s)

    def obj_type_of_entry_type(s):
        if s == 'file':
            return CONTENT
        elif s == 'dir':
            return DIRECTORY
        elif s == 'rev':
            return REVISION
        else:
            raise ValueError(f'invalid directory entry type: {s}')

    if obj_type == SNAPSHOT:
        for name, target in data.items():
            if target['target_type'] != 'alias':
                # alias targets do not point to objects via PIDs; others do
                target['target'] = to_pid(target['target_type'],
                                          target['target'])
    elif obj_type == REVISION:
        data['id'] = to_pid(obj_type, data['id'])
        data['directory'] = to_pid(DIRECTORY, data['directory'])
        for key in ('date', 'committer_date'):
            data[key] = to_date(data[key])
        for parent in data['parents']:
            parent['id'] = to_pid(REVISION, parent['id'])
    elif obj_type == RELEASE:
        data['id'] = to_pid(obj_type, data['id'])
        data['date'] = to_date(data['date'])
        data['target'] = to_pid(data['target_type'], data['target'])
    elif obj_type == DIRECTORY:
        dir_pid = None
        for entry in data:
            dir_pid = dir_pid or to_pid(obj_type, entry['dir_id'])
            entry['dir_id'] = dir_pid
            entry['target'] = to_pid(obj_type_of_entry_type(entry['type']),
                                     entry['target'])
    elif obj_type == CONTENT:
        pass  # nothing to do for contents
    else:
        raise ValueError(f'invalid object type: {obj_type}')

    return data


class WebAPIClient:
    """Client for the Software Heritage archive Web API, see

    https://archive.softwareheritage.org/api/

    """

    def __init__(self, api_url='https://archive.softwareheritage.org/api/1'):
        """Create a client for the Software Heritage Web API

        See: https://archive.softwareheritage.org/api/

        Args:
            api_url: base URL for API calls (default:
                "https://archive.softwareheritage.org/api/1")

        """
        api_url = api_url.rstrip('/')
        u = urlparse(api_url)

        self.api_url = api_url
        self.api_path = u.path

        self._getters: Dict[str, Callable[[PIDish], Any]] = {
            CONTENT: self.content,
            DIRECTORY: self.directory,
            RELEASE: self.release,
            REVISION: self.revision,
            SNAPSHOT: self._get_snapshot,
        }

    def _call(self, query: str, http_method: str = 'get',
              **req_args) -> requests.models.Response:
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
            url = '/'.join([self.api_url, query])
        r = None

        if http_method == 'get':
            r = requests.get(url, **req_args)
            r.raise_for_status()
        elif http_method == 'head':
            r = requests.head(url, **req_args)
        else:
            raise ValueError(f'unsupported HTTP method: {http_method}')

        return r

    def _get_snapshot(self, pid: PIDish) -> Dict[str, Any]:
        """Analogous to self.snapshot(), but zipping through partial snapshots,
        merging them together before returning

        """
        snapshot = {}
        for snp in self.snapshot(pid):
            snapshot.update(snp)

        return snapshot

    def get(self, pid: PIDish, **req_args) -> Any:
        """Retrieve information about an object of any kind

        Dispatcher method over the more specific methods content(),
        directory(), etc.

        Note that this method will buffer the entire output in case of long,
        iterable output (e.g., for snapshot()), see the iter() method for
        streaming.

        """

        pid_ = _get_pid(pid)
        return self._getters[pid_.object_type](pid_)

    def iter(self, pid: PIDish, **req_args) -> Generator[Dict[str, Any],
                                                         None, None]:
        """Stream over the information about an object of any kind

        Streaming variant of get()

        """
        pid_ = _get_pid(pid)
        obj_type = pid_.object_type
        if obj_type == SNAPSHOT:
            yield from self.snapshot(pid_)
        elif obj_type == REVISION:
            yield from [self.revision(pid_)]
        elif obj_type == RELEASE:
            yield from [self.release(pid_)]
        elif obj_type == DIRECTORY:
            yield from self.directory(pid_)
        elif obj_type == CONTENT:
            yield from [self.content(pid_)]
        else:
            raise ValueError(f'invalid object type: {obj_type}')

    def content(self, pid: PIDish, **req_args) -> Dict[str, Any]:
        """Retrieve information about a content object

        Args:
            pid: object identifier
            req_args: extra keyword arguments for requests.get()

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        return typify(
            self._call(f'content/sha1_git:{_get_pid(pid).object_id}/',
                       **req_args).json(),
            CONTENT)

    def directory(self, pid: PIDish, **req_args) -> List[Dict[str, Any]]:
        """Retrieve information about a directory object

        Args:
            pid: object identifier
            req_args: extra keyword arguments for requests.get()

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        return typify(
            self._call(f'directory/{_get_pid(pid).object_id}/',
                       **req_args).json(),
            DIRECTORY)

    def revision(self, pid: PIDish, **req_args) -> Dict[str, Any]:
        """Retrieve information about a revision object

        Args:
            pid: object identifier
            req_args: extra keyword arguments for requests.get()

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        return typify(
            self._call(f'revision/{_get_pid(pid).object_id}/',
                       **req_args).json(),
            REVISION)

    def release(self, pid: PIDish, **req_args) -> Dict[str, Any]:
        """Retrieve information about a release object

        Args:
            pid: object identifier
            req_args: extra keyword arguments for requests.get()

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        return typify(
            self._call(f'release/{_get_pid(pid).object_id}/',
                       **req_args).json(),
            RELEASE)

    def snapshot(self, pid: PIDish,
                 **req_args) -> Generator[Dict[str, Any], None, None]:
        """Retrieve information about a snapshot object

        Args:
            pid: object identifier
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
        query = f'snapshot/{_get_pid(pid).object_id}/'

        while not done:
            r = self._call(query, http_method='get', **req_args)
            yield typify(r.json()['branches'], SNAPSHOT)
            if 'next' in r.links and 'url' in r.links['next']:
                query = r.links['next']['url']
            else:
                done = True

    def content_exists(self, pid: PIDish, **req_args) -> bool:
        """Check if a content object exists in the archive

        Args:
            pid: object identifier
            req_args: extra keyword arguments for requests.head()

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        return bool(self._call(f'content/sha1_git:{_get_pid(pid).object_id}/',
                               http_method='head', **req_args))

    def directory_exists(self, pid: PIDish, **req_args) -> bool:
        """Check if a directory object exists in the archive

        Args:
            pid: object identifier
            req_args: extra keyword arguments for requests.head()

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        return bool(self._call(f'directory/{_get_pid(pid).object_id}/',
                               http_method='head', **req_args))

    def revision_exists(self, pid: PIDish, **req_args) -> bool:
        """Check if a revision object exists in the archive

        Args:
            pid: object identifier
            req_args: extra keyword arguments for requests.head()

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        return bool(self._call(f'revision/{_get_pid(pid).object_id}/',
                               http_method='head', **req_args))

    def release_exists(self, pid: PIDish, **req_args) -> bool:
        """Check if a release object exists in the archive

        Args:
            pid: object identifier
            req_args: extra keyword arguments for requests.head()

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        return bool(self._call(f'release/{_get_pid(pid).object_id}/',
                               http_method='head', **req_args))

    def snapshot_exists(self, pid: PIDish, **req_args) -> bool:
        """Check if a snapshot object exists in the archive

        Args:
            pid: object identifier
            req_args: extra keyword arguments for requests.head()

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        return bool(self._call(f'snapshot/{_get_pid(pid).object_id}/',
                               http_method='head', **req_args))

    def content_raw(self, pid: PIDish,
                    **req_args) -> Generator[bytes, None, None]:
        """Iterate over the raw content of a content object

        Args:
            pid: object identifier
            req_args: extra keyword arguments for requests.get()

        Raises:
          requests.HTTPError: if HTTP request fails

        """
        r = self._call(f'content/sha1_git:{_get_pid(pid).object_id}/raw/',
                       stream=True, **req_args)
        r.raise_for_status()

        yield from r.iter_content(chunk_size=None, decode_unicode=False)
