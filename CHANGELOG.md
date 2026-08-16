# Changelog

All notable changes to TranscodeArr are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Nothing has been published yet, so this file starts at the work that makes the
first public release possible. `1.0.0` is the first tag and the first image on
`ghcr.io/managearr/transcodearr`; the `0.9.1` section below was written as that
first release and was never published as an image of its own. The numbering
starts at `0.9.1` rather than `0.9.0` because a private `0.9.0` image has been
running on real media since 2026-08-15, and shipping different code under a
version somebody is already running makes `/healthz` report a version that is
not the running build - the exact failure the version field exists to prevent.

Entries are grouped by what they mean for someone running this, not by which
file moved.

`1.0.0` is the release to run. Everything in the `0.9.1` section below is in it
and is still true; that section is kept because the reasoning behind each rule
is the point of this file, and 1.0.0 changed almost none of it.

## [1.0.0] - 2026-08-16

### What 1.0.0 means here

Not "finished", and not a rewrite. It is a promise about the API, made now that
there is something worth building against:

- **The HTTP API is frozen.** Every route, every request shape and every
  response field documented in the README is stable for the whole of the 1.x
  line. Fields may be **added**; nothing that is there today is renamed,
  retyped or removed before 2.0. That includes the job object's fifteen fields,
  the `{"error": "..."}` shape on every failure, and the five job states -
  `queued`, `running`, `done`, `failed`, `cancelled` - which are a stored
  database value as well as a wire value and are not being re-spelled for
  anybody's taste in English.
- **The bearer-token model is frozen.** `Authorization: Bearer` with an API key,
  a session token or the bootstrap token, interchangeably, on every route except
  `GET /healthz` and `POST /api/login`. **No cookie will be introduced in 1.x**;
  that is the reason this service has no CSRF surface and it is a compatibility
  promise, not an implementation detail.
- **Settings keys and their env vars are frozen.** A key documented in the
  README's settings table keeps its name and its meaning. New settings arrive
  with defaults that preserve existing behavior. One key changed name and
  meaning on the way in to this release - `process_unhidden` became
  `hidden_only`, see Changed - and it is the last one that will: from here a
  rename is a 2.0 conversation.
- **The backup format is versioned.** `format: 1` is what 1.x writes and reads.
  A backup written by a newer build is refused rather than half-applied.
- **What is not frozen:** log lines, the HTML page, the database schema, and the
  boot sequence. The page and the schema are internal, and pinning them would
  freeze this project's ability to keep the queue safe.
- **The deprecated root aliases are still here.** See Deprecated below. They
  were supposed to go at 1.0 and they are not going at 1.0.

### Added

- **A Start/Stop switch, and a daily convert window.** Converting is gated at
  the moment a worker **claims** a job, never by terminating one. Press Stop, or
  let a window close, and the encode in flight runs to completion, is verified
  and is revealed exactly as it would have been - a 40GB remux at 90% is never
  thrown away to honor a button press. What stops is the next claim.
  - The **watcher keeps queueing while stopped**. Queueing costs one SQLite row,
    and doing it while the gate is shut means the queue is already built the
    moment it opens. A stopped box with a growing queue is this working, and it
    is said in the UI, in the settings help and in the README because it is the
    single most likely thing about this to look broken.
  - `convert_window` is one daily range, `HH:MM-HH:MM`, empty meaning always,
    and it **spans midnight** (`22:00-06:00`) because overnight is when a NAS is
    free. Start inclusive, end exclusive. A malformed window is refused when you
    save it rather than silently read as "always".
  - There is deliberately no way to spell "never". Empty means always, and the
    Stop button is what "never" is - it says so on screen, which a blank text
    field never could.
  - **Reveals are gated too**, so a hidden `.mp4` that only needs renaming waits
    for the window like anything else. That follows from gating the claim, and
    it is documented rather than discovered.
- **`auto_start`, and an honest statement of what it costs.** Run state lives in
  memory and is decided at boot by this setting: on, the container boots
  converting; off, it boots paused and waits for a human. **A manual pause does
  not survive a restart while auto start is on** - that is what auto start
  means, and a box that came back from a power cut still paused from a
  maintenance window three weeks ago is the worse failure.
- **`TZ` is now load-bearing, and treated as such.** The container is UTC unless
  `TZ` is set, so a window typed `01:00-06:00` by somebody in US Eastern runs
  21:00-02:00 their time - all night, every night, with every clock in the UI
  agreeing with itself and nothing looking wrong. It cannot be validated, so it
  is made visible: the zone and the container's current local time are printed
  next to the window everywhere it is shown or logged, in the UI, in
  `GET /api/control`, in `/healthz` and in the boot log. `tzdata` was already in
  the image; `TZ` is now a first-class variable in the compose example with a
  real value and the failure spelled out.
- **An admin login: a password exchanged for a bearer token, not a cookie.**
  `POST /api/login` takes a username and password and returns a session token in
  the **response body**; the page holds it exactly as it already held an API key
  and sends it as `Authorization: Bearer`. No cookie is set anywhere, which is
  precisely why this service still has no CSRF surface, and there is one
  credential model rather than a header path and a cookie path that disagree.
  - Passwords are hashed with stdlib `hashlib.scrypt` and a per-password random
    salt, with the cost parameters stored per row so they can be raised later.
    No password is stored, logged or returned; no route returns a hash.
  - **Sessions are their own table**, storing only a hash, with an expiry
    (`session_days`, 30 by default) and a last-used stamp, listed in the UI and
    revocable individually and on logout. Signing out with an API key in the
    header is refused rather than obeyed - a button labeled "sign out" that
    revoked the key an integration authenticates with would take it down.
  - **Failed logins back off**: three free attempts, then 2s, 4s, 8s, capped at
    five minutes, answered `429` with `Retry-After`. The right password waits
    too, because a backoff a correct guess can walk past is not one. A wrong
    username and a wrong password give the same message. The password-change
    route shares that counter deliberately, so it cannot be the unthrottled
    oracle beside the throttled one.
  - **The bootstrap token still gets in on a fresh container**, and that is how
    the first admin is created. Minted API keys keep working unchanged, since
    that is what every non-browser client authenticates with.
  - **`TRANSCODEARR_RESET_ADMIN`, the way back in after a forgotten password.**
    Set it in the environment to `1`, `true`, `yes` or `on` and the next boot
    deletes the admin account and every session, putting the container back in
    the state it shipped in: no admin, so the bootstrap token gets you in and the
    page offers creating an account. It **deletes rather than resetting to a
    temporary password**, because a temporary value has to be communicated
    somehow and every available channel - the log, an environment variable, a
    route's response - writes it somewhere it outlives the recovery. **Minted API
    keys survive**, so a password recovery does not become an outage for the
    software authenticating with them. It is environment-only, since a stored
    setting outranks the environment here and every route that could change one
    needs the login you have just lost, and it grants nothing new: setting an
    environment variable on this container already means reading
    `TRANSCODEARR_TOKEN` out of that same environment and writing the config
    volume the database lives in. The boot log names the account it deleted, and
    warns on **every** boot while the flag is still set, because left set on a
    container that restarts by itself it would delete the replacement account
    too and leave the box with no login at all.
- **Optional built-in HTTPS, with a reverse proxy documented as the better
  answer.** Set `tls_cert` and `tls_key` and the listening socket is wrapped
  with a stdlib `ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)`; leave both empty and
  it serves plain HTTP exactly as before. `X-Forwarded-Proto` is honored, so a
  proxy that already terminates TLS is understood.
  - **`POST /api/tls/selfsigned`** generates a pair with the `openssl` already
    in the image, into `/config/tls/`, with a `subjectAltName` and the key at
    mode `600`. It never overwrites an existing pair - that path is exactly
    where a real certificate would have been put.
  - A **clear-text warning** is logged when an admin password is set, the server
    is plain HTTP, and nothing indicates a proxy: at boot, and again on any
    login that actually arrives over plain HTTP, which is the only moment a
    proxy is genuinely detectable.
  - **The image's `HEALTHCHECK` now tries both schemes**, http first with an
    https fallback that skips verification. Turning TLS on used to be a way to
    mark a perfectly healthy container unhealthy forever.
- **A job completion webhook.** `webhook_url` is POSTed a JSON summary when a
  job reaches `done` or `failed`. It runs after the media is already correct on
  disk, from a background thread with a 10 second timeout, and every exception
  in both halves is swallowed and logged: **it cannot fail a job, block one or
  take the worker down**, the same rule the arr rescan already followed.
  - The payload carries `event`, `version`, `sent` and `job` - and `job` is
    **exactly** the object `GET /api/jobs` returns, from the same function, so
    the two cannot drift. `log_tail` is deliberately absent, by the same rule
    that keeps it out of the job list.
  - `webhook_secret` adds `X-TranscodeArr-Signature: sha256=<hmac>` over the
    **exact bytes sent**, so a receiver verifies what it was sent rather than
    what it re-serialized.
  - It reuses the arr client's link-local guard rather than a second copy of the
    rule, and its no-redirects opener - a `302` would replay the POST at an
    address the guard never inspected, which would make the check decorative.
- **Config backup and restore.** `GET /api/backup` returns settings, custom
  profiles and arr connections as one stamped JSON document; `POST /api/restore`
  validates and applies it, returning a line per thing that changed.
  - **No secret ever leaves**: no arr API key, no token or session hash, no
    password hash, no webhook signing secret. A backup is a file people attach
    to a forum post.
  - Job history and the trash are not configuration and are not included.
  - A backup written by a newer version is refused. The version comparison is
    numeric, because as text `0.10.0` sorts below `0.9.1` and a string compare
    would wave through exactly the newest backups.
  - A restore **never touches the active profile**, brings every restored
    profile back **untested** so it cannot be activated until a real encode
    passes here, and brings arr connections back keyless and disabled. Ids
    travel, so restoring twice updates rather than duplicating.
- **Throttling for the ffmpeg process:** `encode_nice` (0-19), `encode_idle_io`
  (`ionice -c 3`) and `encode_threads` (a cap for the software encoders). This
  matters most on a CPU-only box, where a libx265 job otherwise pins every core
  and the first anyone hears about it is playback stuttering while their own
  library is being converted. Both binaries were already in the image.
  - `ionice` gets `-t`, so a scheduler that refuses the idle class execs ffmpeg
    anyway instead of failing every job at spawn.
  - Both wrappers **exec** ffmpeg in place rather than forking it, so cancel,
    the stall watchdog and `docker stop` all still reach the encoder.
  - The recorded command line is written after the prefix is applied, so
    `log_tail` says what actually ran.
  - `-threads` lands between `-i` and the output path, or it would cap the
    decoder instead of the encoder. Hardware-vs-software is read from the
    encoder's own flag rather than a second list that could drift.
- **`GET /metrics` in the Prometheus text exposition format**: queue depth, jobs
  by state, bytes saved, encode throughput, run state, uptime and build info.
  It needs the bearer token like every other route - Prometheus reads one
  natively, so the secure default costs one line in a scrape config, and leaking
  the size and shape of somebody's library to whoever finds the port is not
  worth avoiding it. Every job state is emitted even at zero, so an alert
  written against it cannot fire late. `saved_bytes` is a gauge and not a
  counter, because `keep_history_days` prunes rows and a counter that goes down
  reads as a restart.
- **Ten new settings**, all seedable from the environment: `AUTO_START`,
  `CONVERT_WINDOW`, `WEBHOOK_URL`, `WEBHOOK_SECRET`, `ENCODE_NICE`,
  `ENCODE_IDLE_IO`, `ENCODE_THREADS`, `TLS_CERT`, `TLS_KEY`, `SESSION_DAYS`.
  Four new UI groups to hold them: Schedule, Notifications, Performance,
  Security.
- **A secret flag on settings.** `webhook_secret` comes back from
  `GET /api/settings` as `********` rather than its value, and posting the mask
  back keeps the stored value while an empty string clears it. Without that, the
  webhook signing secret shipped to every browser that opened the settings tab.
- **An answer to "it is running and converting nothing".** A scan that walks the
  watched folders and finds nothing it may queue now says so, with the reason,
  instead of leaving a healthy container and a quiet log. That used to be the
  single most likely first experience of this worker, and the default flip above
  is what removes it for a new install. What is left is the deliberate case:
  `hidden_only` on with nothing actually writing the dot. When a scan finds files
  it would have converted and **not one** of them is hidden, the count goes in
  the log with the two ways forward, onto the Queue tab in place of the
  empty-queue hint, and into the run-state object as `visible_only_skipped`, so
  `GET /api/control` and an authenticated `GET /healthz` carry it too. It reports
  the total failure rather than each skipped file, because one hidden file
  anywhere proves the convention is in use and a skip is then the setting
  working. Silence is the failure mode this project exists to remove, and it does
  not get an exemption for being our own.
- **A SECURITY.md**, so a vulnerability has somewhere to go that is not a public
  issue, and the README points at it alongside how to file an ordinary bug.

### Changed

- **`process_unhidden` is now `hidden_only`, and the default means the
  opposite.** A breaking configuration change, made before 1.0.0 was published
  and therefore before the settings freeze above binds it. **An existing install
  keeps converting exactly what it was converting** - the migration is under
  Upgrading below, and it is the half of this entry that decides whether the
  upgrade is safe.
  - **What it was:** `process_unhidden`, default off, meaning only dot-hidden
    files were eligible. Nothing in stock Radarr or Sonarr writes that dot, so a
    stranger who pointed the container at an ordinary library got a healthy
    container that converted nothing, forever, and had to go and find a checkbox
    before the tool would do the thing it is for.
  - **What it is:** `hidden_only` (`HIDDEN_ONLY`), default off, meaning **every**
    matching file in the watched folders is eligible. A fresh install pointed at
    a folder converts what is in that folder, on the first scan. Turning it on
    narrows eligibility to dot-prefixed names, which is the mode that keeps a
    media server from ever seeing a file that is about to be replaced.
  - **The name and the sense flipped together on purpose.** `process_unhidden:
    false` and `hidden_only: false` are opposite behaviors, so keeping the old
    key and quietly reversing its default would have left every existing compose
    file, config backup and forum screenshot describing behavior that had
    silently become the reverse. A key that was renamed cannot be misread; one
    that was re-meaninged can, and nobody would have any reason to look.
  - **Both modes still stage the encode behind a dot** and reveal it only after
    it verifies. The setting decides which files are ELIGIBLE, never how safely
    they are written, and there is no mode in which a half-written file is
    visible.
  - **The UI's confirmation moved to the widening direction.** Turning
    `hidden_only` *off* is now what asks you to confirm and names the trash
    retention window, because off is what makes every visible file in the watched
    folders eligible at once. Turning it on narrows and needs no warning.
  - **A new watch root is now a real decision**, which it was not while the
    default converted nothing. The README's quick start puts `WATCH_ROOTS` in the
    smallest `docker run` for that reason: left unset it watches every media
    root, and the first scan means the whole mount.
- **`GET /healthz` says more.** Anonymously it now also carries `run_state`,
  `converting` and `admin_configured` - whether work is moving is operational
  status like the counts already there, and whether a password exists is what a
  page needs to decide between drawing a login form and a token field. **With a
  token** it additionally carries the whole run-control object: the window, the
  reason, the timezone and the local clock. Those name the schedule, which is
  configuration, so they wait for the token.
- **`auth_configured` now counts an admin password.** An install protected by a
  password with no key minted used to report itself unprotected.
- **The UI grew a sign-in screen** in place of the `prompt()` for a token, with
  the token field kept beside it as the recovery path for somebody who has
  forgotten the password and still holds the bootstrap token. Run controls sit
  at the top of the Queue tab; a System tab holds Schedule, Throttling, Webhook
  and Security; the API keys tab is now **Access** and holds the admin account
  and its sessions as well.
- **Request bodies are capped at 1 MB and must be a JSON object.** Nothing this
  API accepts is bigger, and a non-object body previously reached routes as an
  `AttributeError` - a traceback and a closed socket with no response. A request
  body is a trust boundary now that one of them arrives without a token.
- **Expired sessions are purged on the same sweep that prunes job history.**
- **The documentation describes a standalone tool, because that is what this
  is.** It read as a component of a larger stack, opening with "the transcoding
  worker for a ManageArr media stack", which was both wrong and a way to make a
  stranger feel they were missing a required piece. TranscodeArr needs nothing
  else to be useful. ManageArr is now named where it earns it - one client of the
  API, and one of several ways to produce dot-hidden files - and never as a
  prerequisite.
  - **The README teaches the standalone path first.** The quick start confronts
    the scope question where a reader meets it rather than leaving it to a later
    section: this converts everything in the folders you watch, so point it at
    one small folder, check the results yourself, and widen it.
  - **"Only convert dot-hidden files" is now a documented mode with both sides
    stated**, rather than an assumed setup. What it buys: the media server never
    indexes a file that is about to be replaced, so nobody starts playing one and
    nothing serves a stale codec, bitrate and runtime. What it costs: between the
    arr's import and TranscodeArr's reveal the arr's database names a file that
    is not on disk, and an arr set to search automatically for missing media can
    grab it again inside that window. The reveal asks the arr to rescan, which is
    what closes the loop and what the arr connections are for, but the window is
    real and the README says who the mode is for and who should leave the default
    alone.
  - **A copy-pasteable Custom Script for Radarr and Sonarr**, with the events to
    tick (On Import and On Upgrade, the two that produce a new file; Sonarr
    additionally offers On Import Complete), where the script has to live for the
    arr's own container to execute it, and the note that the rename is in-place
    while it is the rescan that needs the connection's path mapping. It reads
    `radarr_moviefile_path` or `sonarr_episodefile_path` and exits 0 doing
    nothing when neither is set, when the file is already hidden, or when the
    hidden name is taken - a notification script that moves files has to be a
    no-op on every event it was not written for.

### Deprecated

- **`/jobs`, `/jobs/{id}` and `/queue` are still here, and 0.9.1 said they would
  not be.** That promise was made before there was any way to know who was
  calling them, and the log line added for exactly that purpose has not yet had
  a release's worth of production to report from. Breaking a live integration on
  the release that promises a frozen API is the wrong trade.
  - **They are removed at 2.0.** Move to the `/api` spelling; the container
    still logs one line per old path it is being called on, once, naming the
    equivalent. The log line and the README were corrected to say 2.0, because a
    container running 1.0.0 telling its operator that a route "is removed at
    1.0" is worse than no warning at all.

### Security

- Passwords via `hashlib.scrypt` with a per-password random salt; nothing stores,
  logs or returns a password or a hash. `hmac.compare_digest` on bytes for the
  hash **and** the username, and scrypt runs even against a wrong username, so
  timing says nothing about which accounts exist.
- The login backoff, above, so an exposed login form is not a password oracle.
  The counter is in memory rather than in a table on purpose: an unauthenticated
  caller should not be able to make this container write a row per guess into the
  SQLite file the watcher and the worker are contending for. Every non-admin
  username shares one bucket, so a guesser cycling made-up names cannot grow that
  dict until the container runs out of memory.
- **A misconfigured certificate stops the container** rather than quietly serving
  HTTP. Logging it and carrying on would mean the password and the session token
  crossing the network in clear text while the settings page still said HTTPS,
  and nobody re-reads the startup log of a container that came up healthy.
- The webhook goes through the existing link-local guard and refuses redirects.
- A backup carries no secret of any kind, so it is safe to attach to a bug report.

### Upgrading from 0.9.1

- **Nothing is required, and nothing starts converting that was not already
  being converted.** Every existing profile, arr connection and API key is
  untouched, and the ten new settings default to the 0.9.1 behavior: `auto_start`
  on, no window, no webhook, no throttling, no TLS. One setting changed name and
  meaning, and it is migrated so that your container converts exactly what it
  converted before - read the next bullet, it is the one that answers "is this
  safe".
- **The visibility setting was renamed, and your behavior is written down
  explicitly rather than inherited from a default that now means the reverse.**
  `process_unhidden` became `hidden_only`, and `false` on the new key means the
  opposite of `false` on the old one. So the first boot on 1.0.0 migrates the
  database instead of letting the flipped default decide:
  - **If you never turned `process_unhidden` on** - the default, and what an
    install quietly converting only dot-hidden files was running - then
    `hidden_only: true` is **written into your settings table as a real stored
    value**. Nothing widens. The same files are eligible on the first scan after
    the upgrade as on the last scan before it.
  - **If you had turned `process_unhidden` on**, `hidden_only: false` is written,
    which is that same behavior under the new name: every matching file in the
    watched folders, exactly as before.
  - **An old value that is missing or will not parse is treated as the narrow
    answer.** A row nobody can read is not consent to make a whole library
    eligible.
  - The old row is deleted once the new one is written, so there is no second
    key to disagree with the first, and the boot log says which way it went:
    `visibility setting renamed to hidden_only - kept converting only dot-hidden
    files`.
  - **Only a database with history is migrated.** A brand new install - no saved
    settings and no job rows - gets the new default, which is the entire point of
    the change. The rule was proven against the shape of the live deployment
    before it shipped.
  - **What actually changes for you is the checkbox.** It now reads "Only convert
    dot-hidden files" and it is the other way round from the one you remember;
    the state you find it in is your old behavior, saved. Untick it and you are
    making every visible file in your watched folders eligible, which is why that
    direction is the one the UI asks you to confirm.
- **No schema migration.** `admin` and `sessions` are new tables created on
  first boot; no existing table gained or lost a column. The visibility rename
  rewrites one row inside the existing `settings` table and nothing else.
- **Set `TZ` before you set a window**, and check the boot log's `clock:` line
  against your own watch. Setting `TZ` on a container that has never had it
  changes every timestamp the UI renders, which is the point, but it is worth
  knowing before you wonder why yesterday's jobs moved.
- **`/jobs`, `/jobs/{id}` and `/queue` keep working.** Nothing has to be updated
  in the same maintenance window as the container, despite what 0.9.1 said.
- **If you set `tls_cert` or `tls_key`, set both**, and be ready for the
  container to refuse to start if either is wrong. Clearing both is always the
  way back, and because they are settings rather than environment, a bad value
  only bites on the next restart.
- **If you turn on a convert window, expect the queue to grow while it is
  shut.** That is the watcher working. The Queue tab says so, and so does the
  log.
- **If you scrape `/metrics`, mint a key for it** rather than handing Prometheus
  the bootstrap token.
- **1.0.0 is not a re-encode.** As with 0.9.1: nothing in this release re-encodes
  anything, re-qualities anything, or changes which profile is active. Your
  encoding changes when you activate something else, and not before.

## [0.9.1] - 2026-08-16

### Added

- **Five encoding profiles instead of one "Default", one per encoder.**
  "Balanced, on the GPU" (`h264_nvenc`), "Quick Sync, on Intel graphics"
  (`h264_qsv`), "Half the size, on the GPU" (`hevc_nvenc`), "Smallest H.264, on
  the CPU" (`libx264`) and "Smallest of all, on the CPU" (`libx265`) - named for
  the choice somebody is actually making rather than for a codec they have never
  heard of, since the real question is how fast, how small, and whether the TV
  will play it. The two HEVC ones say in the UI that older TVs, browsers and
  streaming sticks cannot direct play HEVC and will make the media server
  transcode on the fly instead.
  - Their quality, preset and codec profile are **derived** from each encoder's
    own recommendation rather than copied into a second list, because a
    hardcoded "23" drifts from the encoder's own advice the first time anybody
    tunes one and not the other.
  - They are **read-only**: editing one is `400` and deleting one is `409`. The
    way to a custom profile is to duplicate the closest and change the copy,
    which keeps five known-good starting points to compare against instead of
    letting an edit turn "Balanced, on the GPU" into something that is neither.
  - Seeding is idempotent. The ids are derived from the encoder name, so every
    later boot refreshes the same five rows rather than adding a sixth.
- **Every stored profile is tested with a real encode at boot, and one that has
  not passed cannot be activated.** The verdict is stored per profile: never
  tested, tested and failed, or works here. Activating a profile is choosing
  what every future job runs, and finding out on the first film that this box
  has no Quick Sync is exactly the silent failure a test encode prevents - the
  same rule the encoder probe already followed, applied to the whole
  configuration rather than to the encoder alone. The cost is real and scales
  with how many profiles are stored: the container's port opens later because of
  it, which is why the image's healthcheck start period went from 30s to 180s.
- **`POST /api/profiles/{id}/test` and `POST /api/profiles/retest`**, to re-test
  one stored profile or all of them and record the verdicts, with **Test** and
  **Re-test all** in the UI. They exist because hardware changes under a
  container more often than the container restarts - a driver reload, a GPU
  freed by another process, the memory-compaction fix in the README - so the
  answer from boot is not permanent. Both run real ffmpeg and answer in seconds;
  `retest` on five-plus profiles can take a minute.
- **A no-progress watchdog on running encodes.** `stall_timeout_minutes` (30 by
  default, `0` disables) kills an encode that has reported nothing for that long
  and fails the job with "no progress for N minutes - encode killed (is the share
  still mounted?)". ffmpeg reports progress roughly twice a second however slow
  the encode is, so silence means the process is wedged - almost always an
  SMB/NFS share that went away - and a wedged job used to hold its worker slot
  until the container was restarted. A stall is deliberately **not** retried down
  the fallback ladder: the share will not come back between attempts, and each
  rung would cost another full timeout.
- **Failed files back off instead of retrying forever.**
  `retry_failed_after_hours` (6 by default, `0` disables) is how long the watcher
  leaves a path alone after a job for it failed. A file that cannot be converted
  at all was previously re-queued on every scan and burned a whole encode attempt
  every few minutes, indefinitely. Queueing from the API ignores the wait
  entirely - somebody asking for a file by name is not the loop this stops.
- **Job history is pruned.** `keep_history_days` (30 by default, `0` keeps
  everything) deletes done, failed and cancelled rows on each scan. Nothing ever
  deleted them, so a library-sized run left tens of thousands of rows in the one
  SQLite file the watcher, the worker and every HTTP thread contend for.
- **A clean stop.** SIGTERM and SIGINT now cancel the running jobs, wait briefly
  for them to write their own `cancelled` rows, and shut the server down inside
  docker's grace period. Python registers nothing for SIGTERM by default, so
  every `docker stop` was dropped on the floor and became a SIGKILL ten seconds
  later - which made a deliberate stop indistinguishable from a crash in the job
  history.
- **A warning when a watched folder does not exist in the container**, named,
  once per folder. A copy-pasted config with a root nobody mounted used to look
  perfectly healthy and convert nothing, which is the exact silence this worker
  exists to remove.
- **`GET /api/jobs` is paged by cursor**, with `before=<the last job's created>`
  alongside the existing `limit` and `state`, and the body now carries `total`
  for the filtered set. By cursor rather than offset because rows are pruned by
  `keep_history_days` while new ones arrive, so page 2 of an offset walk is taken
  against a different list than page 1.
- **`PUT /api/profiles/{id}`**, alongside the `POST` the UI already sends, so it
  matches `PUT /api/arrs/{id}`. One handler behind both spellings.
- **`POST /api/jobs` accepts the path an arr knows the file by.** Every enabled
  connection's `arr_path` -> `worker_path` mapping is applied in turn, so a
  Sonarr webhook naming `/tv/Show/ep.mkv` can be forwarded verbatim. Every
  candidate is re-checked through the same containment guard, because the mapping
  is operator-editable.
- **A `409` from `POST /api/jobs` now carries the job that already exists**, so
  "make sure this is queued, then track it" is two calls instead of listing the
  queue and matching on path. It is `null` in the race where the duplicate
  finished in between.
- **The container no longer runs as root.** It starts as root, drops to
  `PUID:PGID` (default `1000:1000`, the LinuxServer convention the *arrs use)
  with `setpriv`, and execs the daemon from there. Every converted file and
  every trashed source is now owned by that uid and gid instead of `root:root`,
  which is what previously left an arr unable to upgrade or delete its own
  media days after a conversion, nowhere near the cause.
  - Only `/config` is ever chowned. **The media tree is never chowned**, on the
    grounds that it is terabytes we mount read-write, not ownership we get to
    rewrite.
  - `PUID=0` **and** `PGID=0` together mean "stay root", for hosts where nothing
    else can write the mount.
  - A container already started non-root (compose `user:`, a k8s
    `securityContext`) is detected and execs straight through rather than
    crash-looping on a chown it cannot perform.
  - The dropped user is added to whichever group owns each `/dev/dri` node, so
    Intel QSV and AMD VAAPI keep working. Without that step they do not fail
    loudly, they simply probe as unavailable and every job re-encodes on the CPU.
- **A published image.** `ghcr.io/managearr/transcodearr`, built for
  `linux/amd64` and `linux/arm64`, with OCI metadata (title, description,
  source, license, version, revision) and the version injected at build time.
- **A release workflow** on `v*` tags, with a guard that refuses to publish when
  the tag and the version the image would report disagree. A `/healthz` that
  lies about what is deployed is the one field whose whole job is being true.
- **CI builds the Docker image** on every push and pull request, alongside the
  test suite, so a packaging change cannot merge green while broken.
- **Tests for two things that had none:** the link-local guard below (14 cases,
  no network, including the encodings that do not report as link-local on their
  own), and the file-safety rules in the job pipeline (12 cases against a real
  temporary filesystem: the two overwrite guards, the reveal exemption, boot
  cleanup, trash destination, and the encoder fallback ladder).
- **Documentation for a stranger, not just for the author.** The README gained a
  quick start, an honest explanation of how files come to be dot-hidden (stock
  Radarr and Sonarr do not do it, and a container pointed at an ordinary library
  will therefore convert nothing and say nothing), a complete environment
  variable reference marking which vars only seed a first boot, the encoding
  profile model, the full API surface, and a security section that states the
  residual SSRF rather than claiming it is closed. Plus this file.

### Changed

- **The default profile is one of the five, not a sixth row saying the same
  thing.** A fresh install activates the shipped profile for whichever encoder
  won the probe. `FORCE_ENCODER` still steers that choice, since it decides
  which encoder wins.
- **`POST /api/profiles/{id}/activate` answers `409` when the profile has not
  passed a test encode on this machine**, and still `404` for an unknown id. A
  client retrying the first forever would never succeed, so the two have to be
  distinguishable. `DELETE /api/profiles/{id}` now answers `409` for a shipped
  profile as well as for the active one, and `POST`/`PUT` on a shipped profile
  is `400`.
- **A profile object carries `builtin`, `validated_ok` and `usable`.**
  `validated_ok` is `null`, `0` or `1` - never tested, tested and failed, works
  here - because "we have not looked" and "this machine cannot do it" are
  opposite answers to "may I use this?". `usable` is the derived boolean
  (`validated_ok == 1`) that every caller reads, so the UI, the activate route
  and any other client cannot disagree about the untested case. `GET
  /api/profiles` also returns the list already ordered: shipped first in
  encoder-probe order, then the user's own oldest first.
- **The API is namespaced under `/api`.** `/api/jobs`, `/api/jobs/{id}` and
  `/api/queue` join the `/api/*` routes that were already there, so there is one
  namespace rather than a five-route root plus everything else. `GET /healthz`
  stays at the root and stays unauthenticated, because a health check that needs
  a key cannot report a missing key. The old spellings still work - see
  **Deprecated**.
- **Replaced sources are trashed on the same mount as the media.** The default is
  now a `.transcodearr-trash` directory under whichever media root holds the
  source, which makes the move a rename instead of a copy. It was
  `$CONFIG_DIR/trash`, and on a NAS `/config` and the media are different mounts:
  127 GB of full byte copies were measured sitting in the config share of the
  live deployment, next to the SQLite database. `TRASH_DIR` still overrides the
  location and the media-root-relative mirroring is unchanged either way.
- **The encoder fallback ladder actually walks.** An attempt now advances to the
  next rung on any nonzero exit while rungs remain, and reports the error only
  after the last. The old condition also required the subtitles flag, which only
  the first attempt ever has, so the AAC and CPU-decode rungs were unreachable: a
  subtitled remux with copy audio failed on the first rung and never tried the
  two fallbacks written for exactly that file. The cost is that a source failing
  late now burns every rung before giving up; a cancellation and a stall both
  still return immediately.
- **Routes are matched exactly.** Dispatch was `startswith`, so `GET
  /queuegarbage` answered `200` with the real queue and any future `/jobs/stats`
  would have been swallowed by the list handler. The query string is split off
  first, so `/queue?limit=200` is still `/queue`.
- **A job object is an explicit field list, not the database row.** Returning
  every column made the SQLite schema itself the public API - each column added
  for the worker's own bookkeeping shipped to every caller and could never be
  renamed. `priority` is no longer exposed, and neither is anything added later
  unless it is added to that list on purpose.
- **`log_tail` is opt-in and only `GET /api/jobs/{id}` returns it.** Somebody
  asking for one job is debugging it and the ffmpeg argv is the answer; sixty of
  them in a list was a bulk export of absolute container paths.
- **Status codes that were `400` and should not have been.** `PUT
  /api/arrs/{id}`, `POST`/`PUT /api/profiles/{id}` and `DELETE
  /api/profiles/{id}` answer `404` for an unknown id, which used to be
  indistinguishable from a malformed body. Deleting the **active** profile is now
  `409`, not `400`: the identical call succeeds the moment another profile is
  activated. A profile id is checked before the test encode runs, so a dead id
  stops costing two seconds of ffmpeg first.
- **`GET /healthz` splits by authentication.** The anonymous body keeps `ok`,
  `version`, `encoder`, `encoder_reason`, `queued`, `running`, `uptime_seconds`
  and `auth_configured` - everything the UI's status line needs before anyone
  signs in. `media_roots`, `watch_roots` and `process_unhidden` now require a
  valid bearer token, because they are a map of somebody's filesystem on the one
  route with no key on it. An absent token is still not an error here. (That
  third field is `hidden_only` from 1.0.0 - see 1.0.0's Changed.)
- **`docker-compose.example.yml` is now a file a stranger can deploy.** It
  carries four hardware variants as commented blocks (NVIDIA, NVIDIA on QNAP
  Container Station, Intel QSV or AMD VAAPI via `/dev/dri`, and no GPU at all),
  publishes the port on `127.0.0.1` only, drops all capabilities and hands back
  the four that startup actually costs, and separates the variables read on
  every boot from the ones that only seed a first boot. The seed distinction
  matters because `QUALITY` in a compose file stops meaning anything the moment
  an encoding profile exists.
- **The healthcheck resolves `PORT` at runtime** instead of assuming 8484.
  Hardcoding it made every container that changed the port permanently
  unhealthy, and an orchestrator answers "unhealthy" by restarting it forever.
  Its start period is now 180s rather than 30s, because the boot probes and the
  per-profile test encodes are real ffmpeg runs and the port opens only after
  them: a start period shorter than the probe restarts a container that is fine.
- **Turning on "Also process visible files" now asks first**, and the prompt
  names the actual trash retention window rather than a generic warning. It is
  the one control in the settings form that can re-encode an entire library and
  then age the originals out of the trash, and it used to ride along with a
  generic Save. Declining leaves the rest of the form saveable. (At 1.0.0 the
  control is "Only convert dot-hidden files" and the confirmation is on turning
  it *off*, which is the same widening it always guarded.)

### Deprecated

- **`/jobs`, `/jobs/{id}` and `/queue` are the pre-0.9 spellings** of
  `/api/jobs`, `/api/jobs/{id}` and `/api/queue`. They keep working, on the same
  handlers, because the live deployment and older clients call them. **They are
  removed at 1.0.** (They were not. See 1.0.0's Deprecated section: the removal
  moved to 2.0, and this line is left as it was written.)
  - The one difference is the envelope on a single job: the `/api` routes return
    `{"job": {...}}` from `GET /api/jobs/{id}` and from a `201`, while the old
    paths return the bare job object, exactly as before. Lists are `{"jobs":
    [...]}` and cancel receipts are identical on both.
  - The container logs one line per old path it is still being called on, once,
    naming the `/api` equivalent. Removing a route nothing calls is housekeeping;
    removing one the live box still calls is an outage, and the only honest
    source for which it is are the requests actually arriving.

### Fixed

- **The post-encode overwrite race.** The write targets are now re-checked
  immediately before the rename pair, not only before the encode. An encode runs
  for hours, and a file that appeared at either name during it - an arr importing
  an upgrade - was replaced by a result computed from the old source. The reveal
  rename had no such guard at all. On a conflict the `.part` is removed and the
  job fails with the source untouched.
- **The same race on the reveal-only path**, which does no encode and so was
  missed by the fix above. A file already in the right container, or protected by
  a skip rule, is unhidden with a single rename - and the guard in front of it
  was answered before an `ffprobe` that can run for a minute on a 4K remux over
  SMB. An import landing at the visible name inside that window was overwritten,
  and unlike a replaced source it never reached the trash. The check is now
  re-asked immediately before that rename too.
- **A `TRASH_DIR` at or above a media root is refused at startup**, with an error
  in the log, and the default is used instead. The trash is pruned by walking it
  and unlinking anything past `trash_keep_days`, so `TRASH_DIR=/media` was a
  scheduled delete of the whole library. `/media/trash` is still supported and
  still excluded from the scan.
- **`is_within` said nothing was inside the filesystem root.** `os.path.normpath`
  leaves `/` as a separator, so the containment test asked whether a path started
  with `//`. The only root affected is `/` itself - every other root loses its
  trailing separator to `normpath` already - but it made a media root of `/`
  refuse every job in silence, and it was the one case the new `TRASH_DIR` check
  would have waved through.
- **An interrupted job could become permanently unrunnable.** Dying between the
  staging rename and the trash left a complete hidden encode next to a source
  that also still existed, and every retry then failed on "staging name is
  taken", forever. Boot now removes that stranded copy - never when it is the
  source itself, which is what a reveal looks like and would delete the only copy
  of the film. The boot sweep also plans names the way a real job does, so a
  skip-protected `.mkv` can no longer make it delete an unrelated `.mp4` of the
  same stem.
- **Trashing the same relative path twice destroyed the earlier copy.**
  `shutil.move` replaces its destination, so re-processing a path inside the
  retention window silently removed the older source - the one somebody would
  actually want back. Collisions are suffixed (`.1`, `.2`) now. Retention is a
  promise; it has to hold for every copy it was made about.
- **An unknown `?state=` returned an empty list.** It fell through to `WHERE
  state=?` and matched nothing, so a typo or a renamed state read exactly like an
  empty history. It is a `400` naming the five valid states, and an unparseable
  `?before=` is a `400` for the same reason.
- The job history's Cancel button no longer interpolates a server-supplied value
  into an inline event handler. Same behavior, and it stops contradicting the
  rule stated twenty lines above it.
- Removed a dead control that was the last writer of the superseded `quality`
  setting from the encoders screen.

### Security

- **The literal token `change-me` is refused as an API key.** A published image
  makes any placeholder in its own documentation a public credential, so it is
  now treated as no token at all: a container set that way logs a loud startup
  warning naming `openssl rand -hex 24`, reports `auth_configured: false`, and
  rejects every request carrying it. Minted keys still work on such a container.
  The check blanks the value before any comparison, so no timing signal about a
  real key is created.
- **Absolute container paths stopped shipping in bulk.** `log_tail` - the full
  ffmpeg argv, including the source, staging and trash paths - was in every row
  of every job list. It is now only on the single-job `GET`.
- **`/healthz` stopped publishing the host layout to anonymous callers.**
  `media_roots`, `watch_roots` and `process_unhidden` need a token now; the
  fields the UI actually reads before sign-in do not.
- **Outbound requests to Radarr and Sonarr refuse link-local destinations:**
  `169.254.0.0/16` and `fe80::/10`, including the IPv4-mapped
  (`::ffff:169.254.169.254`) and 6to4 (`2002:a9fe:a9fe::`) encodings, and
  hostnames that resolve to any of them. `169.254.169.254` is the cloud metadata
  service, where a "connection test" becomes a read of the host's instance
  credentials.
  - Guarded in the single function every outbound call carrying an `X-Api-Key`
    passes through, so the connection test, the cached library list and the
    post-job rescan are all covered, including connections saved before this
    existed.
  - **Private space is deliberately still reachable.** A real arr lives on the
    LAN, on a Docker bridge, or on loopback, and refusing those would break the
    correct configuration for nearly every user. An authenticated caller can
    therefore still probe the container's own networks. The mitigation for that
    is the API token, which is the real reason not to expose this service.
  - **DNS rebinding is not defended**, and AWS's IPv6 metadata endpoint
    (`fd00:ec2::254`, inside ordinary unique-local space) is deliberately not
    blocked. Both are written down in the README rather than left silent.
- The example compose file no longer ships a placeholder token and no longer
  publishes the port on `0.0.0.0`. It points at `openssl rand -hex 24` and says
  what this API can do to a library, which is the argument for keeping it off
  the internet.
- Both CI workflows declare least-privilege `permissions` blocks; the release
  workflow holds `packages: write` and nothing else beyond read.

### Upgrading

Existing installs, including one running on real media since 2026-08-15:

- **No re-encoding is triggered by upgrading.** Nothing re-queues completed
  work, nothing rescans finished files, and the watcher keeps polling with the
  same size-stability rule it had. Files already converted stay converted.
- **The schema migration is additive.** New tables and indexes are created only
  if absent; the `jobs` table gains its new columns, and `profiles` gains
  `validated_ok`, via `ALTER TABLE ADD COLUMN`, so existing rows keep every
  value they had. A profile row that already had a `validated_at` is recorded as
  a pass, since the old code only wrote that timestamp on success. Nothing is
  dropped, renamed or rewritten. Queued reveals are given the reveal priority so they stop waiting
  behind a transcode backlog, which is a reorder of pending work and not a change
  to it.
- **What is already in `/config/trash` keeps expiring on its old schedule.** New
  trashings go to the media root instead, but the old location is still swept on
  every scan, with the same `trash_keep_days` against the same trashed-at
  timestamps. Nothing already in the trash is deleted early or preserved longer
  by this upgrade, and the directory empties itself as its contents age out.
  There is nothing to move by hand.
- **Existing API clients keep working.** `/jobs`, `/jobs/{id}` and `/queue`
  answer exactly as they did, envelopes included, so nothing has to be updated in
  the same maintenance window as the container. They are removed at 1.0, and the
  log names each one still being called. Two responses did change shape for
  every caller: `priority` is no longer in a job object, and `log_tail` is no
  longer in a job **list** - it is still on the single-job `GET`. The bundled UI
  read neither, and neither did any known client.
- **Three new settings arrive with defaults that change behavior**, which is the
  point of them: a failed file waits 6 hours before the watcher retries it, an
  encode silent for 30 minutes is killed, and finished job rows older than 30
  days are deleted on the next scan. Set `RETRY_FAILED_AFTER_HOURS=0`,
  `STALL_TIMEOUT_MINUTES=0` or `KEEP_HISTORY_DAYS=0` for the previous behavior
  of each. The history prune is the only one that removes anything, and it
  removes rows, never files.
- **The first start chowns `/config` recursively** to `PUID:PGID`. This is
  deliberate: a container that previously ran as root left `transcodearr.db`
  root-owned, and a database the dropped uid cannot write is a daemon that
  starts and then fails every job. It is a one-time, visible change, and it
  touches nothing outside `/config`.
- **Set `PUID`/`PGID` to whoever owns your media before the first restart.** The
  container will not chown the library, so if the new uid cannot write there,
  jobs fail at rename time rather than at startup. If you would rather not
  change anything yet, `PUID=0 PGID=0` keeps the previous behavior exactly.
- **Anything mid-encode when you restart is marked failed and its `.tapart`
  staging file removed.** That was already true, and it is safe: the source is
  never touched until an encode has been verified, so the watcher simply finds
  the untouched file again on the next scan.
- **Your existing "Default" profile is kept exactly as it is, and stays
  active.** An install from before the shipped five had one profile by that
  name, seeded from the settings that container was running. It keeps every
  value it had - encoder, quality, preset, codec profile, resolution - and keeps
  driving every job, so the next file is encoded exactly like the last one. All
  that changes is that it stops claiming to be one of ours: it becomes an
  ordinary profile of yours, editable and deletable, which for settings you
  chose is the truth. The five shipped profiles appear **alongside** it, so an
  upgraded install sees six rows rather than five.
- **Nothing is re-encoded or re-qualitied by upgrading.** The five are new
  options in a list, not a setting applied to your library. Your encoding
  changes when you activate something else, and not before.
- **That carried-over profile is tested at boot like every other**, which
  finally gives it a verdict it was seeded without: it was your settings written
  down, never proved by an encode. If the test fails it is flagged and **left
  active** rather than swapped for something that passes: an upgrade that
  quietly picked a different profile for you would be the re-quality this whole
  design refuses.
- **The first boot after upgrading takes longer**, because it is a real test
  encode per stored profile - six on an upgraded install - and the port opens
  only when they are done. The image's healthcheck start period covers it; an
  external monitor with a shorter patience of its own may need telling.
- **`QUALITY`, `ENCODER_PRESET`, `ENCODER_PROFILE` and `MAX_HEIGHT` no longer
  feed anything.** Seeding that Default once, on an install that predates
  profiles, is the whole of what they ever did. They stay in the settings table
  and stay hidden in the UI. `FORCE_ENCODER` is the exception: it still decides
  which encoder wins the probe, and so which of the five a fresh install
  activates.
