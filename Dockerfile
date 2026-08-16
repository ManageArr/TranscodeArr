# TranscodeArr - transcoding worker for a ManageArr stack.
#
# debian-slim + jellyfin-ffmpeg, NOT a CUDA base image. NVENC needs the driver
# userland (libnvidia-encode and friends) injected AT RUNTIME by the host's
# nvidia runtime - the CUDA toolkit at build time is a gigabyte of nothing.
# jellyfin-ffmpeg is the one widely-deployed ffmpeg carrying NVENC + QSV +
# VAAPI together, maintained by people who transcode for a living.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl gnupg ca-certificates python3 \
    && curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key \
      | gpg --dearmor -o /usr/share/keyrings/jellyfin.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/jellyfin.gpg] https://repo.jellyfin.org/master/debian bookworm main" \
      > /etc/apt/sources.list.d/jellyfin.list \
    && apt-get update && apt-get install -y --no-install-recommends jellyfin-ffmpeg7 \
    && apt-get purge -y curl gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/lib/jellyfin-ffmpeg/ffmpeg /usr/local/bin/ffmpeg \
    && ln -s /usr/lib/jellyfin-ffmpeg/ffprobe /usr/local/bin/ffprobe

# The nvidia runtime reads these to decide which driver libraries to inject.
# "video" injects libnvidia-encode; "compute" injects libcuda - and ffmpeg's
# NVENC initializes THROUGH CUDA, so without compute it loads and then dies at
# cuInit. Found the hard way on a real QNAP.
ENV NVIDIA_DRIVER_CAPABILITIES=compute,video,utility \
    NVIDIA_VISIBLE_DEVICES=all \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY app/ /app/

VOLUME /config
EXPOSE 8484

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
  CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8484/healthz',timeout=5)" || exit 1

CMD ["python3", "/app/main.py"]
