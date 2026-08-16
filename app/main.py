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
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("transcodearr")

# ---------------------------------------------------------------------------
# Configuration - env only, no config file to drift
# ---------------------------------------------------------------------------

MEDIA_ROOTS = [p for p in os.environ.get("MEDIA_ROOTS", "/media").split(":") if p]
WATCH_ROOTS = [p for p in os.environ.get("WATCH_ROOTS", "").split(":") if p] or MEDIA_ROOTS
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL_SECONDS", "300"))
STABLE_SECONDS = int(os.environ.get("STABLE_SECONDS", "120"))
CONVERT_EXTENSIONS = {
    e if e.startswith(".") else f".{e}"
    for e in os.environ.get("CONVERT_EXTENSIONS", ".mkv,.avi,.m4v").lower().split(",")
    if e
}
# Only dot-hidden files are touched unless this is switched on. Sweeping the
# whole visible library into a re-encode is a decision, not a default.
PROCESS_UNHIDDEN = os.environ.get("PROCESS_UNHIDDEN", "false").lower() == "true"
QUALITY = int(os.environ.get("QUALITY", "24"))
TOKEN = os.environ.get("TRANSCODEARR_TOKEN", "")
PORT = int(os.environ.get("PORT", "8484"))
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
DB_PATH = os.path.join(CONFIG_DIR, "transcodearr.db")
TRASH_DIR = os.environ.get("TRASH_DIR", os.path.join(CONFIG_DIR, "trash"))
TRASH_KEEP_DAYS = int(os.environ.get("TRASH_KEEP_DAYS", "7"))
DURATION_TOLERANCE = float(os.environ.get("VERIFY_DURATION_TOLERANCE", "0.015"))
FORCE_ENCODER = os.environ.get("FORCE_ENCODER", "")

VERSION = "0.1.0"
STARTED = time.time()

# ---------------------------------------------------------------------------
# SQLite - the queue is durable state, the worker is just execution
# ---------------------------------------------------------------------------

_local = threading.local()


def db() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        conn = sqlite3.connect(DB_PATH)
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
        -- Watcher memory: last observed size per path, for size-stability.
        CREATE TABLE IF NOT EXISTS seen (
          path TEXT PRIMARY KEY,
          size INTEGER NOT NULL,
          at REAL NOT NULL
        );
        """
    )
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


def enqueue(path: str, kind: str) -> dict | None:
    """Queue a path unless it is already pending - idempotent per file."""
    conn = db()
    dup = conn.execute(
        "SELECT id FROM jobs WHERE path=? AND state IN ('queued','running')", (path,)
    ).fetchone()
    if dup:
        return None
    job_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO jobs (id, path, state, kind, created) VALUES (?,?,?,?,?)",
        (job_id, path, "queued", kind, time.time()),
    )
    conn.commit()
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


def probe_encoders() -> tuple[str, str]:
    """(encoder, reason). Tried in order with a real one-second encode.

    A listed encoder is not a working one - h264_nvenc appears in every ffmpeg
    build's list and then fails at runtime without the driver libraries. Only
    an actual encode proves the path works, and the reason is kept so /healthz
    can say WHY the box is on CPU when it is.
    """
    order = [FORCE_ENCODER] if FORCE_ENCODER else ["h264_nvenc", "h264_qsv", "libx264"]
    reasons = []
    for enc in order:
        if enc not in core.DEFAULT_TEMPLATES:
            reasons.append(f"{enc}: no template")
            continue
        try:
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", "testsrc2=duration=1:size=320x240:rate=30",
                 "-c:v", enc, "-f", "null", "-"],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0:
                return enc, "; ".join(reasons) if reasons else "first choice worked"
            reasons.append(f"{enc}: {(r.stderr or '').strip().splitlines()[-1][:160] if r.stderr else 'failed'}")
        except Exception as e:  # noqa: BLE001
            reasons.append(f"{enc}: {e}")
    return "libx264", "; ".join(reasons)


ENCODER = "libx264"
ENCODER_REASON = "not probed yet"


# ---------------------------------------------------------------------------
# The worker - one at a time, deliberately
# ---------------------------------------------------------------------------
# One 1GbE-attached spindle set and one NVENC session budget do not benefit
# from concurrency; two encodes interleave into two slow encodes.

_cancel = threading.Event()
_current_job: dict = {}


def run_encode(job_id: str, source: str, names: core.JobNames, src_probe: core.Probe) -> tuple[bool, str, str]:
    """One encode attempt cycle: with subtitles, then without. (ok, warning, error)."""
    conn = db()
    template = core.DEFAULT_TEMPLATES[ENCODER]
    attempts = [(True, ""), (False, "text subtitles could not be carried into mp4 - dropped")]
    if src_probe.subtitle_streams == 0:
        attempts = [(False, "")]

    for with_subs, warning in attempts:
        args = core.build_ffmpeg_args(template, source, names.part, QUALITY, with_subs)
        conn.execute("UPDATE jobs SET log_tail=? WHERE id=?", (" ".join(args), job_id))
        conn.commit()
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _current_job["proc"] = proc
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
            if _cancel.is_set():
                proc.terminate()
        proc.wait()
        t.join(timeout=5)
        _current_job.pop("proc", None)

        if _cancel.is_set():
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
    return dest


def prune_trash() -> None:
    cutoff = time.time() - TRASH_KEEP_DAYS * 86400
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


def process(job: dict) -> None:
    conn = db()
    job_id, source = job["id"], job["path"]
    conn.execute("UPDATE jobs SET state='running', started=?, encoder=? WHERE id=?", (time.time(), ENCODER, job_id))
    conn.commit()

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
        names = core.plan_names(source)
        if os.path.exists(names.visible) and names.visible != source:
            return finish("failed", error=f"target already exists: {names.visible} - not overwriting")

        src_probe = ffprobe(source)
        if src_probe is None or src_probe.video_streams < 1:
            return finish("failed", error="source is not a readable video (ffprobe found no video stream)")
        src_bytes = os.path.getsize(source)

        # Already the right container: verify it is whole, then just reveal.
        if job["kind"] == "reveal" or core.should_skip_transcode(src_probe, os.path.splitext(source)[1]):
            if names.hidden:
                os.replace(source, names.visible)
                return finish("done", output=names.visible, src_bytes=src_bytes, out_bytes=src_bytes, progress=100)
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
        verified, why = core.verify_output(src_probe, out_probe or core.Probe(None, 0, 0, 0), DURATION_TOLERANCE)
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
        finish(
            "done",
            output=names.visible,
            warning=warning or None,
            src_bytes=src_bytes,
            out_bytes=out_bytes,
            progress=100,
            log_tail=f"source preserved at {trashed}",
        )
    except Exception as e:  # noqa: BLE001
        log.exception("job %s crashed", job_id[:8])
        finish("failed", error=f"internal: {e}")


def worker_loop() -> None:
    while True:
        row = db().execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
        if row is None:
            time.sleep(2)
            continue
        _cancel.clear()
        _current_job["id"] = row["id"]
        process(job_dict(row))
        _current_job.clear()


# ---------------------------------------------------------------------------
# The watcher - polling, on purpose
# ---------------------------------------------------------------------------
# inotify does not work across SMB/NFS, which is exactly where NAS media
# lives, so event watchers silently degrade anyway. A poll every few minutes
# with size-stability is slower and correct.


def scan_once() -> None:
    conn = db()
    now = time.time()
    for root in WATCH_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                if core.PART_MARKER in name:
                    continue
                hidden = name.startswith(".")
                ext = os.path.splitext(name)[1].lower()
                if not hidden and not PROCESS_UNHIDDEN:
                    continue
                wanted = ext in CONVERT_EXTENSIONS or (hidden and ext == ".mp4")
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
                if prev and core.is_stable(prev["size"], size, now - prev["at"], STABLE_SECONDS):
                    ok, resolved = core.validate_path(path, MEDIA_ROOTS)
                    if ok:
                        kind = "reveal" if hidden and ext == ".mp4" else "transcode"
                        enqueue(resolved, kind)
    conn.execute("DELETE FROM seen WHERE at < ?", (now - 7 * 86400,))
    conn.commit()


def watch_loop() -> None:
    while True:
        try:
            scan_once()
            prune_trash()
        except Exception:  # noqa: BLE001
            log.exception("scan failed")
        time.sleep(SCAN_INTERVAL)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

PAGE = """<!doctype html><meta charset="utf-8"><title>TranscodeArr</title>
<style>
 body{font-family:system-ui;background:#0b1220;color:#cbd5e1;margin:2rem auto;max-width:60rem;padding:0 1rem}
 h1{color:#22d3ee;font-size:1.3rem} table{width:100%;border-collapse:collapse;font-size:.85rem}
 td,th{border-bottom:1px solid #1e293b;padding:.4rem .5rem;text-align:left} .done{color:#34d399}
 .failed{color:#f87171} .running{color:#22d3ee} .queued{color:#94a3b8} code{color:#94a3b8;font-size:.75rem}
</style>
<h1>TranscodeArr</h1><p id="h"></p><table id="t"></table>
<script>
const H=localStorage.token||(localStorage.token=prompt('API token'));
async function go(){
 const h={Authorization:'Bearer '+H};
 const z=await (await fetch('/healthz')).json();
 document.getElementById('h').innerHTML=
  `encoder <b>${z.encoder}</b> - queue ${z.queued} - running ${z.running} - v${z.version}`+
  (z.encoder==='libx264'?` <code>(${z.encoder_reason})</code>`:'');
 const j=await (await fetch('/jobs?limit=40',{headers:h})).json();
 document.getElementById('t').innerHTML='<tr><th>state</th><th>file</th><th>%</th><th>result</th></tr>'+
  j.jobs.map(x=>`<tr><td class="${x.state}">${x.state}</td><td>${x.path.split('/').pop()}</td>`+
   `<td>${x.progress??''}</td><td><code>${x.error||x.warning||x.output||''}</code></td></tr>`).join('');
}
go();setInterval(go,3000);
</script>"""


class Handler(BaseHTTPRequestHandler):
    server_version = f"TranscodeArr/{VERSION}"

    def _send(self, code: int, payload: dict | str, content_type: str = "application/json") -> None:
        body = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        if not TOKEN:
            return False
        return self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def log_message(self, *_args) -> None:  # quiet access log
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/" or self.path.startswith("/index"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/healthz":
            conn = db()
            counts = dict(conn.execute("SELECT state, COUNT(*) FROM jobs GROUP BY state").fetchall())
            return self._send(200, {
                "ok": True, "version": VERSION, "encoder": ENCODER, "encoder_reason": ENCODER_REASON,
                "queued": counts.get("queued", 0), "running": counts.get("running", 0),
                "media_roots": MEDIA_ROOTS, "watch_roots": WATCH_ROOTS,
                "process_unhidden": PROCESS_UNHIDDEN, "uptime_seconds": int(time.time() - STARTED),
                "auth_configured": bool(TOKEN),
            })
        if not self._authed():
            return self._send(401, {"error": "missing or wrong bearer token"})
        m = re.fullmatch(r"/jobs/([0-9a-f-]{36})", self.path)
        if m:
            row = db().execute("SELECT * FROM jobs WHERE id=?", (m.group(1),)).fetchone()
            return self._send(200, job_dict(row)) if row else self._send(404, {"error": "no such job"})
        if self.path.startswith("/jobs"):
            q = dict(p.split("=", 1) for p in self.path.partition("?")[2].split("&") if "=" in p)
            limit = min(int(q.get("limit", "50")), 200)
            state = q.get("state")
            rows = db().execute(
                f"SELECT * FROM jobs {'WHERE state=?' if state else ''} ORDER BY created DESC LIMIT ?",
                ((state, limit) if state else (limit,)),
            ).fetchall()
            return self._send(200, {"jobs": [job_dict(r) for r in rows]})
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authed():
            return self._send(401, {"error": "missing or wrong bearer token"})
        if self.path != "/jobs":
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON"})
        ok, resolved = core.validate_path(str(body.get("path", "")), MEDIA_ROOTS)
        if not ok:
            return self._send(400, {"error": resolved})
        names = core.plan_names(resolved)
        job = enqueue(resolved, "reveal" if names.reveal_only else "transcode")
        if job is None:
            return self._send(409, {"error": "already queued or running for this path"})
        self._send(201, job)

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._authed():
            return self._send(401, {"error": "missing or wrong bearer token"})
        m = re.fullmatch(r"/jobs/([0-9a-f-]{36})", self.path)
        if not m:
            return self._send(404, {"error": "not found"})
        conn = db()
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (m.group(1),)).fetchone()
        if not row:
            return self._send(404, {"error": "no such job"})
        if row["state"] == "queued":
            conn.execute("UPDATE jobs SET state='cancelled', finished=? WHERE id=?", (time.time(), row["id"]))
            conn.commit()
            return self._send(200, {"cancelled": row["id"]})
        if row["state"] == "running" and _current_job.get("id") == row["id"]:
            _cancel.set()
            return self._send(202, {"cancelling": row["id"]})
        self._send(409, {"error": f"job is {row['state']}"})


def main() -> None:
    global ENCODER, ENCODER_REASON  # noqa: PLW0603
    init_db()
    if not TOKEN:
        log.warning("TRANSCODEARR_TOKEN is not set - the API is disabled; only the watcher runs")
    ENCODER, ENCODER_REASON = probe_encoders()
    log.info("encoder: %s (%s)", ENCODER, ENCODER_REASON)
    log.info("media roots: %s | watch roots: %s | unhidden: %s", MEDIA_ROOTS, WATCH_ROOTS, PROCESS_UNHIDDEN)
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=watch_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
