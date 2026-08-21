# TranscodeArr - a self-contained transcoding worker: watch, encode, verify,
# replace. Nothing else has to be running for it to do its job.
#
# debian-slim + jellyfin-ffmpeg, NOT a CUDA base image. NVENC needs the driver
# userland (libnvidia-encode and friends) injected AT RUNTIME by the host's
# nvidia runtime - the CUDA toolkit at build time is a gigabyte of nothing.
# jellyfin-ffmpeg is the one widely-deployed ffmpeg carrying NVENC + QSV +
# VAAPI together, maintained by people who transcode for a living.
#
# ponytail: the base image and jellyfin-ffmpeg7 are tag-pinned, not
# digest-pinned, so two builds of one commit can differ. Pin both digests when a
# build has to be reproducible - resolving them honestly needs a network round
# trip, and an invented digest is worse than an unpinned tag.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl gnupg ca-certificates python3 util-linux \
    && curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key \
      | gpg --dearmor -o /usr/share/keyrings/jellyfin.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/jellyfin.gpg] https://repo.jellyfin.org/master/debian bookworm main" \
      > /etc/apt/sources.list.d/jellyfin.list \
    && apt-get update && apt-get install -y --no-install-recommends jellyfin-ffmpeg7 \
    && apt-get purge -y curl gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/lib/jellyfin-ffmpeg/ffmpeg /usr/local/bin/ffmpeg \
    && ln -s /usr/lib/jellyfin-ffmpeg/ffprobe /usr/local/bin/ffprobe \
# setpriv is the whole mechanism the entrypoint drops root with. If a base
# image ever splits it out of util-linux, this fails the build instead of
# shipping an image that silently runs every ffmpeg as root again.
    && command -v setpriv > /dev/null

# The nvidia runtime reads these to decide which driver libraries to inject.
# "video" injects libnvidia-encode; "compute" injects libcuda - and ffmpeg's
# NVENC initializes THROUGH CUDA, so without compute it loads and then dies at
# cuInit. Found the hard way on a real QNAP.
ENV NVIDIA_DRIVER_CAPABILITIES=compute,video,utility \
    NVIDIA_VISIBLE_DEVICES=all \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY app/ /app/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# This repo is edited on Windows, where a checkout can carry CRLF - and a CR on
# the shebang line makes the kernel hunt for an interpreter named "sh\r".
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

# Declared down here so bumping a version does not invalidate the apt layer.
# The release workflow passes both; the defaults keep a local build working.
ARG VERSION=1.0.5
ARG REVISION=unknown

# VERSION is the LABELs below and nothing else. It used to also become
# ENV TRANSCODEARR_VERSION, which main.py preferred over its own constant -
# and an environment variable is exactly the part of a container that survives
# being rebuilt on a newer image. Container Station recreates a container from
# the env it recorded at create, so that ENV froze /healthz at whatever version
# the container was FIRST built with and no update could move it. main.VERSION
# is now the only answer, compiled in where nothing outside the image can
# reach it.
#
# This default and main.VERSION are still bumped together and a test enforces
# it: they are what the labels and /healthz respectively report, and an image
# whose label and /healthz disagree is the same lie in a different field.

# The description is what GHCR prints on the package page, so it is the first
# thing anyone reads about this image and it has to stand on its own: no stack,
# no companion service, no prerequisite beyond a media folder and this container.
LABEL org.opencontainers.image.title="TranscodeArr" \
      org.opencontainers.image.description="Self-hosted transcoding worker with a web UI: watches a media library, re-encodes to MP4 with NVENC, QSV or VAAPI via jellyfin-ffmpeg, and verifies every result before it replaces the original." \
      org.opencontainers.image.source="https://github.com/managearr/transcodearr" \
      org.opencontainers.image.url="https://github.com/managearr/transcodearr" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

VOLUME /config
EXPOSE 8484

# Reads PORT with the same default as main.py. Hardcoding 8484 here made every
# container that set PORT permanently unhealthy, and an orchestrator answers
# "unhealthy" by restarting it forever.
# Both schemes, http first because it is the common one: set tls_cert and
# tls_key and the server speaks ONLY https, so a single http probe fails on
# every check forever and a perfectly healthy container is restarted into the
# same state - the check itself becoming the outage. Certificate verification
# is off deliberately: the pair a LAN box has is the self-signed one the UI
# generated, and verifying a loopback probe against it fails a container that
# is fine. Nothing is trusted here anyway - the answer only has to prove the
# daemon is still answering on its own port.
# start-period covers the boot probes: a real one-second encode per encoder,
# then a real test encode per stored profile. That is the whole point of both -
# a listed encoder is not a working one - but it means the port opens late, and
# a start-period shorter than the probe restarts a container that is fine.
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s \
  CMD python3 -c "import os,urllib.request as u;u.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8484')+'/healthz',timeout=4)" \
   || python3 -c "import os,ssl,urllib.request as u;c=ssl.create_default_context();c.check_hostname=False;c.verify_mode=ssl.CERT_NONE;u.urlopen('https://127.0.0.1:'+os.environ.get('PORT','8484')+'/healthz',timeout=4,context=c)"

# The entrypoint drops to PUID:PGID and execs this, so CMD stays overridable.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python3", "/app/main.py"]
