"""TranscodeArr - a standalone transcoding worker for a media library.

One container, one job: turn video files into the target container, verified,
and only then let the media server see the result. Everything it needs is in
here - the queue, the settings, the scheduler and the UI that drives them - so
this runs on its own against any library on disk. Nothing else has to be
installed for any feature here to work.

By default every matching file in a watched folder is converted. Turning on
hidden_only narrows that to files hidden behind a leading dot, which is worth
setting up: a dot-hidden file is invisible to the media server, so nothing can
be played midway through being replaced and no half-written import is picked
up as though it were finished. Whatever the mode, the encode itself is always
staged behind a dot and revealed only after it verifies.

The queue lives here, in SQLite. The bundled UI, the arrs and any API client
(ManageArr is one) are clients of the HTTP API under /api, with GET /healthz
outside it because a health check that needs a key cannot report a missing
key. The bare /jobs, /jobs/{id} and /queue are the pre-0.9 spellings of three
of those routes, kept working for the deployments already calling them and
removed at 2.0. Design notes and the incident this replaces are in the README.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import arrs as arr_client
import core
import store
import system
import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("transcodearr")

# ---------------------------------------------------------------------------
# Configuration - the container's shape in env, everything else in the database
# ---------------------------------------------------------------------------

# MEDIA_ROOTS names the volume mounts themselves, so it stays an env var: it
# describes the container's shape, not its configuration, and a runtime change
# would only point the worker at paths it cannot see. Everything else is
# editable in the UI and lives in the database - see store.py for why a stored
# value outranks the environment.
MEDIA_ROOTS = [p for p in os.environ.get("MEDIA_ROOTS", "/media").split(":") if p]
TOKEN = os.environ.get("TRANSCODEARR_TOKEN", "")
# The way back in after a forgotten admin password, and env-only on purpose: a
# stored setting outranks the environment here, and every route that could
# change one needs the login you have just lost.
#
# It grants nothing new. Setting an environment variable on this container
# already means being able to read TRANSCODEARR_TOKEN out of that same
# environment and to write the config volume the database lives in, so anyone
# who can turn this on could already have taken the account by hand. What it
# buys is not having to do that by hand, with sqlite3 and a guess at the schema.
RESET_ADMIN = os.environ.get("TRANSCODEARR_RESET_ADMIN", "").strip().lower() in ("1", "true", "yes", "on")
PORT = int(os.environ.get("PORT", "8484"))
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
DB_PATH = os.path.join(CONFIG_DIR, "transcodearr.db")
# Empty means the default: a .transcodearr-trash directory under whichever media
# root holds the source, so the move is a rename on one filesystem. See
# core.trash_destination for the 127 GB this was measured costing. Kept as a
# module global rather than re-read from the environment per call, because the
# job tests assign it directly to point a whole run at a temporary directory.
TRASH_DIR = os.environ.get("TRASH_DIR", "")
# A TRASH_DIR that IS a media root, or holds one, turns prune_trash into a
# scheduled delete of the whole library: prune walks every trash root and
# unlinks anything older than trash_keep_days, and trash_destination would file
# each source back onto its own path. Refused rather than obeyed - the default
# is correct on every host, and the cost of being wrong here is the library.
if core.trash_override_is_unsafe(TRASH_DIR, MEDIA_ROOTS):
    log.error("TRASH_DIR=%s is or contains a media root - refusing it and using the default "
              "(a .transcodearr-trash directory under each media root)", TRASH_DIR)
    TRASH_DIR = ""
# Where the trash used to live. Still swept, or the live box's existing copies
# would sit in the config volume forever instead of expiring on schedule.
LEGACY_TRASH = os.path.join(CONFIG_DIR, "trash")


def cfg() -> dict:
    """Current settings, read fresh.

    Deliberately not cached in a module global: a value changed in the UI has to
    take effect on the next scan and the next job without a restart, and the
    read is one indexed query against a local SQLite file - cheaper than the
    class of bug where a saved setting quietly does nothing until reboot.
    """
    return store.effective(store.read_settings(db()), dict(os.environ), MEDIA_ROOTS)

# The only place the running version is written, and deliberately not an
# environment lookup any more.
#
# It used to read TRANSCODEARR_VERSION first and fall back to this constant -
# two sources of truth for one fact, and the environment one is the half that
# can go stale. It did: updating the QNAP container to 1.0.1 left the OLD
# container's explicit TRANSCODEARR_VERSION=1.0.0 in place, because Container
# Station rebuilds a container from the environment it recorded at create and
# an explicit env beats the new image's ENV. /healthz reported 1.0.0 while
# 1.0.1 code was running - the exact lie this field exists to prevent, on the
# one field somebody reads when they are already debugging the wrong build,
# and it would have survived every future update down that path.
#
# A constant compiled into the image cannot be overridden from outside it. Bump
# it with the image tag: the release workflow refuses a tag that disagrees with
# it, and a test refuses a Dockerfile that does.
VERSION = "1.0.6"
STARTED = time.time()

# ---------------------------------------------------------------------------
# SQLite - the queue is durable state, the worker is just execution
# ---------------------------------------------------------------------------

_local = threading.local()


def db() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        # The watcher, the worker and every HTTP thread write to this file. The
        # 5-second default gave up while a library walk was mid-transaction.
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        _local.conn = conn
    return _local.conn


def init_db() -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY,
          path TEXT NOT NULL,
          state TEXT NOT NULL,           -- queued|running|done|failed|cancelled
          kind TEXT NOT NULL,            -- transcode|reveal
          created REAL NOT NULL,
          started REAL,
          finished REAL,
          progress INTEGER DEFAULT 0,
          encoder TEXT,
          warning TEXT,
          error TEXT,
          output TEXT,
          src_bytes INTEGER,
          out_bytes INTEGER,
          log_tail TEXT
        );
        CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
        -- The watcher asks "how did this path last go?" for every candidate it
        -- finds, on every scan of the whole library, against a table nothing
        -- used to delete from.
        CREATE INDEX IF NOT EXISTS jobs_path_created ON jobs(path, created);
        -- One pending job per file, enforced by the database rather than by a
        -- SELECT two threads can both pass.
        CREATE UNIQUE INDEX IF NOT EXISTS jobs_pending ON jobs(path)
          WHERE state IN ('queued','running');
        -- Watcher memory: last observed size per path, for size-stability.
        CREATE TABLE IF NOT EXISTS seen (
          path TEXT PRIMARY KEY,
          size INTEGER NOT NULL,
          at REAL NOT NULL
        );
        -- What is in the trash and where each file came FROM. The mirroring is
        -- reversible by arithmetic right up until two files land on one
        -- relative path inside the retention window - trash() then appends
        -- .1, .2, and no rule can tell that suffix apart from a file genuinely
        -- called "Movie.1.mkv". Restore puts media back; guessing its
        -- destination is not something to do with a heuristic.
        CREATE TABLE IF NOT EXISTS trash (
          path TEXT PRIMARY KEY,         -- where it is now, inside a trash root
          original TEXT NOT NULL,        -- where it came from, where Restore returns it
          bytes INTEGER,
          at REAL NOT NULL,              -- when it was trashed; retention counts from here
          job_id TEXT                    -- the job that moved it, when there was one
        );
        """
        + store.SCHEMA
    )
    # Columns added after the first release. CREATE TABLE IF NOT EXISTS does
    # nothing to a table that already exists, so a live database - the only kind
    # that matters here - would never gain them without this.
    have = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    for column, decl in (("rescan", "TEXT"), ("priority", "INTEGER NOT NULL DEFAULT 0")):
        if column not in have:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {decl}")
    # Same reason as the jobs columns above: CREATE TABLE IF NOT EXISTS does
    # nothing to a live database. A profile from before this column existed has
    # never been tested, and NULL is exactly that answer.
    have = {r["name"] for r in conn.execute("PRAGMA table_info(profiles)").fetchall()}
    if "validated_ok" not in have:
        conn.execute("ALTER TABLE profiles ADD COLUMN validated_ok INTEGER")
        # A pre-0.9.1 row recorded a validated_at only on success, so that is a
        # pass we already know about and need not make the user re-run.
        conn.execute("UPDATE profiles SET validated_ok=1 WHERE validated_at IS NOT NULL")
    # process_unhidden became hidden_only, and the default flipped meaning. An
    # install that was quietly converting only what something else had hidden
    # must not wake up with its whole visible library eligible, so its behavior
    # is written down explicitly instead of inherited from a reversed default.
    carried = store.migrate_hidden_only(conn)
    if carried:
        log.info("visibility setting renamed to hidden_only - %s", carried)
    # Existing rows predate priorities. A reveal is a rename that finishes in
    # milliseconds, so leaving hundreds of them behind a two-day transcode
    # backlog keeps files invisible for days that could be visible now.
    conn.execute(f"UPDATE jobs SET priority={REVEAL_PRIORITY} WHERE kind='reveal' AND state='queued' AND priority=0")

    # Boot rule: anything left running died with the previous process. Its
    # .part is deleted; the SOURCE was never touched, so nothing is lost and
    # the watcher will simply find the file again.
    skip_patterns = cfg()["skip_patterns"]
    for row in conn.execute("SELECT id, path, kind FROM jobs WHERE state = 'running'").fetchall():
        source = row["path"]
        # Planned exactly the way process() plans it: a skip-protected .mkv
        # keeps its own extension, and planning it as .mp4 here would point
        # hidden_final at a file this job never wrote and could not delete.
        protected = row["kind"] == "reveal" or core.matches_skip(os.path.basename(source), skip_patterns)
        names = core.plan_names(source, os.path.splitext(source)[1].lower() if protected else ".mp4")
        if os.path.exists(names.part):
            try:
                os.unlink(names.part)
            except OSError:
                pass
        # The source still being there means we died between the staging
        # os.replace and the trash: hidden_final is a complete, verified encode
        # of a file that also still exists, and leaving it behind made every
        # retry fail on "staging name is taken" for good. Never when
        # hidden_final IS the source - that is a reveal, and unlinking it
        # deletes the only copy of the film.
        if names.hidden_final != source and os.path.exists(source):
            try:
                os.unlink(names.hidden_final)
            except OSError:
                pass
        conn.execute(
            "UPDATE jobs SET state='failed', error='interrupted by restart', finished=? WHERE id=?",
            (time.time(), row["id"]),
        )
    conn.commit()


# What a job looks like to a client, listed rather than derived from the row.
# `{k: row[k] for k in row.keys()}` made the SQLite schema itself the public
# API: every column added for the worker's own bookkeeping shipped to every
# caller and could never be renamed again. log_tail is the sharp one - it holds
# the full ffmpeg argv and absolute container paths - so it is opt-in and only
# the single-job GET asks for it.
JOB_FIELDS = ("id", "path", "state", "kind", "created", "started", "finished",
              "progress", "encoder", "warning", "error", "output", "rescan",
              "src_bytes", "out_bytes")


def job_dict(row: sqlite3.Row, log_tail: bool = False) -> dict:
    d = {k: row[k] for k in JOB_FIELDS}
    if log_tail:
        d["log_tail"] = row["log_tail"]
    return d


JOB_STATES = ("queued", "running", "done", "failed", "cancelled")


# Work is taken in (priority, created) order. A reveal costs milliseconds - it
# is a rename, not an encode - so making it wait behind hours of transcoding
# keeps a file invisible for no reason at all.
REVEAL_PRIORITY = -10


def enqueue(path: str, kind: str, force: bool = False) -> dict | None:
    """Queue a path unless it is already pending - idempotent per file.

    force skips the retry cooldown. The cooldown exists to stop the WATCHER
    re-running a permanently failing file every scan interval forever; an API
    caller is a person asking for this file now, and telling them "no, come back
    in six hours" for a file they can see is the silent failure mode again.
    """
    conn = db()
    dup = conn.execute(
        "SELECT id FROM jobs WHERE path=? AND state IN ('queued','running')", (path,)
    ).fetchone()
    if dup:
        return None
    if not force:
        last = conn.execute(
            "SELECT state, finished FROM jobs WHERE path=? ORDER BY created DESC LIMIT 1", (path,)
        ).fetchone()
        if last and last["state"] == "failed" and core.in_retry_cooldown(
                last["finished"], time.time(), cfg()["retry_failed_after_hours"]):
            return None
    job_id = str(uuid.uuid4())
    try:
        conn.execute(
            "INSERT INTO jobs (id, path, state, kind, created, priority) VALUES (?,?,?,?,?,?)",
            (job_id, path, "queued", kind, time.time(), REVEAL_PRIORITY if kind == "reveal" else 0),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # The unique index caught what the SELECT above raced past - the
        # watcher thread and an API call can reach here for the same file.
        return None
    log.info("queued %s (%s)", path, kind)
    return job_dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe
# ---------------------------------------------------------------------------


def ffprobe(path: str) -> core.Probe | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=120,
        )
        if out.returncode != 0:
            return None
        return core.parse_ffprobe(json.loads(out.stdout))
    except Exception:
        return None


def try_encoder(enc: str) -> tuple[bool, str]:
    """One real one-second encode. A LISTED encoder is not a working one.

    ffmpeg lists every encoder it was built with, so av1_nvenc appears on a
    card that cannot do AV1 and h264_nvenc appears with no driver libraries at
    all - both fail only when a real file is already halfway through. Actually
    encoding one second is the only honest test.
    """
    if enc not in core.DEFAULT_TEMPLATES:
        return False, "no template for this encoder"
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", "testsrc2=duration=1:size=320x240:rate=30",
             "-c:v", enc, "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0:
            return True, "verified with a real encode"
        last = (r.stderr or "").strip().splitlines()
        return False, (last[-1][:200] if last else "failed with no output")
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def validate_profile(fields: dict) -> tuple[bool, str]:
    """Actually encode with this profile before letting anyone save it.

    Every setting here has a way of being individually plausible and jointly
    impossible: main10 on an encoder built without it, a preset from a
    different encoder family, copy-audio into a container that cannot hold it,
    a resolution the card refuses. None of that shows up in a form; all of it
    shows up in two seconds of real encoding. This is the same rule the
    encoder probe follows, applied to the whole configuration.
    """
    work = tempfile.mkdtemp(prefix="ta-validate-")
    src = os.path.join(work, "sample.mkv")
    out = os.path.join(work, "out.mp4")
    try:
        # A clip with video, audio AND a subtitle stream, so the subtitle and
        # audio paths are exercised rather than skipped.
        make = subprocess.run(
            ["ffmpeg", "-hide_banner", "-y",
             "-f", "lavfi", "-i", "testsrc2=duration=2:size=640x360:rate=24",
             # sine is mono - the lavfi source has no channel_layout option -
             # so the 5.1 comes from the encoder. A surround test clip is the
             # point: it is what proves a downmix or a copy actually works.
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=48000",
             "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "ac3", "-ac", "6", "-shortest", src],
            capture_output=True, text=True, timeout=120,
        )
        if make.returncode != 0:
            tail = (make.stderr or "").strip().splitlines()
            return False, f"could not build a test clip: {tail[-1][:180] if tail else 'unknown error'}"

        args = profile_args(fields, src, out, with_subs=True, hardware_decode=cfg()["hardware_decode"])
        run = subprocess.run(args, capture_output=True, text=True, timeout=180)
        if run.returncode != 0 and fields.get("audio_codec") == "copy":
            # Same fallback a real job would take, so "copy" is not reported as
            # broken when the job would have coped. Carries the decode setting
            # too: left to default it tests GPU decoding on a box that has it
            # switched off, so a profile that works is refused over a thing the
            # job would never have done.
            args = profile_args(fields, src, out, with_subs=True, audio_codec="aac",
                                hardware_decode=cfg()["hardware_decode"])
            run = subprocess.run(args, capture_output=True, text=True, timeout=180)
            if run.returncode == 0:
                return True, "works, but the original audio cannot be copied into MP4 - jobs will re-encode to AAC"
        if run.returncode != 0:
            tail = [l for l in (run.stderr or "").strip().splitlines() if l.strip()]
            return False, tail[-1][:220] if tail else "ffmpeg failed with no output"

        probe = ffprobe(out)
        if probe is None or probe.video_streams < 1:
            return False, "the encode produced no readable video"
        if probe.audio_streams < 1:
            return False, "the encode produced no audio"
        size = os.path.getsize(out) if os.path.exists(out) else 0
        return True, f"encoded 2s of test video and audio ({size // 1024} KB), streams verified"
    except subprocess.TimeoutExpired:
        return False, "the test encode timed out"
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def gpu_name() -> str | None:
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().splitlines()[0].strip()
    except Exception:  # noqa: BLE001
        pass
    return None


def probe_all() -> list[dict]:
    """Every candidate encoder, each actually tried, with what it is good for."""
    out = []
    for enc in core.ENCODER_ORDER:
        ok, reason = try_encoder(enc)
        info = core.ENCODER_INFO.get(enc, {})
        out.append({
            "name": enc, "available": ok, "reason": reason,
            "codec": info.get("codec"), "hardware": info.get("hardware", False),
            "recommended_quality": info.get("recommended"), "sane_range": info.get("sane"),
            "summary": info.get("summary"), "detail": info.get("detail"),
        })
    return out


def choose_encoder() -> tuple[str, str, list[dict]]:
    """(encoder, why, full probe results). A forced choice is honored if it
    works and refused loudly if it does not - silently ignoring it would leave
    someone convinced they were on the GPU."""
    probes = probe_all()
    by_name = {p["name"]: p for p in probes}
    forced = cfg()["force_encoder"]
    if forced:
        p = by_name.get(forced)
        if p and p["available"]:
            return forced, "forced in settings", probes
        why = p["reason"] if p else "not a known encoder"
        log.warning("forced encoder %s is unusable (%s) - falling back", forced, why)
    for enc in core.ENCODER_ORDER:
        if by_name[enc]["available"]:
            skipped = [f"{p['name']}: {p['reason']}" for p in probes
                       if p["name"] != enc and not p["available"] and core.ENCODER_ORDER.index(p["name"]) < core.ENCODER_ORDER.index(enc)]
            return enc, ("; ".join(skipped) if skipped else "first choice worked"), probes
    return "libx264", "nothing probed successfully - falling back to software", probes


ENCODER = "libx264"
ENCODER_REASON = "not probed yet"
ENCODER_PROBES: list[dict] = []
GPU = None


# ---------------------------------------------------------------------------
# Run state - the gate on CLAIMING work, never on finishing it
# ---------------------------------------------------------------------------
# Two things hold new work back: somebody pressed Stop, or the daily convert
# window is shut. Both gate the claim and nothing else. An encode already
# running is left strictly alone - it finishes, is verified and is revealed like
# any other job - because killing a 40GB remux at 90% throws away hours and buys
# back the same hours again tomorrow night. Pausing DRAINS.
#
# The watcher keeps queueing either way (see watch_loop), so a stopped box with
# a growing queue is working exactly as intended rather than broken.
#
# The state is memory only and is decided at boot from the auto_start setting -
# see main(). A manual Stop therefore does NOT survive a restart while auto
# start is on, because that is what auto start means.

_state_lock = threading.Lock()
# "running" or "paused" - the manual switch, and not to be confused with the
# _running dict below, which is the jobs currently encoding.
RUN_STATE = "running"
# The last gate state written to the log, so a change is announced once.
_announced = ""


def local_clock() -> tuple[int, str]:
    """(minutes since local midnight, "23:48 EDT") - the container's own clock.

    Local and never UTC, and the zone label travels with it to wherever the
    window is shown. The container is UTC unless TZ says otherwise while the NAS
    under it is not, so a window typed as 01:00-06:00 runs four hours out, every
    night, with nothing on any screen that looks wrong. Printing the zone beside
    the window is what turns that into something a person can see.
    """
    now = time.localtime()
    return now.tm_hour * 60 + now.tm_min, time.strftime("%H:%M %Z", now)


def _countdown(minutes: int) -> str:
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def _announce(state: str, why: str) -> None:
    """Log a gate change once - the transitions, not the state.

    Eight workers ask every two seconds and the page polls as well, so a line
    per call would bury the four events that matter (Start, Stop, the window
    opening, the window closing) under a wall of "still paused". The countdown
    inside `why` moves every minute, which is why the comparison is on the state
    and the sentence is only what gets printed.
    """
    global _announced  # noqa: PLW0603
    with _state_lock:
        if state == _announced:
            return
        _announced = state
    log.info("%s", why)


def may_claim() -> tuple[bool, str]:
    """May a worker take a NEW job right now, and the sentence that says why.

    One function and one sentence for the worker, the page and /healthz. Three
    places phrasing "paused" three different ways is how an operator ends up
    trusting none of them and restarting the container to find out which was
    telling the truth.
    """
    minutes, clock = local_clock()
    text = cfg()["convert_window"].strip()
    # Cannot raise: store.parse_value refuses a window it cannot read at the
    # moment it is saved, and _resolve falls back to the empty default for a
    # stored row that no longer parses. What arrives here is valid or "".
    window = core.parse_window(text)
    if RUN_STATE != "running":
        state = "paused"
        why = "stopped - press Start to convert; the watcher keeps queueing meanwhile"
    elif not core.within_window(minutes, window):
        state = "shut"
        why = (f"outside the convert window {text} - it opens in "
               f"{_countdown(core.next_window_change(minutes, window))}, local time now {clock}; "
               "the watcher keeps queueing meanwhile")
    else:
        state = "open"
        closes = core.next_window_change(minutes, window)
        why = (f"converting - the window {text} closes in {_countdown(closes)}, local time now {clock}"
               if closes else "converting")
    _announce(state, why)
    return state == "open", why


def set_run_state(running: bool) -> tuple[bool, str]:
    """Start or stop claiming new work. Never touches an encode already running.

    Returns may_claim(), which also writes the transition to the log - so the
    line in the log after somebody presses Stop is the same sentence the page is
    showing them.
    """
    global RUN_STATE  # noqa: PLW0603
    RUN_STATE = "running" if running else "paused"
    return may_claim()


# ---------------------------------------------------------------------------
# The workers
# ---------------------------------------------------------------------------
# One at a time by default: a NAS is usually one set of spindles behind one
# network link, and two encodes there interleave into two slow encodes rather
# than finishing any sooner. It is a default rather than a law because that
# stops being true on an SSD-backed pool with a professional card - NVIDIA's
# Quadro line has no encode-session cap, unlike GeForce - so the ceiling is
# raised in settings by whoever can measure their own disk.

_jobs_lock = threading.Lock()
# job_id -> {"proc": Popen | None, "cancel": bool}. Replaces a single global
# cancel flag, which with more than one worker would stop whichever job
# happened to be running rather than the one that was asked for.
_running: dict[str, dict] = {}


def cancel_running(job_id: str) -> bool:
    with _jobs_lock:
        entry = _running.get(job_id)
        if entry is None:
            return False
        entry["cancel"] = True
        proc = entry.get("proc")
    if proc is not None:
        # Ask the encode to stop directly. Waiting for the progress loop to
        # notice does not work when ffmpeg is blocked reading a stalled share.
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    return True


def _cancelled(job_id: str) -> bool:
    with _jobs_lock:
        entry = _running.get(job_id)
        return bool(entry and entry["cancel"])


def test_stored_profiles(rows: list[dict]) -> list[dict]:
    """Run a real test encode for each stored profile and record the verdict.

    The same validate_profile a custom profile must pass before it is saved, so
    a shipped profile earns its "works here" the same way. An encoder the probe
    could not run fails here too, with the encoder's own error - which is the
    reason worth showing, rather than a second guess at why.
    """
    out = []
    for row in rows:
        ok, note = validate_profile(row)
        updated = store.record_validation(db(), row["id"], ok, note)
        log.info("  profile %-30s %s %s", row["name"], "works" if ok else "UNUSABLE", note)
        out.append(updated or row)
    return out


def encoding_profile() -> dict:
    """The active profile, or the probed defaults if somehow there is none."""
    row = store.active_profile(db())
    if row:
        return row
    return {"encoder": ENCODER, "quality": core.recommended_quality(ENCODER), "preset": "",
            "profile": "", "max_height": 0, "audio_codec": "aac", "audio_bitrate": 192,
            "audio_channels": 2}


def profile_args(p: dict, source: str, part: str, with_subs: bool, audio_codec: str | None = None,
                 hardware_decode: bool = True, source_pix_fmt: str = "") -> list[str]:
    """The exact argv one profile produces. Shared by real jobs and the test
    encode, so what gets validated is what gets run - a test that builds its
    command differently is a test of something else."""
    encoder = p["encoder"] if p["encoder"] in core.DEFAULT_TEMPLATES else ENCODER
    profile = core.valid_option(encoder, "profiles", p.get("profile", ""))
    # Decided once and handed to both halves of the command: narrowing the
    # picture is a CPU filter, so it also decides whether the decoder may keep
    # frames in GPU memory. Splitting that decision is how they disagree.
    pix_fmt = core.output_pix_fmt(profile, source_pix_fmt)
    return core.build_ffmpeg_args(
        core.DEFAULT_TEMPLATES[encoder], source, part, p["quality"], with_subs,
        preset=core.valid_option(encoder, "presets", p.get("preset", "")),
        profile=profile,
        max_height=p.get("max_height", 0),
        audio=core.audio_args(audio_codec or p.get("audio_codec", "aac"),
                              p.get("audio_bitrate", 192), p.get("audio_channels", 2)),
        pix_fmt=pix_fmt,
        hwaccel=core.hwaccel_args(encoder, p.get("max_height", 0), hardware_decode, pix_fmt),
        # Decided from the resolved encoder, not from the template: the cap is
        # meaningless on NVENC and QSV, where the work is not on the CPU, and a
        # setting that only appears to do something is worse than no setting.
        threads=core.thread_args(encoder, cfg()["encode_threads"]),
    )


# What every rung below actually fixes: a stream or a decoder that would not
# fit. Anything else ffmpeg says is a failure the ladder cannot help with.
# ponytail: substring match on ffmpeg's stderr, which is not a stable API. If it
# ever misses a real fallback case, add the phrase here rather than reopening
# the gate to every nonzero exit.
FALLBACK_WORTHY = re.compile(r"subtitle|mov_text|codec|cuda|nvdec|hwaccel|hardware", re.I)


def run_encode(job_id: str, source: str, names: core.JobNames, src_probe: core.Probe) -> tuple[bool, str, str]:
    """One encode attempt cycle, down the fallback ladder. (ok, warning, error)."""
    conn = db()
    prof = encoding_profile()
    c = cfg()
    hw, stall_minutes = c["hardware_decode"], c["stall_timeout_minutes"]
    # nice and ionice wrap the command instead of being flags to it. Both exec
    # ffmpeg in place rather than forking, so the PID Popen holds IS ffmpeg and
    # cancel, terminate and the stall watchdog all still reach the encoder - a
    # wrapper that forked would have broken cancel without saying so.
    throttle = core.throttle_prefix(c["encode_nice"], c["encode_idle_io"])
    attempts = [(True, prof["audio_codec"], hw, "")]
    if src_probe.subtitle_streams:
        attempts.append((False, prof["audio_codec"], hw, "text subtitles could not be carried into mp4 - dropped"))
    if prof["audio_codec"] == "copy":
        # MP4 cannot hold DTS or TrueHD, and those are exactly the tracks worth
        # copying. Falling back beats failing the job over the audio.
        attempts.append((False, "aac", hw, "the original audio could not be copied into mp4 - re-encoded to AAC"))
    if hw:
        # ffmpeg falls back to software decoding on its own for a codec NVDEC
        # cannot handle, so this last resort is for the rarer case: a driver
        # that accepts the flag and then fails mid-stream. Better one slow
        # encode than a failed file.
        attempts.append((False, "aac", False, "hardware decoding failed - decoded on the CPU instead"))

    error = "no encode attempt ran"
    for with_subs, audio_codec, hardware_decode, warning in attempts:
        # Prefixed before it is recorded, not after: log_tail's whole job is to
        # say what actually ran, and a throttled encode that logs the unthrottled
        # command is a field that lies in exactly the case somebody is reading it.
        args = throttle + profile_args(prof, source, names.part, with_subs, audio_codec, hardware_decode,
                                       src_probe.pix_fmt)
        conn.execute("UPDATE jobs SET log_tail=? WHERE id=?", (" ".join(args), job_id))
        conn.commit()
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        with _jobs_lock:
            _running.setdefault(job_id, {"cancel": False})["proc"] = proc
        if _cancelled(job_id):
            proc.terminate()  # cancelled between the claim and the spawn
        stderr_tail: list[str] = []

        def drain_stderr() -> None:
            for line in proc.stderr or []:
                stderr_tail.append(line.rstrip())
                del stderr_tail[:-50]

        t = threading.Thread(target=drain_stderr, daemon=True)
        t.start()

        # A watchdog, because "for line in proc.stdout" has no timeout of its
        # own: an ffmpeg wedged on a share that went away blocks that loop for
        # the life of the container, holding this worker slot while /healthz
        # goes on reporting ok. Killing the process is what unblocks the read.
        heartbeat = [time.time()]
        finished, stalled = threading.Event(), threading.Event()

        def watchdog(proc=proc, finished=finished, stalled=stalled, heartbeat=heartbeat) -> None:
            # Bound as defaults rather than captured: this thread kills a
            # process, and the loop rebinds every one of these names on the next
            # attempt down the ladder.
            while not finished.wait(5):
                if core.is_stalled(time.time() - heartbeat[0], stall_minutes):
                    stalled.set()
                    log.warning("job %s made no progress for %s minutes - killing it", job_id[:8], stall_minutes)
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    return

        threading.Thread(target=watchdog, daemon=True).start()
        for line in proc.stdout or []:
            heartbeat[0] = time.time()
            pct = core.parse_progress(line, src_probe.duration)
            if pct is not None:
                conn.execute("UPDATE jobs SET progress=? WHERE id=?", (pct, job_id))
                conn.commit()
            if _cancelled(job_id):
                proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            # terminate is a request. An ffmpeg wedged on a stalled share will
            # ignore it, and waiting forever would hold this worker permanently.
            log.warning("job %s did not exit on terminate - killing", job_id[:8])
            proc.kill()
            proc.wait(timeout=10)
        finished.set()
        t.join(timeout=5)
        with _jobs_lock:
            if job_id in _running:
                _running[job_id]["proc"] = None

        if _cancelled(job_id):
            return False, "", "cancelled"
        if proc.returncode == 0:
            return True, warning, ""
        if stalled.is_set():
            # The one failure that is not worth a fallback: a share that has
            # gone away will not come back between attempts, and each rung would
            # hold this worker for another whole timeout to find that out.
            return False, "", f"no progress for {stall_minutes} minutes - encode killed (is the share still mounted?)"
        # The gate and the recorded message read the same text on purpose: a
        # rung that retries on words nobody is ever shown is a rung nobody can
        # explain from the job list.
        tail = core.error_summary(stderr_tail)
        error = f"ffmpeg exited {proc.returncode}: {tail}"
        if not FALLBACK_WORTHY.search(tail):
            # A full disk, a share that went away, an unreadable source: none of
            # those change because the next rung drops subtitles, so retrying
            # only burns the queue three times over on the same failure.
            return False, "", error
        # The gate used to also require with_subs, which only the first attempt
        # ever has, so every rung after it returned immediately and the AAC and
        # CPU-decode rungs were unreachable: a subtitled DTS remux with copy
        # audio failed on the rung above the one that would have worked.
        try:
            os.unlink(names.part)
        except OSError:
            pass
    return False, "", error


def trash(source: str, job_id: str | None = None) -> str:
    """The source is never deleted - it outlives its replacement in the trash.

    Mirrored under its media-root-relative path so a recovery is a move back,
    and pruned by age so the trash cannot eat the disk.

    The move is also written to the trash table, because the mirroring stops
    being reversible the moment the de-duplication below appends a .1: nothing
    can tell that suffix apart from a file genuinely named "Movie.1.mkv", and
    Restore puts media back where this says it came from.
    """
    dest = core.trash_destination(source, MEDIA_ROOTS, TRASH_DIR)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # shutil.move REPLACES an existing destination. Processing the same relative
    # path twice inside the retention window would leave one safety copy where
    # there should be two, and the one it destroys is the older source - the one
    # somebody would actually want back. Retention is a promise; it has to hold
    # for every copy it was made about.
    stem, ext = os.path.splitext(dest)
    attempt = 1
    while os.path.exists(dest):
        dest = f"{stem}.{attempt}{ext}"
        attempt += 1
    shutil.move(source, dest)
    # Stamp it NOW. shutil.move preserves the original mtime, and imported
    # media carries the release's own timestamp - a median of twelve years old
    # in a real library. Pruning by that inherited date deleted every source
    # older than the retention window on the very next sweep, minutes after a
    # job reported "source preserved". Retention has to measure how long the
    # file has been in the trash, not how old the release is.
    try:
        os.utime(dest, None)
    except OSError:
        pass
    # Recorded after the move, so a row never claims a file that is not there.
    # Best effort on purpose: the media is already safe at this point and a
    # bookkeeping failure must not turn a completed conversion into a crash.
    try:
        size = os.path.getsize(dest)
    except OSError:
        size = None
    try:
        db().execute("INSERT OR REPLACE INTO trash (path, original, bytes, at, job_id) VALUES (?,?,?,?,?)",
                     (dest, source, size, time.time(), job_id))
        db().commit()
    except sqlite3.Error:
        log.exception("could not record %s in the trash table - Restore will fall back to deriving its origin", dest)
    return dest


def release_page_cache(*paths: str) -> int:
    """Tell the kernel these files' pages will not be read again. Returns how
    many it actually released, which is what the tests assert on.

    A conversion reads a whole source and writes a whole output, neither of
    which anything reads again - the source is in the trash and the output is
    streamed once, sequentially, by a media server that reads ahead anyway.
    Left alone those pages stay resident, and on the box this was found on 18
    jobs in 90 minutes put 50 GB into page cache and left 1 GB free on a 64 GB
    machine. That is not a caching problem, it is a fragmentation one: the
    kernel then has no high-order pages to hand the NVIDIA driver, cuInit fails
    with CUDA_ERROR_NOT_INITIALIZED, and EVERY encode fails until somebody
    compacts memory by hand.

    This is the only half of that this container can do. /proc/sys is mounted
    read-only in Docker, so drop_caches and compact_memory are refused even to
    root in here - and the fix for that is host tuning (see the README), not
    running this thing privileged. What it CAN do is stop being the thing that
    fills the cache in the first place.

    POSIX_FADV_DONTNEED ignores dirty pages, so each file is flushed first.
    That fsync is worth having on its own: it is the difference between an
    output that is on disk and one that is merely in the page cache of a NAS
    about to lose power, and it happens before the source is trashed.
    """
    advise = getattr(os, "posix_fadvise", None)
    if advise is None:
        return 0  # not Linux; nothing to do and nothing to warn about
    released = 0
    for path in paths:
        if not path:
            continue
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            continue
        try:
            os.fsync(fd)
            advise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            released += 1
        except OSError:
            pass
        finally:
            os.close(fd)
    return released


def file_identity(path: str) -> tuple | None:
    """Enough of a file to tell "still the same one" from "somebody rewrote it".

    Inode, size and mtime together, because each catches a different way an arr
    replaces a file: a rename-into-place changes the inode, an in-place rewrite
    changes size or mtime, and nothing an importer does leaves all three alone.
    None when there is no file there.

    Deliberately not a hash. This is asked about a 40GB remux twice per job, and
    a rule that costs two full reads of the library's biggest file is a rule
    that gets switched off.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def displace(visible: str, expected: tuple | None) -> str:
    """Move the file already at a visible target into the trash. "" if none.

    Never an unlink, and never left for os.replace to do silently: what is
    being displaced is somebody's episode, and unlike a source - which is only
    ever replaced by a verified encode OF that source - it has no other copy
    anywhere. It gets the same retention every replaced source gets, mirrored
    under the same relative path, so undoing this is a move back - and the
    Trash tab is where somebody does that.

    Re-checks the identity it was told to expect. occupied() asked the same
    question moments ago and this is the call that actually destroys something,
    so it does not take that answer on trust.
    """
    if not expected or file_identity(visible) != expected:
        return ""
    return trash(visible)


def displaced_note(replaced: str) -> str | None:
    """The warning a job carries when it replaced a file, or None.

    A warning rather than silence: replacing an episode somebody may have been
    about to watch is not a detail, and the whole reason this is safe is that
    the old file still exists - which is only useful to somebody who is told.
    """
    return f"replaced the file already at this name - the previous one is at {replaced}" if replaced else None


def trash_roots() -> list[str]:
    """Every directory that can be holding trashed sources.

    More than one because the default moved: sources now go under the media root
    that held them, and the live deployment still has 127 GB under the old
    /config/trash. A prune that swept only the new locations would keep all of
    that forever, which is the opposite of what a retention setting promises.
    """
    roots = [os.path.join(r, core.TRASH_DIRNAME) for r in MEDIA_ROOTS]
    if TRASH_DIR:
        roots.append(TRASH_DIR)
    roots.append(LEGACY_TRASH)
    return list(dict.fromkeys(roots))


# A bulk restore or delete is one HTTP request that moves or destroys that many
# files. Capped so a runaway client cannot hand the whole trash to one call, and
# refused rather than truncated - a partial success reported as success is how
# somebody concludes a file was deleted when it was not.
MAX_TRASH_BATCH = 500


def trash_entry(path: str, row: dict | None, c: dict) -> dict:
    """One row of the trash view: where it is, where it would go back to, and
    what restoring it would cost."""
    original = (row["original"] if row else "") or core.trash_origin(path, MEDIA_ROOTS, TRASH_DIR)
    try:
        st = os.stat(path)
        size, at = st.st_size, (row["at"] if row else st.st_mtime)
    except OSError:
        size, at = None, (row["at"] if row else 0)
    return {
        "path": path,
        "original": original,
        # Derived rather than recorded: a file that predates the trash table,
        # or one whose row was lost. Surfaced because its name may carry a
        # de-duplication suffix that no rule can strip safely - see
        # core.trash_origin.
        "origin_known": bool(row and row["original"]),
        "bytes": size,
        "at": at,
        "job_id": row["job_id"] if row else None,
        # Both asked at read time rather than stored: the library moves under
        # this view, and a stale "free" is what would make Restore quietly
        # replace something.
        "occupied": bool(original) and os.path.exists(original),
        "reconverts": bool(original) and core.will_be_converted_again(
            original, c["hidden_only"], c["convert_extensions"], c["skip_patterns"]),
    }


def list_trash(limit: int = 100, offset: int = 0) -> dict:
    """One page of the trash, newest first, with its retention.

    Walks the filesystem rather than the table, because the table only knows
    about files trashed since it existed and a live deployment had 127 GB in
    there before that. The table supplies the exact origin where it has one.

    Paged by offset rather than by a cursor, which is the opposite of the job
    history next door - and safe here for a reason that does not hold there.
    The whole list is rebuilt and re-sorted on every request anyway (the walk
    is what costs, not the slice), and every operation is addressed by PATH.
    A file pruned between two page loads can therefore shift a row, but it
    cannot make a delete land on the wrong file: the worst it does is report
    "not a file in the trash" for something already gone.
    """
    conn = db()
    rows = {r["path"]: dict(r) for r in conn.execute("SELECT * FROM trash").fetchall()}
    c = cfg()
    entries, total_bytes, total = [], 0, 0
    for root in trash_roots():
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                p = os.path.join(dirpath, name)
                total += 1
                entry = trash_entry(p, rows.get(p), c)
                total_bytes += entry["bytes"] or 0
                entries.append(entry)
    # Path breaks the tie, or the order is undefined between two files trashed
    # by one job - and an undefined order under a pager is a row that appears
    # on two pages and another that appears on none.
    entries.sort(key=lambda e: (-e["at"], e["path"]))
    # Rows whose file is gone - pruned, or restored by something else. Cleaned
    # here rather than in the prune, so the table cannot outlive the disk
    # whichever of the two removed it.
    for stale in set(rows) - {e["path"] for e in entries}:
        conn.execute("DELETE FROM trash WHERE path=?", (stale,))
    conn.commit()
    # Clamped to the START of the last page, never to the end of the list. A
    # bulk delete shortens this list under whoever ran it, and an offset that
    # has fallen off the end must show the last page rather than a blank table
    # with a working Previous button and no clue why.
    if not entries:
        offset = 0
    elif offset >= len(entries):
        offset = ((len(entries) - 1) // limit) * limit
    offset = max(0, offset)
    page = entries[offset:offset + limit]
    return {"entries": page, "total": total, "bytes": total_bytes,
            "keep_days": c["trash_keep_days"], "shown": len(page),
            "offset": offset, "limit": limit}


def in_trash(path: str) -> str:
    """A caller-supplied path resolved and proven to be a file in the trash.

    "" for anything else. This is the gate on two operations that delete
    somebody's media, and the API takes paths from the client, so containment
    is checked against the REAL path - a symlink in the trash pointing at the
    library would otherwise make Restore and Delete reach anywhere on disk.
    """
    try:
        resolved = os.path.realpath(path)
    except OSError:
        return ""
    if not core.is_within(resolved, [os.path.realpath(r) for r in trash_roots()]):
        return ""
    return resolved if os.path.isfile(resolved) else ""


def delete_from_trash(paths: list[str]) -> list[dict]:
    """Delete now, rather than when retention expires."""
    conn, results = db(), []
    for raw in paths:
        p = in_trash(raw)
        if not p:
            results.append({"path": raw, "ok": False, "detail": "not a file in the trash"})
            continue
        try:
            os.unlink(p)
        except OSError as e:
            results.append({"path": raw, "ok": False, "detail": str(e)})
            continue
        conn.execute("DELETE FROM trash WHERE path=?", (p,))
        results.append({"path": raw, "ok": True, "detail": "deleted"})
    conn.commit()
    return results


def restore_from_trash(paths: list[str], replace: bool = False) -> list[dict]:
    """Put files back where they came from.

    `replace` is required when something already holds the destination, and the
    thing it displaces is TRASHED rather than deleted. The case this exists for
    is an upgrade that turned out worse than what it replaced - so the file in
    the way is itself a candidate for being restored ten minutes later, and
    deleting it outright would make undoing the undo impossible. It costs one
    more file in the trash and buys back the whole decision.
    """
    conn, results = db(), []
    for raw in paths:
        p = in_trash(raw)
        if not p:
            results.append({"path": raw, "ok": False, "detail": "not a file in the trash"})
            continue
        row = conn.execute("SELECT * FROM trash WHERE path=?", (p,)).fetchone()
        original = (row["original"] if row else "") or core.trash_origin(p, MEDIA_ROOTS, TRASH_DIR)
        if not original:
            results.append({"path": raw, "ok": False,
                            "detail": "nothing recorded this file's origin and it cannot be derived"})
            continue
        # The destination is media, so it goes through the same gate every job
        # does. A trash root pointed somewhere odd must not become a way to
        # write anywhere on the host.
        ok, resolved = core.validate_path(original, MEDIA_ROOTS)
        if not ok:
            results.append({"path": raw, "ok": False, "detail": f"refusing to restore to {original}: {resolved}"})
            continue
        displaced = ""
        if os.path.exists(resolved):
            if not replace:
                results.append({"path": raw, "ok": False, "occupied": True,
                                "detail": f"{os.path.basename(resolved)} already exists - restoring needs replace"})
                continue
            try:
                displaced = trash(resolved)
            except OSError as e:
                results.append({"path": raw, "ok": False, "detail": f"could not move the file in the way: {e}"})
                continue
        try:
            os.makedirs(os.path.dirname(resolved), exist_ok=True)
            shutil.move(p, resolved)
        except OSError as e:
            results.append({"path": raw, "ok": False, "detail": str(e)})
            continue
        conn.execute("DELETE FROM trash WHERE path=?", (p,))
        log.info("restored %s to %s%s", p, resolved, f" (replaced file trashed at {displaced})" if displaced else "")
        results.append({"path": raw, "ok": True, "restored_to": resolved, "displaced": displaced,
                        "detail": "restored" + (" and trashed what was in the way" if displaced else "")})
    conn.commit()
    return results


def prune_trash() -> None:
    # Retention is measured from when a file was TRASHED, and trash() restamps
    # it for exactly that reason - see the note there.
    cutoff = time.time() - cfg()["trash_keep_days"] * 86400
    for root in trash_roots():
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root, topdown=False):
            for f in files:
                p = os.path.join(dirpath, f)
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.unlink(p)
                except OSError:
                    pass
            try:
                if dirpath != root and not os.listdir(dirpath):
                    os.rmdir(dirpath)
            except OSError:
                pass


# One client per connection, kept between jobs so the library list is actually
# cached. Rebuilt when the row changes - a fresh client per job made
# ArrClient.CACHE_SECONDS dead code and re-downloaded the whole library for
# every single finished file.
_arr_clients: dict[str, tuple[tuple, arr_client.ArrClient]] = {}


def _client_for(row: dict) -> arr_client.ArrClient:
    ident = (row["base_url"], row["api_key"], row["arr_path"], row["worker_path"], row["kind"])
    cached = _arr_clients.get(row["id"])
    if cached and cached[0] == ident:
        return cached[1]
    client = arr_client.ArrClient(row)
    _arr_clients[row["id"]] = (ident, client)
    return client


def notify_arrs(visible_path: str) -> str | None:
    """Tell whichever arr owns this file to re-read it.

    Jellyfin does not detect a same-name in-place replacement, and neither does
    an arr until it is asked - so without this the stack keeps serving the old
    file's codec, bitrate and runtime for a file that no longer exists in that
    form. Never fatal: the media is already correct on disk, and a conversion
    is not going to be undone because an arr was unreachable.
    """
    conn = db()
    notes = []
    for row in store.list_arrs(conn, redact=False):
        if not row["enabled"]:
            continue
        try:
            handled, message = _client_for(row).rescan_for(visible_path)
        except Exception as e:  # noqa: BLE001 - an arr must never take a job down
            handled, message = True, f"{row['name']}: {e}"
        if handled:
            notes.append(message)
            store.note_arr_error(conn, row["id"], None if "rescanning" in message else message)
        elif message and "not under" not in message and "no " not in message:
            # An unreachable arr or a rotated key returns "not handled" too, and
            # staying silent there left the UI showing a healthy connection that
            # had not worked for days.
            store.note_arr_error(conn, row["id"], message)
    return "; ".join(notes) if notes else None


def rescan_after(job_id: str, visible_path: str) -> None:
    """Notify the arrs once the job is already recorded as done."""
    try:
        rescan = notify_arrs(visible_path)
    except Exception as e:  # noqa: BLE001
        rescan = f"rescan failed: {e}"
        log.exception("rescan after %s failed", job_id[:8])
    if rescan:
        try:
            conn = db()
            conn.execute("UPDATE jobs SET rescan=? WHERE id=?", (rescan, job_id))
            conn.commit()
        except sqlite3.Error:
            log.warning("could not record rescan result for %s", job_id[:8])


# Short on purpose. A receiver that hangs holds a thread and nothing else, but
# the ceiling is what makes that sentence true.
WEBHOOK_TIMEOUT = 10


def webhook_after(job_id: str) -> None:
    """POST a finished job to whoever asked for it, in the background.

    The same rule as notify_arrs and for the same reason: this runs after the
    media is already correct on disk, so a receiver that is down, slow or wrong
    cannot fail a conversion, cannot hold up the next job and cannot take the
    worker down. A thread, one short timeout and a log line is all of its power.
    """
    try:
        c = cfg()
        url = c["webhook_url"].strip()
        if not url:
            return
        row = db().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return
        # job_dict rather than a second hand-written shape, so the payload is
        # exactly what GET /api/jobs already returns - and log_tail, which holds
        # the full ffmpeg argv and the container's absolute paths, stays out of
        # it by the same rule that keeps it out of the list route.
        payload = {"event": f"job.{row['state']}", "version": VERSION,
                   "sent": time.time(), "job": job_dict(row)}
        threading.Thread(target=_post_webhook, args=(url, c["webhook_secret"], payload, job_id),
                         daemon=True).start()
    except Exception:  # noqa: BLE001 - a webhook never fails a job that is done
        log.exception("could not send the webhook for job %s", job_id[:8])


def _post_webhook(url: str, secret: str, payload: dict, job_id: str) -> None:
    try:
        # A webhook URL is operator-supplied egress exactly like an arr base URL,
        # so it gets the arr client's guard rather than a second copy of the rule
        # that would be the one nobody updates. Its opener comes along too,
        # because that one refuses redirects: a 302 would replay this POST at an
        # address blocked_reason never got to look at.
        blocked = arr_client.blocked_reason(url)
        if blocked:
            log.warning("webhook not sent: %s", blocked)
            return
        body = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", arr_client.USER_AGENT)
        if secret:
            # Signed over the exact bytes on the wire, so the receiver verifies
            # what it was sent rather than what it re-serialized.
            req.add_header("X-TranscodeArr-Signature",
                           "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest())
        with arr_client._opener.open(req, timeout=WEBHOOK_TIMEOUT):  # noqa: SLF001
            pass
    except Exception as e:  # noqa: BLE001 - an unreachable receiver is not a failed job
        log.warning("webhook for job %s failed: %s", job_id[:8], e)


def process(job: dict) -> None:
    conn = db()
    job_id, source = job["id"], job["path"]
    # worker_loop already claimed this row as 'running' - conditionally, so the
    # claim and a concurrent cancel cannot both win.

    def finish(state: str, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(
            f"UPDATE jobs SET state=?, finished=?{', ' + sets if sets else ''} WHERE id=?",
            (state, time.time(), *fields.values(), job_id),
        )
        conn.commit()
        log.info("job %s %s %s", job_id[:8], state, fields.get("error") or fields.get("output") or "")
        if state in ("done", "failed"):
            # Fired here rather than at the two happy endings, because finish()
            # is the single funnel every terminal state goes through - including
            # "source vanished" and every other early return, which are exactly
            # the ones a receiver wants to hear about.
            webhook_after(job_id)

    try:
        if not os.path.exists(source):
            return finish("failed", error="source vanished before processing")
        # A revealed file keeps its own container. Planning with a hard-coded
        # .mp4 would rename a skipped .mkv to .mp4 without converting it -
        # a file whose extension lies about its contents.
        source_ext = os.path.splitext(source)[1].lower()
        # Re-check the skip rules now, not only at enqueue. A rule added while
        # hundreds of files are already queued would otherwise protect none of
        # them - every pending row keeps the 'kind' it was born with.
        protected = job["kind"] == "reveal" or core.matches_skip(os.path.basename(source), cfg()["skip_patterns"])
        names = core.plan_names(source, source_ext if protected else ".mp4")

        # Whatever already sits at the visible target, sampled BEFORE anything
        # is encoded. This is the whole basis of telling a file that was
        # already there apart from one that arrived while we worked.
        existing_target = None if names.visible == source else file_identity(names.visible)

        def occupied() -> str | None:
            """Whichever of our two write targets holds a file we must not touch.

            Called before the encode AND again immediately before every
            os.replace, because os.replace destroys the destination without a
            word and the pre-flight answer is hours stale by the time an encode
            finishes.

            The visible target may hold the file it held at pre-flight: that is
            the previous conversion of an episode that has just been imported
            again, and it is displaced into the trash rather than refused.
            Refusing is what left whole seasons failing every six hours forever
            with nothing on disk changing between attempts. Anything ELSE there
            arrived mid-run - see core.may_replace_target.

            The staging name stays absolutely off limits. A file at
            `.Show - S01E01.mp4` is a hidden import waiting for its own reveal
            job, not a stale output, and displacing it would eat somebody
            else's pending work.
            """
            if names.hidden_final != source and os.path.exists(names.hidden_final):
                return f"staging name is taken: {names.hidden_final} - not overwriting"
            if names.visible != source and not core.may_replace_target(
                    existing_target, file_identity(names.visible)):
                return (f"{names.visible} was written by something else while this job ran - "
                        "not overwriting the newer file")
            return None

        taken = occupied()
        if taken:
            return finish("failed", error=taken)

        src_probe = ffprobe(source)
        if src_probe is None or src_probe.video_streams < 1:
            return finish("failed", error="source is not a readable video (ffprobe found no video stream)")
        src_bytes = os.path.getsize(source)

        # Already the right container, or deliberately protected by a skip rule:
        # verify it is whole, then just reveal it.
        if protected or core.should_skip_transcode(src_probe, source_ext):
            if names.hidden:
                # Re-asked here for the same reason it is re-asked after an
                # encode: the pre-flight answer is already stale. ffprobe above
                # can run for a minute on a 4K remux over SMB, and an arr that
                # imports at the visible name inside that window loses its file
                # to os.replace without a word - and unlike the source, what
                # gets clobbered here never reaches the trash.
                taken = occupied()
                if taken:
                    return finish("failed", error=taken)
                replaced = displace(names.visible, existing_target)
                os.replace(source, names.visible)
                finish("done", output=names.visible, warning=displaced_note(replaced),
                       src_bytes=src_bytes, out_bytes=src_bytes, progress=100)
                return rescan_after(job_id, names.visible)
            return finish("done", output=source, warning="nothing to do", progress=100)

        ok, warning, error = run_encode(job_id, source, names, src_probe)
        if not ok:
            try:
                os.unlink(names.part)
            except OSError:
                pass
            return finish("cancelled" if error == "cancelled" else "failed", error=error)

        # The check whose absence truncated a library: never trust exit 0.
        out_probe = ffprobe(names.part)
        try:
            tolerance = float(cfg()["verify_duration_tolerance"])
        except ValueError:
            tolerance = 0.015  # an unparseable tolerance must not mean "accept anything"
        verified, why = core.verify_output(src_probe, out_probe or core.Probe(None, 0, 0, 0), tolerance)
        if not verified:
            try:
                os.unlink(names.part)
            except OSError:
                pass
            return finish("failed", error=f"output failed verification: {why}")

        out_bytes = os.path.getsize(names.part)
        # Two-writers guard: if the source changed underneath the encode (an
        # arr upgraded it mid-run), this encode is of a file that no longer
        # exists - throw the encode away, never the newer source.
        if not os.path.exists(source) or os.path.getsize(source) != src_bytes:
            try:
                os.unlink(names.part)
            except OSError:
                pass
            return finish("failed", error="source changed during the encode - encode discarded, source untouched")

        # Both guards again, hours after the pre-flight ran them. The reveal
        # below never checked at all, and the pre-flight answer is stale the
        # moment the encode starts: an arr importing the upgraded release
        # mid-run lands on exactly these two names, and os.replace destroys what
        # it finds without a word. Unlike the source, a clobbered file does not
        # even reach the trash. The encode is the disposable side of this.
        taken = occupied()
        if taken:
            try:
                os.unlink(names.part)
            except OSError:
                pass
            return finish("failed", error=taken)

        os.replace(names.part, names.hidden_final)      # hidden, complete, atomic
        # Flushed to disk BEFORE the source is trashed, not after. Until this
        # returns, the verified output exists only in the page cache of a NAS -
        # and the next line moves the one other copy of that episode. It also
        # hands those pages back, which is the point: see release_page_cache.
        release_page_cache(names.hidden_final)
        trashed = trash(source)                          # source survives, in trash
        # The file this job decided at pre-flight to displace, moved aside
        # rather than clobbered. Between the trash above and this one, both
        # copies of the episode outlive the swap by the full retention window.
        replaced = displace(names.visible, existing_target)
        os.replace(names.hidden_final, names.visible)    # the reveal
        # Both of these are in the trash now, where nothing reads them, and
        # between them they are most of what this job put in the page cache.
        release_page_cache(trashed, replaced)
        # Marked done BEFORE talking to any arr. The media is already correct on
        # disk at this point; letting an unreachable arr throw into the handler
        # below would stamp 'failed' on a conversion that completely succeeded,
        # and re-running it would then trip the staging-name guard.
        finish(
            "done",
            output=names.visible,
            warning="; ".join(filter(None, [warning, displaced_note(replaced)])) or None,
            src_bytes=src_bytes,
            out_bytes=out_bytes,
            progress=100,
            # Both safety copies, because a swap that replaced a file has two
            # things somebody might want back, not one.
            log_tail="; ".join(filter(None, [f"source preserved at {trashed}",
                                             f"replaced file preserved at {replaced}" if replaced else ""])),
        )
        rescan_after(job_id, names.visible)
    except Exception as e:  # noqa: BLE001
        log.exception("job %s crashed", job_id[:8])
        finish("failed", error=f"internal: {e}")


def worker_loop() -> None:
    # Guarded like watch_loop is. Without this, one OperationalError - from a
    # write that lost a race for the database - ends the only worker thread.
    # The process keeps running, /healthz keeps saying ok, and the queue simply
    # never moves again, which is the worst way for this to fail.
    while True:
        try:
            # The gate, and the only place it belongs: nothing below this line
            # ever reaches a job that is already encoding. A window that closes
            # or a Stop that arrives mid-encode lets that encode finish, verify
            # and reveal exactly as it would have - it is the next claim that
            # does not happen.
            allowed, _why = may_claim()
            if not allowed:
                time.sleep(2)
                continue
            conn = db()
            row = None
            # Claim under the lock so the concurrency limit is honored: without
            # it two idle workers both see room for one more and both take work.
            with _jobs_lock:
                if len(_running) >= max(1, cfg()["max_concurrent"]):
                    row = None
                else:
                    # (priority, created): reveals first, then oldest first.
                    candidate = conn.execute(
                        "SELECT * FROM jobs WHERE state='queued' ORDER BY priority, created LIMIT 1"
                    ).fetchone()
                    if candidate is not None:
                        # Conditional: a cancel may have taken this row since the
                        # read, and an unconditional write would stamp 'running'
                        # over 'cancelled' and encode it anyway.
                        claimed = conn.execute(
                            "UPDATE jobs SET state='running', started=?, encoder=? WHERE id=? AND state='queued'",
                            (time.time(), ENCODER, candidate["id"]),
                        )
                        conn.commit()
                        if claimed.rowcount:
                            row = candidate
                            _running[row["id"]] = {"cancel": False, "proc": None}
            if row is None:
                time.sleep(2)
                continue
            try:
                process(job_dict(row))
            finally:
                with _jobs_lock:
                    _running.pop(row["id"], None)
        except Exception:  # noqa: BLE001
            log.exception("worker loop error")
            time.sleep(5)


# ---------------------------------------------------------------------------
# The watcher - polling, on purpose
# ---------------------------------------------------------------------------
# inotify does not work across SMB/NFS, which is exactly where NAS media
# lives, so event watchers silently degrade anyway. A poll every few minutes
# with size-stability is slower and correct.

# Watch roots already complained about, so the warning below stays a warning
# instead of becoming the same line repeated every scan interval forever.
_warned_missing: set[str] = set()

# How many files the last scan would have converted but for their missing
# leading dot - and 0 whenever the scan found any hidden file at all, because
# then the dot convention IS in use here and the filter is doing its job.
#
# Nothing in stock Radarr or Sonarr hides an import, so a container pointed at
# an ordinary library filters out every single file, boots clean, reports
# healthy and converts nothing forever. That silence is the exact failure this
# project exists to remove, so the count is kept where _run_state_view can
# publish it instead of leaving the page showing an empty queue with no reason.
# Written by the watcher thread and read by the HTTP threads, with no lock: it
# is one rebind of an int, and a reader racing a scan gets the previous scan's
# number rather than a torn one.
VISIBLE_ONLY_SKIPPED = 0


def scan_once(force: bool = False) -> dict:
    """One walk of the watched folders. Returns what it found, for the caller
    that asked for it by hand - the interval's own scan throws the answer away.

    `force` ignores the retry cooldown, and only the on-demand scan sets it.
    The cooldown exists to stop the WATCHER re-running a permanently failing
    file every interval forever; somebody pressing "Check for files to convert"
    is a person asking about those files now, which is the same reason
    POST /api/jobs has always ignored it. Without this the button answers
    "nothing new to convert" about 23 files it can see perfectly well and is
    simply declining to mention - the silence this worker exists to remove.
    """
    global VISIBLE_ONLY_SKIPPED  # noqa: PLW0603
    conn = db()
    now = time.time()
    queued = 0
    # Eligible, stable, and still not queued. Split by reason, because "nothing
    # new to convert" and "23 files are sitting out a cooldown you can override"
    # are different answers and only one of them means there is nothing to do.
    cooling = pending = 0
    missing: list[str] = []
    # A candidate the stability window has not finished holding yet. Worth
    # counting separately: "found nothing to do" and "found six files that are
    # still being copied" are different answers, and only one of them means
    # come back later.
    settling = 0
    c = cfg()
    watch_roots = c["watch_roots"] or MEDIA_ROOTS
    convert_extensions = set(c["convert_extensions"])
    hidden_only, skip_patterns = c["hidden_only"], c["skip_patterns"]
    # The default trash is dot-prefixed and would be skipped by the dot rule
    # below like any other hidden directory, but TRASH_DIR can be set to a plain
    # name inside a media root - and walking the trash means re-queueing every
    # source in it, converting the safety copies and then trashing those.
    trash_dirs = set(trash_roots())
    # Counted inside the walk that already filters. Answering "why is nothing
    # queued" with a second pass would mean stat-ing an entire library twice
    # every scan interval to produce one log line.
    visible_skipped = hidden_found = 0
    for root in watch_roots:
        if not os.path.isdir(root):
            missing.append(root)
            # The bare `continue` this replaces is why a copy-pasted config with
            # a root nobody mounted looks perfectly healthy and converts
            # nothing - the silence this worker exists to remove.
            if root not in _warned_missing:
                _warned_missing.add(root)
                log.warning("watch root %s does not exist in this container - nothing under it "
                            "will ever be converted", root)
            continue
        _warned_missing.discard(root)
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs
                       if not d.startswith(".") and os.path.join(dirpath, d) not in trash_dirs]
            for name in files:
                if core.PART_MARKER in name:
                    continue
                hidden = name.startswith(".")
                ext = os.path.splitext(name)[1].lower()
                # The extension test moved ahead of the visibility test so that
                # a skipped file can be counted: what makes the skip worth
                # reporting is that the file would otherwise have been
                # converted. A stray .nfo or .srt is not evidence of anything.
                wanted = ext in convert_extensions or (hidden and ext == ".mp4")
                if not wanted:
                    continue
                if hidden:
                    hidden_found += 1
                elif hidden_only:
                    visible_skipped += 1
                    continue
                path = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                prev = conn.execute("SELECT size, at FROM seen WHERE path=?", (path,)).fetchone()
                conn.execute("INSERT OR REPLACE INTO seen (path, size, at) VALUES (?,?,?)",
                             (path, size, prev["at"] if prev and prev["size"] == size else now))
                if prev and core.is_stable(prev["size"], size, now - prev["at"], c["stable_seconds"]):
                    ok, resolved = core.validate_path(path, MEDIA_ROOTS)
                    if ok:
                        # A skip rule does not mean "ignore" - the file is still
                        # stuck behind a dot. It gets revealed in its own
                        # container instead of re-encoded.
                        protected = core.matches_skip(name, skip_patterns)
                        kind = "reveal" if (hidden and ext == ".mp4") or protected else "transcode"
                        if enqueue(resolved, kind, force=force):
                            queued += 1
                        else:
                            # enqueue says no without saying why, and the two
                            # reasons mean opposite things to somebody reading
                            # the answer. One indexed lookup on the index it
                            # just used.
                            last = conn.execute(
                                "SELECT state FROM jobs WHERE path=? ORDER BY created DESC LIMIT 1",
                                (resolved,)).fetchone()
                            if last and last["state"] in ("queued", "running"):
                                pending += 1
                            else:
                                cooling += 1
                else:
                    settling += 1
            # Commit per directory. sqlite3 opens a transaction on the first
            # write and holds the WAL write lock until commit - across a whole
            # library walk that is minutes, during which every other writer
            # (the worker finishing a job, a settings save) times out.
            conn.commit()
    # Not one hidden file anywhere means the dot convention is not what this
    # library uses, so the visibility filter is not protecting an in-flight
    # import - it is rejecting the entire library, silently, every scan.
    # Logged on the change of state and not per scan: the same line every
    # interval is what buries the events actually worth reading.
    stalled = visible_skipped if not hidden_found else 0
    if stalled and not VISIBLE_ONLY_SKIPPED:
        log.warning(
            "nothing is being queued: the watched folders hold %s that would be converted, and not one "
            "of them is hidden behind a leading dot. Only dot-hidden files are eligible while 'Only "
            "convert dot-hidden files' is on, so every scan is skipping the whole library. Two ways "
            "forward - turn that setting OFF (it makes every visible file in the watched folders "
            "eligible, so read what it says first), or have whatever imports your media write each file "
            "with a leading dot and let this worker reveal it once the encode verifies.",
            "1 video file" if stalled == 1 else f"{stalled} video files")
    elif VISIBLE_ONLY_SKIPPED and not stalled:
        # The other half of the state change, so whoever just flipped the
        # setting or fixed the imports gets an answer without waiting to see
        # whether a job eventually shows up.
        log.info("no longer skipping every candidate for being visible")
    VISIBLE_ONLY_SKIPPED = stalled
    conn.execute("DELETE FROM seen WHERE at < ?", (now - 7 * 86400,))
    # Nothing ever deleted a finished job row. A library-sized run leaves tens of
    # thousands of them in the one file the watcher, the worker and every HTTP
    # thread are already contending for.
    if c["keep_history_days"]:
        conn.execute(
            "DELETE FROM jobs WHERE state IN ('done','failed','cancelled') "
            "AND COALESCE(finished, created) < ?",
            (now - c["keep_history_days"] * 86400,),
        )
    # verify_session already refuses an expired row; this is what stops the table
    # keeping every one of them forever, and it is the only sweep that runs.
    store.purge_expired_sessions(conn)
    conn.commit()
    return {"queued": queued, "eligible": hidden_found, "settling": settling, "cooling": cooling,
            "already_queued": pending, "skipped_visible": visible_skipped,
            "missing_roots": missing, "at": now}


# One walk at a time. The interval's scan and an operator pressing the button
# would otherwise stat the whole library twice at once and fight over the WAL
# write lock the walk already holds per directory.
_scan_lock = threading.Lock()


def scan_now() -> dict:
    """A scan somebody asked for, rather than the interval's.

    Answers with what it found instead of only queueing it, because the
    question behind the button is "is there anything to do" - and "nothing"
    is an answer the queue alone cannot give: an empty queue looks identical
    whether the walk found nothing or never ran.
    """
    if not _scan_lock.acquire(blocking=False):
        # Refused rather than queued behind the other one: the caller is
        # waiting on an HTTP response, and a library walk is minutes.
        return {"scanned": False, "detail": "a scan was already running - its results land in the queue"}
    try:
        return {"scanned": True, **scan_once(force=True)}
    finally:
        _scan_lock.release()


def watch_loop() -> None:
    # Deliberately not gated by may_claim(). Queueing costs a row in SQLite, and
    # doing it while the window is shut means the queue is already built the
    # moment it opens instead of waiting a whole scan interval to notice. A
    # stopped box with a climbing queue is this working, not this broken.
    while True:
        try:
            with _scan_lock:
                scan_once()
            prune_trash()
        except Exception:  # noqa: BLE001
            log.exception("scan failed")
        time.sleep(max(15, cfg()["scan_interval_seconds"]))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

# Whether this process is terminating TLS itself. Only _scheme() reads it, to
# tell a proxied install apart from an unprotected one before it warns about a
# password in the clear.
SERVING_TLS = False


def tls_context() -> ssl.SSLContext | None:
    """The wrap for the listening socket, or None to serve plain HTTP as before.

    A certificate that is configured and unusable STOPS the container. The
    tempting alternative - log it and carry on over HTTP - means the password
    and the session token cross the network in clear text while the settings
    still say HTTPS is on, and nobody re-reads the startup log of a container
    that came up healthy. Refusing to start is the only version of this that
    cannot be missed.
    """
    c = cfg()
    cert, key = c["tls_cert"].strip(), c["tls_key"].strip()
    if not cert and not key:
        return None
    if not cert or not key:
        log.error("TLS needs both halves - tls_cert=%r tls_key=%r. Set both, or clear both to serve "
                  "plain HTTP.", cert, key)
        raise SystemExit(2)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # Pinned here rather than inherited: PROTOCOL_TLS_SERVER rules out SSLv2 and
    # SSLv3 by itself and nothing else, so TLS 1.0 is off only because Debian's
    # openssl.cnf happens to say MinProtocol=TLSv1.2. Rebase this image on a
    # distro with a looser system policy and the admin password would cross the
    # network under a protocol broken since 2011, with nothing here to say so.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.load_cert_chain(cert, key)
    except (OSError, ssl.SSLError, ValueError) as e:
        log.error("TLS is configured but %s and %s could not be loaded: %s. Fix the paths or the file "
                  "permissions, or clear both settings - this container will not quietly downgrade a "
                  "configured HTTPS back to HTTP.", cert, key, e)
        raise SystemExit(2)
    return ctx


def _run_state_view() -> dict:
    """The run state as the page, /healthz and any API client all read it.

    One builder for all three, because the window is the thing this feature can
    be silently wrong about: the container is UTC unless TZ says otherwise and
    the NAS under it is not, so the zone and the local clock travel with the
    window everywhere it is shown rather than only in the log.
    """
    allowed, why = may_claim()
    minutes, clock = local_clock()
    c = cfg()
    window = c["convert_window"].strip()
    # Nothing changes on its own while it is stopped: the window keeps turning,
    # but the gate does not follow it again until somebody presses Start.
    changes = core.next_window_change(minutes, core.parse_window(window)) if RUN_STATE == "running" else 0
    return {
        "run_state": RUN_STATE,
        "converting": allowed,
        "reason": why,
        "convert_window": window,
        # Empty means TZ is unset, which means this container is on UTC. The
        # abbreviation inside local_time is what makes that visible next to a
        # window somebody typed in their own zone.
        "timezone": os.environ.get("TZ", ""),
        "local_time": clock,
        "next_change_seconds": changes * 60 if changes else None,
        "auto_start": c["auto_start"],
        # The difference between "nothing to do yet" and "nothing will EVER be
        # queued here". Carried on the run state because that is what the page
        # already polls, so the empty queue can explain itself instead of
        # looking like a worker with nothing to do. Zero on a stack that hides
        # its imports, so it never appears on a healthy install.
        "visible_only_skipped": VISIBLE_ONLY_SKIPPED,
    }


# Prometheus' own exposition format version, which is what a scraper content
# negotiates on. Sending plain text/plain works by accident and stops working
# the day anything parses strictly.
METRICS_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _label(value: str) -> str:
    """A label value that cannot break the exposition format it sits in.

    VERSION comes from a build argument, so it is operator-supplied text landing
    inside quotes - one stray quote would make the whole scrape unparseable.
    """
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _metrics() -> str:
    """The queue in Prometheus text exposition format.

    Behind the bearer token like every other route. Prometheus reads a token
    natively (authorization: in a scrape config, one line), and the alternative
    is publishing the shape and the size of somebody's library to whoever finds
    the port.
    """
    conn = db()
    counts = dict(conn.execute("SELECT state, COUNT(*) FROM jobs GROUP BY state").fetchall())
    saved = conn.execute(
        "SELECT COALESCE(SUM(src_bytes - out_bytes), 0) FROM jobs WHERE state='done' "
        "AND src_bytes IS NOT NULL AND out_bytes IS NOT NULL"
    ).fetchone()[0]
    # The same throughput number the UI shows, from the same function - a second
    # estimate computed differently is two answers to one question.
    view = _queue_view(0)
    allowed, _why = may_claim()
    lines = [
        "# HELP transcodearr_build_info The running version and the encoder it chose.",
        "# TYPE transcodearr_build_info gauge",
        f'transcodearr_build_info{{version="{_label(VERSION)}",encoder="{_label(ENCODER)}"}} 1',
        "# HELP transcodearr_queue_depth Jobs waiting to be claimed.",
        "# TYPE transcodearr_queue_depth gauge",
        f"transcodearr_queue_depth {view['queued_total']}",
        "# HELP transcodearr_jobs Jobs by state, as far back as the history is kept.",
        "# TYPE transcodearr_jobs gauge",
    ]
    # Every state emitted even at zero. A series that only appears once it is
    # non-zero makes an alert written against it fire late, or never.
    lines += [f'transcodearr_jobs{{state="{s}"}} {counts.get(s, 0)}' for s in JOB_STATES]
    lines += [
        # A gauge and not a counter on purpose: keep_history_days prunes finished
        # jobs, so this total goes DOWN, and a counter that goes down is read as
        # a process restart and counted again from zero.
        "# HELP transcodearr_saved_bytes Bytes reclaimed by the finished jobs still inside the history window.",
        "# TYPE transcodearr_saved_bytes gauge",
        f"transcodearr_saved_bytes {int(saved)}",
        "# HELP transcodearr_encode_seconds Mean wall-clock seconds per transcode, over the last 20.",
        "# TYPE transcodearr_encode_seconds gauge",
        f"transcodearr_encode_seconds {view['seconds_per_job'] or 0}",
        "# HELP transcodearr_converting 1 when a worker may claim new work right now.",
        "# TYPE transcodearr_converting gauge",
        f"transcodearr_converting {1 if allowed else 0}",
        "# HELP transcodearr_run_state The Start/Stop switch, 1 on the state in effect.",
        "# TYPE transcodearr_run_state gauge",
        f'transcodearr_run_state{{state="running"}} {1 if RUN_STATE == "running" else 0}',
        f'transcodearr_run_state{{state="paused"}} {0 if RUN_STATE == "running" else 1}',
        "# HELP transcodearr_uptime_seconds Seconds since this process started.",
        "# TYPE transcodearr_uptime_seconds gauge",
        f"transcodearr_uptime_seconds {int(time.time() - STARTED)}",
    ]
    return "\n".join(lines) + "\n"


def _queue_view(limit: int) -> dict:
    """What is converting now, and what is next - in the order it will happen.

    The order is not a display choice: worker_loop takes
    `WHERE state='queued' ORDER BY priority, created LIMIT 1`, so reveals first
    and then oldest-first IS the running order, and the query below has to stay
    the same one. Listing newest-first (which the job history does, correctly,
    as history) would show the queue backwards.
    """
    conn = db()
    running = [job_dict(r) for r in
               conn.execute("SELECT * FROM jobs WHERE state='running' ORDER BY started").fetchall()]
    queued = [job_dict(r) for r in
              conn.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY priority, created LIMIT ?",
                           (limit,)).fetchall()]
    total = conn.execute("SELECT COUNT(*) FROM jobs WHERE state='queued'").fetchone()[0]
    transcodes = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE state='queued' AND kind='transcode'").fetchone()[0]

    # Throughput measured from real completions, transcodes only: a reveal is a
    # rename that finishes in milliseconds, and averaging those in would promise
    # that a queue drains in minutes when it actually takes days.
    rows = conn.execute(
        "SELECT started, finished FROM jobs WHERE state='done' AND kind='transcode' "
        "AND started IS NOT NULL AND finished IS NOT NULL ORDER BY finished DESC LIMIT 20"
    ).fetchall()
    spans = [r["finished"] - r["started"] for r in rows
             if r["started"] is not None and r["finished"] is not None and r["finished"] > r["started"]]
    per_job = sum(spans) / len(spans) if spans else None
    return {
        "running": running,
        "queued": queued,
        "queued_total": total,
        "seconds_per_job": round(per_job) if per_job else None,
        # Reveals are excluded from the estimate AND from what it has to cover:
        # they are renames, and they run first anyway.
        "eta_seconds": round(per_job * transcodes / max(1, cfg()["max_concurrent"])) if per_job else None,
        "sampled": len(spans),
        "max_concurrent": cfg()["max_concurrent"],
    }


def _query(path: str) -> dict[str, str]:
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(path).query).items()}


# Alias spellings already logged, so a client stuck on the old paths is named
# once rather than every poll for the life of the container.
_warned_deprecated: set[str] = set()

# The routes that answer to two spellings. Everything else is matched literally,
# so stripping the prefix here must not invent a bare /settings or /tokens that
# was never a route and would then have to be supported forever.
ALIASED = ("/jobs", "/queue")


def _route(raw: str) -> tuple[str, bool]:
    """(path without its query string, whether it arrived under /api).

    Routing used to be `startswith`, which is why GET /queuegarbage answered 200
    with the real queue and a future /jobs/stats would have been swallowed whole
    by the list handler. Matching is exact from here on; the query string is
    split off first because /queue?limit=200 is the same route as /queue.

    /api/jobs and /jobs reach one handler, not two: the bare spellings are what
    the live deployment and pre-0.9 clients call. The flag is the only
    difference between them - a single job comes back as {"job": ...} under
    /api and bare under the alias - so the two cannot drift apart before the
    aliases go at 2.0.
    """
    path = urllib.parse.urlparse(raw).path
    for alias in ALIASED:
        if path == "/api" + alias or path.startswith("/api" + alias + "/"):
            return path[len("/api"):], True
    return path, False


def _deprecated(method: str, route: str, api: bool) -> None:
    """Name a caller still on the pre-0.9 paths, once, so 2.0 can drop them.

    Removing a route nothing calls is housekeeping; removing one the live box
    still uses is an outage. Something has to write down which it is, and the
    only honest source is the requests actually arriving.
    """
    if api or not (route in ALIASED or route.startswith("/jobs/")):
        return
    key = f"{method} /{route.split('/')[1]}"
    if key not in _warned_deprecated:
        _warned_deprecated.add(key)
        log.info("%s %s is the pre-0.9 path and is removed at 2.0 - the same call is /api%s",
                 method, route, route)


def _jobs_page(q: dict[str, str]) -> tuple[int, dict]:
    """The job history, newest first, one page at a time.

    Paged by `before` rather than an offset: rows are pruned by
    keep_history_days and new ones arrive constantly, so page 2 of an offset
    walk is taken against a different list than page 1 and quietly repeats or
    skips whatever moved across the boundary. The cursor is a value already in
    the data - pass the last job's `created` back as ?before= for the next page.
    """
    state = q.get("state")
    if state is not None and state not in JOB_STATES:
        # An unknown filter used to fall through to `WHERE state=?` and match
        # nothing, so a typo or a renamed state reads exactly like an empty
        # history - the same silence this worker exists to remove.
        return 400, {"error": f"state must be one of {', '.join(JOB_STATES)}"}
    try:
        limit = min(max(int(q.get("limit", "50")), 1), 200)
    except ValueError:
        limit = 50
    where, args = [], []
    if state:
        where.append("state=?")
        args.append(state)
    total = db().execute(
        f"SELECT COUNT(*) FROM jobs{' WHERE ' + ' AND '.join(where) if where else ''}", args
    ).fetchone()[0]
    if "before" in q:
        try:
            args.append(float(q["before"]))
        except ValueError:
            return 400, {"error": "before must be a job's created timestamp"}
        # Strictly less-than, so two rows sharing a created timestamp to the
        # microsecond would straddle the boundary and one would be skipped.
        # time.time() does not repeat between two inserts in practice, and the
        # cost of being wrong is a missing history row, not a missing file.
        where.append("created < ?")
    rows = db().execute(
        f"SELECT * FROM jobs{' WHERE ' + ' AND '.join(where) if where else ''} "
        "ORDER BY created DESC LIMIT ?", (*args, limit),
    ).fetchall()
    return 200, {"jobs": [job_dict(r) for r in rows], "total": total}


def _resolve_job_path(raw: str) -> tuple[bool, str]:
    """The container's path for a file a client named in somebody else's terms.

    Radarr calls it /movies/Film/file.mkv, this container mounts the same
    directory at /media/Movies/Film/file.mkv, and a consumer forwarding an arr
    event only ever has the first one. Every enabled connection already stores
    that pair for notify_arrs, so it is used in both directions rather than
    making callers guess or making the operator configure the mapping twice.

    Every candidate goes back through core.validate_path. That is the
    containment guard - it is what stops this route being a way to run ffmpeg
    on /etc - and a translated path is no more trusted than the one that
    arrived, especially since the prefix it was built from is user-editable.
    """
    ok, resolved = core.validate_path(raw, MEDIA_ROOTS)
    if ok:
        return True, resolved
    for row in store.list_arrs(db()):
        if not row["enabled"]:
            continue
        # to_arr_path is a prefix swap. Handed the same pair the other way
        # round it translates arr -> worker, so there is one mapping function
        # rather than two that can disagree about a trailing slash.
        candidate = arr_client.to_arr_path(raw.strip(), row["arr_path"], row["worker_path"])
        if candidate:
            ok, translated = core.validate_path(candidate, MEDIA_ROOTS)
            if ok:
                log.info("%s resolved to %s via the %s mapping", raw, translated, row["name"])
                return True, translated
    # The original complaint, about the path they actually sent. Reporting the
    # last failed translation instead would name a path the caller never wrote.
    return False, resolved


def _browse(raw: str) -> tuple[int, dict]:
    """Directories the folder picker may show.

    Containment is checked on the RESOLVED path, not the requested one, so a
    symlink or a ../ cannot walk the picker out of the mounts and turn a
    convenience into a way to read the host filesystem over the network.
    """
    if not raw or raw == "/":
        return 200, {"path": "/", "entries": [{"name": p, "path": p} for p in MEDIA_ROOTS]}
    resolved = os.path.realpath(raw)
    if not core.is_within(resolved, MEDIA_ROOTS):
        return 400, {"error": "that path is outside the media roots"}
    if not os.path.isdir(resolved):
        return 404, {"error": "not a directory"}
    try:
        entries = sorted(
            ({"name": e.name, "path": os.path.join(resolved, e.name)}
             for e in os.scandir(resolved) if e.is_dir() and not e.name.startswith(".")),
            key=lambda e: e["name"].lower(),
        )
    except OSError as e:
        return 400, {"error": str(e)}
    # A library root with thousands of series would otherwise ship the whole
    # list to the browser on every click.
    return 200, {"path": resolved, "entries": entries[:1000]}


# The largest body worth reading. A config backup is the big one and those are
# kilobytes. The cap exists because POST /api/login is deliberately open: without
# it anyone who can reach the port can make this process allocate whatever
# Content-Length they feel like claiming.
MAX_BODY = 1 << 20


class Handler(BaseHTTPRequestHandler):
    server_version = f"TranscodeArr/{VERSION}"
    # Without this a client that connects and then says nothing parks a thread
    # in readline() forever, holding a socket and a file descriptor, with no
    # authentication needed to do it.
    timeout = 30

    def _send(self, code: int, payload: dict | str, content_type: str = "application/json",
              headers: dict | None = None) -> None:
        body = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Nothing here was cacheable and nothing said so. The UI is one 80KB
        # file that changes every release and carried no Cache-Control, no
        # ETag and no Last-Modified, so a browser had nothing to validate
        # against and served the previous version after an update - a new tab
        # shipped, deployed and verified, and simply absent on screen until
        # somebody thought to hard-reload. The API bodies are worse: they are a
        # live view of a queue, where a cached answer is a lie with a timestamp
        # on it. Overridable, because the caller that sets its own is the one
        # that means it.
        if "Cache-Control" not in (headers or {}):
            self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        return store.verify_token(db(), header[len("Bearer "):].strip(), TOKEN)

    def _bearer(self) -> str:
        return self.headers.get("Authorization", "")[len("Bearer "):].strip()

    def _scheme(self) -> str:
        """http or https as the CLIENT saw it, a TLS proxy included.

        X-Forwarded-Proto is the only way this process can know a proxy already
        terminated TLS on its behalf. Without honoring it the clear-text warning
        below fires on every correctly proxied install, and a warning that is
        wrong most of the time is one everybody learns to scroll past.
        """
        forwarded = self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
        if forwarded in ("http", "https"):
            return forwarded
        return "https" if SERVING_TLS else "http"

    def log_message(self, *_args) -> None:  # quiet access log
        pass

    def do_GET(self) -> None:  # noqa: N802
        route, api = _route(self.path)
        if route in ("/", "/index.html"):
            return self._send(200, web.PAGE, "text/html; charset=utf-8")
        # Public on purpose, with the page and /healthz, and it has to be above
        # the bearer check to stay that way: a browser fetches a favicon with no
        # Authorization header at all, so behind the check this route would
        # answer 401 forever and the tab would simply never get an icon. What it
        # returns is the project's own logo, which says nothing about this box.
        if route == "/favicon.svg":
            return self._send(200, web.FAVICON, "image/svg+xml")
        if route == "/healthz":
            conn = db()
            counts = dict(conn.execute("SELECT state, COUNT(*) FROM jobs GROUP BY state").fetchall())
            state = _run_state_view()
            has_admin = store.admin_exists(conn)
            body = {
                "ok": True, "version": VERSION, "encoder": ENCODER, "encoder_reason": ENCODER_REASON,
                "queued": counts.get("queued", 0), "running": counts.get("running", 0),
                "uptime_seconds": int(time.time() - STARTED),
                # Whether work is moving is operational status, the same kind of
                # thing as the counts above. WHY it is not moving names the
                # window, which is configuration, so it waits for the token.
                "run_state": state["run_state"], "converting": state["converting"],
                # store.verify_token refuses the placeholder, so counting it as
                # configured told an operator their container was protected
                # while every request carrying that token was being rejected.
                "auth_configured": (bool(TOKEN) and TOKEN != store.PLACEHOLDER_TOKEN)
                or bool(store.list_tokens(conn)) or has_admin,
                # Whether to render the login form. It says only "this box has a
                # password", which one POST to /api/login would establish anyway.
                "admin_configured": has_admin,
            }
            if self._authed():
                # Which volumes are mounted, which of them are watched and
                # whether unhidden files are fair game is a map of somebody's
                # filesystem, and this is the one route with no key on it. The
                # UI's status line needs the encoder and the counts; it does
                # not need the mounts, and an unauthenticated scanner needs
                # neither.
                c = cfg()
                body.update(media_roots=MEDIA_ROOTS, watch_roots=c["watch_roots"] or MEDIA_ROOTS,
                            hidden_only=c["hidden_only"])
                body.update(state)
            return self._send(200, body)
        if not self._authed():
            return self._send(401, {"error": "missing or wrong bearer token"})
        _deprecated("GET", route, api)
        if route == "/api/settings":
            rows, env, roots = store.read_settings(db()), dict(os.environ), MEDIA_ROOTS
            return self._send(200, {
                "specs": [
                    {"key": s.key, "kind": s.kind, "label": s.label, "help": s.help, "group": s.group,
                     # So the page renders a password field and sends the mask
                     # back untouched instead of a readable secret in a text box.
                     "secret": s.secret, "env": s.env}
                    for s in store.SPECS if not s.hidden
                ],
                # The one gate between effective() and a response body. Without
                # it the webhook signing secret ships to every browser that opens
                # the settings tab, and to anything holding any key.
                "values": store.redact_secrets(store.effective(rows, env, roots)),
                "sources": store.sources(rows, env, roots),
                "media_roots": MEDIA_ROOTS,
            })
        if route == "/api/control":
            return self._send(200, _run_state_view())
        if route == "/api/sessions":
            # The admin's username rides along because this is the list of that
            # account's logins - the page has nowhere else to read who it is.
            return self._send(200, {"sessions": store.list_sessions(db()),
                                    "admin": store.admin_username(db())})
        if route == "/api/backup":
            # Named so a folder of these sorts by date and says what it is.
            filename = time.strftime("transcodearr-config-%Y%m%d-%H%M%S.json")
            return self._send(200, store.export_config(db(), VERSION),
                              headers={"Content-Disposition": f'attachment; filename="{filename}"'})
        if route == "/metrics":
            return self._send(200, _metrics(), METRICS_TYPE)
        if route == "/api/encoders":
            c = cfg()
            opts = core.ENCODER_OPTIONS.get(ENCODER, {})
            return self._send(200, {
                "gpu": GPU,
                "in_use": ENCODER,
                "why": ENCODER_REASON,
                "quality": c["quality"],
                "recommended_for_current": core.recommended_quality(ENCODER),
                "encoders": ENCODER_PROBES,
                "presets": [{"value": v, "label": l} for v, l in opts.get("presets", [])],
                "profiles": [{"value": v, "label": l} for v, l in opts.get("profiles", [])],
                "preset": c["encoder_preset"] or opts.get("default_preset", ""),
                "profile": c["encoder_profile"] or opts.get("default_profile", ""),
                "default_preset": opts.get("default_preset", ""),
                "default_profile": opts.get("default_profile", ""),
                "resolutions": [{"value": v, "label": l, "help": h} for v, l, h in core.RESOLUTIONS],
                "max_height": c["max_height"],
                "sane_range": core.ENCODER_INFO.get(ENCODER, {}).get("sane", [18, 30]),
            })
        if route == "/api/system":
            snap = system.snapshot()
            with _jobs_lock:
                snap["converting"] = len(_running)
            snap["max_concurrent"] = cfg()["max_concurrent"]
            return self._send(200, snap)
        if route == "/api/profiles":
            available = [p["name"] for p in ENCODER_PROBES if p["available"]]
            return self._send(200, {
                "profiles": store.list_profiles(db()),
                "available_encoders": [
                    {"name": p["name"], "codec": p["codec"], "hardware": p["hardware"],
                     "recommended_quality": p["recommended_quality"], "sane_range": p["sane_range"],
                     "summary": p["summary"],
                     "presets": [{"value": v, "label": l} for v, l in core.ENCODER_OPTIONS.get(p["name"], {}).get("presets", [])],
                     "profiles": [{"value": v, "label": l} for v, l in core.ENCODER_OPTIONS.get(p["name"], {}).get("profiles", [])],
                     "default_preset": core.ENCODER_OPTIONS.get(p["name"], {}).get("default_preset", ""),
                     "default_profile": core.ENCODER_OPTIONS.get(p["name"], {}).get("default_profile", "")}
                    for p in ENCODER_PROBES if p["name"] in available
                ],
                "resolutions": [{"value": v, "label": l, "help": h} for v, l, h in core.RESOLUTIONS],
                "audio_codecs": [{"value": v, "label": l} for v, l in store.AUDIO_CODECS],
                "audio_channels": [{"value": v, "label": l} for v, l in store.AUDIO_CHANNELS],
            })
        if route == "/api/trash":
            q = _query(self.path)
            # Clamped rather than refused: a pager is a URL people edit, and a
            # 400 for limit=1000 helps nobody. The ceiling is the batch cap, so
            # a page can always be acted on in one request.
            try:
                limit = min(max(int(q.get("limit", "100")), 1), MAX_TRASH_BATCH)
            except ValueError:
                limit = 100
            try:
                offset = max(int(q.get("offset", "0")), 0)
            except ValueError:
                offset = 0
            return self._send(200, list_trash(limit, offset))
        if route == "/api/tokens":
            return self._send(200, {"tokens": store.list_tokens(db())})
        if route == "/api/arrs":
            return self._send(200, {"arrs": store.list_arrs(db())})
        if route == "/api/fs":
            return self._send(*_browse(_query(self.path).get("path", "")))
        if route == "/queue":
            try:
                limit = min(max(int(_query(self.path).get("limit", "100")), 1), 500)
            except ValueError:
                limit = 100
            return self._send(200, _queue_view(limit))
        m = re.fullmatch(r"/jobs/([0-9a-f-]{36})", route)
        if m:
            row = db().execute("SELECT * FROM jobs WHERE id=?", (m.group(1),)).fetchone()
            if not row:
                return self._send(404, {"error": "no such job"})
            # The one route that ships log_tail: somebody asking for this
            # specific job is debugging it, and the ffmpeg argv is the answer.
            # A list of sixty of them is a bulk export of container paths to
            # anyone holding any key.
            job = job_dict(row, log_tail=True)
            return self._send(200, {"job": job} if api else job)
        if route == "/jobs":
            return self._send(*_jobs_page(_query(self.path)))
        self._send(404, {"error": "not found"})

    def _body(self) -> dict:
        # A malformed Content-Length raises ValueError, which the callers'
        # `except json.JSONDecodeError` does not catch - the connection closed
        # with no response at all rather than a 400.
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            raise json.JSONDecodeError("bad Content-Length", "", 0)
        if length > MAX_BODY:
            # Refused as unreadable rather than read: nothing this API accepts is
            # a megabyte, and the login route takes bodies from callers who have
            # not proved anything yet.
            raise json.JSONDecodeError("body too large", "", 0)
        parsed = json.loads(self.rfile.read(max(0, length)) or b"{}")
        if not isinstance(parsed, dict):
            # Every route here reads its body with .get, so a bare list or a
            # bare string reached them as an AttributeError - a traceback and a
            # closed socket instead of a response, and POST /api/login takes its
            # body from callers who have not authenticated yet.
            raise json.JSONDecodeError("body must be a JSON object", "", 0)
        return parsed

    def _write_profile(self, route: str, body: dict) -> None:
        """Create, update or dry-run a profile. Shared by POST and PUT.

        PUT /api/profiles/{id} is the spelling that matches PUT /api/arrs/{id};
        POST on the same path is what the bundled UI already sends and stays.
        One body for both, because two would eventually validate differently
        and only one of them would be the one that runs.
        """
        m = re.fullmatch(r"/api/profiles/([0-9a-f-]{36})", route)
        # Before the test encode, not after: a nonexistent id is a 404 whatever
        # the body says, and finding that out used to cost two seconds of
        # ffmpeg first.
        if m and not store.get_profile(db(), m.group(1)):
            return self._send(404, {"error": "no such profile"})
        available = [p["name"] for p in ENCODER_PROBES if p["available"]]
        try:
            fields = store.clean_profile(body, available)
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        ok, note = validate_profile(fields)
        if route == "/api/profiles/test":
            return self._send(200, {"ok": ok, "detail": note, "command": " ".join(
                profile_args(fields, "<input>", "<output>", with_subs=True))})
        if not ok:
            # Never store a configuration that has been shown not to work -
            # the alternative is discovering it on the first real film.
            return self._send(400, {"error": f"That profile does not work on this machine: {note}"})
        try:
            row = store.save_profile(db(), fields, m.group(1) if m else None, note)
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        log.info("profile saved: %s (%s)", row["name"], note)
        self._send(200 if m else 201, {"profile": row, "detail": note})

    def do_PUT(self) -> None:  # noqa: N802
        route, _api = _route(self.path)
        if not self._authed():
            return self._send(401, {"error": "missing or wrong bearer token"})
        try:
            body = self._body()
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON"})
        if route == "/api/settings":
            try:
                written = store.save_settings(db(), body, MEDIA_ROOTS)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            log.info("settings changed: %s", ", ".join(written))
            return self._send(200, {"saved": written})
        if re.fullmatch(r"/api/profiles/([0-9a-f-]{36})", route):
            return self._write_profile(route, body)
        m = re.fullmatch(r"/api/arrs/([0-9a-f-]{36})", route)
        if m:
            # Asked here rather than read out of save_arr's ValueError, which
            # is one string among several validation messages: "no such
            # connection" is a missing resource and answering 400 made a
            # deleted id indistinguishable from a malformed body.
            if not store.get_arr(db(), m.group(1)):
                return self._send(404, {"error": "no such connection"})
            try:
                row = store.save_arr(db(), body, m.group(1))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"arr": {**row, "api_key": "********"}})
        self._send(404, {"error": "not found"})

    def _login(self) -> None:
        """A password in, a session token out. The only POST with no token on it.

        No cookie, deliberately. The token goes back in the body and the page
        holds it exactly as it already holds an API key. A cookie is attached by
        the browser to every cross-site request aimed at this port, which is the
        CSRF surface this service does not have today and is not acquiring for
        the sake of a login form.
        """
        try:
            body = self._body()
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON"})
        conn = db()
        if not store.admin_exists(conn):
            # 409 and not 401: the credentials are not wrong, there is nothing
            # yet for them to be right about, and a page that cannot tell those
            # apart tells a first-time operator their password is bad.
            return self._send(409, {"error": "no admin account exists yet - create one with POST /api/admin "
                                             "using the bootstrap token, then log in here"})
        ok, wait = store.attempt_login(conn, str(body.get("username", "")), str(body.get("password", "")))
        if not ok:
            # The username is deliberately NOT logged, here or anywhere: people
            # type their password into the username box, and a log file is the
            # one place a password must never turn up.
            log.warning("failed login from %s", self.client_address[0])
            if wait:
                seconds = int(wait) + 1
                return self._send(429, {"error": f"too many failed logins - try again in {seconds} seconds"},
                                  headers={"Retry-After": str(seconds)})
            # One message for a wrong name and for a wrong password. Two would
            # make this form an oracle for which accounts exist.
            return self._send(401, {"error": "wrong username or password"})
        raw, row = store.create_session(conn, cfg()["session_days"])
        if self._scheme() != "https":
            log.warning("that password just crossed the network in clear text - this server is plain HTTP and "
                        "nothing upstream sent X-Forwarded-Proto: https. Set tls_cert and tls_key, or put a "
                        "TLS-terminating proxy in front of it")
        log.info("login accepted, session %s", row["prefix"])
        self._send(200, {"token": raw, "expires": row["expires"], "username": store.admin_username(conn)})

    def _set_admin(self, body: dict) -> None:
        """Create the admin account, or change its username or password.

        Reached with a bearer token like every other write, which is what makes
        the FIRST admin creatable at all: a fresh container has the bootstrap
        token and no password. Once one exists, changing it costs the current
        password as well - a leaked API key must not be a way to lock the owner
        out of his own box, and any integration holds one of those keys.
        """
        conn = db()
        existed = store.admin_exists(conn)
        if existed:
            # attempt_login and not verify_password, so this door has the same
            # backoff the login form does rather than being the unthrottled way
            # in around it. Checked against the STORED username, because the body
            # may be asking to rename the account.
            ok, wait = store.attempt_login(conn, store.admin_username(conn),
                                           str(body.get("current_password", "")))
            if not ok:
                if wait:
                    seconds = int(wait) + 1
                    return self._send(429, {"error": f"too many wrong passwords - try again in {seconds} seconds"},
                                      headers={"Retry-After": str(seconds)})
                return self._send(403, {"error": "current_password is wrong"})
        try:
            name = store.set_admin(conn, str(body.get("username", "")), str(body.get("password", "")))
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        log.info("admin account %s: %s", "changed" if existed else "created", name)
        self._send(200 if existed else 201, {"username": name})

    def _self_signed(self, body: dict) -> None:
        """Generate a certificate and key with the openssl already in the image.

        For a LAN box that wants TLS and has no certificate authority. Written
        into the config volume so the pair outlives the container, and never over
        an existing one: this is the obvious place to have put a real
        certificate too, and clobbering somebody's private key to save them a
        click is not a trade this codebase makes.
        """
        host = str(body.get("host", "")).strip() or "localhost"
        if not re.fullmatch(r"[A-Za-z0-9.:-]{1,253}", host):
            return self._send(400, {"error": "host must be a hostname or an IP address"})
        try:
            days = min(max(int(body.get("days", 3650)), 1), 7300)
        except (TypeError, ValueError):
            return self._send(400, {"error": "days must be a whole number"})
        directory = os.path.join(CONFIG_DIR, "tls")
        cert, key = os.path.join(directory, "cert.pem"), os.path.join(directory, "key.pem")
        if os.path.exists(cert) or os.path.exists(key):
            return self._send(409, {"error": f"{cert} or its key already exists - delete both first if you "
                                             "really do mean to replace them"})
        os.makedirs(directory, exist_ok=True)
        # A subjectAltName, not just a CN: browsers have refused CN-only
        # certificates for years, so the one-argument version of this generates a
        # certificate that nothing will load.
        alt = ("IP:" if re.fullmatch(r"[0-9.]+", host) or ":" in host else "DNS:") + host
        try:
            r = subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                 "-days", str(days), "-subj", "/CN=" + host, "-addext", "subjectAltName=" + alt],
                capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            return self._send(500, {"error": f"could not run openssl: {e}"})
        if r.returncode != 0:
            tail = (r.stderr or "").strip().splitlines()
            return self._send(500, {"error": "openssl failed: " + (tail[-1][:200] if tail else "no output")})
        # A private key in a volume the operator may well have shared.
        try:
            os.chmod(key, 0o600)
        except OSError:
            pass
        log.info("generated a self-signed certificate for %s at %s", host, cert)
        self._send(201, {"cert": cert, "key": key, "host": host, "days": days,
                         "detail": "Save these two paths in the TLS settings and restart the container to "
                                   "serve HTTPS. Browsers warn once about a self-signed certificate."})

    def do_POST(self) -> None:  # noqa: N802
        route, api = _route(self.path)
        # Before the auth check and before the body is read, because this is how
        # somebody with no token gets one. Everything below this line still needs
        # a bearer token, so exactly one POST is open and it is this one.
        if route == "/api/login":
            return self._login()
        if not self._authed():
            return self._send(401, {"error": "missing or wrong bearer token"})
        try:
            body = self._body()
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON"})
        _deprecated("POST", route, api)

        if route == "/api/logout":
            row = db().execute("SELECT id FROM sessions WHERE hash=?",
                               (store.hash_token(self._bearer()),)).fetchone()
            if row is None:
                # An API key is not a login, and revoking one from a button
                # labeled "log out" would cut off whatever holds that key.
                return self._send(400, {"error": "that bearer token is an API key, not a browser session"})
            store.revoke_session(db(), row["id"])
            return self._send(200, {"revoked": row["id"]})
        if route == "/api/admin":
            return self._set_admin(body)
        if route in ("/api/control/start", "/api/control/stop"):
            # Never touches an encode already running - see may_claim. Stopping
            # DRAINS: the in-flight job finishes, verifies and reveals.
            set_run_state(route.endswith("start"))
            return self._send(200, _run_state_view())
        if route == "/api/restore":
            # The body IS the backup document, so restoring is `curl -d @file`
            # and the browser can post the file it just read, unwrapped.
            try:
                changed = store.import_config(db(), body, VERSION, MEDIA_ROOTS)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            # Logged as well as returned: this rewrites what every future job
            # does, and the operator's own record of when is the log.
            log.info("config restored: %s", "; ".join(changed) or "nothing changed")
            return self._send(200, {"changed": changed})
        if route == "/api/tls/selfsigned":
            return self._self_signed(body)

        if route in ("/api/trash/restore", "/api/trash/delete"):
            paths = body.get("paths")
            if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
                return self._send(400, {"error": "paths must be a non-empty list of strings"})
            if len(paths) > MAX_TRASH_BATCH:
                # A bulk action over media, so the cap is an explicit refusal
                # rather than a silent truncation that reports success for
                # files it never touched.
                return self._send(400, {"error": f"at most {MAX_TRASH_BATCH} paths per request"})
            if route.endswith("delete"):
                results = delete_from_trash(paths)
            else:
                results = restore_from_trash(paths, replace=bool(body.get("replace")))
            done = sum(1 for r in results if r["ok"])
            log.info("trash %s: %s of %s", route.rsplit("/", 1)[1], done, len(results))
            return self._send(200, {"results": results, "ok": done, "failed": len(results) - done})

        if route == "/api/scan":
            # Walk the watched folders now instead of at the next interval, and
            # say what was found. Someone who has just fixed a mount, flipped
            # the dot-hidden rule or dropped files in wants an answer in
            # seconds, and waiting out a five-minute interval to learn that the
            # setting was wrong is the silence this worker exists to remove.
            result = scan_now()
            if result.get("scanned"):
                log.info("scan on request: queued %s of %s eligible (%s settling, %s skipped for being visible)",
                         result["queued"], result["eligible"], result["settling"], result["skipped_visible"])
            return self._send(200, result)

        if route == "/api/encoders/probe":
            # Re-probe on demand: hardware changes under a container more often
            # than the container restarts - a driver reload, a GPU freed by
            # another process, the memory-fragmentation fix in the README.
            global ENCODER, ENCODER_REASON, ENCODER_PROBES, GPU  # noqa: PLW0603
            GPU = gpu_name()
            ENCODER, ENCODER_REASON, ENCODER_PROBES = choose_encoder()
            log.info("re-probed encoders: %s (%s)", ENCODER, ENCODER_REASON)
            return self._send(200, {"gpu": GPU, "in_use": ENCODER, "why": ENCODER_REASON,
                                    "encoders": ENCODER_PROBES})
        if route == "/api/profiles/retest":
            # Re-test every stored profile, shipped and custom alike. Hardware
            # changes under a container more often than the container restarts,
            # so the answer from boot is not permanent.
            results = test_stored_profiles(store.list_profiles(db()))
            log.info("re-tested %d profiles: %d usable", len(results),
                     sum(1 for r in results if r["usable"]))
            return self._send(200, {"profiles": store.list_profiles(db())})
        m = re.fullmatch(r"/api/profiles/([0-9a-f-]{36})/test", route)
        if m:
            row = store.get_profile(db(), m.group(1))
            if not row:
                return self._send(404, {"error": "no such profile"})
            [tested] = test_stored_profiles([row])
            return self._send(200, {"profile": tested, "ok": tested["usable"],
                                    "detail": tested["validated_note"]})
        if route in ("/api/profiles", "/api/profiles/test") or re.fullmatch(r"/api/profiles/([0-9a-f-]{36})", route):
            return self._write_profile(route, body)
        m = re.fullmatch(r"/api/profiles/([0-9a-f-]{36})/activate", route)
        if m:
            row, why = store.activate_profile(db(), m.group(1))
            if not row:
                # 404 for a missing id, 409 for one that exists but this machine
                # cannot run - a client retrying the second forever would never
                # succeed, and it needs to be able to tell them apart.
                return self._send(404 if why == "no such profile" else 409, {"error": why})
            log.info("active profile: %s", row["name"])
            return self._send(200, {"profile": row})
        if route == "/api/tokens":
            raw, row = store.mint_token(db(), str(body.get("name", "")))
            log.info("api key minted: %s", row["name"])
            # The only time the raw key exists outside the caller's hands.
            return self._send(201, {"token": raw, **row})
        if route == "/api/arrs":
            try:
                row = store.save_arr(db(), body)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(201, {"arr": {**row, "api_key": "********"}})
        if route == "/api/arrs/test":
            key = str(body.get("api_key", "")).strip()
            base = str(body.get("base_url", "")).strip().rstrip("/")
            if not key and body.get("id"):
                # An edit form never receives the stored key, so a blank key
                # means "test the one you already have" - and then the URL must
                # come from that same stored row too. Pairing a stored secret
                # with a caller-supplied address is a way to make this service
                # post your Radarr key to any host on request.
                existing = store.get_arr(db(), str(body["id"]))
                if not existing:
                    return self._send(404, {"error": "no such connection"})
                key, base = existing["api_key"], existing["base_url"]
            if not base or not key:
                return self._send(400, {"error": "base_url and api_key are required to test"})
            if not re.match(r"^https?://", base):
                return self._send(400, {"error": "base_url must start with http:// or https://"})
            ok, detail = arr_client.test(base, key)
            return self._send(200, {"ok": ok, "detail": detail})

        if route != "/jobs":
            return self._send(404, {"error": "not found"})
        ok, resolved = _resolve_job_path(str(body.get("path", "")))
        if not ok:
            return self._send(400, {"error": resolved})
        names = core.plan_names(resolved)
        protected = core.matches_skip(os.path.basename(resolved), cfg()["skip_patterns"])
        # force: somebody asked for this file by name. The retry cooldown is
        # there to stop the watcher looping on a file that always fails, not to
        # tell a caller to come back in six hours.
        job = enqueue(resolved, "reveal" if names.reveal_only or protected else "transcode", force=True)
        if job is None:
            # enqueue found the duplicate and threw it away. Handing back the id
            # makes "make sure this is queued, then watch it" two calls; without
            # it a caller has to list the queue and match on path to find the
            # job it was just told about.
            existing = db().execute(
                "SELECT * FROM jobs WHERE path=? AND state IN ('queued','running')", (resolved,)
            ).fetchone()
            return self._send(409, {"error": "already queued or running for this path",
                                    "job": job_dict(existing) if existing else None})
        self._send(201, {"job": job} if api else job)

    def do_DELETE(self) -> None:  # noqa: N802
        route, api = _route(self.path)
        if not self._authed():
            return self._send(401, {"error": "missing or wrong bearer token"})
        _deprecated("DELETE", route, api)
        m = re.fullmatch(r"/api/tokens/([0-9a-f-]{36})", route)
        if m:
            return self._send(*((200, {"revoked": m.group(1)}) if store.revoke_token(db(), m.group(1))
                                else (404, {"error": "no such key"})))
        m = re.fullmatch(r"/api/sessions/([0-9a-f-]{36})", route)
        if m:
            # Revoking the session you are holding is a logout, which is fine -
            # the next request from that browser is a 401 and the login form.
            return self._send(*((200, {"revoked": m.group(1)}) if store.revoke_session(db(), m.group(1))
                                else (404, {"error": "no such session"})))
        m = re.fullmatch(r"/api/profiles/([0-9a-f-]{36})", route)
        if m:
            if not store.get_profile(db(), m.group(1)):
                return self._send(404, {"error": "no such profile"})
            ok, why = store.delete_profile(db(), m.group(1))
            # Both refusals are conflicts with the current state rather than bad
            # requests: the active profile becomes deletable the moment another
            # is activated, and a shipped profile is never deletable at all.
            # That is what 409 means and 400 does not.
            return self._send(*((200, {"deleted": m.group(1)}) if ok else (409, {"error": why})))
        m = re.fullmatch(r"/api/arrs/([0-9a-f-]{36})", route)
        if m:
            return self._send(*((200, {"deleted": m.group(1)}) if store.delete_arr(db(), m.group(1))
                                else (404, {"error": "no such connection"})))
        m = re.fullmatch(r"/jobs/([0-9a-f-]{36})", route)
        if not m:
            return self._send(404, {"error": "not found"})
        conn = db()
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (m.group(1),)).fetchone()
        if not row:
            return self._send(404, {"error": "no such job"})
        if row["state"] == "queued":
            # Conditional: the worker may have claimed this row since the read,
            # and stamping 'cancelled' over 'running' would report a cancel that
            # never happened while the encode ran to completion.
            cur = conn.execute(
                "UPDATE jobs SET state='cancelled', finished=? WHERE id=? AND state='queued'",
                (time.time(), row["id"]),
            )
            conn.commit()
            if cur.rowcount:
                return self._send(200, {"cancelled": row["id"]})
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        if row["state"] == "running" and cancel_running(row["id"]):
            # "canceling", one l, next to a "cancelled" key with two: the 200
            # above echoes the job STATE, which is a stored database value and a
            # frozen enum API clients code against, so that spelling is not ours
            # to change. This 202 is not a state - it is a receipt saying the
            # terminate was sent - so it follows the American spelling the rest
            # of this codebase uses. Do not "fix" either one into the other.
            return self._send(202, {"canceling": row["id"]})
        self._send(409, {"error": f"job is {row['state']}"})


# The pool is sized to the maximum the setting allows, and each worker checks
# the CURRENT limit before claiming - so raising or lowering "convert at once"
# takes effect on the next job rather than needing a restart. Idle threads cost
# a sleep loop and nothing else.
WORKER_POOL = 8


def main() -> None:
    global ENCODER, ENCODER_REASON, ENCODER_PROBES, GPU, RUN_STATE, SERVING_TLS  # noqa: PLW0603
    init_db()
    if TOKEN == store.PLACEHOLDER_TOKEN:
        log.warning("TRANSCODEARR_TOKEN is still %r and is REFUSED as a key - a published image makes that "
                    "exact string public. Generate a real one: openssl rand -hex 24", store.PLACEHOLDER_TOKEN)
    if TOKEN in ("", store.PLACEHOLDER_TOKEN) and not store.list_tokens(db()):
        # Nothing can reach this container yet, and asking someone to go away,
        # invent a secret, edit their compose file and come back is a worse
        # first five minutes than handing them one. Minted like any other key,
        # so only its hash is stored and it can be revoked from the UI the
        # moment they have made something they would rather use.
        #
        # Printed in the log because that is the one channel a fresh container
        # already has. Anyone who can read `docker logs` can also `docker exec`,
        # so this tells them nothing their access did not already give them.
        raw, _row = store.mint_token(db(), "first run")
        log.warning("=" * 72)
        log.warning("No API key was configured, so one has been generated for you:")
        log.warning("    %s", raw)
        log.warning("Sign in with it at http://<this host>:%s - it is shown only this once.", PORT)
        log.warning("Revoke it under API Keys once you have created an account or minted your own.")
        log.warning("=" * 72)
    if RESET_ADMIN:
        cleared = store.clear_admin(db())
        if cleared:
            log.warning("TRANSCODEARR_RESET_ADMIN: deleted the admin account %r and signed out every session. "
                        "Sign in with an API key and create a new account.", cleared)
        else:
            log.warning("TRANSCODEARR_RESET_ADMIN is set but there was no admin account to delete.")
        # Every boot, not just the one that did something. Left set on a
        # container that restarts by itself, this deletes the replacement
        # account too, and the box sits there with no login at all - which is
        # a worse place to be than the forgotten password it was fixing.
        log.warning("TRANSCODEARR_RESET_ADMIN is still set. Remove it and restart, or the next restart "
                    "deletes the account you are about to create.")
    GPU = gpu_name()
    ENCODER, ENCODER_REASON, ENCODER_PROBES = choose_encoder()
    c = cfg()
    # An install from before the shipped five kept exactly one profile, named
    # Default and seeded from this container's env. It stays exactly as it is,
    # still active, and simply becomes the user's own - because an upgrade that
    # re-qualities somebody's library is the failure this project exists around.
    adopted = store.adopt_legacy_default(db())
    if adopted:
        log.info("kept your existing profile as a custom one - the five shipped profiles are new alongside it")
    store.ensure_shipped_profiles(db())
    # Every profile is tested with a real encode before any of them can be
    # chosen. A listed encoder is not a working one, and the same is true of a
    # whole profile: main10 on a card built without it looks perfectly
    # reasonable in a list and fails on the first film.
    log.info("testing profiles (a real encode each - this is why boot takes a moment)")
    tested = test_stored_profiles(store.list_profiles(db()))
    # Nothing active yet means a fresh install: adopt the shipped profile for
    # whichever encoder won the probe. That is the Default, and it is one of the
    # five rather than a sixth row saying the same thing.
    if not store.active_profile(db()):
        preferred = store.shipped_profile_id(ENCODER)
        for candidate in [preferred] + [p["id"] for p in tested if p["usable"]]:
            row, _why = store.activate_profile(db(), candidate)
            if row:
                log.info("active profile: %s", row["name"])
                break
        else:
            log.warning("no profile works on this machine - fix the encoder before queueing anything")
    log.info("gpu: %s", GPU or "none detected")
    log.info("encoder: %s (%s)", ENCODER, ENCODER_REASON)
    for p in ENCODER_PROBES:
        log.info("  %-12s %-13s %s", p["name"], "available" if p["available"] else "unavailable", p["reason"])
    log.info("media roots: %s | watch roots: %s | dot-hidden only: %s | at once: %s",
             MEDIA_ROOTS, c["watch_roots"] or MEDIA_ROOTS, c["hidden_only"], c["max_concurrent"])
    # The zone and the local time are printed NEXT TO the window because that is
    # the only way the mismatch is ever visible: the container is UTC unless the
    # compose file sets TZ, the NAS under it is not, and a window typed as
    # 01:00-06:00 then runs four hours out every night with nothing on any screen
    # that looks wrong.
    log.info("clock: %s local, TZ=%s | convert window: %s | auto start: %s",
             local_clock()[1], os.environ.get("TZ") or "unset, so UTC",
             c["convert_window"].strip() or "always", c["auto_start"])
    RUN_STATE = "running" if c["auto_start"] else "paused"
    may_claim()  # logs the boot state in the same words the page will show

    # Built before the socket AND before the workers, because a broken
    # certificate has to stop this process rather than serve the login form in
    # clear text - and SystemExit raised after the pool is up kills daemon
    # threads mid-encode, so a mistyped path became a restart loop that started
    # and abandoned an ffmpeg on every boot.
    tls = tls_context()
    SERVING_TLS = tls is not None
    if not tls and store.admin_exists(db()):
        log.warning("an admin password is set and this server is plain HTTP - that password crosses the "
                    "network in clear text unless a TLS-terminating proxy fronts it. Set tls_cert and "
                    "tls_key, or use a proxy that sends X-Forwarded-Proto: https")

    for _ in range(WORKER_POOL):
        threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=watch_loop, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    if tls:
        httpd.socket = tls.wrap_socket(httpd.socket, server_side=True)
        # Said out loud because the container's HEALTHCHECK is the thing that
        # breaks here: an http:// probe against an https:// port fails forever
        # and marks a perfectly healthy container unhealthy.
        log.info("HTTPS is on - the health check must fetch https://127.0.0.1:%d/healthz, not http", PORT)
    log.info("listening on %s://0.0.0.0:%d", "https" if tls else "http", PORT)

    def stop(signum, _frame) -> None:
        """Stop on purpose rather than being killed for not answering.

        Python registers nothing for SIGTERM, so `docker stop` was dropped on
        the floor, the container was SIGKILLed at the end of the grace period,
        and ffmpeg died mid-encode - every ordinary stop reading as a crash in
        the job history. The entrypoint execs this process, so the signal does
        arrive here now.
        """
        log.info("%s - stopping, canceling %d running job(s)", signal.Signals(signum).name, len(_running))
        for job_id in list(_running):
            cancel_running(job_id)
        # shutdown() blocks until serve_forever() returns, and serve_forever IS
        # this thread - signal handlers run on the main thread - so calling it
        # here deadlocks until docker gives up and SIGKILLs us, which is the
        # exact outcome this handler exists to avoid.
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    httpd.serve_forever()
    # The workers are daemon threads and the process is about to exit under
    # them. A moment for the cancelled encodes to write their own 'cancelled'
    # row: without it the next boot finds them 'running' and reports
    # "interrupted by restart" for a stop that was deliberate. Kept under
    # docker's default 10s grace, or the SIGKILL lands during the wait and
    # undoes the point of waiting.
    deadline = time.time() + 8
    while _running and time.time() < deadline:
        time.sleep(0.25)
    log.info("stopped")


if __name__ == "__main__":
    main()
