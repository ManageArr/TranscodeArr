"""Runtime settings, API keys and arr connections - the things that used to be
frozen into container environment variables.

**The precedence rule, and why it is this way round:** a value saved in the UI
lives in the database and wins over the environment from then on. An env var is
only the seed for a key nobody has set yet. The other way round - env always
wins - means every `docker run` with a stale env silently reverts what someone
changed in the UI, and they find out when their library has been re-encoded to
the wrong quality. The UI says which values are stored and which are still
coming from the container, so the override is visible rather than mysterious.

`media_roots` is deliberately NOT settable here: it names the volume mounts
themselves, so changing it at runtime would point the worker at paths the
container cannot see. It stays an env var because it belongs to the container's
shape, not to its configuration.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from hashlib import scrypt, sha256
from typing import Any

import core

# ---------------------------------------------------------------------------
# Setting specs - one table describing every knob, so the API is self-describing
# and the GUI renders itself without a second hand-maintained list.
# ---------------------------------------------------------------------------


def is_within_any(path: str, roots: list[str]) -> bool:
    """Is `path` inside one of `roots`?

    Delegates to core.is_within rather than keeping a second copy of the rule -
    a containment check that exists twice is one that will disagree with itself
    eventually. Resolved first, so a symlink cannot point out of the roots.
    """
    return core.is_within(os.path.realpath(path), [os.path.realpath(r) for r in roots])


@dataclass
class Spec:
    key: str
    env: str
    kind: str  # text | int | bool | paths | exts | patterns
    default: Any
    label: str
    help: str
    group: str = "General"
    # Superseded by encoding profiles. Still read - they seed the first profile
    # on upgrade - but not shown, because a visible control that no longer does
    # anything is worse than no control at all.
    hidden: bool = False
    # Never leaves the container in readable form: masked in API responses and
    # left out of a config backup entirely. The daemon still reads the real
    # value through effective(), which is the only place it is needed.
    secret: bool = False


SPECS: list[Spec] = [
    Spec("watch_roots", "WATCH_ROOTS", "paths", [], "Watched folders",
         "Folders scanned for files to convert. Leave empty to watch every media root.", "Locations"),
    Spec("scan_interval_seconds", "SCAN_INTERVAL_SECONDS", "int", 300, "Scan interval (seconds)",
         "How often the watched folders are walked.", "Locations"),
    Spec("stable_seconds", "STABLE_SECONDS", "int", 120, "Stability window (seconds)",
         "A file is only touched once its size has held still for this long. Never based on modified time, "
         "which imports preserve from the release.", "Locations"),
    Spec("hidden_only", "HIDDEN_ONLY", "bool", False, "Only convert dot-hidden files",
         "Off, the default, converts every matching file in the folders you watch - point it at one folder "
         "first, because everything in there becomes eligible. On, only files whose name starts with a dot "
         "are eligible, which is worth setting up: a dot-hidden file is invisible to the media server, so "
         "nobody can start playing a file that is midway through being replaced, and a half-finished import "
         "is never picked up as though it were complete. Nothing writes that dot for you - see the README "
         "for the Radarr and Sonarr setup.", "Locations"),
    Spec("convert_extensions", "CONVERT_EXTENSIONS", "exts", [".mkv", ".avi", ".m4v", ".m2ts", ".mts", ".vob"],
         "Convert these extensions", "Dot-hidden .mp4 files are revealed without re-encoding regardless.", "Rules"),
    Spec("skip_patterns", "SKIP_PATTERNS", "patterns", [], "Never convert files matching",
         "Case-insensitive substrings matched against the file name. A match is revealed as-is instead of "
         "re-encoded - the way to protect Remux copies you would rather keep whole.", "Rules"),
    Spec("quality", "QUALITY", "int", 24, "Quality (CQ/CRF)",
         "Lower is better looking and bigger. 24 is a sane default; 18-20 is near-transparent.", "Rules", hidden=True),
    Spec("verify_duration_tolerance", "VERIFY_DURATION_TOLERANCE", "fraction", 0.015, "Duration tolerance",
         "How far the output length may drift from the source before the result is rejected, as a fraction: "
         "0.015 is 1.5%. Capped at 0.5 - a tolerance loose enough to accept half a film is not a tolerance.", "Rules"),
    Spec("force_encoder", "FORCE_ENCODER", "text", "", "Encoder",
         "Leave empty to pick the best one that actually works on this machine. Each encoder has its own "
         "quality scale, so change the quality above to match when you pin one.", "Rules", hidden=True),
    Spec("encoder_preset", "ENCODER_PRESET", "text", "", "Speed vs size",
         "The one real tradeoff in encoding: slower settings spend more time to make a smaller file at the "
         "same quality. Leave empty for the balanced default of whichever encoder is in use.", "Rules", hidden=True),
    Spec("encoder_profile", "ENCODER_PROFILE", "text", "", "Codec profile",
         "How modern a decoder the file expects. High is right for anything made this century; drop to Main "
         "or Baseline only for genuinely old hardware.", "Rules", hidden=True),
    Spec("max_height", "MAX_HEIGHT", "int", 0, "Resolution",
         "Caps the picture height. Never upscales - asking for 1080p leaves a 720p file at 720p, because "
         "scaling up costs space and invents nothing. 0 keeps the source resolution.", "Rules", hidden=True),
    Spec("hardware_decode", "HARDWARE_DECODE", "bool", True, "Decode on the GPU too",
         "The encoder was always on the GPU; the decoder was not, and decoding 1080p in software is what "
         "actually pins a NAS CPU. Measured on a real episode: 21.3s of CPU became 3.9s and the job ran 45% "
         "faster, for a byte-identical file. Only applies to NVIDIA encoders, and ffmpeg falls back to CPU "
         "decoding by itself for anything the card cannot decode.", "Rules"),
    Spec("max_concurrent", "MAX_CONCURRENT", "int", 1, "Convert at once",
         "One at a time suits a NAS: a single set of spindles behind a single network link turns two encodes "
         "into two slow ones. Raise it if your media sits on SSD or you have a card with no encode-session "
         "limit, and watch whether the total throughput actually improves.", "Rules"),
    Spec("retry_failed_after_hours", "RETRY_FAILED_AFTER_HOURS", "int", 6, "Retry failed files after (hours)",
         "How long the watcher leaves a file alone after a job for it failed. Without a wait, a file that "
         "cannot be converted at all is queued again on every scan and burns a whole encode attempt every "
         "few minutes forever. 0 retries on the next scan; queueing a file from the API ignores this.", "Rules"),
    Spec("stall_timeout_minutes", "STALL_TIMEOUT_MINUTES", "int", 30, "Stall timeout (minutes)",
         "An encode that reports no progress for this long is killed and its job is failed. ffmpeg reports "
         "progress about twice a second however slow the encode is, so silence means it is wedged - usually "
         "on a share that went away - and not that the file is large. 0 turns the watchdog off, and a "
         "stalled job then holds its slot for the life of the container.", "Rules"),
    Spec("replace_bad_source", "REPLACE_BAD_SOURCE", "bool", False,
         "Ask Sonarr/Radarr to replace an unreadable file",
         "Off by default, and it starts a download. When a conversion fails VERIFICATION because the source "
         "is short or missing a stream - not because the GPU stopped working, and not because a name was "
         "taken - the arr that owns the file is asked to mark the release it came from as failed. That "
         "blocklists it, which is the point: deleting the file and searching lets the arr hand back the "
         "identical release and reproduce the identical failure forever. Nothing here deletes your media; "
         "the arr replaces the file when its own search finds something. Asked once per file, so a "
         "replacement that is also bad stops and waits for a person. If the grab was a season pack, the "
         "PACK is blocklisted - it contains the unreadable episode, so any future grab of it brings the "
         "problem back.", "Rules"),
    Spec("replace_bad_source_packs", "REPLACE_BAD_SOURCE_PACKS", "bool", False,
         "...even when it came from a season pack",
         "Only matters when the setting above is on, and only for Sonarr - movies do not come in packs. A "
         "grab records what it was: SingleEpisode, MultiEpisode or SeasonPack. Off, only a single-episode "
         "release is ever blocklisted, so one unreadable episode can never retire the release the rest of a "
         "season came from - a pack is reported on the job and left for you to judge. On, the pack is "
         "blocklisted too, which is the only way the arr will stop handing that same pack back for this "
         "episode. Blocklisting a pack does not delete anything and does not touch the episodes already "
         "imported from it; it stops that release being grabbed again.", "Rules"),
    Spec("trash_keep_days", "TRASH_KEEP_DAYS", "int", 7, "Keep replaced sources (days)",
         "Replaced originals are moved to trash, never deleted outright. This is how long they survive. "
         "Raise it before a large batch - a source pruned mid-run is one you cannot get back.", "General"),
    Spec("keep_history_days", "KEEP_HISTORY_DAYS", "int", 30, "Keep job history (days)",
         "Done, failed and cancelled jobs older than this are deleted on the next scan. Nothing deleted them "
         "before, so a library-sized run left tens of thousands of rows in the same database file every "
         "writer here contends for. 0 keeps every row forever.", "General"),
    Spec("auto_start", "AUTO_START", "bool", True, "Start converting on boot",
         "On, the container starts converting as soon as it boots. Off, it boots paused and waits for someone "
         "to press Start - the right setting for a first run against a library nobody has swept yet. Run state "
         "lives in memory, so a manual pause does NOT survive a restart while this is on: that is what auto "
         "start means, and it is better said here than discovered.", "Schedule"),
    Spec("convert_window", "CONVERT_WINDOW", "text", "", "Only convert between",
         "One daily range as HH:MM-HH:MM, and 22:00-06:00 spans midnight the way you would expect. Empty means "
         "always. READ IN THE CONTAINER'S TIMEZONE, which is UTC unless you set TZ - a NAS on America/New_York "
         "with the default UTC container runs a window typed here four hours off, silently and all night, so "
         "set TZ in the compose file and check the local time this page shows beside the window. Only new work "
         "is gated: an encode already running always finishes, and the watcher keeps queueing while the window "
         "is shut, so a paused box with a growing queue is working exactly as intended.", "Schedule"),
    Spec("webhook_url", "WEBHOOK_URL", "text", "", "Webhook URL",
         "POSTed a JSON summary when a job finishes, done or failed. Empty disables it. Nothing about the "
         "webhook can affect a job: it is sent after the file is already correct on disk, so a receiver that "
         "is down, slow or wrong cannot fail an encode or take the worker with it.", "Notifications"),
    Spec("webhook_secret", "WEBHOOK_SECRET", "text", "", "Webhook signing secret",
         "Optional. When set, each call carries an HMAC-SHA256 of the exact body it sent, so the receiver can "
         "tell a real notification from anyone who guessed the URL. Never shown again once saved.",
         "Notifications", secret=True),
    Spec("encode_nice", "ENCODE_NICE", "int", 0, "Encoder CPU priority (nice)",
         "0 is normal, 19 is 'only take a core nobody else wants'. On a CPU-only box a libx265 job pins every "
         "core, and the first anyone hears about it is playback stuttering while their own library is being "
         "converted. Only lowers priority - raising it needs privileges this container does not have.",
         "Performance"),
    Spec("encode_idle_io", "ENCODE_IDLE_IO", "bool", False, "Encode at idle disk priority",
         "Reads and writes only when nothing else wants the disk. Niceness governs CPU, not IO, and on a NAS "
         "it is usually the spindles rather than the cores that a 40GB remux takes away from playback.",
         "Performance"),
    Spec("encode_threads", "ENCODE_THREADS", "int", 0, "Encoder threads",
         "0 lets ffmpeg decide, which means every core it can find. Cap it to keep a core free for the media "
         "server. The GPU encoders barely touch the CPU, so this is about libx264 and libx265.", "Performance"),
    Spec("tls_cert", "TLS_CERT", "text", "", "TLS certificate path",
         "Absolute path, inside the container, to a PEM certificate. Set this and the key together to serve "
         "HTTPS directly; leave both empty to serve plain HTTP. A reverse proxy that already terminates TLS is "
         "the better answer - this is for a LAN box that has no proxy.", "Security"),
    Spec("tls_key", "TLS_KEY", "text", "", "TLS private key path",
         "Absolute path, inside the container, to the matching PEM private key. Set one of the pair without "
         "the other, or point either at a file that cannot be loaded, and the container REFUSES TO START - a "
         "quiet downgrade would put your password on the wire in clear text while this page still said HTTPS. "
         "Clearing both is always the way back.", "Security"),
    Spec("session_days", "SESSION_DAYS", "int", 30, "Stay logged in for (days)",
         "How long a browser login lasts before the password is asked for again. Sessions can be revoked one "
         "by one, so a long window is not a lost key. API keys are not sessions and never expire.", "Security"),
]

SPEC_BY_KEY = {s.key: s for s in SPECS}

SECRET_KEYS = frozenset(s.key for s in SPECS if s.secret)
# The same mask an arr API key wears, so one thing in this UI means "stored,
# not shown" rather than two things that look different for no reason.
SECRET_MASK = "********"

# Sessions are capped at the same year the setting is capped at, so a caller
# that passes its own days cannot mint a token that outlives the policy.
SESSION_MAX_DAYS = 365


def parse_window(value: str) -> tuple[int, int] | None:
    """Read "HH:MM-HH:MM" as minutes past local midnight; None when it is empty.

    Spanning midnight is the normal case, not the edge case - overnight is when
    a NAS is free - so 22:00-06:00 comes back as (1320, 360) and every reader
    has to handle start > end. in_window() below is that reader, and it is the
    only one, precisely so the wrap rule cannot be implemented twice and
    disagree with itself.
    """
    text = (value or "").strip()
    if not text:
        return None
    m = re.fullmatch(r"([0-2]?\d):([0-5]\d)\s*-\s*([0-2]?\d):([0-5]\d)", text)
    if not m:
        raise ValueError("Time window must look like 22:00-06:00, or be empty for always")
    sh, sm, eh, em = (int(g) for g in m.groups())
    if sh > 23 or eh > 23:
        raise ValueError("Time window hours must be 00-23 - it is a 24 hour clock, and 24:00 is 00:00")
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end:
        # 06:00-06:00 is either zero minutes or the whole day depending on which
        # way you read it, and guessing wrong either converts nothing forever or
        # converts around the clock. Neither is worth guessing at.
        raise ValueError("Time window start and end are the same - leave it empty if you mean always")
    return start, end


def in_window(value: str, now: datetime | None = None) -> bool:
    """Is the local clock inside the convert window? Empty window means always.

    datetime.now() is deliberately naive local time: that is what TZ sets, and
    TZ is how the *arr ecosystem is told which clock the operator means.
    """
    try:
        span = parse_window(value)
    except ValueError:
        # Unreachable through the settings API - parse_value refuses a bad
        # window and _resolve falls back to empty - but a window nobody can
        # parse must not be able to hold the queue shut forever with no error
        # anywhere. Fail open: converting at an odd hour is an annoyance,
        # converting never is a broken container.
        return True
    if not span:
        return True
    start, end = span
    at = now or datetime.now()
    minutes = at.hour * 60 + at.minute
    return start <= minutes < end if start < end else (minutes >= start or minutes < end)


def parse_value(spec: Spec, raw: Any, roots: list[str] | None = None) -> Any:
    """Coerce a value from env text or JSON into the shape the daemon expects.

    Raises ValueError with a message meant for a person - it is shown in the UI.
    """
    if spec.kind == "fraction":
        # Bounded here rather than at the caller, because the caller is the
        # verification step: a tolerance of "15" (meaning 15%, a very easy
        # thing to type) accepted a three-second file in place of a
        # sixteen-minute one, which is the exact failure this project exists
        # to prevent. inf and nan fail the range test too.
        try:
            v = float(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{spec.label} must be a number, e.g. 0.015 for 1.5%")
        if not 0 < v <= 0.5:
            raise ValueError(f"{spec.label} must be greater than 0 and at most 0.5 (50%)")
        return v
    if spec.kind == "int":
        try:
            n = int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{spec.label} must be a whole number")
        if n < 0:
            raise ValueError(f"{spec.label} cannot be negative")
        if spec.key == "max_height" and n and not 240 <= n <= 4320:
            raise ValueError("Resolution must be 0 (source) or a height between 240 and 4320")
        if spec.key == "max_concurrent" and not 1 <= n <= 8:
            # Above a handful the disk is the limit on any NAS, and an
            # unbounded value here would spawn workers until the box gave up.
            raise ValueError("Convert at once must be between 1 and 8")
        if spec.key == "stall_timeout_minutes" and n and not 1 <= n <= 1440:
            # A watchdog shorter than the gap between progress lines would kill
            # healthy encodes, which is worse than the stall it guards against.
            raise ValueError("Stall timeout must be 0 (off) or between 1 and 1440 minutes")
        if spec.key == "encode_nice" and n > 19:
            # nice(1) tops out at 19; anything higher is silently clamped by the
            # kernel, which would leave a saved 30 meaning something it does not.
            raise ValueError("Encoder CPU priority must be between 0 (normal) and 19 (lowest)")
        if spec.key == "encode_threads" and n > 64:
            # Past the core count more threads cost coordination and save
            # nothing, and a typo of 1000 would have ffmpeg allocate for 1000.
            raise ValueError("Encoder threads must be 0 (let ffmpeg decide) or at most 64")
        if spec.key == "session_days" and not 1 <= n <= SESSION_MAX_DAYS:
            # 0 mints a session that has already expired, which reads as a login
            # form that accepts the right password and then refuses every page.
            raise ValueError(f"Stay logged in for must be between 1 and {SESSION_MAX_DAYS} days")
        return n
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if spec.kind in ("paths", "exts", "patterns"):
        if isinstance(raw, str):
            parts = [p.strip() for p in re.split(r"[:\n,]", raw)]
        elif isinstance(raw, (list, tuple)):
            parts = [str(p).strip() for p in raw]
        else:
            raise ValueError(f"{spec.label} must be a list")
        parts = [p for p in parts if p]
        if spec.kind == "exts":
            parts = [p if p.startswith(".") else f".{p}" for p in (x.lower() for x in parts)]
        if spec.kind == "paths":
            for p in parts:
                if not p.startswith("/"):
                    raise ValueError(f"{p} is not an absolute path")
                # Containment belongs here, not only at the point a file is
                # queued: a watch root outside the mounts cannot produce a job
                # (validate_path still refuses it) but it would still send the
                # scanner walking the host filesystem every few minutes.
                if roots and not is_within_any(p, roots):
                    raise ValueError(f"{p} is outside the media roots")
        return parts
    text = "" if raw is None else str(raw).strip()
    if text:
        if spec.key == "convert_window":
            parse_window(text)  # raises with the message the UI shows
        # Shape only. The link-local guard that stops an operator-supplied URL
        # reaching the cloud metadata service is arrs.blocked_reason, and it
        # belongs at the moment of sending, exactly as the arr client does it:
        # parse_value runs on every cfg() read, so resolving a name here would
        # be a DNS lookup per scan, per job and per page poll.
        if spec.key == "webhook_url" and not re.match(r"^https?://", text):
            raise ValueError("Webhook URL must start with http:// or https://")
        if spec.key in ("tls_cert", "tls_key") and not text.startswith("/"):
            # Relative to what? The daemon's working directory is not something
            # anyone typing this can see, and a certificate that silently is not
            # found means the server quietly keeps serving plain HTTP.
            raise ValueError(f"{spec.label} must be an absolute path inside the container")
    return text


def effective(rows: dict[str, str], env: dict[str, str], roots: list[str] | None = None) -> dict[str, Any]:
    """Resolve every setting: stored value, else env, else the built-in default.

    Pure - takes the stored rows and the environment as plain dicts so the
    precedence rule can be asserted in a test rather than trusted.
    """
    out: dict[str, Any] = {}
    for spec in SPECS:
        out[spec.key] = _resolve(spec, rows, env, roots)[0]
    return out


def _resolve(spec: Spec, rows: dict[str, str], env: dict[str, str], roots: list[str] | None):
    """(value, where-it-came-from) for one spec. One function so the value and
    the label the UI shows can never disagree about which source won."""
    if spec.key in rows:
        try:
            return parse_value(spec, json.loads(rows[spec.key]), roots), "stored"
        except (ValueError, json.JSONDecodeError):
            pass  # a corrupt or now-invalid row must not take the daemon down
    raw = env.get(spec.env)
    if raw not in (None, ""):
        try:
            return parse_value(spec, raw, roots), "env"
        except ValueError:
            pass
    return spec.default, "default"


def sources(rows: dict[str, str], env: dict[str, str], roots: list[str] | None = None) -> dict[str, str]:
    """Where each effective value actually came from, for the UI to show.

    Shares _resolve with effective() rather than re-deriving: a stored row that
    no longer parses is ignored by the value, and saying "stored" next to a
    value that came from somewhere else is how someone spends an hour editing
    a field that is doing nothing.
    """
    return {s.key: _resolve(s, rows, env, roots)[1] for s in SPECS}


def redact_secrets(values: dict[str, Any]) -> dict[str, Any]:
    """Every secret setting replaced by the mask, for anything a client sees.

    effective() returns the real values because the daemon needs them; this is
    the one gate between that dict and a response body, so a new secret spec is
    covered by setting one flag rather than by remembering to filter it at each
    route that happens to return settings.
    """
    return {k: (SECRET_MASK if k in SECRET_KEYS and v else v) for k, v in values.items()}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,           -- JSON
  updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  hash TEXT NOT NULL,            -- sha256 of the raw token; the raw is shown once
  prefix TEXT NOT NULL,          -- first 8 chars, so a key is identifiable in a list
  created REAL NOT NULL,
  last_used REAL
);
-- Named encoding profiles, in the HandBrake sense: one bundle of every choice
-- that produces a file, so switching is one decision instead of six.
-- validated_at is only set by a real test encode - see validate_profile.
CREATE TABLE IF NOT EXISTS profiles (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  encoder TEXT NOT NULL,
  quality INTEGER NOT NULL,
  preset TEXT NOT NULL DEFAULT '',
  profile TEXT NOT NULL DEFAULT '',
  max_height INTEGER NOT NULL DEFAULT 0,
  audio_codec TEXT NOT NULL DEFAULT 'aac',
  audio_bitrate INTEGER NOT NULL DEFAULT 192,
  audio_channels INTEGER NOT NULL DEFAULT 2,
  active INTEGER NOT NULL DEFAULT 0,
  -- builtin means SHIPPED: we wrote it, so it is read-only. Someone who wants a
  -- variant duplicates it, which keeps the five known-good starting points
  -- honest instead of letting an edit turn "Balanced, on the GPU" into
  -- something that is neither.
  builtin INTEGER NOT NULL DEFAULT 0,
  validated_at REAL,
  -- NULL never tested, 0 tested and failed, 1 tested and works. Distinguishing
  -- the first two matters: "we have not looked" and "this machine cannot do it"
  -- are opposite answers to "may I use this?".
  validated_ok INTEGER,
  validated_note TEXT,
  created REAL NOT NULL
);
-- The single admin login. id is pinned to 1 by the database rather than by a
-- convention in this file: two admin rows would make "which password is the
-- password" a race between whichever query ran first.
-- The scrypt cost is stored per row so it can be raised later without
-- invalidating the password somebody already set - the whole point of keeping
-- n, r and p beside the hash instead of hardcoding them at the verify site.
CREATE TABLE IF NOT EXISTS admin (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  username TEXT NOT NULL,
  hash TEXT NOT NULL,            -- hex scrypt output; never leaves this file
  salt TEXT NOT NULL,            -- hex, generated per password
  n INTEGER NOT NULL,
  r INTEGER NOT NULL,
  p INTEGER NOT NULL,
  updated REAL NOT NULL
);
-- Browser logins. Stored exactly like an API key - only a hash of the token,
-- with the same entropy - so a copy of this database is not a set of live
-- sessions. Separate from tokens because these expire and those do not, and
-- because revoking every browser must not touch the key an integration holds.
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  hash TEXT NOT NULL,
  prefix TEXT NOT NULL,          -- first 11 chars, so the page can point at its own row
  created REAL NOT NULL,
  expires REAL NOT NULL,
  last_used REAL
);
CREATE TABLE IF NOT EXISTS arrs (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,            -- radarr|sonarr
  base_url TEXT NOT NULL,
  api_key TEXT NOT NULL,
  arr_path TEXT NOT NULL DEFAULT '',     -- what the arr calls the library root
  worker_path TEXT NOT NULL DEFAULT '',  -- what this container calls the same place
  enabled INTEGER NOT NULL DEFAULT 1,
  last_error TEXT,
  created REAL NOT NULL
);
"""


def read_settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings").fetchall()}


def save_settings(conn: sqlite3.Connection, updates: dict[str, Any], roots: list[str] | None = None) -> list[str]:
    """Validate and store. Returns the keys written; raises ValueError on the first bad one.

    Everything is validated before anything is written, so a bad third field
    cannot leave the first two applied and the form half-saved.
    """
    checked = []
    for key, raw in updates.items():
        spec = SPEC_BY_KEY.get(key)
        if not spec:
            raise ValueError(f"{key} is not a setting")
        if spec.secret and raw == SECRET_MASK:
            # The form is sent back whole, and a secret is only ever rendered as
            # the mask - so a save of any unrelated field would otherwise store
            # the literal asterisks as the signing secret and every webhook
            # signature would be wrong with nothing in any log to say why.
            # An empty string still clears it, which is how you remove one.
            continue
        checked.append((key, parse_value(spec, raw, roots)))

    written = []
    for key, value in checked:
        conn.execute(
            "INSERT INTO settings (key, value, updated) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (key, json.dumps(value), time.time()),
        )
        written.append(key)
    conn.commit()
    return written


def reset_setting(conn: sqlite3.Connection, key: str) -> None:
    """Forget a stored value so the container env (or the default) applies again."""
    conn.execute("DELETE FROM settings WHERE key=?", (key,))
    conn.commit()


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


def hash_token(raw: str) -> str:
    return sha256(raw.encode("utf-8", "surrogateescape")).hexdigest()


def mint_token(conn: sqlite3.Connection, name: str) -> tuple[str, dict]:
    """Create a key. The raw value is returned once and never stored - only its hash."""
    raw = "ta_" + secrets.token_hex(24)
    row = {
        "id": str(uuid.uuid4()),
        "name": name.strip() or "unnamed",
        "hash": hash_token(raw),
        "prefix": raw[:11],
        "created": time.time(),
        "last_used": None,
    }
    conn.execute(
        "INSERT INTO tokens (id, name, hash, prefix, created, last_used) VALUES (?,?,?,?,?,?)",
        (row["id"], row["name"], row["hash"], row["prefix"], row["created"], None),
    )
    conn.commit()
    return raw, {k: v for k, v in row.items() if k != "hash"}


# The fill-this-in marker every example config has carried. Once the image is
# published this exact string is known to everyone, and a deploy pasted straight
# out of an example would be an API that queues encodes and replaces files in a
# library, open to whoever finds the port.
PLACEHOLDER_TOKEN = "change-me"


def verify_token(conn: sqlite3.Connection, raw: str, env_token: str) -> bool:
    """A presented token is good if it matches the bootstrap env token, any
    minted key, or a live browser session.

    All three, not one of three: an integration (ManageArr is one) authenticates
    with a minted API key and a first run has nothing but the env token, so a
    login system that replaced either would lock out the integration and the
    operator on the same upgrade. A session is checked last only because it is the newest and the
    rarest of the three, not because it is weaker.

    compare_digest throughout: a plain == on a secret leaks its prefix through
    timing, and this is the one function standing between the internet and a
    process that rewrites media files.
    """
    if not raw:
        return False
    # The placeholder is compared against the CONFIGURED value, never against
    # the presented one, so refusing it costs no timing signal about a real key.
    # Minted keys still work on a container that was started with it.
    if env_token == PLACEHOLDER_TOKEN:
        env_token = ""
    # Compared as BYTES: compare_digest raises TypeError on a non-ASCII str,
    # and headers arrive latin-1 decoded, so any high byte in an Authorization
    # header would kill the request with no response at all.
    if env_token and hmac.compare_digest(raw.encode("utf-8", "surrogateescape"),
                                         env_token.encode("utf-8", "surrogateescape")):
        return True
    presented = hash_token(raw)
    now = time.time()
    for row in conn.execute("SELECT id, hash, last_used FROM tokens").fetchall():
        if hmac.compare_digest(presented, row["hash"]):
            # Stamped at most once a minute. The UI polls every few seconds, and
            # a write per request turns every page view into lock contention
            # with the watcher and the worker for a field nobody reads to the
            # second.
            if not row["last_used"] or now - row["last_used"] > 60:
                try:
                    conn.execute("UPDATE tokens SET last_used=? WHERE id=?", (now, row["id"]))
                    conn.commit()
                except sqlite3.OperationalError:
                    pass  # a busy database must not cost someone their session
            return True
    return verify_session(conn, raw)


def list_tokens(conn: sqlite3.Connection) -> list[dict]:
    return [
        {"id": r["id"], "name": r["name"], "prefix": r["prefix"], "created": r["created"], "last_used": r["last_used"]}
        for r in conn.execute("SELECT * FROM tokens ORDER BY created DESC").fetchall()
    ]


def revoke_token(conn: sqlite3.Connection, token_id: str) -> bool:
    cur = conn.execute("DELETE FROM tokens WHERE id=?", (token_id,))
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# The admin login
#
# A password is never stored, never logged and never returned - the only things
# that leave this section are booleans, a username, and a session token the
# caller is meant to hold.
# ---------------------------------------------------------------------------

MIN_PASSWORD_LENGTH = 8

# scrypt cost. n=2**14 with r=8 is about 16MB and a few tens of milliseconds on
# a NAS CPU: expensive enough that a stolen database is not a wordlist away from
# the password, cheap enough that logging in is not a visible pause on an ARM
# box. Written into each row (see the admin table) so raising it later re-hashes
# on the next password change instead of locking the current password out.
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN = 2 ** 14, 8, 1, 32


def _password_bytes(password: str) -> bytes:
    # surrogatepass, not the strict default: a JSON body can legally carry a
    # lone surrogate, and a UnicodeEncodeError here would be a 500 that any
    # unauthenticated caller could trigger at will by posting one.
    return str(password).encode("utf-8", "surrogatepass")


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> str:
    return scrypt(_password_bytes(password), salt=salt, n=n, r=r, p=p, dklen=SCRYPT_DKLEN).hex()


def set_admin(conn: sqlite3.Connection, username: str, password: str) -> str:
    """Create or replace the single admin. Returns the stored username."""
    name = str(username).strip()
    if not name:
        raise ValueError("Give the admin account a username")
    if len(str(password)) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    salt = secrets.token_bytes(16)
    conn.execute(
        "INSERT INTO admin (id, username, hash, salt, n, r, p, updated) VALUES (1,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET username=excluded.username, hash=excluded.hash, salt=excluded.salt, "
        "n=excluded.n, r=excluded.r, p=excluded.p, updated=excluded.updated",
        (name, _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P), salt.hex(),
         SCRYPT_N, SCRYPT_R, SCRYPT_P, time.time()),
    )
    conn.commit()
    # Setting a password is proof of who you are, so it clears any backoff the
    # account is sitting under - otherwise fixing a forgotten password from the
    # console still leaves you locked out of the form for five minutes.
    _login_failures.pop(name.lower(), None)
    return name


def admin_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM admin WHERE id=1").fetchone() is not None


def admin_username(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT username FROM admin WHERE id=1").fetchone()
    return row["username"] if row else None


def clear_admin(conn: sqlite3.Connection) -> str | None:
    """Delete the admin account and every login it owns. Returns the name, if any.

    The way back in after a forgotten password. Deleting rather than resetting
    to something: a temporary password has to be communicated somehow, and every
    channel available here - the log, an environment variable, the response of a
    route - writes it somewhere it outlives the recovery. With no admin the
    container is back to the state it shipped in, where the bootstrap token gets
    you in and the first thing the page offers is creating an account.

    Sessions go too. A forgotten password and a leaked one look identical from
    here, and leaving live sessions behind would make the reset useless in the
    second case. Minted API keys are deliberately kept: they are separate
    credentials that other software authenticates with, and taking them out
    would turn a password recovery into an outage.
    """
    name = admin_username(conn)
    if name is None:
        return None
    conn.execute("DELETE FROM admin WHERE id=1")
    conn.execute("DELETE FROM sessions")
    conn.commit()
    _login_failures.clear()
    return name


def verify_password(conn: sqlite3.Connection, username: str, password: str) -> bool:
    """Is this the admin password? No rate limiting - see attempt_login."""
    row = conn.execute("SELECT username, hash, salt, n, r, p FROM admin WHERE id=1").fetchone()
    if not row:
        return False
    # scrypt is run even when the username is already wrong, and both results
    # are combined afterwards, so how long a rejected login takes says nothing
    # about which half of it was right.
    computed = _scrypt(password, bytes.fromhex(row["salt"]), row["n"], row["r"], row["p"])
    # Compared as bytes for the same reason verify_token does it: compare_digest
    # raises TypeError on a non-ASCII str, and a username can be anything.
    name_ok = hmac.compare_digest(str(username).strip().lower().encode("utf-8", "surrogatepass"),
                                  row["username"].lower().encode("utf-8", "surrogatepass"))
    hash_ok = hmac.compare_digest(computed, row["hash"])
    return name_ok and hash_ok


# Failed logins are counted in memory, not in a table, and that is a deliberate
# choice: this counter is written by an UNAUTHENTICATED caller, so a row per
# guess hands anyone who can reach the port a way to hammer the same SQLite
# file the watcher and the worker contend for - a password guess would become a
# way to stall encodes. Losing the counts on a restart is the accepted cost;
# restarting this container is not something a remote guesser can ask for.
# ponytail: per-process dict, so several workers would each count separately -
# fine while this is one process, revisit if it is ever fronted by more.
_login_failures: dict[str, tuple[int, float]] = {}
LOGIN_FREE_ATTEMPTS = 3      # fat fingers happen; the backoff starts after these
LOGIN_MAX_BACKOFF = 300.0    # 2s, 4s, 8s ... capped at five minutes
# Every username that is not the admin's shares this one bucket. Keyed by the
# name as presented, a guesser cycling a million made-up usernames would grow
# this dict until the container ran out of memory - and none of those names can
# ever succeed anyway, so one bucket for all of them loses nothing.
_UNKNOWN_ACCOUNT = "?"
# Password checks run ONE AT A TIME across the whole process, and that is a
# security control rather than tidiness. attempt_login is the only unauthenticated
# path here that spends real memory: scrypt at n=2**14 allocates 16MB per call and
# ThreadingHTTPServer hands every connection its own thread, so a few hundred
# simultaneous POSTs to /api/login is gigabytes of allocation on a NAS that is
# also transcoding - a container OOM asked for by anyone who can reach the port.
# The same lock closes the race in the counter below: unserialized, a burst all
# reads "no backoff yet" before any of them records a failure, which turns three
# free attempts into three free rounds of however many sockets you can open.
_login_lock = threading.Lock()


def _login_key(conn: sqlite3.Connection, username: str) -> str:
    name = str(username).strip().lower()
    return name if name == (admin_username(conn) or "").lower() else _UNKNOWN_ACCOUNT


def login_retry_after(conn: sqlite3.Connection, username: str, now: float | None = None) -> float:
    """Seconds this account must wait before its next attempt; 0 when allowed."""
    _, until = _login_failures.get(_login_key(conn, username), (0, 0.0))
    return max(0.0, until - (time.time() if now is None else now))


def attempt_login(conn: sqlite3.Connection, username: str, password: str) -> tuple[bool, float]:
    """(ok, seconds to wait). The call a login route should make.

    One entry point rather than "check the throttle, then check the password",
    because the version of that route which forgets the first line still works
    perfectly - and is a password oracle anyone can run at wire speed.

    Held under _login_lock for its whole length, including the scrypt call, so a
    burst of concurrent guesses is counted as a burst of guesses rather than one.
    """
    with _login_lock:
        key = _login_key(conn, username)
        wait = login_retry_after(conn, username)
        if wait:
            return False, wait
        if verify_password(conn, username, password):
            _login_failures.pop(key, None)
            return True, 0.0
        # Capped: left to climb, 2 ** fails becomes an OverflowError after a few
        # days of patient guessing, and the exception would skip the line below
        # and quietly leave the account with no backoff at all.
        fails = min(_login_failures.get(key, (0, 0.0))[0] + 1, 99)
        delay = 0.0 if fails <= LOGIN_FREE_ATTEMPTS else min(2.0 ** (fails - LOGIN_FREE_ATTEMPTS), LOGIN_MAX_BACKOFF)
        _login_failures[key] = (fails, time.time() + delay)
        return False, delay


# ---------------------------------------------------------------------------
# Browser sessions
# ---------------------------------------------------------------------------


def create_session(conn: sqlite3.Connection, days: int) -> tuple[str, dict]:
    """Mint a session. The raw token is returned once and only its hash is kept.

    Same entropy and the same hashing as a minted API key - a session is not a
    lesser secret just because it expires - with a ts_ prefix so a token in a
    log line can be told apart from an API key at a glance.
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(days, SESSION_MAX_DAYS))
    raw = "ts_" + secrets.token_hex(24)
    row = {
        "id": str(uuid.uuid4()),
        "prefix": raw[:11],
        "created": time.time(),
        "expires": time.time() + days * 86400,
        "last_used": None,
    }
    conn.execute(
        "INSERT INTO sessions (id, hash, prefix, created, expires, last_used) VALUES (?,?,?,?,?,NULL)",
        (row["id"], hash_token(raw), row["prefix"], row["created"], row["expires"]),
    )
    conn.commit()
    return raw, row


def verify_session(conn: sqlite3.Connection, raw: str) -> bool:
    """Is this a live session token? Expired is refused, not renewed."""
    if not raw:
        return False
    presented = hash_token(raw)
    now = time.time()
    for row in conn.execute("SELECT id, hash, expires, last_used FROM sessions").fetchall():
        if not hmac.compare_digest(presented, row["hash"]):
            continue
        if row["expires"] <= now:
            return False
        # Stamped at most once a minute, for the reason the tokens table does
        # it: the UI polls every few seconds and a write per request turns every
        # page view into lock contention with the watcher and the worker.
        if not row["last_used"] or now - row["last_used"] > 60:
            try:
                conn.execute("UPDATE sessions SET last_used=? WHERE id=?", (now, row["id"]))
                conn.commit()
            except sqlite3.OperationalError:
                pass  # a busy database must not cost someone their session
        return True
    return False


def list_sessions(conn: sqlite3.Connection) -> list[dict]:
    return [
        {"id": r["id"], "prefix": r["prefix"], "created": r["created"],
         "expires": r["expires"], "last_used": r["last_used"]}
        for r in conn.execute("SELECT * FROM sessions ORDER BY created DESC").fetchall()
    ]


def revoke_session(conn: sqlite3.Connection, session_id: str) -> bool:
    cur = conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    conn.commit()
    return cur.rowcount > 0


def purge_expired_sessions(conn: sqlite3.Connection) -> int:
    """Delete sessions that have run out. Returns how many went.

    verify_session already refuses them, so this is housekeeping rather than
    security: without it the table only ever grows, in the same file every
    other writer here is contending for.
    """
    cur = conn.execute("DELETE FROM sessions WHERE expires <= ?", (time.time(),))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Arr connections
# ---------------------------------------------------------------------------

ARR_FIELDS = ("name", "kind", "base_url", "api_key", "arr_path", "worker_path", "enabled")


def list_arrs(conn: sqlite3.Connection, redact: bool = True) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM arrs ORDER BY created").fetchall():
        row = {k: r[k] for k in r.keys()}
        row["enabled"] = bool(row["enabled"])
        if redact:
            # The key is never sent back to the browser; it only ever travels inward.
            row["api_key"] = "********" if row["api_key"] else ""
        out.append(row)
    return out


def get_arr(conn: sqlite3.Connection, arr_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM arrs WHERE id=?", (arr_id,)).fetchone()
    return {k: r[k] for k in r.keys()} if r else None


def clean_arr(body: dict) -> dict:
    """Validate a submitted connection into storable values.

    The API key is deliberately not here. It has two rules of its own that only
    make sense at the write - blank means "keep the stored one" on an edit, and
    a restored backup carries no key at all - and folding them in here would
    make a validator that also decides secrets.
    """
    kind = str(body.get("kind", "")).lower()
    if kind not in ("radarr", "sonarr"):
        raise ValueError("kind must be radarr or sonarr")
    base_url = str(body.get("base_url", "")).strip().rstrip("/")
    if not re.match(r"^https?://", base_url):
        raise ValueError("base_url must start with http:// or https://")
    return {
        "name": str(body.get("name", "")).strip() or kind,
        "kind": kind,
        "base_url": base_url,
        "arr_path": str(body.get("arr_path", "")).strip().rstrip("/"),
        "worker_path": str(body.get("worker_path", "")).strip().rstrip("/"),
        "enabled": 1 if body.get("enabled", True) else 0,
    }


def save_arr(conn: sqlite3.Connection, body: dict, arr_id: str | None = None) -> dict:
    fields = clean_arr(body)
    name, kind, base_url = fields["name"], fields["kind"], fields["base_url"]
    arr_path, worker_path, enabled = fields["arr_path"], fields["worker_path"], fields["enabled"]

    if arr_id:
        existing = get_arr(conn, arr_id)
        if not existing:
            raise ValueError("no such connection")
        # An empty key on edit means "keep the one you already have" - the UI
        # never receives the real key, so it cannot send it back.
        api_key = str(body.get("api_key", "")).strip() or existing["api_key"]
        conn.execute(
            "UPDATE arrs SET name=?, kind=?, base_url=?, api_key=?, arr_path=?, worker_path=?, enabled=? WHERE id=?",
            (name, kind, base_url, api_key, arr_path, worker_path, enabled, arr_id),
        )
    else:
        api_key = str(body.get("api_key", "")).strip()
        if not api_key:
            raise ValueError("api_key is required")
        arr_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO arrs (id, name, kind, base_url, api_key, arr_path, worker_path, enabled, created) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (arr_id, name, kind, base_url, api_key, arr_path, worker_path, enabled, time.time()),
        )
    conn.commit()
    return get_arr(conn, arr_id)  # type: ignore[return-value]


def delete_arr(conn: sqlite3.Connection, arr_id: str) -> bool:
    cur = conn.execute("DELETE FROM arrs WHERE id=?", (arr_id,))
    conn.commit()
    return cur.rowcount > 0


def note_arr_error(conn: sqlite3.Connection, arr_id: str, error: str | None) -> None:
    conn.execute("UPDATE arrs SET last_error=? WHERE id=?", (error, arr_id))
    conn.commit()


# ---------------------------------------------------------------------------
# Encoding profiles
# ---------------------------------------------------------------------------

PROFILE_FIELDS = ("name", "encoder", "quality", "preset", "profile", "max_height",
                  "audio_codec", "audio_bitrate", "audio_channels")

AUDIO_CODECS = [
    ("aac", "AAC - re-encoded, plays everywhere"),
    ("copy", "Copy the original track - no quality loss, but MP4 cannot hold DTS or TrueHD"),
]
AUDIO_CHANNELS = [
    (0, "Same as source - keeps 5.1 intact"),
    (2, "Stereo - smaller, and what most TVs and phones actually output"),
    (6, "5.1"),
]


def profile_row(r: sqlite3.Row) -> dict:
    d = {k: r[k] for k in r.keys()}
    d["active"] = bool(d["active"])
    d["builtin"] = bool(d["builtin"])
    # One derived field so every caller agrees on what "may I use this?" means,
    # rather than each of the UI, the activate route and the API re-deriving it
    # from validated_ok and getting a different answer on the untested case.
    d["usable"] = d["validated_ok"] == 1
    return d


def list_profiles(conn: sqlite3.Connection) -> list[dict]:
    # Shipped first and in the order core probes encoders, so the list reads the
    # same way on every machine; a user's own profiles follow, oldest first.
    rows = [profile_row(r) for r in conn.execute("SELECT * FROM profiles").fetchall()]
    order = {shipped_profile_id(e): i for i, e in enumerate(core.ENCODER_ORDER)}
    return sorted(rows, key=lambda p: (0, order.get(p["id"], 99)) if p["builtin"] else (1, p["created"]))


# A shipped profile's id is derived from its encoder, so re-seeding finds the
# existing row instead of inserting a sixth copy on every boot. uuid5 keeps it
# UUID-shaped, which every route regex in main.py already expects.
_SHIPPED_NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/managearr/transcodearr/profiles")


def shipped_profile_id(encoder: str) -> str:
    return str(uuid.uuid5(_SHIPPED_NS, encoder))


def get_profile(conn: sqlite3.Connection, profile_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
    return profile_row(r) if r else None


def active_profile(conn: sqlite3.Connection) -> dict | None:
    r = conn.execute("SELECT * FROM profiles WHERE active=1 LIMIT 1").fetchone()
    return profile_row(r) if r else None


def clean_profile(body: dict, allowed_encoders: list[str] | None = None) -> dict:
    """Validate a submitted profile into storable values."""
    name = str(body.get("name", "")).strip()
    if not name:
        raise ValueError("Give the profile a name")
    encoder = str(body.get("encoder", "")).strip()
    if allowed_encoders is not None and encoder not in allowed_encoders:
        # Offering an encoder this machine cannot run is how someone builds a
        # profile that fails on the first real file instead of on the test.
        raise ValueError(f"{encoder or 'That encoder'} does not work on this machine")
    try:
        quality = int(body.get("quality", 23))
    except (TypeError, ValueError):
        raise ValueError("Quality must be a whole number")
    if not 1 <= quality <= 51:
        raise ValueError("Quality must be between 1 and 51")
    try:
        max_height = int(body.get("max_height", 0) or 0)
    except (TypeError, ValueError):
        raise ValueError("Resolution must be a whole number")
    if max_height and not 240 <= max_height <= 4320:
        raise ValueError("Resolution must be 0 (source) or a height between 240 and 4320")
    audio_codec = str(body.get("audio_codec", "aac")).strip() or "aac"
    if audio_codec not in [c for c, _ in AUDIO_CODECS]:
        raise ValueError("Audio must be aac or copy")
    try:
        audio_bitrate = int(body.get("audio_bitrate", 192))
        audio_channels = int(body.get("audio_channels", 2))
    except (TypeError, ValueError):
        raise ValueError("Audio bitrate and channels must be whole numbers")
    if not 32 <= audio_bitrate <= 640:
        raise ValueError("Audio bitrate must be between 32 and 640 kbps")
    if audio_channels not in [c for c, _ in AUDIO_CHANNELS]:
        raise ValueError("Audio channels must be source, stereo or 5.1")
    return {
        "name": name, "encoder": encoder, "quality": quality,
        # Normalized to this encoder's own vocabulary, so what is stored is what
        # actually runs. Keeping "p7" on a libx264 profile - which the encoder
        # would silently replace with "medium" - saves a profile that describes
        # settings it does not use, and the card in the UI would be a lie.
        "preset": core.valid_option(encoder, "presets", str(body.get("preset", "")).strip()),
        "profile": core.valid_option(encoder, "profiles", str(body.get("profile", "")).strip()),
        "max_height": max_height, "audio_codec": audio_codec,
        "audio_bitrate": audio_bitrate, "audio_channels": audio_channels,
    }


def save_profile(conn: sqlite3.Connection, fields: dict, profile_id: str | None,
                 validated_note: str | None) -> dict:
    """Store a profile. validated_note comes from a real test encode - a profile
    is never written without one, which is the whole point of the test.

    So the row is written validated_ok=1: the caller refuses to save a profile
    whose test failed, and a saved-but-unusable profile could never be activated
    and would look broken the moment it appeared.
    """
    now = time.time()
    if profile_id:
        existing = get_profile(conn, profile_id)
        if not existing:
            raise ValueError("no such profile")
        if existing["builtin"]:
            # The five we ship are the known-good starting points. Editing one
            # in place would leave a profile whose name still promises what it
            # no longer does, and nothing to compare a custom profile against.
            raise ValueError("shipped profiles cannot be edited - duplicate it and change the copy")
        conn.execute(
            "UPDATE profiles SET name=?, encoder=?, quality=?, preset=?, profile=?, max_height=?, "
            "audio_codec=?, audio_bitrate=?, audio_channels=?, validated_at=?, validated_ok=1, validated_note=? WHERE id=?",
            (*[fields[k] for k in PROFILE_FIELDS], now, validated_note, profile_id),
        )
    else:
        profile_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO profiles (id, name, encoder, quality, preset, profile, max_height, audio_codec, "
            "audio_bitrate, audio_channels, active, builtin, validated_at, validated_ok, validated_note, created) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,0,0,?,1,?,?)",
            (profile_id, *[fields[k] for k in PROFILE_FIELDS], now, validated_note, now),
        )
    conn.commit()
    return get_profile(conn, profile_id)  # type: ignore[return-value]


def record_validation(conn: sqlite3.Connection, profile_id: str, ok: bool, note: str) -> dict | None:
    """Write the result of a real test encode against a stored profile."""
    if not get_profile(conn, profile_id):
        return None
    conn.execute(
        "UPDATE profiles SET validated_at=?, validated_ok=?, validated_note=? WHERE id=?",
        (time.time(), 1 if ok else 0, note, profile_id),
    )
    conn.commit()
    return get_profile(conn, profile_id)


def activate_profile(conn: sqlite3.Connection, profile_id: str) -> tuple[dict | None, str]:
    """(profile, reason). A profile that has not passed a real encode here is
    refused: activating one is choosing what every future job runs, and finding
    out on the first film that this box has no Quick Sync is exactly the silent
    failure the test encode exists to prevent."""
    row = get_profile(conn, profile_id)
    if not row:
        return None, "no such profile"
    if not row["usable"]:
        why = row["validated_note"] or "it has not been tested on this machine yet"
        return None, f"that profile does not work here: {why}"
    conn.execute("UPDATE profiles SET active=0")
    conn.execute("UPDATE profiles SET active=1 WHERE id=?", (profile_id,))
    conn.commit()
    return get_profile(conn, profile_id), "activated"


def delete_profile(conn: sqlite3.Connection, profile_id: str) -> tuple[bool, str]:
    row = get_profile(conn, profile_id)
    if not row:
        return False, "no such profile"
    if row["builtin"]:
        # Deleting a shipped profile would only come back on the next boot, so
        # refusing it is more honest than reseeding it behind their back.
        return False, "shipped profiles cannot be deleted"
    if row["active"]:
        # Deleting what every job is using leaves the worker with no settings
        # at all; make the replacement an explicit choice first.
        return False, "that profile is in use - activate another one first"
    conn.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
    conn.commit()
    return True, "deleted"


def ensure_shipped_profiles(conn: sqlite3.Connection) -> list[str]:
    """Seed the five profiles we ship, one per encoder. Returns their ids.

    Idempotent: ids are derived from the encoder name, so a restart updates the
    settings of a shipped profile it already wrote rather than adding a sixth.
    Refreshing them is safe precisely because they are read-only - nobody's
    tuning can be sitting in one to be overwritten.
    """
    ids = []
    now = time.time()
    for encoder in core.ENCODER_ORDER:
        fields = core.shipped_profile(encoder)
        pid = shipped_profile_id(encoder)
        ids.append(pid)
        if get_profile(conn, pid):
            conn.execute(
                "UPDATE profiles SET name=?, encoder=?, quality=?, preset=?, profile=?, max_height=?, "
                "audio_codec=?, audio_bitrate=?, audio_channels=?, builtin=1 WHERE id=?",
                (*[fields[k] for k in PROFILE_FIELDS], pid),
            )
        else:
            conn.execute(
                "INSERT INTO profiles (id, name, encoder, quality, preset, profile, max_height, "
                "audio_codec, audio_bitrate, audio_channels, active, builtin, validated_at, "
                "validated_ok, validated_note, created) VALUES (?,?,?,?,?,?,?,?,?,?,0,1,NULL,NULL,?,?)",
                (pid, *[fields[k] for k in PROFILE_FIELDS], "not tested on this machine yet", now),
            )
    conn.commit()
    return ids


def adopt_legacy_default(conn: sqlite3.Connection) -> str | None:
    """Turn a pre-0.9.1 'Default' into one of the user's own profiles.

    Installs before the shipped five had exactly one profile, named Default,
    marked builtin, and seeded from whatever QUALITY and FORCE_ENCODER happened
    to be set to. It is somebody's real encoding settings, and an upgrade must
    not change how their library is encoded - so it keeps its values and its
    active flag and simply stops claiming to be one of ours. It becomes
    editable and deletable, which for a value they chose is the truth.
    """
    row = conn.execute(
        "SELECT id FROM profiles WHERE builtin=1 AND id NOT IN (%s)"
        % ",".join("?" * len(core.ENCODER_ORDER)),
        tuple(shipped_profile_id(e) for e in core.ENCODER_ORDER),
    ).fetchone()
    if not row:
        return None
    conn.execute("UPDATE profiles SET builtin=0 WHERE id=?", (row["id"],))
    conn.commit()
    return row["id"]


# ---------------------------------------------------------------------------
# Configuration backup and restore
# ---------------------------------------------------------------------------

# Bumped only when the shape changes in a way an older build cannot read. This
# integer is the hard gate on import: it is one comparison with no parsing, and
# it is right even when the version string is a git describe or a release
# candidate. The app version below it is the softer, human-facing check.
BACKUP_FORMAT = 1

RESTORED_NOTE = "restored from a backup - not tested on this machine yet"


def _version_tuple(v: str) -> tuple[int, ...]:
    """Version string to numbers: "0.10.0" becomes (0, 10, 0).

    Numbers because as text "0.10.0" sorts BELOW "0.9.1", and the check that
    refuses a newer backup would wave through exactly the newest ones.
    """
    return tuple(int(n) for n in re.findall(r"\d+", str(v).split("-", 1)[0])[:3])


def export_config(conn: sqlite3.Connection, version: str) -> dict:
    """Settings, profiles and arr connections as one restorable document.

    NO SECRETS LEAVE. Not an arr API key, not a token or session hash, not the
    password hash - a backup is a file people attach to a forum post when they
    want help with it. Job history and the trash are not configuration and are
    not here either; nothing in this document describes a media file.

    STORED settings only, never effective ones. A value currently coming from
    the container environment belongs to the container's shape, and writing it
    into the database on the far side would pin it there for good - the next
    `docker run` with a corrected env would silently do nothing, which is the
    precedence rule at the top of this file broken by its own backup.
    """
    settings = {}
    for key, raw in read_settings(conn).items():
        if key not in SPEC_BY_KEY or key in SECRET_KEYS:
            continue
        try:
            settings[key] = json.loads(raw)
        except json.JSONDecodeError:
            continue  # one corrupt row is not a reason to have no backup
    return {
        "format": BACKUP_FORMAT,
        "version": version,
        "exported": time.time(),
        "settings": settings,
        # Shipped profiles are re-seeded from code on every boot, so carrying
        # them would only ever restore rows that already exist.
        "profiles": [{"id": p["id"], **{k: p[k] for k in PROFILE_FIELDS}}
                     for p in list_profiles(conn) if not p["builtin"]],
        "arrs": [{"id": a["id"], **{k: a[k] for k in ARR_FIELDS if k != "api_key"}}
                 for a in list_arrs(conn)],
    }


def import_config(conn: sqlite3.Connection, doc: dict, version: str,
                  roots: list[str] | None = None) -> list[str]:
    """Apply a backup. Returns a line per thing that changed, for the operator.

    Ids are carried, so restoring yesterday's backup onto the same box updates
    the rows it came from instead of leaving a second copy of every arr
    connection quietly rescanning the library twice.
    """
    if not isinstance(doc, dict):
        raise ValueError("That is not a TranscodeArr backup")
    try:
        fmt = int(doc.get("format") or 0)
    except (TypeError, ValueError):
        fmt = 0
    if fmt < 1:
        raise ValueError("That file is not a TranscodeArr backup - it carries no format stamp")
    if fmt > BACKUP_FORMAT:
        raise ValueError(f"This backup is format {fmt} and this build reads format {BACKUP_FORMAT}. "
                         "Upgrade TranscodeArr before restoring it.")
    wrote = str(doc.get("version") or "")
    if _version_tuple(wrote) > _version_tuple(version):
        raise ValueError(f"This backup was written by TranscodeArr {wrote} and this is {version}. "
                         "Upgrade first - restoring it here would drop whatever this build cannot read.")

    settings = doc.get("settings") or {}
    profiles = doc.get("profiles") or []
    arr_rows = doc.get("arrs") or []
    if (not isinstance(settings, dict) or not isinstance(profiles, list) or not isinstance(arr_rows, list)
            or not all(isinstance(x, dict) for x in profiles + arr_rows)):
        raise ValueError("Backup is malformed: settings must be an object, profiles and arrs lists of objects")

    # Everything is validated before anything is written - the rule
    # save_settings already follows - so a bad connection at the bottom of the
    # file cannot leave the settings applied and half a restore done.
    changes: list[str] = []
    known = {}
    for key, value in settings.items():
        if key in SPEC_BY_KEY:
            known[key] = value
        else:
            # Named, not dropped. A key this build does not have usually means a
            # backup from a newer one, and silently ignoring it is how someone
            # believes a setting was restored when it never was.
            changes.append(f"ignored a setting this version does not have: {key}")
    cleaned_profiles = [(str(p.get("id") or ""), clean_profile(p)) for p in profiles]
    cleaned_arrs = [(str(a.get("id") or ""), clean_arr(a)) for a in arr_rows]

    written = save_settings(conn, known, roots) if known else []
    if written:
        changes.append("settings: " + ", ".join(written))

    shipped = {shipped_profile_id(e) for e in core.ENCODER_ORDER}
    now = time.time()
    for pid, fields in cleaned_profiles:
        existing = get_profile(conn, pid) if pid else None
        if pid in shipped or (existing and existing["builtin"]):
            changes.append(f"skipped a shipped profile: {fields['name']}")
            continue
        if existing and existing["active"]:
            # The active profile is what every future job runs. Rewriting it
            # from an uploaded file would change how a library is encoded
            # halfway through, without anybody choosing it - the silent change
            # this project exists to prevent.
            changes.append(f"left the profile in use alone: {existing['name']}")
            continue
        # Restored profiles are always UNTESTED, whatever the backup claimed.
        # A backup proves somebody once had these settings; it does not prove
        # this machine can run them, and it is usually a different machine that
        # is being restored onto. usable stays false until a real test encode
        # passes here, so a restore can never activate an encoder this box does
        # not have and find out on the first film.
        if existing:
            conn.execute(
                "UPDATE profiles SET name=?, encoder=?, quality=?, preset=?, profile=?, max_height=?, "
                "audio_codec=?, audio_bitrate=?, audio_channels=?, validated_at=NULL, validated_ok=NULL, "
                "validated_note=? WHERE id=?",
                (*[fields[k] for k in PROFILE_FIELDS], RESTORED_NOTE, pid),
            )
            changes.append(f"profile updated: {fields['name']} - test it before you can use it")
            continue
        if not re.fullmatch(r"[0-9a-f-]{36}", pid):
            # Every profile route matches a UUID-shaped id, so a row keyed
            # anything else would be one nobody could ever edit or delete.
            pid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO profiles (id, name, encoder, quality, preset, profile, max_height, audio_codec, "
            "audio_bitrate, audio_channels, active, builtin, validated_at, validated_ok, validated_note, created) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,0,0,NULL,NULL,?,?)",
            (pid, *[fields[k] for k in PROFILE_FIELDS], RESTORED_NOTE, now),
        )
        changes.append(f"profile added: {fields['name']} - test it before you can use it")

    for aid, fields in cleaned_arrs:
        existing = get_arr(conn, aid) if aid else None
        api_key = existing["api_key"] if existing else ""
        # A connection with no key answers every rescan with 401 and marks
        # itself in error. Disabled and visible is the honest state, and it says
        # exactly what is missing.
        enabled = 1 if (fields["enabled"] and api_key) else 0
        if existing:
            conn.execute(
                "UPDATE arrs SET name=?, kind=?, base_url=?, arr_path=?, worker_path=?, enabled=? WHERE id=?",
                (fields["name"], fields["kind"], fields["base_url"], fields["arr_path"],
                 fields["worker_path"], enabled, aid),
            )
        else:
            if not re.fullmatch(r"[0-9a-f-]{36}", aid):
                aid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO arrs (id, name, kind, base_url, api_key, arr_path, worker_path, enabled, created) "
                "VALUES (?,?,?,?,'',?,?,?,?)",
                (aid, fields["name"], fields["kind"], fields["base_url"],
                 fields["arr_path"], fields["worker_path"], enabled, now),
            )
        changes.append(f"connection {'updated' if existing else 'added'}: {fields['name']}"
                       + ("" if api_key else " - paste its API key to enable it"))
    conn.commit()
    return changes


def migrate_hidden_only(conn: sqlite3.Connection) -> str | None:
    """Carry a pre-1.0 install's visibility behavior across the rename.

    Before 1.0 the setting was `process_unhidden`, defaulting to OFF, so an
    install that never touched it was converting ONLY dot-hidden files. The
    replacement `hidden_only` defaults to OFF meaning the opposite - convert
    everything - because a tool nobody can use until they find a checkbox is a
    tool that looks broken.

    That flip is safe for a new install and catastrophic for an existing one:
    a library that has been quietly converting the handful of files something
    else hid would, on the next scan, find every visible file in every watched
    folder eligible at once. So the old behavior is written down explicitly
    rather than inherited from a default that now means the reverse. Only a
    database with history is migrated; a fresh one gets the new default.
    """
    rows = read_settings(conn)
    if "hidden_only" in rows:
        return None  # already migrated, or the operator has since chosen
    had_history = bool(rows) or conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    if not had_history:
        return None  # a fresh install is entitled to the new default
    old = rows.get("process_unhidden")
    try:
        was_converting_visible = json.loads(old) is True if old is not None else False
    except (TypeError, ValueError):
        was_converting_visible = False  # an unreadable row is not consent to widen
    hidden_only = not was_converting_visible
    conn.execute("INSERT OR REPLACE INTO settings (key, value, updated) VALUES (?,?,?)",
                 ("hidden_only", json.dumps(hidden_only), time.time()))
    conn.execute("DELETE FROM settings WHERE key='process_unhidden'")
    conn.commit()
    return ("kept converting only dot-hidden files" if hidden_only
            else "kept converting every matching file")
