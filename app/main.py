"""TranscodeArr - the transcoding worker for a ManageArr stack.

One container, one job: turn media the arrs import (hidden behind a leading
dot) into the target container, verified, and only then reveal it to the
media server. The queue lives here, in SQLite; ManageArr and the arrs are
clients of the five-route API. Design notes and the incident this replaces
are in the README.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse
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
PORT = int(os.environ.get("PORT", "8484"))
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
DB_PATH = os.path.join(CONFIG_DIR, "transcodearr.db")
TRASH_DIR = os.environ.get("TRASH_DIR", os.path.join(CONFIG_DIR, "trash"))


def cfg() -> dict:
    """Current settings, read fresh.

    Deliberately not cached in a module global: a value changed in the UI has to
    take effect on the next scan and the next job without a restart, and the
    read is one indexed query against a local SQLite file - cheaper than the
    class of bug where a saved setting quietly does nothing until reboot.
    """
    return store.effective(store.read_settings(db()), dict(os.environ), MEDIA_ROOTS)

# Bump this with the image tag. /healthz reporting a version that is not the
# running build makes the one field whose job is "what is deployed" a liar.
VERSION = "0.8.0"
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
    os.makedirs(TRASH_DIR, exist_ok=True)
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
    # Existing rows predate priorities. A reveal is a rename that finishes in
    # milliseconds, so leaving hundreds of them behind a two-day transcode
    # backlog keeps files invisible for days that could be visible now.
    conn.execute(f"UPDATE jobs SET priority={REVEAL_PRIORITY} WHERE kind='reveal' AND state='queued' AND priority=0")

    # Boot rule: anything left running died with the previous process. Its
    # .part is deleted; the SOURCE was never touched, so nothing is lost and
    # the watcher will simply find the file again.
    for row in conn.execute("SELECT id, path FROM jobs WHERE state = 'running'").fetchall():
        names = core.plan_names(row["path"])
        if os.path.exists(names.part):
            try:
                os.unlink(names.part)
            except OSError:
                pass
        conn.execute(
            "UPDATE jobs SET state='failed', error='interrupted by restart', finished=? WHERE id=?",
            (time.time(), row["id"]),
        )
    conn.commit()


def job_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


# Work is taken in (priority, created) order. A reveal costs milliseconds - it
# is a rename, not an encode - so making it wait behind hours of transcoding
# keeps a file invisible for no reason at all.
REVEAL_PRIORITY = -10


def enqueue(path: str, kind: str) -> dict | None:
    """Queue a path unless it is already pending - idempotent per file."""
    conn = db()
    dup = conn.execute(
        "SELECT id FROM jobs WHERE path=? AND state IN ('queued','running')", (path,)
    ).fetchone()
    if dup:
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

        args = profile_args(fields, src, out, with_subs=True)
        run = subprocess.run(args, capture_output=True, text=True, timeout=180)
        if run.returncode != 0 and fields.get("audio_codec") == "copy":
            # Same fallback a real job would take, so "copy" is not reported as
            # broken when the job would have coped.
            args = profile_args(fields, src, out, with_subs=True, audio_codec="aac")
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
    """(encoder, why, full probe results). A forced choice is honoured if it
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


def encoding_profile() -> dict:
    """The active profile, or the probed defaults if somehow there is none."""
    row = store.active_profile(db())
    if row:
        return row
    return {"encoder": ENCODER, "quality": core.recommended_quality(ENCODER), "preset": "",
            "profile": "", "max_height": 0, "audio_codec": "aac", "audio_bitrate": 192,
            "audio_channels": 2}


def profile_args(p: dict, source: str, part: str, with_subs: bool, audio_codec: str | None = None) -> list[str]:
    """The exact argv one profile produces. Shared by real jobs and the test
    encode, so what gets validated is what gets run - a test that builds its
    command differently is a test of something else."""
    encoder = p["encoder"] if p["encoder"] in core.DEFAULT_TEMPLATES else ENCODER
    return core.build_ffmpeg_args(
        core.DEFAULT_TEMPLATES[encoder], source, part, p["quality"], with_subs,
        preset=core.valid_option(encoder, "presets", p.get("preset", "")),
        profile=core.valid_option(encoder, "profiles", p.get("profile", "")),
        max_height=p.get("max_height", 0),
        audio=core.audio_args(audio_codec or p.get("audio_codec", "aac"),
                              p.get("audio_bitrate", 192), p.get("audio_channels", 2)),
    )


def run_encode(job_id: str, source: str, names: core.JobNames, src_probe: core.Probe) -> tuple[bool, str, str]:
    """One encode attempt cycle: with subtitles, then without. (ok, warning, error)."""
    conn = db()
    prof = encoding_profile()
    attempts = [(True, prof["audio_codec"], "")]
    if src_probe.subtitle_streams:
        attempts.append((False, prof["audio_codec"], "text subtitles could not be carried into mp4 - dropped"))
    if prof["audio_codec"] == "copy":
        # MP4 cannot hold DTS or TrueHD, and those are exactly the tracks worth
        # copying. Falling back beats failing the job over the audio.
        attempts.append((False, "aac", "the original audio could not be copied into mp4 - re-encoded to AAC"))

    for with_subs, audio_codec, warning in attempts:
        args = profile_args(prof, source, names.part, with_subs, audio_codec)
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
        for line in proc.stdout or []:
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
        t.join(timeout=5)
        with _jobs_lock:
            if job_id in _running:
                _running[job_id]["proc"] = None

        if _cancelled(job_id):
            return False, "", "cancelled"
        if proc.returncode == 0:
            return True, warning, ""
        # Only retry without subtitles when subtitles are plausibly the problem.
        tail = "\n".join(stderr_tail[-8:])
        if with_subs and re.search(r"subtitle|mov_text|codec", tail, re.I):
            try:
                os.unlink(names.part)
            except OSError:
                pass
            continue
        return False, "", f"ffmpeg exited {proc.returncode}: {tail[-400:]}"
    return False, "", "ffmpeg failed with and without subtitles"


def trash(source: str) -> str:
    """The source is never deleted - it outlives its replacement in the trash.

    Mirrored under its media-root-relative path so a recovery is a move back,
    and pruned by age so the trash cannot eat the disk.
    """
    rel = os.path.basename(source)
    for root in MEDIA_ROOTS:
        if core.is_within(source, [root]):
            rel = os.path.relpath(source, root)
            break
    dest = os.path.join(TRASH_DIR, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
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
    return dest


def prune_trash() -> None:
    # Retention is measured from when a file was TRASHED, and trash() restamps
    # it for exactly that reason - see the note there.
    cutoff = time.time() - cfg()["trash_keep_days"] * 86400
    for dirpath, _dirs, files in os.walk(TRASH_DIR, topdown=False):
        for f in files:
            p = os.path.join(dirpath, f)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.unlink(p)
            except OSError:
                pass
        try:
            if dirpath != TRASH_DIR and not os.listdir(dirpath):
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
        if os.path.exists(names.visible) and names.visible != source:
            return finish("failed", error=f"target already exists: {names.visible} - not overwriting")
        # hidden_final is written with os.replace, which silently destroys
        # whatever is there. For a reveal it IS the source, which is fine; any
        # other existing file is somebody's media and must stop the job.
        if names.hidden_final != source and os.path.exists(names.hidden_final):
            return finish("failed", error=f"staging name is taken: {names.hidden_final} - not overwriting")

        src_probe = ffprobe(source)
        if src_probe is None or src_probe.video_streams < 1:
            return finish("failed", error="source is not a readable video (ffprobe found no video stream)")
        src_bytes = os.path.getsize(source)

        # Already the right container, or deliberately protected by a skip rule:
        # verify it is whole, then just reveal it.
        if protected or core.should_skip_transcode(src_probe, source_ext):
            if names.hidden:
                os.replace(source, names.visible)
                finish("done", output=names.visible, src_bytes=src_bytes, out_bytes=src_bytes, progress=100)
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

        os.replace(names.part, names.hidden_final)      # hidden, complete, atomic
        trashed = trash(source)                          # source survives, in trash
        os.replace(names.hidden_final, names.visible)    # the reveal
        # Marked done BEFORE talking to any arr. The media is already correct on
        # disk at this point; letting an unreachable arr throw into the handler
        # below would stamp 'failed' on a conversion that completely succeeded,
        # and re-running it would then trip the "target already exists" guard.
        finish(
            "done",
            output=names.visible,
            warning=warning or None,
            src_bytes=src_bytes,
            out_bytes=out_bytes,
            progress=100,
            log_tail=f"source preserved at {trashed}",
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
            conn = db()
            row = None
            # Claim under the lock so the concurrency limit is honoured: without
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


def scan_once() -> None:
    conn = db()
    now = time.time()
    c = cfg()
    watch_roots = c["watch_roots"] or MEDIA_ROOTS
    convert_extensions = set(c["convert_extensions"])
    process_unhidden, skip_patterns = c["process_unhidden"], c["skip_patterns"]
    for root in watch_roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                if core.PART_MARKER in name:
                    continue
                hidden = name.startswith(".")
                ext = os.path.splitext(name)[1].lower()
                if not hidden and not process_unhidden:
                    continue
                wanted = ext in convert_extensions or (hidden and ext == ".mp4")
                if not wanted:
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
                        enqueue(resolved, kind)
            # Commit per directory. sqlite3 opens a transaction on the first
            # write and holds the WAL write lock until commit - across a whole
            # library walk that is minutes, during which every other writer
            # (the worker finishing a job, a settings save) times out.
            conn.commit()
    conn.execute("DELETE FROM seen WHERE at < ?", (now - 7 * 86400,))
    conn.commit()


def watch_loop() -> None:
    while True:
        try:
            scan_once()
            prune_trash()
        except Exception:  # noqa: BLE001
            log.exception("scan failed")
        time.sleep(max(15, cfg()["scan_interval_seconds"]))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _queue_view(limit: int) -> dict:
    """What is converting now, and what is next - in the order it will happen.

    The order is not a display choice: worker_loop takes
    `WHERE state='queued' ORDER BY created LIMIT 1`, so oldest-first IS the
    running order. Listing newest-first (which the job history does, correctly,
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


class Handler(BaseHTTPRequestHandler):
    server_version = f"TranscodeArr/{VERSION}"
    # Without this a client that connects and then says nothing parks a thread
    # in readline() forever, holding a socket and a file descriptor, with no
    # authentication needed to do it.
    timeout = 30

    def _send(self, code: int, payload: dict | str, content_type: str = "application/json") -> None:
        body = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        return store.verify_token(db(), header[len("Bearer "):].strip(), TOKEN)

    def log_message(self, *_args) -> None:  # quiet access log
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/" or self.path.startswith("/index"):
            return self._send(200, web.PAGE, "text/html; charset=utf-8")
        if self.path == "/healthz":
            conn = db()
            counts = dict(conn.execute("SELECT state, COUNT(*) FROM jobs GROUP BY state").fetchall())
            c = cfg()
            return self._send(200, {
                "ok": True, "version": VERSION, "encoder": ENCODER, "encoder_reason": ENCODER_REASON,
                "queued": counts.get("queued", 0), "running": counts.get("running", 0),
                "media_roots": MEDIA_ROOTS, "watch_roots": c["watch_roots"] or MEDIA_ROOTS,
                "process_unhidden": c["process_unhidden"], "uptime_seconds": int(time.time() - STARTED),
                "auth_configured": bool(TOKEN) or bool(store.list_tokens(conn)),
            })
        if not self._authed():
            return self._send(401, {"error": "missing or wrong bearer token"})
        if self.path == "/api/settings":
            rows, env, roots = store.read_settings(db()), dict(os.environ), MEDIA_ROOTS
            return self._send(200, {
                "specs": [
                    {"key": s.key, "kind": s.kind, "label": s.label, "help": s.help, "group": s.group, "env": s.env}
                    for s in store.SPECS if not s.hidden
                ],
                "values": store.effective(rows, env, roots),
                "sources": store.sources(rows, env, roots),
                "media_roots": MEDIA_ROOTS,
            })
        if self.path == "/api/encoders":
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
        if self.path == "/api/system":
            snap = system.snapshot()
            with _jobs_lock:
                snap["converting"] = len(_running)
            snap["max_concurrent"] = cfg()["max_concurrent"]
            return self._send(200, snap)
        if self.path == "/api/profiles":
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
        if self.path == "/api/tokens":
            return self._send(200, {"tokens": store.list_tokens(db())})
        if self.path == "/api/arrs":
            return self._send(200, {"arrs": store.list_arrs(db())})
        if self.path.startswith("/api/fs"):
            q = _query(self.path)
            return self._send(*_browse(q.get("path", "")))
        if self.path.startswith("/queue"):
            try:
                limit = min(max(int(_query(self.path).get("limit", "100")), 1), 500)
            except ValueError:
                limit = 100
            return self._send(200, _queue_view(limit))
        m = re.fullmatch(r"/jobs/([0-9a-f-]{36})", self.path)
        if m:
            row = db().execute("SELECT * FROM jobs WHERE id=?", (m.group(1),)).fetchone()
            return self._send(200, job_dict(row)) if row else self._send(404, {"error": "no such job"})
        if self.path.startswith("/jobs"):
            q = _query(self.path)
            try:
                limit = min(max(int(q.get("limit", "50")), 1), 200)
            except ValueError:
                limit = 50
            state = q.get("state")
            rows = db().execute(
                f"SELECT * FROM jobs {'WHERE state=?' if state else ''} ORDER BY created DESC LIMIT ?",
                ((state, limit) if state else (limit,)),
            ).fetchall()
            return self._send(200, {"jobs": [job_dict(r) for r in rows]})
        self._send(404, {"error": "not found"})

    def _body(self) -> dict:
        # A malformed Content-Length raises ValueError, which the callers'
        # `except json.JSONDecodeError` does not catch - the connection closed
        # with no response at all rather than a 400.
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            raise json.JSONDecodeError("bad Content-Length", "", 0)
        return json.loads(self.rfile.read(max(0, length)) or b"{}")

    def do_PUT(self) -> None:  # noqa: N802
        if not self._authed():
            return self._send(401, {"error": "missing or wrong bearer token"})
        try:
            body = self._body()
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON"})
        if self.path == "/api/settings":
            try:
                written = store.save_settings(db(), body, MEDIA_ROOTS)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            log.info("settings changed: %s", ", ".join(written))
            return self._send(200, {"saved": written})
        m = re.fullmatch(r"/api/arrs/([0-9a-f-]{36})", self.path)
        if m:
            try:
                row = store.save_arr(db(), body, m.group(1))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"arr": {**row, "api_key": "********"}})
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authed():
            return self._send(401, {"error": "missing or wrong bearer token"})
        try:
            body = self._body()
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON"})

        if self.path == "/api/encoders/probe":
            # Re-probe on demand: hardware changes under a container more often
            # than the container restarts - a driver reload, a GPU freed by
            # another process, the memory-fragmentation fix in the README.
            global ENCODER, ENCODER_REASON, ENCODER_PROBES, GPU  # noqa: PLW0603
            GPU = gpu_name()
            ENCODER, ENCODER_REASON, ENCODER_PROBES = choose_encoder()
            log.info("re-probed encoders: %s (%s)", ENCODER, ENCODER_REASON)
            return self._send(200, {"gpu": GPU, "in_use": ENCODER, "why": ENCODER_REASON,
                                    "encoders": ENCODER_PROBES})
        if self.path in ("/api/profiles", "/api/profiles/test") or re.fullmatch(r"/api/profiles/([0-9a-f-]{36})", self.path):
            available = [p["name"] for p in ENCODER_PROBES if p["available"]]
            try:
                fields = store.clean_profile(body, available)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            ok, note = validate_profile(fields)
            if self.path == "/api/profiles/test":
                return self._send(200, {"ok": ok, "detail": note, "command": " ".join(
                    profile_args(fields, "<input>", "<output>", with_subs=True))})
            if not ok:
                # Never store a configuration that has been shown not to work -
                # the alternative is discovering it on the first real film.
                return self._send(400, {"error": f"That profile does not work on this machine: {note}"})
            m = re.fullmatch(r"/api/profiles/([0-9a-f-]{36})", self.path)
            try:
                row = store.save_profile(db(), fields, m.group(1) if m else None, note)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            log.info("profile saved: %s (%s)", row["name"], note)
            return self._send(200 if m else 201, {"profile": row, "detail": note})
        m = re.fullmatch(r"/api/profiles/([0-9a-f-]{36})/activate", self.path)
        if m:
            row = store.activate_profile(db(), m.group(1))
            if not row:
                return self._send(404, {"error": "no such profile"})
            log.info("active profile: %s", row["name"])
            return self._send(200, {"profile": row})
        if self.path == "/api/tokens":
            raw, row = store.mint_token(db(), str(body.get("name", "")))
            log.info("api key minted: %s", row["name"])
            # The only time the raw key exists outside the caller's hands.
            return self._send(201, {"token": raw, **row})
        if self.path == "/api/arrs":
            try:
                row = store.save_arr(db(), body)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(201, {"arr": {**row, "api_key": "********"}})
        if self.path == "/api/arrs/test":
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

        if self.path != "/jobs":
            return self._send(404, {"error": "not found"})
        ok, resolved = core.validate_path(str(body.get("path", "")), MEDIA_ROOTS)
        if not ok:
            return self._send(400, {"error": resolved})
        names = core.plan_names(resolved)
        protected = core.matches_skip(os.path.basename(resolved), cfg()["skip_patterns"])
        job = enqueue(resolved, "reveal" if names.reveal_only or protected else "transcode")
        if job is None:
            return self._send(409, {"error": "already queued or running for this path"})
        self._send(201, job)

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._authed():
            return self._send(401, {"error": "missing or wrong bearer token"})
        m = re.fullmatch(r"/api/tokens/([0-9a-f-]{36})", self.path)
        if m:
            return self._send(*((200, {"revoked": m.group(1)}) if store.revoke_token(db(), m.group(1))
                                else (404, {"error": "no such key"})))
        m = re.fullmatch(r"/api/profiles/([0-9a-f-]{36})", self.path)
        if m:
            ok, why = store.delete_profile(db(), m.group(1))
            return self._send(200 if ok else 400, {"deleted": m.group(1)} if ok else {"error": why})
        m = re.fullmatch(r"/api/arrs/([0-9a-f-]{36})", self.path)
        if m:
            return self._send(*((200, {"deleted": m.group(1)}) if store.delete_arr(db(), m.group(1))
                                else (404, {"error": "no such connection"})))
        m = re.fullmatch(r"/jobs/([0-9a-f-]{36})", self.path)
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
            return self._send(202, {"cancelling": row["id"]})
        self._send(409, {"error": f"job is {row['state']}"})


# The pool is sized to the maximum the setting allows, and each worker checks
# the CURRENT limit before claiming - so raising or lowering "convert at once"
# takes effect on the next job rather than needing a restart. Idle threads cost
# a sleep loop and nothing else.
WORKER_POOL = 8


def main() -> None:
    global ENCODER, ENCODER_REASON, ENCODER_PROBES, GPU  # noqa: PLW0603
    init_db()
    if not TOKEN and not store.list_tokens(db()):
        log.warning("No API key: set TRANSCODEARR_TOKEN to get in the first time, then mint keys in the UI")
    GPU = gpu_name()
    ENCODER, ENCODER_REASON, ENCODER_PROBES = choose_encoder()
    c = cfg()
    # The first profile is whatever the daemon is already doing, written down.
    # An upgrade must not quietly change how anything is encoded.
    opts = core.ENCODER_OPTIONS.get(ENCODER, {})
    store.ensure_default_profile(
        db(), ENCODER, c["quality"],
        core.valid_option(ENCODER, "presets", c["encoder_preset"]) or opts.get("default_preset", ""),
        core.valid_option(ENCODER, "profiles", c["encoder_profile"]) or opts.get("default_profile", ""),
        c["max_height"],
    )
    log.info("gpu: %s", GPU or "none detected")
    log.info("encoder: %s (%s)", ENCODER, ENCODER_REASON)
    for p in ENCODER_PROBES:
        log.info("  %-12s %-13s %s", p["name"], "available" if p["available"] else "unavailable", p["reason"])
    log.info("media roots: %s | watch roots: %s | unhidden: %s | at once: %s",
             MEDIA_ROOTS, c["watch_roots"] or MEDIA_ROOTS, c["process_unhidden"], c["max_concurrent"])
    for _ in range(WORKER_POOL):
        threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=watch_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
