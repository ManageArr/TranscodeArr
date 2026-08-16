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
import time
import uuid
from dataclasses import dataclass
from hashlib import sha256
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


SPECS: list[Spec] = [
    Spec("watch_roots", "WATCH_ROOTS", "paths", [], "Watched folders",
         "Folders scanned for files to convert. Leave empty to watch every media root.", "Locations"),
    Spec("scan_interval_seconds", "SCAN_INTERVAL_SECONDS", "int", 300, "Scan interval (seconds)",
         "How often the watched folders are walked.", "Locations"),
    Spec("stable_seconds", "STABLE_SECONDS", "int", 120, "Stability window (seconds)",
         "A file is only touched once its size has held still for this long. Never based on modified time, "
         "which imports preserve from the release.", "Locations"),
    Spec("process_unhidden", "PROCESS_UNHIDDEN", "bool", False, "Also process visible files",
         "Off means only dot-hidden files are eligible. Turning this on sweeps your whole visible library "
         "into re-encoding - a decision, not a default.", "Locations"),
    Spec("convert_extensions", "CONVERT_EXTENSIONS", "exts", [".mkv", ".avi", ".m4v", ".m2ts", ".mts", ".vob"],
         "Convert these extensions", "Dot-hidden .mp4 files are revealed without re-encoding regardless.", "Rules"),
    Spec("skip_patterns", "SKIP_PATTERNS", "patterns", [], "Never convert files matching",
         "Case-insensitive substrings matched against the file name. A match is revealed as-is instead of "
         "re-encoded - the way to protect Remux copies you would rather keep whole.", "Rules"),
    Spec("quality", "QUALITY", "int", 24, "Quality (CQ/CRF)",
         "Lower is better looking and bigger. 24 is a sane default; 18-20 is near-transparent.", "Rules"),
    Spec("verify_duration_tolerance", "VERIFY_DURATION_TOLERANCE", "fraction", 0.015, "Duration tolerance",
         "How far the output length may drift from the source before the result is rejected, as a fraction: "
         "0.015 is 1.5%. Capped at 0.5 - a tolerance loose enough to accept half a film is not a tolerance.", "Rules"),
    Spec("force_encoder", "FORCE_ENCODER", "text", "", "Force encoder",
         "Leave empty to probe at boot (NVENC, then QSV, then libx264). Set to pin one.", "Rules"),
    Spec("trash_keep_days", "TRASH_KEEP_DAYS", "int", 7, "Keep replaced sources (days)",
         "Replaced originals are moved to trash, never deleted outright. This is how long they survive. "
         "Raise it before a large batch - a source pruned mid-run is one you cannot get back.", "General"),
]

SPEC_BY_KEY = {s.key: s for s in SPECS}


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
    return "" if raw is None else str(raw).strip()


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


def verify_token(conn: sqlite3.Connection, raw: str, env_token: str) -> bool:
    """A presented token is good if it matches the bootstrap env token or any minted key.

    compare_digest throughout: a plain == on a secret leaks its prefix through
    timing, and this is the one function standing between the internet and a
    process that rewrites media files.
    """
    if not raw:
        return False
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
    return False


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


def save_arr(conn: sqlite3.Connection, body: dict, arr_id: str | None = None) -> dict:
    kind = str(body.get("kind", "")).lower()
    if kind not in ("radarr", "sonarr"):
        raise ValueError("kind must be radarr or sonarr")
    base_url = str(body.get("base_url", "")).strip().rstrip("/")
    if not re.match(r"^https?://", base_url):
        raise ValueError("base_url must start with http:// or https://")
    name = str(body.get("name", "")).strip() or kind
    arr_path = str(body.get("arr_path", "")).strip().rstrip("/")
    worker_path = str(body.get("worker_path", "")).strip().rstrip("/")
    enabled = 1 if body.get("enabled", True) else 0

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
