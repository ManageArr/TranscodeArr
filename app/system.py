"""Host CPU, memory and GPU, read from inside the container.

**This needs no per-host code, and that surprises people.** `/proc/stat`,
`/proc/meminfo`, `/proc/loadavg` and `/proc/uptime` are not namespaced by
Linux, so a container reads the HOST's figures through them - the same on
QNAP, TrueNAS, Synology, Unraid or plain Docker. There is nothing vendor
specific to special-case.

Two real exceptions, both handled by degrading rather than guessing:

* Some LXC/LXD hosts mount **lxcfs**, which virtualizes those files to show
  the container's own limits instead of the host's. The numbers stay
  meaningful, they just describe a smaller machine, so nothing breaks.
* The **GPU** is the genuinely host-specific part, because it depends on how
  it was passed in: NVIDIA's runtime injects `nvidia-smi`, while Intel and AMD
  appear as `/dev/dri` and need entirely different tools. So the GPU is probed
  rather than assumed, exactly like the encoders are.

Every reader returns None instead of raising. A stats panel is a convenience,
and it must never be able to take down a transcoder.
"""

from __future__ import annotations

import os
import subprocess
import threading

_lock = threading.Lock()
_prev_cpu: tuple[int, int] | None = None  # (busy, total)


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def cpu() -> dict | None:
    """Host CPU use since the previous call.

    /proc/stat is cumulative, so a single read says nothing about *now* - the
    percentage is the delta between two reads. The first call after start has
    no previous sample and honestly reports None rather than inventing one.
    """
    raw = _read("/proc/stat")
    if not raw:
        return None
    for line in raw.splitlines():
        if line.startswith("cpu "):
            parts = [int(v) for v in line.split()[1:] if v.isdigit()]
            break
    else:
        return None
    if len(parts) < 4:
        return None
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)  # idle + iowait
    total = sum(parts)
    busy = total - idle
    global _prev_cpu  # noqa: PLW0603
    with _lock:
        prev = _prev_cpu
        _prev_cpu = (busy, total)
    percent = None
    if prev:
        d_busy, d_total = busy - prev[0], total - prev[1]
        if d_total > 0:
            percent = round(max(0.0, min(100.0, 100.0 * d_busy / d_total)), 1)
    return {"percent": percent, "cores": os.cpu_count(), "model": cpu_model()}


def cpu_model() -> str | None:
    raw = _read("/proc/cpuinfo") or ""
    for line in raw.splitlines():
        if line.lower().startswith("model name"):
            return line.split(":", 1)[1].strip()
    return None


def memory() -> dict | None:
    raw = _read("/proc/meminfo")
    if not raw:
        return None
    fields = {}
    for line in raw.splitlines():
        key, _, rest = line.partition(":")
        value = rest.strip().split(" ")[0]
        if value.isdigit():
            fields[key] = int(value) * 1024
    total = fields.get("MemTotal")
    if not total:
        return None
    # MemAvailable is the kernel's own estimate of what a new workload could
    # actually get, which is the honest number - "free" excludes reclaimable
    # cache and makes every busy NAS look full.
    available = fields.get("MemAvailable", fields.get("MemFree", 0))
    return {"total": total, "available": available, "used": total - available,
            "percent": round(100.0 * (total - available) / total, 1)}


def load() -> dict | None:
    raw = _read("/proc/loadavg")
    if not raw:
        return None
    parts = raw.split()
    try:
        one, five, fifteen = float(parts[0]), float(parts[1]), float(parts[2])
    except (IndexError, ValueError):
        return None
    cores = os.cpu_count() or 1
    return {"one": one, "five": five, "fifteen": fifteen, "cores": cores,
            "per_core": round(one / cores, 2)}


def uptime_seconds() -> float | None:
    raw = _read("/proc/uptime")
    try:
        return float((raw or "").split()[0])
    except (IndexError, ValueError):
        return None


GPU_FIELDS = ("name", "utilization.gpu", "utilization.memory", "memory.used", "memory.total",
              "temperature.gpu", "encoder.stats.sessionCount", "encoder.stats.averageFps")


def gpu() -> dict | None:
    """NVIDIA only, via the nvidia-smi the runtime injects.

    encoder.stats.sessionCount is the interesting one here: it is how many
    encode sessions the card is actually running, which is the honest check on
    whether raising "convert at once" bought anything.
    """
    try:
        r = subprocess.run(
            ["nvidia-smi", f"--query-gpu={','.join(GPU_FIELDS)}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    values = [v.strip() for v in r.stdout.strip().splitlines()[0].split(",")]
    if len(values) < len(GPU_FIELDS):
        return None

    def num(v):
        try:
            return float(v)
        except ValueError:
            return None

    return {
        "name": values[0],
        "percent": num(values[1]),
        "memory_percent": num(values[2]),
        "memory_used_mb": num(values[3]),
        "memory_total_mb": num(values[4]),
        "temperature_c": num(values[5]),
        "encoder_sessions": num(values[6]),
        "encoder_fps": num(values[7]),
    }


def other_gpu() -> str | None:
    """Intel/AMD present? Named, not measured - reading their utilization needs
    tools and privileges a media worker has no business holding."""
    try:
        if os.path.isdir("/dev/dri") and os.listdir("/dev/dri"):
            return "an Intel or AMD GPU is present (/dev/dri)"
    except OSError:
        pass
    return None


def snapshot() -> dict:
    return {
        "cpu": cpu(),
        "memory": memory(),
        "load": load(),
        "uptime_seconds": uptime_seconds(),
        "gpu": gpu(),
        "other_gpu": other_gpu(),
        # Said plainly, because a container reporting 64 GB of RAM it cannot
        # all use is confusing until you know these are the host's figures.
        "note": "Figures are the host machine's, read through /proc, which Linux does not namespace.",
    }
