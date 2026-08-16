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
# No -level is pinned. It used to say 4.2, which cannot represent 4K at all:
# ffmpeg either refuses or silently produces a stream that claims a level it
# exceeds. Letting the encoder derive the level from the actual picture is
# both correct and one less thing to get wrong.
DEFAULT_TEMPLATES: dict[str, str] = {
    "h264_nvenc": (
        "-map 0:v:0 -map 0:a? {subs} {filters}"
        "-c:v h264_nvenc -preset {preset} -profile:v {profile} -rc vbr -cq {quality} -b:v 0 "
        "{audio} -movflags +faststart"
    ),
    "h264_qsv": (
        "-map 0:v:0 -map 0:a? {subs} {filters}"
        "-c:v h264_qsv -preset {preset} -profile:v {profile} -global_quality {quality} "
        "{audio} -movflags +faststart"
    ),
    "libx264": (
        "-map 0:v:0 -map 0:a? {subs} {filters}"
        "-c:v libx264 -preset {preset} -profile:v {profile} -crf {quality} "
        "{audio} -movflags +faststart"
    ),
    # HEVC roughly halves the file at the same visual quality. The -tag:v hvc1
    # is not optional: without it Apple devices and Safari refuse to play HEVC
    # in MP4 at all, and the failure looks like a corrupt file rather than an
    # unsupported one.
    "hevc_nvenc": (
        "-map 0:v:0 -map 0:a? {subs} {filters}"
        "-c:v hevc_nvenc -preset {preset} -profile:v {profile} -tag:v hvc1 -rc vbr -cq {quality} -b:v 0 "
        "{audio} -movflags +faststart"
    ),
    "libx265": (
        "-map 0:v:0 -map 0:a? {subs} {filters}"
        "-c:v libx265 -preset {preset} -profile:v {profile} -tag:v hvc1 -crf {quality} "
        "{audio} -movflags +faststart"
    ),
}

# The speed/size dial and the codec profile, per encoder. Both were hardcoded,
# which meant the one genuine tradeoff in transcoding - time against file size -
# was not adjustable at all.
NVENC_PRESETS = [
    ("p1", "Fastest"), ("p2", "Faster"), ("p3", "Fast"), ("p4", "Balanced"),
    ("p5", "Slower"), ("p6", "Slowest"), ("p7", "Highest quality"),
]
CPU_PRESETS = [
    ("ultrafast", "Fastest"), ("veryfast", "Very fast"), ("faster", "Faster"),
    ("medium", "Balanced"), ("slow", "Slower"), ("slower", "Slowest"), ("veryslow", "Highest quality"),
]
H264_PROFILES = [
    ("high", "High - best compression, plays on anything modern"),
    ("main", "Main - a little larger, for genuinely old hardware"),
    ("baseline", "Baseline - largest, for very old phones and set-top boxes"),
]
HEVC_PROFILES = [("main", "Main - 8-bit, the compatible choice"),
                 ("main10", "Main 10 - 10-bit, keeps HDR-grade gradients, narrower support")]

ENCODER_OPTIONS: dict[str, dict] = {
    "h264_nvenc": {"presets": NVENC_PRESETS, "default_preset": "p4",
                   "profiles": H264_PROFILES, "default_profile": "high"},
    "hevc_nvenc": {"presets": NVENC_PRESETS, "default_preset": "p4",
                   "profiles": HEVC_PROFILES, "default_profile": "main"},
    "h264_qsv": {"presets": CPU_PRESETS, "default_preset": "medium",
                 "profiles": H264_PROFILES, "default_profile": "high"},
    "libx264": {"presets": CPU_PRESETS, "default_preset": "medium",
                "profiles": H264_PROFILES, "default_profile": "high"},
    "libx265": {"presets": CPU_PRESETS, "default_preset": "medium",
                "profiles": HEVC_PROFILES, "default_profile": "main"},
}

# 0 means "leave the picture alone", which is the default: re-scaling is a
# one-way loss and most libraries do not want it.
RESOLUTIONS = [
    (0, "Same as source", "No scaling. The safe answer unless you are short of space."),
    (2160, "4K (2160p)", "Only affects anything larger than 4K."),
    (1080, "1080p", "The usual ceiling. Shrinks 4K substantially; leaves everything else untouched."),
    (720, "720p", "Much smaller files, visibly softer on a big screen."),
    (480, "480p", "For phones and tiny storage."),
]


def hwaccel_args(encoder: str, max_height: int, enabled: bool = True) -> list[str]:
    """Decoder-side arguments, which go BEFORE -i.

    The encoder was always on the GPU; the decoder was not, and decoding 1080p
    H.264 in software is what actually pins a NAS CPU. Measured on a real
    episode: 21.3s of CPU became 3.9s, and the job got 45% faster, with a
    byte-identical output size.

    Two modes, because the frames have to live somewhere:

    * No resolution cap - the default - means no filter, so frames can stay in
      GPU memory end to end (`-hwaccel_output_format cuda`). Fastest.
    * With a cap, the scale filter runs on the CPU and cannot read GPU memory,
      so frames are decoded on the GPU and handed back to system RAM. Still
      about a third of the original CPU cost. (`scale_cuda` would keep them on
      the GPU, but it breaks precisely when ffmpeg falls back to software
      decoding, which is the case this has to survive.)

    Verified safe on a codec NVDEC cannot decode: ffmpeg silently falls back to
    software decoding rather than failing, so this needs no per-file logic.
    """
    if not enabled or not encoder.endswith("_nvenc"):
        return []
    if max_height:
        return ["-hwaccel", "cuda"]
    return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]


def audio_args(codec: str, bitrate: int, channels: int) -> str:
    """The audio half of an encode.

    `copy` keeps the original track bit-for-bit, which is free and lossless -
    but MP4 cannot carry DTS or TrueHD, so it fails on exactly the discs people
    most want untouched, and the caller falls back to AAC when it does.

    Channels defaults to stereo because that is what almost every TV and phone
    actually outputs, but 0 keeps a 5.1 mix intact - the downmix is silent
    otherwise, and someone with a receiver would never know it happened.
    """
    if codec == "copy":
        return "-c:a copy"
    parts = ["-c:a aac", f"-b:a {int(bitrate)}k"]
    if channels:
        parts.append(f"-ac {int(channels)}")
    return " ".join(parts)


def valid_option(encoder: str, kind: str, value: str) -> str:
    """A preset/profile for THIS encoder, or that encoder's default.

    Encoders do not share these vocabularies: NVENC speaks p1-p7 and libx264
    speaks ultrafast-veryslow, so a value left behind by a previous encoder
    would abort every job before the first frame.
    """
    opts = ENCODER_OPTIONS.get(encoder, {})
    allowed = [v for v, _ in opts.get(kind, [])]
    default = opts.get("default_preset" if kind == "presets" else "default_profile", "")
    return value if value in allowed else default


def scale_filter(max_height: int) -> str:
    """A -vf that caps height and NEVER upscales.

    min(ih,H) is the whole point: asking for 1080p with a 720p source must
    leave it at 720p, because upscaling costs space and invents nothing. The
    -2 keeps width even and proportional, which H.264 requires. The comma is
    escaped because ffmpeg's filter syntax uses commas to chain filters.
    """
    if not max_height:
        return ""
    return f"-vf scale=-2:min(ih\\,{int(max_height)})"

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
    preset: str = "",
    profile: str = "",
    max_height: int = 0,
    audio: str = "-c:a aac -b:a 192k -ac 2",
    hwaccel: list[str] | None = None,
) -> list[str]:
    """argv for one encode attempt.

    MP4 cannot carry PGS/VOBSUB image subtitles, so text subs are converted to
    mov_text and the caller retries without subtitles when that fails - a
    dropped subtitle is recorded as a warning, never a silent loss.
    """
    subs = "-map 0:s? -c:s mov_text" if with_subtitles else "-sn"
    filters = scale_filter(max_height)
    rendered = template.format(
        subs=subs,
        quality=quality,
        preset=preset or "medium",
        profile=profile or "high",
        filters=f"{filters} " if filters else "",
        audio=audio,
    )
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-y",
        # Decoder arguments only mean anything before the input they apply to.
        *(hwaccel or []),
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
