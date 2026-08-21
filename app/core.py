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
        # rstrip because normpath leaves the filesystem root AS a separator: "/"
        # stays "/", so a bare `r + os.sep` asks whether the path starts with
        # "//" and nothing is ever inside "/". Every other root loses its
        # trailing separator to normpath already, so this changes nothing for
        # them - only the root directory, where the answer was wrong.
        if norm == r or norm.startswith(r.rstrip(os.sep) + os.sep):
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
# Where a replaced source is kept
# ---------------------------------------------------------------------------

# Dot-prefixed twice over on purpose: the media server ignores dot-directories,
# and so does the watcher's own walk, so a trashed source is never served and
# never picked up for a second conversion.
TRASH_DIRNAME = ".transcodearr-trash"


def trash_override_is_unsafe(override: str, roots: list[str]) -> bool:
    """Would this TRASH_DIR put the trash at or above a media root?

    The daemon prunes by walking every trash root and unlinking whatever is past
    the retention window, so a trash root that CONTAINS the library is a
    scheduled delete of the library. `/media/trash` is fine and supported;
    `/media` is not. The direction matters: asking the other question - is the
    override inside a root - refuses the supported case and waves the fatal one
    through.
    """
    return bool(override) and any(is_within(root, [override]) for root in roots)


def trash_destination(source: str, roots: list[str], override: str = "") -> str:
    """Where this source's safety copy belongs.

    Under the media root that CONTAINS the source, because same mount means the
    move is a rename that cannot half-finish. The old default put it in the
    config volume, which on a NAS is a different bind mount, so every replaced
    source became a full byte copy: 127 GB of them were measured sitting in the
    config share of the live deployment, next to the SQLite database. TRASH_DIR
    overrides the location and keeps the media-root-relative mirroring either
    way, so recovering a file is still a move back to where it came from.
    """
    for root in roots:
        if is_within(source, [root]):
            return os.path.join(override or os.path.join(root, TRASH_DIRNAME),
                                os.path.relpath(source, root))
    # Outside every root the mirroring has no anchor, so keep the copy beside
    # the file - still one filesystem, which is the property that matters.
    return os.path.join(override or os.path.join(os.path.dirname(source), TRASH_DIRNAME),
                        os.path.basename(source))


# ---------------------------------------------------------------------------
# Verification - the check whose absence truncated a library
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    duration: float | None
    video_streams: int
    audio_streams: int
    subtitle_streams: int
    # The FIRST video stream's pixel format, because -map 0:v:0 is what gets
    # encoded. Defaulted so a Probe built by hand stays a four-field call, and
    # so an ffprobe that does not say reads as "unknown" rather than "8-bit" -
    # see output_pix_fmt, where unknown means "change nothing".
    pix_fmt: str = ""


def parse_ffprobe(payload: dict) -> Probe:
    streams = payload.get("streams") or []
    fmt = payload.get("format") or {}
    duration = None
    try:
        duration = float(fmt.get("duration"))
    except (TypeError, ValueError):
        pass
    count = lambda kind: sum(1 for s in streams if s.get("codec_type") == kind)  # noqa: E731
    video = [s for s in streams if s.get("codec_type") == "video"]
    return Probe(
        duration=duration,
        video_streams=count("video"),
        audio_streams=count("audio"),
        subtitle_streams=count("subtitle"),
        pix_fmt=str(video[0].get("pix_fmt") or "") if video else "",
    )


def may_replace_target(before: tuple | None, now: tuple | None) -> bool:
    """Whether the file sitting at a job's visible target may be displaced.

    Both arguments identify a file - (inode, size, mtime) or None for "nothing
    there". `before` is what sat there when the job started; `now` is what sits
    there at the moment of the replace, hours of encoding later.

    Finding a file there is no longer fatal. It is usually the previous
    conversion of an episode an arr has re-imported at better quality, and
    refusing left those files failing every six hours forever with nothing on
    disk changing between attempts. The displaced file goes to the trash, not
    under os.replace, so it keeps the same 30 days every replaced source gets.

    What is still fatal is finding a DIFFERENT file there than the one this job
    decided to displace. That one arrived while the encode ran - an arr
    importing an upgrade mid-run lands on exactly this name - so it is newer
    than the source this job converted, and it has no other copy anywhere.
    """
    return now is None or now == before


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
    v1 keeps it to "an .mp4 needs no transcode", which is the behavior the
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


def in_retry_cooldown(last_failure_at: float | None, now: float, cooldown_hours: float) -> bool:
    """Should the WATCHER leave a path that recently failed alone?

    Queued work is deduped per path, but a finished failure is not: a file that
    cannot be converted at all - a truncated source, audio nothing here can
    read - was re-queued on the next scan and every scan after it, burning a
    full ffmpeg attempt on the GPU every few minutes forever. 0 hours retries as
    soon as the watcher sees it again; queueing a file from the API is somebody
    asking for it now and ignores this either way.
    """
    if not cooldown_hours or last_failure_at is None:
        return False
    return now - last_failure_at < cooldown_hours * 3600


def is_stalled(seconds_since_progress: float, timeout_minutes: float) -> bool:
    """Has an encode gone quiet for long enough to be dead rather than slow?

    Measured against ffmpeg's -progress output, which is emitted roughly every
    500ms of WALL clock however slow the encode itself runs - so silence means
    the process is wedged, typically on an SMB/NFS share that went away, and
    never that the file is big. 0 disables the watchdog.
    """
    if not timeout_minutes:
        return False
    return seconds_since_progress > timeout_minutes * 60


# ---------------------------------------------------------------------------
# The conversion window - when the worker may CLAIM a job
# ---------------------------------------------------------------------------
#
# The window gates the CLAIM and never terminates a running encode. A closing
# window drains: the in-flight job finishes, is verified and revealed like any
# other, and nothing new is claimed. Killing at the boundary would throw away a
# 40GB remux at 90% and buy back the same hours the next night.
#
# The watcher goes on queueing while the window is shut, because queueing costs
# nothing and it means the queue is already built when the window opens. A
# paused box with a growing queue is working, not broken.
#
# These take minutes since LOCAL midnight, which the caller derives from the TZ
# the container was given. That is the trap worth stating twice: the container
# is UTC by default and the NAS this runs on is EDT, so a window typed as
# 01:00-06:00 runs at 21:00 local unless TZ is set - four hours wrong, every
# night, with nothing on screen that looks wrong.

_WINDOW_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def parse_window(text: str) -> tuple[int, int] | None:
    """"HH:MM-HH:MM" as (start, end) minutes since midnight. None means always.

    Malformed text raises instead of degrading to "always", because that is the
    expensive direction to be wrong in: a typo in the one setting whose whole
    job is "do not encode while I am watching television" would become a box
    that encodes at 8pm and never says why.
    """
    if not text or not text.strip():
        return None
    match = _WINDOW_RE.match(text)
    if not match:
        raise ValueError(
            f"'{text.strip()}' is not a time window. Write two 24-hour times joined by a hyphen, "
            "for example 22:00-06:00, or leave it empty to convert at any hour."
        )
    start_h, start_min, end_h, end_min = (int(g) for g in match.groups())
    if start_h > 23 or end_h > 23 or start_min > 59 or end_min > 59:
        raise ValueError(f"'{text.strip()}' has a time outside 00:00-23:59.")
    return start_h * 60 + start_min, end_h * 60 + end_min


def within_window(now_minutes: int, window: tuple[int, int] | None) -> bool:
    """Is this local minute inside the window? Start inclusive, end exclusive.

    Spanning midnight is the normal case here rather than the edge one - "encode
    overnight" is 22:00-06:00 - so the naive `start <= now < end` would hold that
    window shut for all twenty-four hours while the queue grew forever.

    Equal start and end is a FULL day, not a zero-length window. Empty text
    already means always, so reading it as zero-length would give this setting a
    second way to say "never convert" and no way at all to say "from 02:00 round
    to 02:00" - and "never" belongs to the Stop button, which says so on screen,
    not to a window that reads like a schedule.
    """
    if window is None:
        return True
    start, end = window
    if start == end:
        return True
    if start < end:
        return start <= now_minutes < end
    return now_minutes >= start or now_minutes < end


def next_window_change(now_minutes: int, window: tuple[int, int] | None) -> int:
    """Minutes until the window next opens or closes. 0 when it never will.

    So the UI can say "starts converting in 3h 12m" rather than only "paused".
    A paused worker with a queue that keeps climbing is the most alarming thing
    this feature does, and the countdown is what makes it read as a schedule
    instead of as a worker that has died quietly.
    """
    if window is None or window[0] == window[1]:
        return 0
    target = window[1] if within_window(now_minutes, window) else window[0]
    return (target - now_minutes) % 1440


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


def hwaccel_args(encoder: str, max_height: int, enabled: bool = True, pix_fmt: str = "") -> list[str]:
    """Decoder-side arguments, which go BEFORE -i.

    The encoder was always on the GPU; the decoder was not, and decoding 1080p
    H.264 in software is what actually pins a NAS CPU. Measured on a real
    episode: 21.3s of CPU became 3.9s, and the job got 45% faster, with a
    byte-identical output size.

    Two modes, because the frames have to live somewhere:

    * No CPU-side filter - the default - means frames can stay in GPU memory
      end to end (`-hwaccel_output_format cuda`). Fastest.
    * With one, the filter cannot read GPU memory, so frames are decoded on the
      GPU and handed back to system RAM. Still about a third of the original
      CPU cost. (`scale_cuda` would keep them on the GPU, but it breaks
      precisely when ffmpeg falls back to software decoding, which is the case
      this has to survive - measured, it also ran a 10-bit 1080p source at
      0.6x against 2.5x for the hand-back below.)

    Both a height cap and a pixel-format narrowing are CPU-side filters, and
    passing `-pix_fmt` while frames are still in GPU memory does not fail
    loudly - ffmpeg reports "Impossible to convert between the formats" and the
    encoder thread dies with the same -22 the narrowing exists to prevent.

    Verified safe on a codec NVDEC cannot decode: ffmpeg silently falls back to
    software decoding rather than failing, so this needs no per-file logic.
    """
    if not enabled or not encoder.endswith("_nvenc"):
        return []
    if max_height or pix_fmt:
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


# A pixel format wider than 8 bits, read off the depth suffix ffprobe reports:
# yuv420p10le, p010le, gray16le. Deliberately not a substring test for "10" -
# yuv410p and yuv411p are both 8-bit and would match one.
HIGH_DEPTH_PIX_FMT = re.compile(r"(10|12|14|16)(le|be)$")

# The profiles this worker offers that can carry more than 8 bits. Every other
# profile is a promise about bit depth that nothing was enforcing, and on a
# 10-bit source h264_nvenc answers "10 bit encode not supported / No capable
# devices found" and exits -22 before the first frame. ffmpeg does not rescue
# it: NVENC advertises one shared pixel-format list for H.264 and HEVC, so p010
# is accepted during format negotiation and only refused at encoder init.
#
# Only what ENCODER_OPTIONS actually lists belongs here - valid_option means
# nothing else ever reaches output_pix_fmt, so H.264's high10 would be a rule
# that never fires. A test holds that invariant, so adding high10 to the UI
# without adding it here fails loudly rather than re-opening this bug.
TEN_BIT_PROFILES = {"main10"}


def output_pix_fmt(profile: str, source_pix_fmt: str) -> str:
    """The -pix_fmt this encode needs, or "" to leave ffmpeg's negotiation alone.

    Only ever NARROWS, and only when the source is wider than the profile can
    hold. An 8-bit source needs no flag, and setting one anyway would cost the
    fully-on-GPU decode path in hwaccel_args for nothing - a pixel conversion
    is a CPU filter, and a CPU filter cannot read frames that stayed in GPU
    memory. An unreadable source format is left alone for the same reason.
    """
    if not source_pix_fmt or profile in TEN_BIT_PROFILES:
        return ""
    return "yuv420p" if HIGH_DEPTH_PIX_FMT.search(source_pix_fmt) else ""


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


# The profiles TranscodeArr ships with: one per encoder, named for the choice
# someone is actually making rather than for the codec they have never heard of.
# Only the name lives here - quality, preset and codec profile are read from
# ENCODER_INFO and ENCODER_OPTIONS below, because a second hardcoded copy of
# "23" is a number that drifts from the encoder's own recommendation the first
# time anybody tunes one and not the other.
SHIPPED_PROFILE_NAMES: dict[str, str] = {
    "h264_nvenc": "Balanced, on the GPU",
    "h264_qsv": "Quick Sync, on Intel graphics",
    "hevc_nvenc": "Half the size, on the GPU",
    "libx264": "Smallest H.264, on the CPU",
    "libx265": "Smallest of all, on the CPU",
}


def shipped_profile(encoder: str) -> dict:
    """The stored fields for one shipped profile - derived, never duplicated.

    Audio is AAC stereo for all five: it is the combination every client can
    direct play, and someone who wants their 5.1 kept has to say so, because a
    silent downmix is the one audio mistake nobody notices until the receiver
    stays quiet.
    """
    opts = ENCODER_OPTIONS.get(encoder, {})
    return {
        "name": SHIPPED_PROFILE_NAMES[encoder],
        "encoder": encoder,
        "quality": recommended_quality(encoder),
        "preset": opts.get("default_preset", ""),
        "profile": opts.get("default_profile", ""),
        "max_height": 0,
        "audio_codec": "aac",
        "audio_bitrate": 192,
        "audio_channels": 2,
    }


# ---------------------------------------------------------------------------
# Throttling - keeping one encode from starving the media server
# ---------------------------------------------------------------------------


def throttle_prefix(nice: int, idle_io: bool) -> list[str]:
    """The argv that wraps ffmpeg with nice and ionice. Empty when neither is on.

    A list and never a shell string: everything downstream of this prefix is an
    operator-supplied path, and a string would need quoting to survive a file
    name with a space in it - which is most of them.

    ionice gets -t so an IO scheduler that refuses the idle class execs ffmpeg
    anyway. Without it, switching a throttle on would fail every job at spawn
    with a nonzero exit and no message resembling an encoding error.

    Niceness is clamped to 0-19, only ever nicer. A negative value asks for MORE
    CPU than the media server this is supposed to yield to, which is the exact
    opposite of why the setting exists, and it is the one direction that needs
    privilege the container may genuinely have.
    """
    prefix: list[str] = []
    if idle_io:
        prefix += ["ionice", "-t", "-c", "3"]
    level = max(0, min(19, int(nice or 0)))
    if level:
        prefix += ["nice", "-n", str(level)]
    return prefix


def thread_args(encoder: str, threads: int) -> list[str]:
    """-threads for the SOFTWARE encoders, empty for the hardware ones.

    libx264 and libx265 take every core they can see, which is what makes a
    CPU-only box stutter mid-film while a job runs. NVENC and QSV do the work on
    the chip, where this flag caps a few coordination threads and buys nothing,
    so passing it there would be a knob that only appears to do something.

    Which encoders are software is read from ENCODER_INFO rather than listed
    again, because a second copy of that list drifts the first time an encoder
    is added to one of them and not the other.
    """
    info = ENCODER_INFO.get(encoder)
    count = int(threads or 0)
    if count < 1 or not info or info["hardware"]:
        return []
    return ["-threads", str(count)]


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
    threads: list[str] | None = None,
    pix_fmt: str = "",
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
        # Given by the caller as output_pix_fmt(profile, source) exactly as
        # hwaccel is, and empty for almost every job: it only appears when the
        # source carries more bits than the chosen profile can, which the
        # encoder would otherwise refuse at init rather than convert.
        *(["-pix_fmt", pix_fmt] if pix_fmt else []),
        # Given by the caller as thread_args(encoder, n) exactly as hwaccel is,
        # because the encoder name is what decides whether it means anything and
        # this function is handed a template rather than an encoder. It sits
        # after -i on purpose: the same flag before the input caps the DECODER,
        # which is not the half pinning the cores.
        *(threads or []),
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


# Everything ffmpeg says on the way out that is not a reason. One layer says
# what went wrong; every layer above it restates the same errno, and then the
# stats epilogue runs. Keeping the raw tail kept only the restatements, so a
# card that cannot encode 10-bit and a driver that never initialised both
# reduced to "-22 (Invalid argument)" - identical text for the two failures a
# person reading a failed job most needs told apart.
_RESTATEMENT = re.compile(
    r"(Task finished with error code|Terminating thread with return code"
    r"|Last message repeated|Qavg:|^frame=|^\s*$|^Conversion failed!)")
_POINTER = re.compile(r" @ 0x[0-9a-f]+")


def error_summary(lines: list[str], keep: int = 6, width: int = 600) -> str:
    """What an ffmpeg failure actually said, oldest line first.

    The other half of the noise is the input dump, which is what pushed the
    real diagnostics out of an eight-line window in the first place. It is
    dropped by indentation: ffmpeg prints stream metadata indented under its
    "Input #0" header and its own diagnostics hard against column 0, and that
    is the only structural separator between the two - but it is stable across
    every codec, which a keyword list would not be.

    Address pointers are stripped: they carry nothing, they eat the width, and
    leaving them in gave the same failure a different error string on every
    attempt, which is what stopped the job list from showing that forty files
    were failing for one reason.

    Falls back to the raw lines if filtering leaves nothing, because an empty
    error field is the silence this worker exists to remove.
    """
    said = [_POINTER.sub("", ln).rstrip() for ln in lines
            if ln[:1] not in (" ", "\t") and not _RESTATEMENT.search(ln)]
    return "\n".join((said or [ln.rstrip() for ln in lines])[-keep:])[:width]
