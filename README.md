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

## API

Bearer token (`TRANSCODEARR_TOKEN`) on everything except `GET /healthz`.

| Route | What |
| --- | --- |
| `POST /jobs` `{path}` | Queue a file. Path must resolve inside `MEDIA_ROOTS`. 409 if already queued. |
| `GET /jobs?state=&limit=` | Recent jobs. |
| `GET /jobs/{id}` | One job: state, progress, encoder, warning/error, sizes. |
| `DELETE /jobs/{id}` | Cancel (queued: immediately; running: terminates the encode, source untouched). |
| `GET /healthz` | Encoder in use and why, queue depth, roots, version. Unauthenticated. |

`GET /` is a small status page.

## Configuration (env)

| Var | Default | Notes |
| --- | --- | --- |
| `TRANSCODEARR_TOKEN` | - | Required for the API; without it only the watcher runs. |
| `MEDIA_ROOTS` | `/media` | Colon-separated containment roots - nothing outside is ever touched. |
| `WATCH_ROOTS` | = MEDIA_ROOTS | What the scanner walks. Start narrow. |
| `PROCESS_UNHIDDEN` | `false` | Off = only dot-hidden files are processed. Sweeping the visible library into a re-encode is a decision, not a default. |
| `CONVERT_EXTENSIONS` | `.mkv,.avi,.m4v` | What gets transcoded; dot-hidden `.mp4` is revealed without re-encoding. |
| `SCAN_INTERVAL_SECONDS` / `STABLE_SECONDS` | `300` / `120` | Poll cadence and the size-stability window. |
| `QUALITY` | `24` | CQ/CRF value fed to the encoder template. |
| `TRASH_KEEP_DAYS` | `7` | How long replaced sources survive in `/config/trash`. |
| `FORCE_ENCODER` | - | Skip probing: `h264_nvenc`, `h264_qsv`, or `libx264`. |
| `VERIFY_DURATION_TOLERANCE` | `0.015` | How far output duration may drift from the source. |

## Tests

`python -m unittest discover tests` - the pure rules (path containment,
staging-name arithmetic, verification, stability, argv construction) with the
incident cases stated by name.
