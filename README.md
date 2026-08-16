# TranscodeArr

The transcoding worker for a ManageArr media stack. Radarr/Sonarr import media
hidden behind a leading dot (Jellyfin ignores dot-prefixed files); TranscodeArr
finds it, converts it to MP4 with hardware encoding where the box has it,
**verifies the result**, and only then reveals it to the media server. The
source is never deleted - it outlives its replacement in a pruned trash.

Successor to a hand-run Python watcher, and shaped by its failure: that script
trusted `mtime` for "is this file finished copying" (imports preserve the
release's own timestamp, so every file passed instantly), transcoded whatever
bytes existed, and deleted the source on exit code 0. Thirty-six movies in a
real library are permanently short because of that combination. Every design
rule below exists to make that class of loss impossible:

- **Readiness is size-stability**, never mtime: a file is only touched when its
  size has held still across a real interval.
- **Exit code 0 is not verification.** The output must ffprobe as video, keep
  its audio, and match the source duration within 1.5% before it may replace
  anything.
- **Nothing is deleted.** The source moves to `/config/trash` (mirrored paths,
  pruned after `TRASH_KEEP_DAYS`).
- **No visible partial files, ever.** Encodes go to a dot-hidden `.tapart`
  staging name in the same directory (same filesystem, so `os.replace` is
  atomic), become a hidden final, then a single rename is the reveal.
- **Two-writers guard:** if the source changed size during the encode (an arr
  upgraded it mid-run), the encode is discarded, never the newer source.
- **Text subtitles are carried** into MP4 as `mov_text`; when a source's
  subtitles cannot be (PGS and friends), the job retries without them and says
  so in a warning rather than silently dropping them.

Design prior art: [Cdarr](https://github.com/jbreuer95/cdarr) (MIT, Jelle
Breuer) proved the hide-then-reveal pattern with arr webhooks in 2020;
TranscodeArr is a fresh implementation of that idea, not a fork - Cdarr was
CPU-only Laravel/PHP and had been unmaintained for years.

## Running

See `docker-compose.example.yml`. The queue is SQLite in `/config`; one worker
on purpose (one disk, one NVENC session budget - concurrency here is a
throughput regression disguised as a feature). On restart, interrupted jobs are
marked failed, their staging files removed, and the watcher simply finds the
untouched source again.

Hardware encoding is probed at boot with a real one-second encode - a listed
encoder is not a working one - falling back NVENC -> QSV -> libx264, and
`/healthz` reports which one won and why. On QNAP Container Station the NVIDIA
runtime is registered as `nvidia-runtime`.

## The web interface

`GET /` is the whole application: jobs with live progress, watched folders with
a folder picker that only shows what the container can actually see, conversion
rules, Radarr/Sonarr connections, and API keys. No build step and no CDN - a NAS
container that needs npm to draw its own settings screen is one that stops
drawing it the day a registry goes down.

Sign in with any API key. The first one is `TRANSCODEARR_TOKEN` from the
container environment; after that, mint named keys in the UI and give each
consumer its own revocable one.

## Configuration

Settings are edited in the UI and stored in the database. **A stored value wins
over the environment from then on** - the env var is only the seed for a key
nobody has set yet. The other way round means every `docker run` with a stale
env silently reverts what someone changed in the UI, and they find out when the
library has been re-encoded at the wrong quality. Each field shows whether it is
saved here, coming from the container, or still the built-in default.

Two things stay environment-only, on purpose:

| Var | Default | Why it cannot move |
| --- | --- | --- |
| `MEDIA_ROOTS` | `/media` | Names the volume mounts themselves. Editing it at runtime would point the worker at paths the container cannot see. Nothing outside these roots is ever touched, browsed or queued. |
| `TRANSCODEARR_TOKEN` | - | The bootstrap key. Without it, and with no minted keys, there is no way into a fresh container. |

Everything else - watched folders, scan interval, stability window, extensions,
skip rules, quality, duration tolerance, forced encoder, trash retention - is in
the UI, and its env var (`WATCH_ROOTS`, `QUALITY`, `TRASH_KEEP_DAYS`, ...) still
works to seed a first boot.

### Skip rules

A file whose name matches a skip rule is **revealed in its own container rather
than re-encoded**. Arr naming puts the quality in the file name, so `Remux` is
usually the only rule worth writing: it keeps disc-quality copies whole while
still un-hiding them for the media server, which plays MKV perfectly well.

## Radarr / Sonarr

Optional, and only ever two calls: read the library list, and ask one title to
rescan. Nothing writes to the arr's database.

It matters because TranscodeArr replaces files in place, and neither Jellyfin
nor an arr notices a same-name in-place replacement on its own
(jellyfin#13565, closed "not planned") - so without it the whole stack keeps
publishing the old file's codec, bitrate and runtime for a file that no longer
exists in that form, and makes direct-play decisions off it.

Each connection carries a path mapping, because the arr and this container mount
the same directory in different places (Sonarr's `/tv` is this container's
`/media/TV`). A file outside a connection's root is simply not that connection's
business, which is how two arrs coexist without guessing.

## API

Bearer token on everything except `GET /healthz` and the page itself.

| Route | What |
| --- | --- |
| `POST /jobs` `{path}` | Queue a file. Path must resolve inside `MEDIA_ROOTS`. 409 if already queued. |
| `GET /jobs?state=&limit=` | Recent jobs (capped at 200). |
| `GET /jobs/{id}` | One job: state, progress, encoder, warning/error, sizes, rescan result. |
| `DELETE /jobs/{id}` | Cancel (queued: immediately; running: terminates the encode, source untouched). |
| `GET /healthz` | Encoder in use and why, queue depth, roots, version. Unauthenticated. |
| `GET`/`PUT` `/api/settings` | Every setting, its current value, and where that value came from. |
| `GET`/`POST` `/api/tokens`, `DELETE /api/tokens/{id}` | API keys. The raw key is returned once; only its hash is stored. |
| `GET`/`POST` `/api/arrs`, `PUT`/`DELETE /api/arrs/{id}`, `POST /api/arrs/test` | Radarr/Sonarr connections. |
| `GET /api/fs?path=` | Directories under the media roots, for the folder picker. Containment is checked on the resolved path. |

## Tests

`python -m unittest discover tests` - the pure rules (path containment,
staging-name arithmetic, verification, stability, argv construction) with the
incident cases stated by name.

## Ops notes from the first real deployment (QNAP, NVIDIA T1000)

- Container Station registers the NVIDIA runtime as `nvidia-runtime`, not
  `nvidia`. `NVIDIA_DRIVER_CAPABILITIES` must include `compute` - NVENC loads
  without it and then fails at `cuInit`.
- On a long-uptime NAS, `cuInit` can fail with `CUDA_ERROR_NOT_INITIALIZED`
  while `nvidia-smi` works fine. dmesg shows `NV_ERR_NO_MEMORY` from the UVM
  fault-buffer allocation: kernel memory is too fragmented to hand the driver
  the contiguous pages it needs (observed after 101 days of uptime, with the
  high-order page pools empty). Fix without a reboot, as root:
  `sync; echo 3 > /proc/sys/vm/drop_caches; echo 1 > /proc/sys/vm/compact_memory`
  This also un-breaks hardware transcoding for every other container on the
  box - a Jellyfin on the same host had been silently falling back to CPU.
- The boot probe exists precisely because of the above: a listed encoder is
  not a working one, and `/healthz` says which encoder actually won and why.
