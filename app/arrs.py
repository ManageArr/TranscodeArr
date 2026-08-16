"""Talking to Radarr and Sonarr.

Why this exists at all: TranscodeArr replaces a file in place, and a Jellyfin
library scan provably does not notice a same-name in-place replacement
(jellyfin#13565, closed "not planned"). The arr is what re-reads the file and
re-publishes its real codec, bitrate and runtime - without this step the whole
stack keeps serving stale metadata about a file that no longer exists in that
form, and makes wrong direct-play decisions off it.

Only two calls are ever made against an arr: read its library list, and ask it
to rescan one title. Nothing here writes to the arr's database.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

TIMEOUT = 20

# urllib announces itself as "Python-urllib/3.x", which Cloudflare and several
# reverse proxies answer with a flat 403 before the request ever reaches the
# arr. Found against a real proxied Sonarr, where a correct API key looked
# exactly like a rejected one.
USER_AGENT = "TranscodeArr"


def _request(method: str, url: str, api_key: str, body: dict | None = None) -> tuple[Any, str | None]:
    req = urllib.request.Request(url, method=method)
    req.add_header("X-Api-Key", api_key)
    req.add_header("User-Agent", USER_AGENT)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=TIMEOUT) as res:
            raw = res.read()
            return (json.loads(raw) if raw else None), None
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return None, "Invalid API key"
        if e.code == 403:
            # Sonarr answers a bad key with 401; a 403 usually came from
            # something in front of it. Saying "invalid API key" here sends
            # people to re-copy a key that was right all along.
            return None, "Refused (403) - the arr, or a proxy in front of it, rejected the request"
        detail = ""
        try:
            detail = json.loads(e.read() or b"{}").get("message", "")
        except Exception:  # noqa: BLE001 - a non-JSON error body is still just an error
            pass
        return None, f"HTTP {e.code}{': ' + detail if detail else ''}"
    except urllib.error.URLError as e:
        return None, f"Cannot reach {url}: {e.reason}"
    except (TimeoutError, json.JSONDecodeError, ValueError) as e:
        return None, str(e)


def test(base_url: str, api_key: str) -> tuple[bool, str]:
    """Connection test: returns (ok, detail-or-error)."""
    res, error = _request("GET", f"{base_url}/api/v3/system/status", api_key)
    if error:
        return False, error
    name = (res or {}).get("appName") or "arr"
    return True, f"{name} {(res or {}).get('version', '')}".strip()


def to_arr_path(worker_path: str, worker_root: str, arr_root: str) -> str | None:
    """Translate a path this container sees into the path the arr sees.

    The two mount the same directory at different places - the container may
    call it /media/TV while Sonarr calls it /tv. Returns None when the file is
    not under this connection's root at all, which is how one arr ignores
    another's libraries instead of guessing.
    """
    if not worker_root or not arr_root:
        return None
    root = worker_root.rstrip("/")
    if worker_path != root and not worker_path.startswith(root + "/"):
        return None
    return arr_root.rstrip("/") + worker_path[len(root):]


def find_item(items: list[dict], arr_path: str) -> dict | None:
    """The movie/series whose folder contains this file.

    Longest path first: a library with both /tv/Show and /tv/Show Extras would
    otherwise match the shorter one by accident.
    """
    best = None
    for item in items:
        folder = str(item.get("path") or "").rstrip("/")
        if not folder:
            continue
        if arr_path == folder or arr_path.startswith(folder + "/"):
            if best is None or len(folder) > len(str(best.get("path") or "")):
                best = item
    return best


class ArrClient:
    """One arr, with its library list cached - a rescan should not re-download
    2,800 movies for every file that finishes."""

    CACHE_SECONDS = 600

    def __init__(self, row: dict) -> None:
        self.id = row["id"]
        self.name = row["name"]
        self.kind = row["kind"]
        self.base_url = row["base_url"].rstrip("/")
        self.api_key = row["api_key"]
        self.arr_path = row.get("arr_path", "")
        self.worker_path = row.get("worker_path", "")
        self._items: list[dict] = []
        self._fetched = 0.0

    @property
    def _list_path(self) -> str:
        return "/api/v3/movie" if self.kind == "radarr" else "/api/v3/series"

    @property
    def _command(self) -> str:
        return "RescanMovie" if self.kind == "radarr" else "RescanSeries"

    @property
    def _id_field(self) -> str:
        return "movieId" if self.kind == "radarr" else "seriesId"

    def items(self, force: bool = False) -> tuple[list[dict], str | None]:
        if not force and self._items and time.time() - self._fetched < self.CACHE_SECONDS:
            return self._items, None
        res, error = _request("GET", f"{self.base_url}{self._list_path}", self.api_key)
        if error:
            return self._items, error
        self._items = res or []
        self._fetched = time.time()
        return self._items, None

    def rescan_for(self, worker_file: str) -> tuple[bool, str]:
        """Ask this arr to re-read the title that owns `worker_file`.

        Returns (handled, message). handled=False means "not mine" - the caller
        tries the next connection rather than treating it as a failure.
        """
        arr_file = to_arr_path(worker_file, self.worker_path, self.arr_path)
        if arr_file is None:
            return False, "not under this connection's root"
        items, error = self.items()
        if error and not items:
            return False, error
        item = find_item(items, arr_file)
        if item is None:
            # A title added since the cache was filled is the likely cause, so
            # spend one refresh before giving up.
            items, error = self.items(force=True)
            if error:
                return False, error
            item = find_item(items, arr_file)
        if item is None:
            return False, f"no {self.kind} title owns {arr_file}"
        _, error = _request(
            "POST", f"{self.base_url}/api/v3/command", self.api_key,
            {"name": self._command, self._id_field: item.get("id")},
        )
        if error:
            return True, error
        return True, f"{self.name}: rescanning {item.get('title', item.get('id'))}"
