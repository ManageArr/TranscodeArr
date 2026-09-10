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

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TIMEOUT = 20

# An interactive search is not a request to an arr, it is a request to every
# indexer the arr has, and it takes as long as the slowest of them. Measured on
# a real library: 4s warm, 28s cold, and one indexer behind FlareSolverr is
# configured for 90s on its own. At the 20s every other call uses, a cold search
# failed at 21s with "the read operation timed out" while Sonarr was still
# waiting perfectly happily.
#
# High enough that the arr is always the one that gives up first. It already has
# a per-indexer timeout and returns what did answer, and a partial list of real
# releases beats an error about a limit this worker invented.
SEARCH_TIMEOUT = 180

# trackedDownloadState values that mean "finished downloading, and the arr has
# not taken it". Deliberately not "importing", which is the arr getting on with
# it, and not "imported", which is the happy ending.
AWAITING_IMPORT = frozenset({"importPending", "importBlocked", "importFailed"})

# Queue states that mean the DOWNLOAD CLIENT has not started this yet, as
# opposed to anything the arr is or is not doing. Worth telling apart because
# the two look identical in trackedDownloadState and the answer to them is
# opposite: one is waiting its turn and needs nothing, the other is stuck.
CLIENT_WAITING = frozenset({"queued", "paused"})

# urllib announces itself as "Python-urllib/3.x", which Cloudflare and several
# reverse proxies answer with a flat 403 before the request ever reaches the
# arr. Found against a real proxied Sonarr, where a correct API key looked
# exactly like a rejected one.
USER_AGENT = "TranscodeArr"


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse redirects instead of following them.

    urllib replays the request - including X-Api-Key - at whatever Location it
    is handed, so one 302 turns a connection test into delivering the arr's API
    key to a third party. An arr never legitimately redirects these two
    endpoints, so refusing is free.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirects)


def blocked_reason(url: str) -> str | None:
    """Why this URL must never be requested, or None when it is allowed.

    An arr connection is caller-supplied, so this endpoint is a request the
    container makes on a stranger's behalf. Private space is deliberately NOT
    blocked: a real Radarr lives on the LAN (192.168/10.x), on a Docker bridge
    (172.16/12, or just the container name), or on loopback when the container
    is host-networked, and refusing those would break the correct setup for
    almost everyone. The one destination that is never an arr is link-local -
    169.254.169.254 is the cloud metadata service, where a "connection test"
    becomes a read of the host's instance credentials.

    Names are resolved before deciding, so a hostname aimed at metadata is
    caught as well as the literal address.
    """
    host = urllib.parse.urlsplit(url).hostname
    if not host:
        return "Refusing to connect: no host in the URL"
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        # A name that does not resolve cannot reach metadata either, and the
        # real DNS error is far more useful to whoever typed the URL.
        return None
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:  # not an address family we can route to anyway
            continue
        # ::ffff:169.254.169.254 and 2002:a9fe:a9fe:: land on the same metadata
        # service, and neither reports is_link_local in its wrapped form.
        ip = getattr(ip, "ipv4_mapped", None) or getattr(ip, "sixtofour", None) or ip
        if ip.is_link_local:
            return (f"Refusing to connect to {host} ({ip}): link-local addresses "
                    "(169.254.0.0/16, fe80::/10) are the cloud metadata service, not an arr")
    return None


def _request(method: str, url: str, api_key: str, body: dict | None = None,
             timeout: int = TIMEOUT) -> tuple[Any, str | None]:
    # Guarded here rather than at the API handler because every outbound call
    # carrying the X-Api-Key goes through this one function, including the
    # rescans that run later from a stored row.
    blocked = blocked_reason(url)
    if blocked:
        return None, blocked
    req = urllib.request.Request(url, method=method)
    req.add_header("X-Api-Key", api_key)
    req.add_header("User-Agent", USER_AGENT)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with _opener.open(req, data=data, timeout=timeout) as res:
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
    except TimeoutError:
        # "The read operation timed out" names neither the wait nor its length,
        # which is the whole content of the answer when a call is slow by nature.
        return None, f"no answer in {timeout}s"
    except (json.JSONDecodeError, ValueError) as e:
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


def _release_view(r: dict) -> dict:
    """One release, reduced to what a person choosing between them needs.

    Not the raw arr object: that is fifty fields wide, carries three different
    spellings of the episode numbering, and would make this API a passthrough
    for whatever the next arr version decides to add. `rejections` is kept in
    full and never summarised - it is the reason a release is refused, and
    hiding it would leave somebody clicking Grab with no idea what they are
    overriding.
    """
    quality = ((r.get("quality") or {}).get("quality") or {}).get("name") or ""
    return {
        "guid": r.get("guid") or "",
        "indexer_id": r.get("indexerId"),
        "indexer": r.get("indexer") or "",
        "title": r.get("title") or "",
        "size": r.get("size") or 0,
        "age_days": r.get("age"),
        "seeders": r.get("seeders"),
        "leechers": r.get("leechers"),
        "protocol": r.get("protocol") or "",
        "quality": quality,
        "custom_format_score": r.get("customFormatScore"),
        "rejected": bool(r.get("rejected")),
        "rejections": [str(x) for x in (r.get("rejections") or [])],
        "download_allowed": bool(r.get("downloadAllowed", True)),
        "release_weight": r.get("releaseWeight"),
    }


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

    def _owning_item(self, worker_file: str) -> tuple[dict | None, str]:
        """The movie/series that owns this file, or (None, why)."""
        arr_file = to_arr_path(worker_file, self.worker_path, self.arr_path)
        if arr_file is None:
            return None, "not under this connection's root"
        items, error = self.items()
        if error and not items:
            return None, error
        item = find_item(items, arr_file)
        if item is None:
            items, error = self.items(force=True)
            if error:
                return None, error
            item = find_item(items, arr_file)
        if item is None:
            return None, f"no {self.kind} title owns {arr_file}"
        return item, arr_file

    def _grab(self, item: dict, arr_file: str) -> tuple[dict | None, str]:
        """The grab that produced this file, as {id, release, type, episode_id}.

        A dict rather than a tuple on purpose: this grew from two fields to
        four while it was being written, and every widening silently changed
        the arity of eight early returns.

        The grab is what gets marked failed, and marking a grab failed is what
        puts the release on the blocklist - the only thing that stops the next
        search handing back the identical file and reproducing the identical
        failure.
        """
        episode_id = None
        if self.kind == "radarr":
            if not (item.get("movieFile") or {}).get("id"):
                return None, "radarr has no file recorded for this movie"
            res, error = _request(
                "GET", f"{self.base_url}/api/v3/history/movie?movieId={item.get('id')}", self.api_key)
            if error:
                return None, error
            records = res or []
        else:
            res, error = _request(
                "GET", f"{self.base_url}/api/v3/episodefile?seriesId={item.get('id')}", self.api_key)
            if error:
                return None, error
            # relativePath is what the arr stores; arr_file is absolute.
            wanted = arr_file[len(str(item.get("path") or "").rstrip("/")) + 1:]
            episode_file = next((f for f in (res or []) if f.get("relativePath") == wanted), None)
            if episode_file is None:
                return None, f"sonarr has no file recorded at {wanted}"
            res, error = _request(
                "GET",
                f"{self.base_url}/api/v3/episode?seriesId={item.get('id')}"
                f"&episodeFileId={episode_file.get('id')}",
                self.api_key)
            if error:
                return None, error
            episode = next((e for e in (res or []) if e.get("episodeFileId") == episode_file.get("id")), None)
            if episode is None:
                return None, "sonarr has no episode for that file"
            episode_id = episode.get("id")
            res, error = _request(
                "GET", f"{self.base_url}/api/v3/history?episodeId={episode_id}&pageSize=50", self.api_key)
            if error:
                return None, error
            records = (res or {}).get("records") or []
        grab = next((h for h in records if h.get("eventType") == "grabbed"), None)
        if grab is None:
            # Imported by hand, or the history has aged out. Nothing to
            # blocklist, and searching without one would grab it straight back.
            return None, "no grab in this arr's history - nothing to blocklist"
        return {
            "id": grab.get("id"),
            "release": str(grab.get("sourceTitle") or ""),
            # What the arr itself recorded the release as: SingleEpisode,
            # MultiEpisode or SeasonPack. Authoritative, where reading "S07"
            # out of the title would be a guess about a naming convention.
            "type": str((grab.get("data") or {}).get("releaseType") or ""),
            "episode_id": episode_id,
        }, ""

    def replace_bad_file(self, worker_file: str, allow_packs: bool = False) -> tuple[bool, str, dict | None]:
        """Blocklist the release this file came from, so a search finds another.

        Returns (handled, message). handled=False means "not mine" - the caller
        tries the next connection, exactly as rescan_for does.

        Marking the grab failed is the whole point. Deleting the file and
        searching would let the arr hand back the identical release, convert it,
        fail on the identical unreadable stretch, and go round again - which is
        the loop this exists to break. The arr starts its own search off the
        back of the blocklist, so nothing here asks for one.

        `allow_packs` is the operator's answer to the one question this cannot
        decide for them. A grab records what it was - SingleEpisode,
        MultiEpisode or SeasonPack - and blocklisting a pack retires the release
        the rest of a season came from on the strength of one bad episode.
        Off, a pack is reported and left alone; on, it is blocklisted, which is
        the only thing that stops the arr handing that same pack straight back.
        Radarr has no packs, so this never applies to a movie.
        """
        item, why = self._owning_item(worker_file)
        if item is None:
            return ((False, why, None) if "not under" in why or "owns" in why else (True, why, None))
        grab, problem = self._grab(item, why)
        if grab is None:
            return True, problem, None
        detail, release_type = grab["release"], grab["type"]
        # SingleEpisode is the only kind that cannot cost somebody else an
        # episode, so it is the only one allowed without an explicit opt-in.
        # An arr that recorded nothing is refused too: unknown is not the same
        # as safe when the consequence is retiring a release.
        if not allow_packs and release_type != "SingleEpisode":
            was = f"was a {release_type}" if release_type else "is a release this arr did not classify"
            return True, (f"{self.name}: {detail or 'that release'} {was} - not blocklisting it. Turn on "
                          "'even when it came from a season pack' to allow this, or replace the file "
                          "yourself."), None
        _, error = _request(
            "POST", f"{self.base_url}/api/v3/history/failed/{grab['id']}", self.api_key, {})
        if error:
            return True, f"could not blocklist: {error}", None
        title = item.get("title", item.get("id"))
        kind = f" ({release_type})" if release_type else ""
        return True, (f"{self.name}: blocklisted {detail or 'the release'}{kind} and asked for a "
                      f"replacement of {title}"), {"arr_id": self.id, "arr_name": self.name, "kind": self.kind,
                                                   "item_id": item.get("id"), "episode_id": grab["episode_id"],
                                                   "release": detail}

    def queue_status(self, item_id: int, episode_id: int | None) -> dict | None:
        """How the replacement download is going, or None if it is not started.

        None covers three different situations that look identical from here -
        the search found nothing yet, the grab has not happened, or it finished
        and was imported - so the caller says "waiting" rather than inventing a
        reason it cannot know. An arr that could not be READ is not one of them,
        and raises: "there is no download" and "I got no answer" must not come
        out as the same sentence.

        Scoped to the one series or film instead of paging the whole queue. The
        paged form read the first 200 rows, which was fine until the download
        client was given a queue of its own - the arr then holds a row per
        grab rather than per active download, and the live queue is 1,916 rows
        long. Two of the three replacements being waited on sat at rows 872 and
        949, so this returned None for both and the page called them
        "searching" while they were downloading perfectly well.
        """
        q = f"seriesId={item_id}" if self.kind == "sonarr" else f"movieId={item_id}"
        res, error = _request("GET", f"{self.base_url}/api/v3/queue/details?{q}", self.api_key)
        if error:
            raise RuntimeError(f"could not read {self.name}'s queue: {error}")
        for record in res or []:
            same = (record.get("episodeId") == episode_id if episode_id is not None
                    else record.get("movieId") == item_id)
            if not same:
                continue
            size, left = record.get("size") or 0, record.get("sizeleft") or 0
            state = record.get("trackedDownloadState") or ""
            # Every reason the arr gave for not finishing, flattened. Sonarr
            # nests them as {title, messages[]} and the title alone is usually
            # the release name, so the messages are the part worth reading.
            # Only the messages. An entry's `title` is the FILE the arr is
            # talking about, not the reason - and these strings are what decides
            # whether a refusal may be overridden, so feeding a release name to
            # that rule refused every force with a filename as the explanation.
            messages = []
            for m in record.get("statusMessages") or []:
                messages.extend(str(x) for x in (m.get("messages") or []) if str(x).strip())
            return {
                "title": record.get("title") or "",
                # The download is done and the arr has not taken it. That is the
                # state this worker can actually do something about, and it is
                # NOT the same as "importing" - one is stuck, the other is busy.
                "awaiting_import": state in AWAITING_IMPORT,
                "download_id": record.get("downloadId") or "",
                "messages": [m for m in messages if m],
                # trackedDownloadState is the honest one for what the ARR is
                # doing: "downloading" in status can still mean stalled,
                # waiting for an import, or failed and about to be retried.
                # It is not honest about the CLIENT, though - it says
                # "downloading" for a torrent the client has queued and not
                # started, and with a download queue turned on that is most of
                # them: 1,489 of 1,890 rows the day this was written. So the
                # client's own word wins when it is the one holding things up.
                "status": (record.get("status") if record.get("status") in CLIENT_WAITING
                           else state or record.get("status") or ""),
                "percent": round((size - left) / size * 100) if size else None,
                "size": size,
                "client": record.get("downloadClient") or "",
                "error": record.get("errorMessage") or "",
            }
        return None

    def import_candidates(self, download_id: str) -> tuple[list[dict], str | None]:
        """What the arr would import from a finished download, and why it will not.

        The same list its own Manual Import screen shows. Each entry carries the
        arr's rejections, which is the whole basis for deciding whether forcing
        it is an override or a mistake - "not an upgrade" is the file we already
        have talking, "invalid video file" is this download talking, and only
        one of those is ours to overrule.
        """
        res, error = _request(
            "GET", f"{self.base_url}/api/v3/manualimport?downloadId={download_id}&filterExistingFiles=false",
            self.api_key, timeout=SEARCH_TIMEOUT)
        if error:
            return [], error
        out = []
        for f in res or []:
            episodes = [e.get("id") for e in (f.get("episodes") or []) if e.get("id")]
            out.append({
                "id": f.get("id"),
                "path": f.get("path") or "",
                "folder_name": f.get("folderName") or "",
                "item_id": ((f.get("series") or f.get("movie")) or {}).get("id"),
                "episode_ids": episodes,
                "quality": f.get("quality"),
                "languages": f.get("languages") or [],
                "release_group": f.get("releaseGroup") or "",
                "indexer_flags": f.get("indexerFlags") or 0,
                "size": f.get("size") or 0,
                # {reason, type} in newer arrs, a bare string in older ones.
                "rejections": [str(r.get("reason") if isinstance(r, dict) else r)
                               for r in (f.get("rejections") or [])],
            })
        return out, None

    def force_import(self, candidate: dict, download_id: str) -> tuple[bool, str]:
        """Import one file the arr has declined to import on its own.

        This is the arr's own Manual Import, which is the only thing that
        overrules an import rejection - and it is a real override: the arr
        REPLACES the existing file, and with no recycle bin configured it
        deletes it rather than keeping a copy. Never called on a rejection that
        is about this download; see core.overridable_only.
        """
        payload = {
            "path": candidate["path"],
            "folderName": candidate.get("folder_name") or "",
            "downloadId": download_id,
            "quality": candidate.get("quality"),
            "languages": candidate.get("languages") or [],
            "releaseGroup": candidate.get("release_group") or "",
            "indexerFlags": candidate.get("indexer_flags") or 0,
        }
        if self.kind == "sonarr":
            payload["seriesId"] = candidate.get("item_id")
            payload["episodeIds"] = candidate.get("episode_ids") or []
        else:
            payload["movieId"] = candidate.get("item_id")
        _, error = _request("POST", f"{self.base_url}/api/v3/command", self.api_key,
                            {"name": "ManualImport", "importMode": "move", "files": [payload]})
        return (False, error) if error else (True, "")

    def find_target(self, worker_file: str) -> tuple[dict | None, str]:
        """What a replacement search needs for this file: item, episode, title.

        Resolved from the PATH rather than from the grab that produced the
        file. A job can fail verification on a file whose grab the arr has
        long since pruned from its history, or that was imported by hand and
        never grabbed at all, and those are exactly the files somebody wants to
        go looking for a replacement of. replace_bad_file needs the grab
        because it blocklists it; a search does not.
        """
        item, arr_file = self._owning_item(worker_file)
        if item is None:
            return None, arr_file
        if self.kind == "radarr":
            return {"item_id": item.get("id"), "episode_id": None,
                    "title": item.get("title") or str(item.get("id"))}, ""
        res, error = _request(
            "GET", f"{self.base_url}/api/v3/episode?seriesId={item.get('id')}&includeEpisodeFile=true",
            self.api_key)
        if error:
            return None, f"{self.name}: {error}"
        for ep in res or []:
            if ((ep.get("episodeFile") or {}).get("path") or "") == arr_file:
                return {"item_id": item.get("id"), "episode_id": ep.get("id"),
                        "title": "%s S%02dE%02d" % (item.get("title") or "",
                                                    ep.get("seasonNumber") or 0,
                                                    ep.get("episodeNumber") or 0)}, ""
        return None, f"{self.name}: no episode of {item.get('title')} has this file"

    def search_releases(self, item_id: int, episode_id: int | None) -> tuple[list[dict], str | None]:
        """Every release the indexers offer for this item, in the ARR's order.

        The same call the arr's own Interactive Search makes, and the ordering
        it returns is kept. This list is read beside that page, and a worker
        that re-scored the results would disagree with it and have no way to
        explain why.

        Slow on purpose - it really does go out to every indexer - so nothing
        calls it on a timer or from a view that refreshes.
        """
        q = f"episodeId={episode_id}" if episode_id is not None else f"movieId={item_id}"
        res, error = _request("GET", f"{self.base_url}/api/v3/release?{q}", self.api_key,
                              timeout=SEARCH_TIMEOUT)
        if error:
            return [], f"{error} - the indexers were still being asked" if "no answer" in error else error
        return [_release_view(r) for r in (res or [])], None

    def grab_release(self, guid: str, indexer_id: int) -> tuple[bool, str]:
        """Tell the arr to download one specific release, refusals and all.

        This is a MANUAL grab and that is the whole point. Every release
        offered for a file this worker is waiting on comes back rejected with
        "Existing file meets cutoff", because the unreadable file is still on
        disk and still satisfies the profile. The arr is not wrong to refuse -
        it cannot see that the file is unplayable. A person can.
        """
        _, error = _request("POST", f"{self.base_url}/api/v3/release", self.api_key,
                            {"guid": guid, "indexerId": indexer_id})
        return (False, error) if error else (True, "")

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
