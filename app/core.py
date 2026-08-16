"""The pure logic of TranscodeArr - no I/O, no subprocesses, no clock.

Everything here is a function from values to values, so the rules that keep
media safe can be tested exactly, without a filesystem or an ffmpeg. The
daemon (main.py) is deliberately thin around this.

The rules exist because of a real incident: a watcher that trusted mtime for
"is this file finished copying" queued files the moment their first byte
landed (imports preserve the release's own timestamp, so every file looked
old), transcoded the fragment that existed, and deleted the source on exit
code 0. Thirty-six movies in a real library are now permanently short.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

# Blu-ray's .m2ts and DVD's .vob are here because a real library had them: a
# file whose extension is missing from this set is refused by the API and
# skipped by the watcher, so it stays hidden forever without ever appearing as
# an error. Silence is the failure mode this whole worker exists to avoid.
VIDEO_EXTENSIONS = {".mkv", ".avi", ".m4v", ".mp4", ".mov", ".wmv", ".ts", ".m2ts", ".mts",
                    ".vob", ".webm", ".flv", ".mpg", ".mpeg"}

# What the API and watcher will act on. The staging marker must never match a
# convertible extension or the worker would eat its own output.
PART_MARKER = ".tapart"


def is_within(path: str, roots: list[str]) -> bool:
    """Whether a RESOLVED path sits inside one of the given roots.

    The caller resolves symlinks first (os.path.realpath); checking the raw
    path would let a symlink walk straight out of the root.
    """
    norm = os.path.normcase(os.path.normpath(path))
    for root in roots:
        r = os.path.normcase(os.path.normpath(root))
        if norm == r or norm.startswith(r + os.sep):
            return True
    return False


def validate_path(raw: str, roots: list[str], realpath=os.path.realpath) -> tuple[bool, str]:
    """(ok, resolved-or-reason). The one gate every job goes through.

    This API accepts a filesystem path and spawns a process on it, so the
    checks are an allowlist: inside a configured media root (after resolving),
    a known video extension, not our own staging file.
    """
    if not raw or not raw.strip():
        return False, "no path given"
    if "\x00" in raw or "\n" in raw or "\r" in raw:
        return False, "path contains a control character"
    if not roots:
        return False, "no media roots configured - refusing to touch anything"
    resolved = realpath(raw.strip())
    if not is_within(resolved, roots):
        return False, f"{resolved} is outside every configured media root"
    name = os.path.basename(resolved)
    ext = os.path.splitext(name)[1].lower()
    if ext not in VIDEO_EXTENSIONS:
        return False, f"{ext or '(no extension)'} is not a video extension"
    if PART_MARKER in name:
        return False, "refusing to process a staging file"
    return True, resolved


# ---------------------------------------------------------------------------
# The hide / stage / reveal name arithmetic
# ---------------------------------------------------------------------------
#
# Jellyfin ignores dot-prefixed files (a hardcoded "**/.*" in its ignore
# patterns - undocumented but stable since the Emby fork). That accident of
# Unix convention is the whole hiding mechanism:
#
#   arr imports        .Movie (2026).mkv      hidden, complete
#   worker encodes to  .Movie (2026).tapart.mp4   hidden, partial
#   verified, becomes  .Movie (2026).mp4      hidden, complete   (os.replace)
#   source -> trash
#   revealed as        Movie (2026).mp4       visible            (os.replace)
#
# Never a visible partial file, never two visible copies, and the source
# outlives the encode in the trash.


@dataclass(frozen=True)
class JobNames:
    source: str          # what we were given
    hidden: bool         # whether the source name starts with a dot
    part: str            # hidden staging path the encoder writes
    hidden_final: str    # hidden, verified output before the reveal
    visible: str         # what the world eventually sees
    reveal_only: bool    # already an acceptable container - just needs unhiding


def plan_names(source: str, target_ext: str = ".mp4") -> JobNames:
    directory, base = os.path.split(source)
    hidden = base.startswith(".")
    stem, ext = os.path.splitext(base[1:] if hidden else base)
    join = lambda name: os.path.join(directory, name)  # noqa: E731
    return JobNames(
        source=source,
        hidden=hidden,
        part=join(f".{stem}{PART_MARKER}{target_ext}"),
        hidden_final=join(f".{stem}{target_ext}"),
        visible=join(f"{stem}{target_ext}"),
        reveal_only=hidden and ext.lower() == target_ext,
    )


# ---------------------------------------------------------------------------
# Verification - the check whose absence truncated a library
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    duration: float | None
    video_streams: int
    audio_streams: int
    subtitle_streams: int


def parse_ffprobe(payload: dict) -> Probe:
    streams = payload.get("streams") or []
    fmt = payload.get("format") or {}
    duration = None
    try:
        duration = float(fmt.get("duration"))
    except (TypeError, ValueError):
        pass
    count = lambda kind: sum(1 for s in streams if s.get("codec_type") == kind)  # noqa: E731
    return Probe(
        duration=duration,
        video_streams=count("video"),
        audio_streams=count("audio"),
        subtitle_streams=count("subtitle"),
    )


def verify_output(source: Probe, output: Probe, tolerance: float = 0.015) -> tuple[bool, str]:
    """Whether the encode may replace the source. Exit code 0 is not asked.

    ffmpeg (and HandBrake before it) will exit 0 having encoded whatever
    bytes existed - a file still copying encodes into a short, valid,
    wrong file. Only comparing durations catches that.
    """
    if output.video_streams < 1:
        return False, "output has no video stream"
    if output.audio_streams < 1 and source.audio_streams > 0:
        return False, "output lost every audio stream"
    if source.duration is None or output.duration is None:
        return False, "duration unreadable on one side - refusing to replace the source"
    if source.duration <= 0:
        return False, "source duration is zero"
    drift = abs(output.duration - source.duration) / source.duration
    if drift > tolerance:
        return (
            False,
            f"duration mismatch: source {source.duration:.0f}s, output {output.duration:.0f}s "
            f"({drift * 100:.1f}% off) - source kept",
        )
    return True, "ok"


def should_skip_transcode(probe: Probe, container_ext: str) -> bool:
    """Already-fine files are revealed, not re-encoded.

    Placeholder for the richer remux rule (H.264-in-anything -> -c copy);
    v1 keeps it to "an .mp4 needs no transcode", which is the behaviour the
    original script had.
    """
    return container_ext.lower() == ".mp4"


def matches_skip(filename: str, patterns: list[str]) -> bool:
    """Should this file be left in its own container rather than re-encoded?

    Plain case-insensitive substring matching on the file name, not a regex:
    the thing people actually want to write is "Remux", and a regex dialect is
    a way to get that wrong. Arr naming puts the quality in the file name, so a
    substring is enough to protect exactly the copies worth protecting.
    """
    name = os.path.basename(filename).lower()
    return any(p.strip().lower() in name for p in patterns if p.strip())


# ---------------------------------------------------------------------------
# Readiness - is the file actually finished being written?
# ---------------------------------------------------------------------------


def is_stable(size_then: int | None, size_now: int, seconds_between: float, needed: float) -> bool:
    """Size-based stability: unchanged size across a real interval.

    Never mtime. Imported media carries the release's own timestamp - a
    library's median mtime age was measured at twelve YEARS - so any
    mtime-age rule passes the instant the first byte lands.
    """
    return size_then is not None and size_now == size_then and seconds_between >= needed


# ---------------------------------------------------------------------------
# ffmpeg command construction
# ---------------------------------------------------------------------------

# {in} and {out} are replaced with real paths, passed as argv entries - no
# shell is ever involved, so paths need no quoting and cannot inject.
DEFAULT_TEMPLATES: dict[str, str] = {
    "h264_nvenc": (
        "-map 0:v:0 -map 0:a? {subs} "
        "-c:v h264_nvenc -preset p4 -profile:v main -level 4.2 -rc vbr -cq {quality} -b:v 0 "
        "-c:a aac -b:a 192k -ac 2 -movflags +faststart"
    ),
    "h264_qsv": (
        "-map 0:v:0 -map 0:a? {subs} "
        "-c:v h264_qsv -preset medium -profile:v main -global_quality {quality} "
        "-c:a aac -b:a 192k -ac 2 -movflags +faststart"
    ),
    "libx264": (
        "-map 0:v:0 -map 0:a? {subs} "
        "-c:v libx264 -preset medium -profile:v main -crf {quality} "
        "-c:a aac -b:a 192k -ac 2 -movflags +faststart"
    ),
    # HEVC roughly halves the file at the same visual quality. The -tag:v hvc1
    # is not optional: without it Apple devices and Safari refuse to play HEVC
    # in MP4 at all, and the failure looks like a corrupt file rather than an
    # unsupported one.
    "hevc_nvenc": (
        "-map 0:v:0 -map 0:a? {subs} "
        "-c:v hevc_nvenc -preset p4 -tag:v hvc1 -rc vbr -cq {quality} -b:v 0 "
        "-c:a aac -b:a 192k -ac 2 -movflags +faststart"
    ),
    "libx265": (
        "-map 0:v:0 -map 0:a? {subs} "
        "-c:v libx265 -preset medium -tag:v hvc1 -crf {quality} "
        "-c:a aac -b:a 192k -ac 2 -movflags +faststart"
    ),
}

# What each encoder is for, in the terms someone choosing between them actually
# cares about: is it using the GPU, what quality number suits it, and what is
# the catch. The quality scales are NOT interchangeable - CRF 21 on x264 and
# CQ 21 on NVENC are different pictures and different file sizes, which is why
# the recommendation travels with the encoder rather than being one global
# default.
ENCODER_INFO: dict[str, dict] = {
    "h264_nvenc": {
        "codec": "H.264", "hardware": True, "recommended": 23, "sane": [19, 28],
        "summary": "Fast, on the GPU, and plays on everything.",
        "detail": "The safe default. Bigger files than libx264 at the same quality, but minutes per "
                  "film instead of hours, and every client made in the last fifteen years can direct play it.",
    },
    "hevc_nvenc": {
        "codec": "HEVC", "hardware": True, "recommended": 26, "sane": [22, 32],
        "summary": "About half the size, on the GPU, with a compatibility catch.",
        "detail": "Roughly half the file of H.264 at similar quality and still GPU-fast. Older TVs, "
                  "browsers and some streaming sticks cannot direct play HEVC and will make your server "
                  "transcode on the fly instead - which costs more than the space it saves if it happens often.",
    },
    "libx264": {
        "codec": "H.264", "hardware": False, "recommended": 21, "sane": [18, 26],
        "summary": "Smallest H.264 files, but on the CPU.",
        "detail": "Better quality per byte than NVENC, at perhaps a tenth of the speed. Reasonable for a "
                  "handful of files, painful for a library.",
    },
    "libx265": {
        "codec": "HEVC", "hardware": False, "recommended": 24, "sane": [20, 30],
        "summary": "Smallest files of all, and by far the slowest.",
        "detail": "The best compression here and the worst throughput - hours per film on a NAS CPU. "
                  "Carries the same HEVC playback caveat as hevc_nvenc.",
    },
    "h264_qsv": {
        "codec": "H.264", "hardware": True, "recommended": 23, "sane": [19, 28],
        "summary": "Intel Quick Sync - the GPU built into an Intel CPU.",
        "detail": "Comparable to NVENC in speed and quality. Needs the container to have /dev/dri passed "
                  "through and an Intel chip with Quick Sync.",
    },
}

# Probed in this order, and the first that works becomes the automatic choice.
# H.264 on the GPU leads because it is the one that is both fast and plays
# everywhere - the two things a library owner notices.
ENCODER_ORDER = ["h264_nvenc", "h264_qsv", "hevc_nvenc", "libx264", "libx265"]


def recommended_quality(encoder: str) -> int:
    info = ENCODER_INFO.get(encoder)
    return int(info["recommended"]) if info else 23


def build_ffmpeg_args(
    template: str,
    source: str,
    part: str,
    quality: int,
    with_subtitles: bool,
) -> list[str]:
    """argv for one encode attempt.

    MP4 cannot carry PGS/VOBSUB image subtitles, so text subs are converted to
    mov_text and the caller retries without subtitles when that fails - a
    dropped subtitle is recorded as a warning, never a silent loss.
    """
    subs = "-map 0:s? -c:s mov_text" if with_subtitles else "-sn"
    rendered = template.format(subs=subs, quality=quality)
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-y",
        "-i", source,
        *rendered.split(),
        "-progress", "pipe:1",
        part,
    ]


PROGRESS_RE = re.compile(r"out_time_us=(\d+)")


def parse_progress(line: str, duration: float | None) -> int | None:
    """Percent complete from one line of ffmpeg -progress output."""
    m = PROGRESS_RE.search(line)
    if not m or not duration or duration <= 0:
        return None
    return min(99, int((int(m.group(1)) / 1_000_000) / duration * 100))
