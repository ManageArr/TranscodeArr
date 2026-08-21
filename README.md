# TranscodeArr

TranscodeArr converts a media library to MP4. It watches the folders you name,
waits until a file has finished landing, encodes it with whatever hardware the
box actually has, **verifies the result**, and only then puts it in place. The
original is not deleted: it outlives its replacement in a pruned trash. One
container, with a web UI, an HTTP API and a SQLite queue in it.

It is a standalone tool and works entirely on its own. Radarr and Sonarr
connections are optional, every feature below works without them, and anything
that speaks HTTP can drive the API.

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
- **Nothing is deleted.** The source moves to the trash (mirrored paths, pruned
  after `TRASH_KEEP_DAYS`).
- **No visible partial files, ever.** Encodes go to a dot-hidden `.tapart`
  staging name in the same directory (same filesystem, so `os.replace` is
  atomic), become a hidden final, then a single rename is the reveal.
- **Two-writers guard:** if the source changed size during the encode (an arr
  upgraded it mid-run), the encode is discarded, never the newer source.
- **Text subtitles are carried** into MP4 as `mov_text`; when a source's
  subtitles cannot be (PGS and friends), the job retries without them and says
  so in a warning rather than silently dropping them.

Said as plainly as it deserves: **this tool re-encodes your files and replaces
them.** A conversion is lossy, the new MP4 takes the source's place, and the
source goes to trash and is deleted for good once `trash_keep_days` (7 by
default) has passed. Nothing is overwritten, nothing else is touched, and no
file is deleted the moment a job finishes - but that retention window is the
whole of your safety net, so raise it before a large batch.

Design prior art: [Cdarr](https://github.com/jbreuer95/cdarr) (MIT, Jelle
Breuer) proved the hide-then-reveal pattern with arr webhooks in 2020;
TranscodeArr is a fresh implementation of that idea, not a fork - Cdarr was
CPU-only Laravel/PHP and had been unmaintained for years.

## Works with

None of this is required. It is what happens to fit alongside a container that
converts files and answers HTTP.

- **Any media server**, because the output is an ordinary MP4 sitting where the
  source sat. [Only convert dot-hidden files](#only-convert-dot-hidden-files)
  additionally keeps a file invisible until it is finished and verified, which
  works because Jellyfin ignores dot-prefixed names - check your own server
  before relying on that.
- **Radarr and Sonarr**, optionally, and only ever to read the library list and
  ask one title to rescan after a file is replaced in place. Nothing writes to
  an arr's database. See [Radarr / Sonarr](#radarr--sonarr).
- **Anything that speaks HTTP.** The [API](#api) is the whole surface, one
  bearer token in front of it, and it is frozen for the 1.x line. ManageArr,
  the media stack manager this worker was first written for, is one such client;
  a shell script with `curl` is another, and neither is more supported than the
  other.
- **Prometheus**, at [`GET /metrics`](#metrics), for anyone who wants a graph
  of what their library is doing.

## Quick start

```
docker pull ghcr.io/managearr/transcodearr:latest
```

`latest` follows the newest release. Every release also publishes `1`, `1.2`
and `1.2.3` style tags, so pin whichever of those you want if you would rather
decide when this thing changes on a library it is actively rewriting.

There is no default API key, and you do not have to invent one. On its first
boot with no key configured, the container generates one and prints it:

```
========================================================================
No API key was configured, so one has been generated for you:
    ta_1f4c...
Sign in with it at http://<this host>:8484 - it is shown only this once.
Revoke it under API Keys once you have created an account or minted your own.
========================================================================
```

Read it back with `docker logs transcodearr`. Only its hash is stored, so that
line is the one time the key exists in readable form. If you would rather
choose the key yourself, set `TRANSCODEARR_TOKEN` and nothing is generated.

The smallest thing that runs:

```
docker run -d --name transcodearr \
  -p 127.0.0.1:8484:8484 \
  -e TZ=America/New_York \
  -e PUID=1000 -e PGID=1000 \
  -e WATCH_ROOTS=/media/ConvertMe \
  -v /srv/media:/media \
  -v /srv/appdata/transcodearr:/config \
  ghcr.io/managearr/transcodearr:latest
```

**`WATCH_ROOTS` is in there on purpose, and it should be a folder you made for
this.** Left out, it watches every media root - the whole of `/media` - and
everything convertible under it becomes eligible on the first scan. See
[What it may convert](#what-it-may-convert), which is the last step of this
quick start and the one to read before the first boot rather than after.

That has no GPU passed through, so it will encode on the CPU. **Deploy from
`docker-compose.example.yml` instead** - it carries the hardware passthrough in
four commented variants (NVIDIA, NVIDIA on QNAP Container Station, Intel QSV or
AMD VAAPI via `/dev/dri`, and no GPU at all), the reduced capability set, and
the reason for every line. Copy it and change four things: the GPU block, the
token, the two volume paths with `WATCH_ROOTS` pointing at a folder underneath
them, and `TZ`.

**Set `TZ` even if you never use a convert window.** An unset `TZ` means the
container is UTC, and every clock it prints - the job history, the log, the
window - is then four to eight hours away from the one on your NAS. See
[Timezone](#timezone-the-trap-worth-one-paragraph).

`PUID`/`PGID` should be the uid and gid that already own your media, exactly as
with the *arrs. See [Users, groups and
permissions](#users-groups-and-permissions).

### What happens on first boot

1. Every candidate encoder is tried with a **real one-second encode**, and each
   result is logged with the reason it worked or did not. This takes a few
   seconds and is the only honest way to know: ffmpeg lists encoders it was
   built with, not encoders this machine can run.
2. `/config/transcodearr.db` is created (queue, settings, API key hashes).
3. The five shipped [encoding profiles](#encoding-profiles) are written, one per
   encoder, and then **every stored profile is tested with a real encode of its
   own**. Each verdict is stored and logged by name.
4. The shipped profile for whichever encoder won is activated. That is the
   default, and it is one of the five rather than a sixth row saying the same
   thing.
5. The HTTP server comes up on `PORT` (8484). Open it, sign in with the token
   you generated, and check the header: it names the encoder actually in use and
   why. The Queue tab's top card says whether it is converting, and the local
   clock with its zone beside it - check that clock now, not the first time a
   window does something unexpected.
6. It boots **running**, because `auto_start` defaults to on. If you would
   rather sweep a library on your own schedule, press **Stop**, or set
   `AUTO_START=false` before the first boot. See [Start, Stop and
   draining](#start-stop-and-draining).

Steps 1 and 3 are both real ffmpeg runs, which is why the port opens a minute or
so after `docker start` rather than instantly, and why the image's healthcheck
carries a 180-second start period. The alternative to paying that once is
finding out on your first film.

### What it may convert

**Mode 1, the default: everything in the folders you watch.** Point it at a
folder and it converts what is in there. No setting to find, nothing to wire up,
and it is the right answer for most people - especially anyone converting an
existing library once.

The warning that comes with it is the scope. **Every** matching file under a
watched folder is eligible from the first scan, not just what arrives from now
on:

- **Eligible** means any file under a watched folder whose extension is in
  `convert_extensions` (`.mkv .avi .m4v .m2ts .mts .vob` by default). Files that
  are already MP4 are left alone, because `.mp4` is not in that list.
- Each one is re-encoded to MP4 at the active profile's quality, and the
  original is **moved to trash, where it is deleted for good after
  `trash_keep_days`** (7 by default). Raise that before a large batch.
- **So point Watched folders at one small folder first**, and make one if you
  have to. Ten files, not ten thousand. Let them convert, watch a couple of them
  on the TV you actually use rather than only in the job list, and widen it in
  the UI once you believe the results. `WATCH_ROOTS` left empty watches every
  media root, which is the whole mount.
- Every safety rule still applies and only the scope changes: size-stability
  before a file is touched, verification before anything is replaced, the
  two-writers guard, and no visible partial file at any point.

**Mode 2: only convert dot-hidden files.** Turn `hidden_only` on and a file is
eligible only if its name begins with a dot. What that buys is a media server
that never sees a file at all until TranscodeArr has finished and verified it -
so nobody can start playing the copy that is about to be replaced. **Stock Radarr
and Sonarr do not create dot-prefixed files**, so something has to do the
renaming, and there is a cost to weigh as well as a payoff. Both are in [Only
convert dot-hidden files](#only-convert-dot-hidden-files), with a Custom Script
you can copy.

**The toggle decides which files are eligible, never how safely they are
written.** Either way the encode is staged to a dot-hidden `.tapart` name and
revealed only once it verifies. There is no mode in which a half-written file is
visible.

In both modes you can queue any file by hand at any time with
`POST /api/jobs {"path": "..."}`, which ignores `hidden_only` entirely.

And whichever you pick, a scan that walks your watched folders and finds nothing
it may queue **says so in the log, with the reason**. A healthy container
converting nothing in silence is the exact failure this project exists to
remove, so it is not a shape this one is allowed to take.

## Only convert dot-hidden files

Optional, off by default, and worth reading before you decide either way. Turn on
**Only convert dot-hidden files** (`hidden_only`, `HIDDEN_ONLY`) and a file is
eligible only when its name begins with a dot. Nothing else about a job changes.

```
file arrives as    .Movie (2026).mkv          hidden, complete
worker encodes to  .Movie (2026).tapart.mp4   hidden, partial
verified, becomes  .Movie (2026).mp4          hidden, complete   (os.replace)
source -> trash
revealed as        Movie (2026).mp4           visible            (os.replace)
```

The bottom four lines happen in both modes: an encode in progress is always a
dot-hidden `.tapart` file, whatever the source was called, and the reveal is
always a single rename after verification. **This setting only adds the first
line** - the *source* is hidden too, so the whole window between a file landing
and its conversion finishing is invisible to your media server rather than
merely short.

### Why: what goes wrong without it

A file your media server can see is a file somebody can press play on. In the
default mode that file is also about to be replaced, and two things follow from
that, neither of which announces itself:

- **Somebody starts watching the copy you are about to convert.** A 40GB remux
  lands, the media server indexes it, someone presses play, and partway through
  the worker replaces it with the MP4. Nothing is lost - the source went to
  trash, not to `/dev/null` - but it is no longer at the path it was playing
  from. An already-open handle survives a rename on Linux, so a direct play in
  flight often finishes; a session that re-opens the file to seek, or that was
  being transcoded on the fly, does not.
- **The media server keeps serving the old file's numbers.** It cached that
  file's codec, bitrate and runtime when it indexed it, and it makes
  direct-play-or-transcode decisions from them. Neither Jellyfin nor an arr
  notices a same-name in-place replacement on its own (jellyfin#13565, closed
  "not planned"), so until something rescans, the whole stack is reasoning about
  a file that no longer exists in that form.

With `hidden_only` on, neither can happen, because nothing is ever visible except
a finished, verified file. The library gains an MP4 in one atomic rename and
never held anything else.

**This works because media servers ignore dot-prefixed files.** Jellyfin has a
hardcoded `**/.*` in its ignore patterns - undocumented, but stable since the
Emby fork - and the same Unix convention is why the `.transcodearr-trash`
directory and the `.tapart` staging file are invisible too. That is the
convention, not a trick played on your media server. It is still worth
confirming on your own server before you rely on it: hide one file by hand,
rescan, and check that it did not turn up.

### The cost: a window where the arr's path is stale

Here is the honest half. **Between the arr's import and TranscodeArr's reveal,
the arr's database points at a filename that is not on disk.** The arr imported
`Movie (2026).mkv`, the hiding step renamed it to `.Movie (2026).mkv`, and until
the conversion finishes that is where the file actually is.

TranscodeArr closes that loop from its end. The moment a job reveals a finished
file, it asks every enabled arr connection whose root contains that path to
rescan the title - `RescanMovie` for Radarr, `RescanSeries` for Sonarr - which is
exactly what those connections exist for, and it happens on a reveal-only job as
well as after a transcode. Once the rescan lands, the arr's database names the
MP4 that is really there. That is not a workaround bolted on for this mode; it is
the same rescan that makes in-place replacement work at all. See [Radarr /
Sonarr](#radarr--sonarr).

But the window is real, and this is what can happen inside it: **if the arr runs
a scan of its own while the file is still hidden, and it is set to search
automatically for missing media, it can decide the episode is missing and grab it
again.** Nothing on this side can prevent that. The arr is acting correctly on
what it can see, and what it can see is an empty slot.

So before you turn this on, check your own arr for whatever it is configured to
do about a file it expects and cannot find - the "search for missing" options and
the scheduled tasks that act on them. If your arr will go and re-download a
missing episode by itself, that is the setting that turns a hidden file into a
second copy of something you already have.

The window is as long as the conversion. On a GPU that is minutes; on a CPU-only
box running libx265 it is hours, and if the queue is days deep, the window on the
last file in it is days.

**Who this is for.** A stack where the arrs and the media server are all wired up
and running together, where TranscodeArr holds arr connections with their path
mappings set, and where the queue keeps up with what lands. There the window is
short, the rescan closes it, and the payoff is that nobody ever sees a file that
is about to be replaced.

**Who should leave the default alone.** Anyone converting an existing library
once. There is no import to hook, nothing is arriving, and this mode's cost buys
nothing you need. Point the default at a folder, watch it work, and widen it.
That is why it is the default.

### How: hiding on import from Radarr and Sonarr

Both Radarr and Sonarr ship a **Custom Script** connection, and its only fields
are a path to a script and its arguments. The arr runs that script itself and
hands it the event in environment variables, so the whole hiding step is one
small shell script that renames the file the arr just imported.

#### The script

```sh
#!/bin/sh
# Hide a just-imported file until TranscodeArr has converted and verified it.
# Renames "Movie (2026).mkv" to ".Movie (2026).mkv" in the same directory;
# TranscodeArr reveals the finished MP4 under the visible name and then asks
# this arr to rescan.
#
# Every exit here is 0 on purpose. A non-zero exit is reported by the arr as a
# failed notification, and "there was nothing to rename" is not a failure.

# Radarr sets radarr_moviefile_path, Sonarr sets sonarr_episodefile_path, and
# an event with no file (a test, a grab) sets neither. Unset means there is
# nothing to do, which is the whole guard: no path, no rename.
file="${radarr_moviefile_path:-$sonarr_episodefile_path}"
[ -n "$file" ] || exit 0
[ -f "$file" ] || exit 0

dir=$(dirname "$file")
name=$(basename "$file")

# Already hidden - this event fired twice, or the file arrived hidden. Turning
# ".Name.mkv" into "..Name.mkv" would break the reveal as well as double-hide.
case "$name" in .*) exit 0 ;; esac

# Never clobber. If something is already sitting at the hidden name, that is
# somebody's media or a job in flight, and this script does not get to decide.
[ -e "$dir/.$name" ] && exit 0

mv -- "$file" "$dir/.$name"
exit 0
```

It touches exactly one file: the one the arr just told it about. `radarr_eventtype`
and `sonarr_eventtype` are set too and are worth logging if you want a record,
but nothing above branches on them - the file path variable is the honest test of
"is there a file here to hide", and it is the one that is unset on every event
that has no file.

#### Which events to enable

In the Custom Script connection's settings, tick:

- **On Import** (both arrs) - the event that fires when a downloaded file is
  imported into the library. This is the one that matters. Older versions label
  it On Download; it is the `supportsOnDownload` capability either way.
- **On Upgrade** (both arrs) - an upgrade puts a *new* file in the library, which
  needs hiding exactly as much as the first one did.
- **On Import Complete** (Sonarr only) - Sonarr additionally offers this one.
  The script is a no-op on any event that hands it no file path, so ticking it
  costs nothing.

Leave the rest off, **On Rename in particular**. A rename produces no new file to
hide, and the fewer events reach a script whose job is moving files, the smaller
the set of things it can be wrong about.

#### Where the script has to live

The arr executes this, from inside the arr's own container, so the path you type
into the Custom Script field is a path **that container** can see. The arr's
config volume is the natural home, since it survives a recreate:

```
/config/scripts/hide-for-transcodearr.sh
```

It has to be executable, and executable by whoever the arr runs as:

```
chmod +x /config/scripts/hide-for-transcodearr.sh
```

Leave the **Arguments** field empty - the script reads its environment, and takes
nothing on the command line.

Press **Test** on the connection after saving. A test event carries no file path,
so the script does nothing at all - which means what a passing test proves is
exactly the thing that goes wrong most often: that the arr can find the script
and is allowed to execute it.

#### The two containers have to agree about paths

The rename itself needs nothing configured. It happens in place, in one
directory, on whichever mount both containers already share - Sonarr renames
`/tv/Show/S01E01.mkv` and TranscodeArr finds `/media/TV/Show/.S01E01.mkv`,
because it is the same file seen through two mounts.

What does need the mapping is the rescan afterwards, and that is what the
`arr_path` -> `worker_path` field on each connection is already for. Set it and
the reveal notifies the right title; leave it wrong and the rescan reports that
no title owns the path. See [Radarr / Sonarr](#radarr--sonarr).

**Hide the file, not the folder.** The watcher skips dot-prefixed directories
outright - that is what keeps it out of its own trash - so a hidden *folder* is
never walked into and nothing inside it is ever queued.

### Other ways to produce a hidden file

The Custom Script above is the one to set up for a live stack. These work too:

| Way | How |
| --- | --- |
| By hand | `mv "Movie (2026).mkv" ".Movie (2026).mkv"` for one file, or a `find -exec` for a batch you want converted. |
| The API | `POST /api/jobs {"path": "..."}` converts any file inside `MEDIA_ROOTS`, hidden or not, and ignores `hidden_only` entirely. It accepts the path an enabled Radarr/Sonarr connection knows the file by as well as this container's own, so an arr webhook can be forwarded verbatim. |
| A stack manager that already does it | ManageArr hides on import, for example. Nothing else to configure on this side. |

### If nothing is writing the dot

`hidden_only` with no hiding step is a container that walks your library every
scan interval and queues none of it. That silence is the failure this project
exists to remove, so it is reported rather than left to be discovered.

When a scan finds files it would otherwise have converted and **not one** of them
is hidden, the count is logged with the two ways forward, shown on the Queue tab
in place of the empty-queue hint, and carried in the run-state object as
`visible_only_skipped` - so `GET /api/control`, an authenticated `GET /healthz`
and any dashboard reading them get the same answer.

It reports the total failure rather than each individual skipped file on purpose.
Once a single hidden file turns up anywhere in the watched folders, the dot
convention plainly *is* in use here, and a visible file being skipped is the
setting doing its job rather than a misconfiguration.

## Running

The queue is SQLite in `/config`; one worker on purpose (one disk, one NVENC
session budget - concurrency here is a throughput regression disguised as a
feature, though **Convert at once** raises it to 8 for people whose media sits
on SSD). On restart, interrupted jobs are marked failed, their `.tapart` staging
files removed - both the `.tapart`, and a finished hidden copy stranded by a
death between the staging rename and the trash, which used to make every retry
of that file fail forever on a name already taken. The watcher then simply finds
the untouched source again. The source is never touched until an encode has been
verified, so a crash, a `docker stop` or a power cut costs at most the time spent
encoding.

The watcher polls rather than using inotify, which does not work across SMB or
NFS - exactly where NAS media lives - and so degrades silently.

### Start, Stop and draining

**Start converting** and **Stop converting** sit in the card at the top of the
Queue tab, with `POST /api/control/start` and `POST /api/control/stop` behind
them. Stop is not a kill switch, and the difference matters on a 40GB remux:

**Converting is gated at the moment a worker claims a job, never by terminating
one.** Press Stop while something is encoding and that encode runs to
completion, is verified, and is revealed exactly as it would have been. What
stops is the *next* claim. A 40GB remux at 90% is never thrown away to honor a
button press, and the same is true when a [convert window](#the-convert-window)
closes underneath a running job.

That is what "draining" means here, and it is the only sane reading: throwing
away an hour of GPU time to be paused two minutes sooner buys nothing, and the
partial file would have to be cleaned up anyway. So a Stop is not instant, and
the UI says so - the state word goes to **Stopped** immediately while the job
list keeps showing the encode that is finishing.

**The watcher keeps queueing the whole time.** A stopped box still walks the
watched folders on its usual interval - it does that either way - and still adds
what it finds to the queue. Queueing is a row in SQLite and nothing else, and
doing it while stopped means the queue is already built the moment somebody
presses Start. The alternative is pressing Start and then waiting up to
`scan_interval_seconds` for anything at all to happen.

Trash pruning keeps running too, on that same sweep. Stopping is about
converting, not about freezing the container.

> **A stopped box with a growing queue is this working, not a bug.** It is the
> single most likely thing to look broken about this feature, so it is worth
> reading twice. The queue depth climbing while nothing converts means the
> watcher is finding your media and the gate is shut, which is exactly the two
> things you asked for.

**Reveals are gated too**, and this is the one consequence worth knowing before
you set a window. A job whose whole content is a rename - a dot-hidden `.mp4`
that needs no encoding, or a file a [skip rule](#skip-rules) protects - is
claimed like any other job, so while the gate is shut it stays hidden and your
media server cannot see it. A Remux that landed at nine in the morning is
invisible until 22:00.

That follows from gating the claim rather than the encode, and it is stated here
rather than discovered. Reveals do keep their queue priority, so they are the
first thing that happens when the window opens rather than waiting behind a
transcode backlog. A conversion still reveals **its own** output the moment it
verifies, window or no window - that rename is part of the job that was already
claimed, and nothing in this feature can strand a converted file behind a
`.tapart` name.

### Auto start, and what a restart forgets

Run state lives **in memory**. It is decided at boot by the `auto_start`
setting and nowhere else:

| `auto_start` | The container boots | A manual Stop survives a restart |
| --- | --- | --- |
| `true` (default) | running | **No.** It comes back running. |
| `false` | paused, waiting for a human | Yes, in the sense that it is paused either way. |

**A manual pause does not survive a restart while `auto_start` is on**, and that
is deliberate rather than an oversight: it is precisely what auto start means.
If you want a box that stays stopped across a `docker restart`, a host reboot or
a Watchtower update, turn `auto_start` off - that is the setting for it. Wanting
"stopped now, and still stopped tomorrow" while leaving auto start on is the one
combination this does not do.

Nothing here is stored per-restart because a persisted pause has a worse failure
mode: a box that comes back from a power cut still paused from a maintenance
window three weeks ago, converting nothing, with no error anywhere.

### The convert window

`convert_window` is a single daily range, `HH:MM-HH:MM`, and empty means always.
Set it to keep encoding off the box while people are watching things on it.

- **It spans midnight**, which is the normal case rather than the edge case:
  `22:00-06:00` is 22:00 tonight through 05:59 tomorrow. Overnight is when a NAS
  is free, so this had to work before anything else did.
- Start is inclusive, end is exclusive. `22:00-06:00` covers 22:00 and does not
  cover 06:00.
- Empty means always. There is deliberately **no way to say "never"** - that is
  the Stop button, which says so on screen. A window whose start and end are the
  same is refused when you save it, with a message telling you to empty the box
  if you meant always.
- A malformed window is refused **at the moment you save it**, not silently read
  as "always". A typo that quietly turned into a 24-hour window is how a library
  gets converted during dinner.
- It gates the same claim the Stop button gates, so everything in [Start, Stop
  and draining](#start-stop-and-draining) applies: the encode in flight when the
  window closes finishes and is revealed, and the watcher keeps queueing all day
  while the window is shut.
- Stop beats an open window. Both have to agree before a worker claims anything,
  which is why the UI shows one state word rather than two switches.

The Queue tab shows the window, the zone, the container's local clock and a
countdown ("opens in 4h 06m", "closes in 41m"). The same sentence goes in the
log, once per transition rather than once per poll, and comes back from
`GET /api/control` as `reason`. One sentence, three places, by construction -
three different phrasings of "paused" is how an operator ends up trusting none
of them.

### Timezone: the trap worth one paragraph

**The container is UTC unless you set `TZ`.** Your NAS is not. The window is
read against the container's clock, and nothing about the mismatch looks wrong
on any screen. This is the paragraph in this section that is actually worth
your time:

> You are in US Eastern in summer (EDT, UTC-4). You want encoding to happen
> overnight, so you type `01:00-06:00` and go to bed. `TZ` is unset, so the
> container is on UTC. UTC 01:00 is **21:00 EDT**: the box starts hammering
> your library at nine in the evening, in the middle of the film everyone is
> watching, and stops at **02:00 EDT**, four hours before you thought it would.
> Every clock in the UI agrees with itself, no error is logged, the window is
> exactly what you typed, and the behavior is wrong all night.

The fix is one line, and it is the same `TZ` variable Radarr, Sonarr and every
other *arr container uses:

```yaml
environment:
  TZ: "America/New_York"
```

`tzdata` is in the image and Python's `zoneinfo` resolves real zone names, so
any [IANA name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)
works and daylight saving is handled for you - `America/New_York`, not `EST` or
`UTC-5`, or you get the wrong answer for half the year.

Because this cannot be validated (a UTC container is a perfectly legitimate
configuration), it is made **visible** instead. Everywhere the window is shown
or logged, the zone and the container's current local time are printed next to
it:

```
clock: 23:48 EDT local, TZ=America/New_York | convert window: 22:00-06:00 | auto start: True
```

Check that line against your own watch on the first boot after you set a
window. If it says `TZ=unset, so UTC` and the time is not yours, that is the
four-hour bug before it happens.

### Stopping the container

`docker stop` is a clean stop, and it is a different thing from the Stop button
above - it ends the process, so it *does* cancel a running encode. SIGTERM and
SIGINT cancel whatever is running, the cancelled encodes write their own
`cancelled` rows, and the server shuts down inside docker's default grace
period. Nothing was registered for SIGTERM before, so every ordinary stop was
dropped on the floor and became a SIGKILL ten seconds later - which meant a
deliberate stop was indistinguishable from a crash in the job history.

Nothing is lost either way: the source is untouched until an encode has been
verified, so a cancelled job costs the time spent encoding and nothing else.

### A file that always fails

The watcher leaves a path alone for `retry_failed_after_hours` (6 by default)
after a job for it failed. Without that wait, a file that cannot be converted at
all - a truncated source, audio nothing here can read - was queued again on
every scan and burned a whole encode attempt every few minutes forever. Queueing
it from the API ignores the wait entirely: somebody asking for a file by name is
not the loop this exists to stop.

**A 10-bit source is not one of those files**, though it used to be. H.264
NVENC cannot encode 10 bits, and handed a `yuv420p10le` source it exits `-22
(Invalid argument)` before the first frame rather than converting - ffmpeg does
not catch it either, because NVENC advertises one shared pixel-format list for
H.264 and HEVC. Since 1.0.1 the picture is narrowed to 8-bit with `-pix_fmt
yuv420p` whenever the source carries more bits than the chosen profile can, so
an arr upgrading a season to 10-bit HEVC releases no longer parks it in the
retry loop forever. The `main10` profile is exempt: it exists to keep those
bits. The narrowing is a CPU filter, so those jobs decode on the GPU and hand
frames back to system RAM rather than staying on the card end to end; 8-bit
sources are unaffected and still run the fully-on-GPU path.

An encode that reports **no progress** for `stall_timeout_minutes` (30 by
default) is killed and its job failed. ffmpeg reports progress roughly twice a
second however slow the encode itself is, so silence means the process is
wedged, typically on a share that went away, and never that the file is large. A
stall is not retried down the fallback ladder - a share that has gone will not
come back between attempts, and each attempt would cost another full timeout of
a held worker slot.

### When the file itself is bad, and Sonarr/Radarr can find another

Some sources are simply unreadable in part. A file that converts to 96% and
then fails `duration mismatch: source 2724s, output 2624s` has about a hundred
seconds ffmpeg cannot get through, and it will fail that way every retry
forever - the verification is correct, and the source is the problem.

Turn on **Ask Sonarr/Radarr to replace an unreadable file** (`REPLACE_BAD_SOURCE`,
off by default) and the arr that owns the file is asked to mark the release it
came from as **failed**, which blocklists it. The arr then searches for a
different release on its own.

**Blocklisting is the point, not a detail.** Deleting the file and searching
lets the arr hand back the identical release, which converts to the identical
failure, forever - the loop this exists to break. Marking the grab failed is
the only thing that takes that release out of the running.

Three things it deliberately will not do:

- **It never fires for a failure of the machine.** Only a verification failure
  that names the source qualifies - short output, missing video, missing audio.
  A GPU whose driver stopped initializing, a share that went away, a name
  already taken: those are conditions of the host and the library, they clear
  on their own, and answering one by retiring a release and pulling gigabytes
  would be the most expensive possible wrong guess.
- **It never deletes your media.** The unreadable file stays exactly where it
  is; the arr replaces it when its own search finds something.
- **It asks once per file.** If the replacement is also unreadable, another
  download will not fix it, and it stops for a person to look.

One consequence worth knowing before you switch it on: when the grab was a
**season pack**, the pack is what gets blocklisted. That is the correct
behaviour - the pack contains the unreadable episode, so any future grab of it
brings the same problem back - but one bad episode can retire the release the
rest of the season came from.

### When the name is already taken

A conversion of `.Show - S01E01.mkv` wants to end up at `Show - S01E01.mp4`. It
is normal to find something already there: an arr upgrades an episode, imports
the new release behind a dot, and the previous conversion is still sitting at
the visible name.

**Since 1.0.2 the incoming conversion wins, and the file it displaces goes to
the trash** rather than under `os.replace`. Before that the job failed with
"target already exists" and never stopped failing - nothing on disk changed
between attempts, so the same file failed every `retry_failed_after_hours`
forever. A real library had 26 of them.

What is still refused, and always will be, is a **different** file appearing at
that name after the job started. The check runs twice - once before the encode
and once immediately before the replace - and the second one compares inode,
size and mtime against the first. An arr finishing an import during a two-hour
encode lands on exactly this name, and that file is newer than the source this
job converted; the encode is the disposable side of that trade. It fails with
`was written by something else while this job ran`, which is deliberately not
the same sentence as the old one.

The hidden staging name (`.Show - S01E01.mp4`) is never displaced under any
circumstances. A file there is a hidden import waiting for its own reveal job,
not a stale output, and taking it would consume somebody else's pending work.

Done, failed and cancelled rows are deleted after `keep_history_days` (30 by
default) on each scan. Nothing deleted them before, and a library-sized run
leaves tens of thousands of rows in the one SQLite file the watcher, the worker
and every HTTP thread are already contending for.

### Hardware encoding

Probed at boot with a real one-second encode per candidate, because a listed
encoder is not a working one. The first that works becomes the automatic choice,
in this order:

```
h264_nvenc  ->  h264_qsv  ->  hevc_nvenc  ->  libx264  ->  libx265
```

`/healthz` and the UI report which one won and why (including the reason each
skipped encoder failed). You can re-probe without restarting from the Encoding
tab; hardware changes under a container more often than the container restarts.

**The HEVC caveat, because the order can reach it:** on a box where `h264_nvenc`
fails but `hevc_nvenc` works, the automatic choice produces HEVC. That is a
smaller file, but older TVs, browsers and streaming sticks cannot direct play
HEVC and will make your media server transcode on the fly instead, which costs
more than the space it saves if it happens often. If that is not what you want,
activate one of the H.264 [profiles](#encoding-profiles) instead. All five are
tested at boot, so the list already says which of them this box can run.

On QNAP Container Station the NVIDIA runtime is registered as `nvidia-runtime`.

### Throttling ffmpeg

Three optional settings that make an encode yield to whatever else the box is
doing. They matter most on a **CPU-only box**, where a libx265 job otherwise
pins every core and the first anyone hears about it is playback stuttering
while their own library is being converted.

| Setting | Env var | Default | What it does |
| --- | --- | --- | --- |
| Encoder CPU priority (nice) | `ENCODE_NICE` | `0` | 0 is normal, 19 is "only take a core nobody else wants". Only ever lowers priority - a negative value would need privileges this container does not have, so it is refused rather than silently ignored. |
| Encode at idle disk priority | `ENCODE_IDLE_IO` | `false` | `ionice -c 3`: read and write only when nothing else wants the disk. Niceness governs CPU, not IO, and on a NAS it is usually the spindles rather than the cores that a 40GB remux takes away from playback. |
| Encoder threads | `ENCODE_THREADS` | `0` | `0` lets ffmpeg decide, which means every core it can find. Cap it to keep a core free for the media server. Applies to `libx264` and `libx265` only - it is read from the encoder's own hardware flag, so the GPU encoders get no `-threads` at all and there is no second list of software encoders to drift. |

`nice` and `ionice` are already in the image and wrap the command rather than
being flags to it, so the log's recorded command line is the command that ran:

```
ionice -t -c 3 nice -n 10 ffmpeg -hide_banner -nostdin -y ...
```

Two details that are load-bearing rather than decorative. `ionice` gets `-t`, so
an IO scheduler that refuses the idle class execs ffmpeg anyway instead of
failing every job at spawn. And both `nice` and `ionice` **exec** ffmpeg in
place rather than forking it, which is why cancel, the stall watchdog and
`docker stop` all still reach the encoder - a wrapper that forked would have
broken cancel without saying so.

Throttling applies to real jobs and to the profile test encode. The boot
probes are deliberately left alone: they are one-second encodes whose result
gates startup, and slowing them down would only make the port open later.

## The web interface

`GET /` is the whole application: the run controls and the queue, jobs with
live progress, watched folders with a folder picker that only shows what the
container can actually see, encoding profiles, conversion rules, Radarr/Sonarr
connections, schedule and throttling and TLS settings, config backup and
restore, and the admin account with its sessions and API keys. No build step and
no CDN - a NAS container that needs npm to draw its own settings screen is one
that stops drawing it the day a registry goes down.

## Signing in

Two ways in, and both end up as the same `Authorization: Bearer` header.

- **An API key.** `TRANSCODEARR_TOKEN` from the container environment is the
  bootstrap one; after that, mint named keys in the UI and give each consumer
  its own revocable one. This is how a script, a dashboard or any other client
  authenticates, and none of it changed at 1.0.
- **A username and password**, once you create an admin account. The login form
  is the front door for a person; the key stays the front door for a machine.

### The admin account

A fresh container has **no admin account**, and the UI says so in a red banner
until you make one. Create it from the Access tab, or:

```
curl -X POST http://nas:8484/api/admin \
  -H "Authorization: Bearer $TRANSCODEARR_TOKEN" \
  -d '{"username": "admin", "password": "at least eight characters"}'
```

The bootstrap token is what gets you in to create the first admin, which is why
that token still matters on a box that has a password. **Once an admin exists,
changing the username or the password costs the current password as well** - a
leaked API key must not be a way to lock you out of your own container, and by
then some integration is holding one of those keys. Forgotten the password
itself? See [Locked out](#locked-out-resetting-the-admin-account).

Passwords are hashed with stdlib `hashlib.scrypt` and a per-password random
salt, with the cost parameters stored per row so they can be raised later
without invalidating anything. **No password is ever stored, logged or
returned**, and no route returns a hash. A failed login logs the client address
and nothing else, because people type their password into the username box.

### It is a bearer token, not a cookie, and that is the point

`POST /api/login` exchanges a username and password for a **session token in the
JSON response body**. The page keeps it in `localStorage` exactly as it already
kept an API key, and sends it as `Authorization: Bearer` on every request.

**No cookie is set anywhere in this service.** That is a deliberate refusal, not
an omission:

- A browser attaches a cookie to *every* request aimed at this origin, including
  ones started by another site you happen to have open. That is CSRF, and it is
  a whole class of vulnerability - plus the tokens, the `SameSite` policy and
  the double-submit machinery to defend against it - that a bearer header simply
  does not have. This API has no CSRF surface today, and adding a login form was
  not worth acquiring one.
- One credential model instead of two. A session token, a minted API key and the
  bootstrap token are interchangeable on every route, so there is one code path
  to get right rather than a header path and a cookie path that disagree about
  what "authenticated" means.
- It stays curl-able and stays scriptable. A session token works from the
  command line exactly like an API key.

Sessions live in their own table with only a **hash** of the token, an expiry
(`session_days`, 30 by default) and a last-used stamp. They are listed in the
Access tab and revocable one at a time, and **Sign out** revokes the one you are
holding. Revoking your own session signs that browser out on its next poll,
which is what "log out everywhere including here" should do.

Signing out with an **API key** in the header is refused rather than obeyed: a
button labeled "sign out" that revoked the key some integration authenticates
with would take that integration down.

### Failed logins back off

Three free attempts, then a doubling wait - 2s, 4s, 8s and so on, capped at five
minutes - answered as `429` with a `Retry-After` header. **The right password
waits too** once the backoff has bitten; a backoff a correct guess can walk past
is not one. An exposed login form with no backoff is a password oracle, and this
service is one whose whole job is spawning processes on your filesystem.

A wrong username and a wrong password give the **same** message, so the form
cannot be used to learn which accounts exist. The counter is in memory rather
than in a table, on the grounds that an unauthenticated caller should not be
able to make this container write a row per guess into the same SQLite file the
watcher and the worker are contending for. A restart clears it, which is the
accepted cost.

The password-change route shares that counter deliberately. It verifies
`current_password` through the same throttle, so it cannot become the
unthrottled oracle sitting next to the throttled one - with the visible
consequence that fumbling your current password in the settings form locks the
login form for the same few seconds.

### Locked out: resetting the admin account

If the admin password is gone, `TRANSCODEARR_RESET_ADMIN` is the way back in.
It is read from the environment once at boot, and it **deletes the admin account
and signs out every session**. That leaves the container in the state it shipped
in: no admin, so an API key gets you in and the page offers creating an account.

**Minted API keys are not touched.** Whatever authenticates with one keeps
working through the whole procedure, because a forgotten password should not
take an integration down with it. Live browser sessions do end - a forgotten
password and a leaked one look identical from here.

It deletes rather than resetting to a temporary password because a temporary
password has to be communicated somehow, and every channel available here - the
log, an environment variable, the response of a route - writes it somewhere it
outlives the recovery.

#### Compose

1. Add the variable to the `environment:` block:

   ```yaml
       environment:
         TRANSCODEARR_RESET_ADMIN: "1"
   ```

   `1`, `true`, `yes` or `on`, in any case. Anything else is off.

2. Recreate the container so it reads the new value:

   ```
   docker compose up -d
   ```

   **`docker compose restart` is not enough.** It restarts the process with the
   environment the container already has; `up -d` is what recreates it against
   the edited file.

3. Read the log and confirm what happened:

   ```
   docker compose logs transcodearr | grep RESET_ADMIN
   ```

   The line naming the account is the one that means it worked:

   ```
   TRANSCODEARR_RESET_ADMIN: deleted the admin account 'admin' and signed out every session. Sign in with an API key and create a new account.
   ```

   If it instead says `TRANSCODEARR_RESET_ADMIN is set but there was no admin
   account to delete`, there was no account in that database - check you mounted
   the `/config` volume you think you did.

4. Sign in with an API key - the bootstrap `TRANSCODEARR_TOKEN`, or any key you
   minted - and create the new admin from the Access tab, or:

   ```
   curl -X POST http://nas:8484/api/admin \
     -H "Authorization: Bearer $TRANSCODEARR_TOKEN" \
     -d '{"username": "admin", "password": "at least eight characters"}'
   ```

5. **Delete the `TRANSCODEARR_RESET_ADMIN` line** and recreate once more:

   ```
   docker compose up -d
   ```

#### `docker run`

A container's environment is fixed when it is created, so this is a recreate
rather than a restart. `/config` is a volume, so the queue, the settings and the
API keys survive it.

```
docker stop transcodearr && docker rm transcodearr
docker run -d --name transcodearr \
  -e TRANSCODEARR_RESET_ADMIN=1 \
  ... every other flag exactly as before ... \
  ghcr.io/managearr/transcodearr:latest
docker logs transcodearr | grep RESET_ADMIN
```

Confirm the same log line, sign in with an API key, create the account, then stop
and remove it and run it once more **without** the `-e TRANSCODEARR_RESET_ADMIN=1`
line.

#### Removing it again is a step, not tidying up

The flag is acted on at **every** boot, not only the one that did something.
Left set on a container with `restart: unless-stopped`, the next restart - a host
reboot, a crash, an image pull - deletes the account you just created, and then
the box has no login at all. That is a worse place to be than the forgotten
password you started with, so every boot with the flag still set logs a second
warning:

```
TRANSCODEARR_RESET_ADMIN is still set. Remove it and restart, or the next restart deletes the account you are about to create.
```

If that line is in your log, the job is not finished.

#### No admin password and no API key either

Step 4 asks for an API key, which looks circular if you have lost both. It is
not, and there are two ways out depending on what the container still has.

If you never minted a key and `TRANSCODEARR_TOKEN` is empty, the reset boot
also leaves the container with no way in at all - which is the condition that
makes it mint one and print it. Read it back from the log exactly as on a first
boot, and use that for step 4.

If you did mint keys once and simply do not know them any more, nothing is
generated, because a container with keys already has a way in. Set
`TRANSCODEARR_TOKEN` instead: it is in the same environment block you are
already editing, so put any unguessable value there alongside the reset flag
and recreate. It is the bootstrap key, and a container with no admin account is
exactly the case it exists for. Afterward, either leave it set or mint a named
key in the UI and clear it; it is a real credential either way, and the literal
`change-me` is refused as a key.

#### Why an environment variable here is not a new weakness

Worth stating plainly, because it is a fair question to ask of software you are
handing your library to. Setting an environment variable on this container
already means being able to read `TRANSCODEARR_TOKEN` out of that same
environment, and to write the config volume `transcodearr.db` sits in. Anyone
who can turn this flag on could already have taken the account by hand with
`sqlite3` and a look at the schema. The flag grants no capability that was not
already there; what it removes is the need to do that by hand.

It is environment-only for the same reason it is safe. A stored setting outranks
the environment in this codebase, so a reset switch that lived in settings would
have to be a route, and every route that can change a stored setting requires the
login you have just lost.

## HTTPS

**A reverse proxy is the better answer, and that is the honest
recommendation.** If this is going anywhere near the internet, or through a
domain name, or in front of anyone but you, put Caddy, Traefik, nginx or your
NAS's own proxy in front of it and let that terminate TLS. A proxy renews
certificates, has been attacked professionally for twenty years, and does not
restart your transcoder to reload a certificate. The built-in TLS below is for
the case a proxy does not cover: a LAN box with no proxy, no domain and no
certificate authority, where the alternative is a password crossing your own
network in clear text.

Either way, **do not expose this to the internet.** TLS encrypts the
credential; it does not make an API that spawns processes on your filesystem
safe to publish. See [Security](#security).

### Behind a proxy

Nothing to configure. Serve plain HTTP, bind to localhost, and have the proxy
send `X-Forwarded-Proto: https` - which every proxy above does by default. That
header is honored, and it is what stops the clear-text warning below from
firing at a setup that is already correct.

### Built in

Two settings, both absolute paths inside the container:

| Setting | Env var |
| --- | --- |
| TLS certificate path | `TLS_CERT` |
| TLS private key path | `TLS_KEY` |

Set both and the listening socket is wrapped with a stdlib
`ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)`. Leave both empty and it serves plain
HTTP exactly as before. TLS is read **at boot**, so a change here needs a
container restart.

**Set one without the other, or point either at a file that cannot be loaded,
and the container refuses to start.** That is deliberate and it is the one
change in 1.0 that can take a live box down on a typo. The alternative - log it
and carry on over HTTP - means the password and the session token cross the
network in clear text while the settings page still says HTTPS is on, and nobody
re-reads the startup log of a container that came up healthy. The log line names
both paths and the fix, and **clearing both settings is always the way back**.
Because these are settings rather than environment, a bad value only bites on
the next restart; the running server keeps serving until then.

### A self-signed pair, for a LAN box

The **Self-signed certificate** box, on the Security card of the System tab,
shells out to the `openssl` already in the image and writes
`/config/tls/cert.pem` and `/config/tls/key.pem` - inside the config volume, so
the pair survives a container recreate. The key is written mode `600`, and the
certificate carries a `subjectAltName`, because browsers have refused CN-only
certificates for years.

It **never overwrites an existing pair** (`409` if either file is there): that
path is exactly where somebody would have put a real certificate. And it does
not save the settings for you - it fills the two paths into the form and leaves
pressing Save a decision you make, since a bad pair stops the container.

Browsers warn once about a self-signed certificate. That is the trade for
encrypting a LAN with no certificate authority on it.

### The warning you will see

If an admin password is set, the server is on plain HTTP, and nothing indicates
a proxy, a warning is logged - at boot, and again on any login that actually
arrives over plain HTTP with no `X-Forwarded-Proto`. That second one is the only
moment a proxy is genuinely detectable, so it is the one that means something.

### The healthcheck already handles both

The image's `HEALTHCHECK` tries `http://127.0.0.1:$PORT/healthz` and falls back
to `https://` with verification off. Turning TLS on does not mark your container
unhealthy forever, and the self-signed pair the button generates does not either.
Nothing is trusted by that probe - it only has to prove the daemon is still
answering on its own port.

## Encoding profiles

Quality, encoder, resolution and audio are **not** individual settings any more.
They are bundled into named encoding profiles, in the HandBrake sense: one
profile is every choice that produces a file, so switching is one decision
instead of six. Exactly one profile is active - the UI marks it **default** -
and it is what every job uses.

### The five that ship

One per encoder, named for the choice somebody is actually making rather than
for a codec they have never heard of. The real question is never "which codec",
it is how fast, how small, and whether the TV will play it:

| Profile | Encoder | What it is for |
| --- | --- | --- |
| **Balanced, on the GPU** | `h264_nvenc` | The safe answer, and the one a fresh install usually lands on. Minutes per film instead of hours, and everything made in the last fifteen years can direct play it. Larger files than libx264 at the same quality. |
| **Quick Sync, on Intel graphics** | `h264_qsv` | The same trade on the GPU built into an Intel CPU. Comparable to NVENC in speed and quality; needs `/dev/dri` passed through. |
| **Half the size, on the GPU** | `hevc_nvenc` | Roughly half the file at similar quality and still GPU-fast. **The catch is playback:** older TVs, browsers and some streaming sticks cannot direct play HEVC, so your media server transcodes it on the fly instead - which costs more than the space it saves if it happens often. |
| **Smallest H.264, on the CPU** | `libx264` | Better quality per byte than NVENC, at perhaps a tenth of the speed. Reasonable for a handful of files, painful for a library. |
| **Smallest of all, on the CPU** | `libx265` | The best compression here and the worst throughput, hours per film on a NAS CPU. Carries the same HEVC playback catch as above. |

**Their settings are not a separate opinion.** Quality, speed preset and codec
profile are read from each encoder's own recommendation, the same numbers the
profile form suggests the moment you pick that encoder, rather than copied into
a second list that drifts the first time somebody tunes one and not the other. That matters because the
quality scales are not interchangeable: CQ 23 on NVENC and CRF 21 on libx264 are
different pictures and different sizes, so the number has to travel with the
encoder.

All five leave the resolution at the source and use AAC stereo at 192 kbps: the
combination every client can direct play. Keeping a 5.1 mix intact is something
you have to ask for, because a silent downmix is the one audio mistake nobody
notices until the receiver stays quiet.

### They are read-only. Duplicate one to make your own

The five cannot be edited and cannot be deleted. **Duplicate** the closest one
and change the copy: the original stays a known-good starting point to compare
against and to fall back to, which an in-place edit would destroy - leaving a
profile whose name still promises "Balanced, on the GPU" while it means
something else entirely.

Your copy is an ordinary profile. It can be edited, deleted, activated, and it
still has to pass a real test encode before it is stored at all.

### Everything is tested with a real encode, and only a pass can be activated

Every stored profile - the five and yours - is put through an actual encode at
boot and the verdict is written down: never tested, tested and failed, or works
here. **A profile that has not passed cannot be activated**, by the UI or by the
API, because activating one is choosing what every future job runs and finding
out on the first film that this box has no Quick Sync is exactly the silent
failure the test exists to prevent.

All five exist on every machine, including the ones this box cannot run. A
profile listed as "will not run on this machine" with the encoder's own error
beside it says more than a name that quietly never appeared, and it is the
difference between "you have no Intel graphics" and "you did something wrong".

The cost is honest and worth stating: the container's port opens later than it
otherwise would, which is why the healthcheck start period is generous, and the
cost scales with how many profiles are stored. Five shipped plus your own is
five-plus real encodes on every boot.

Two buttons exist because that answer is not permanent:

- **Test** on a single profile re-runs the encode for that one and updates its
  verdict.
- **Re-test all** does it for every stored profile, which takes a minute or more
  and is worth it after anything changed underneath.

Hardware changes under a container more often than the container restarts: a
driver reload, a GPU freed by whatever else was holding it, the memory
compaction fix in [Ops notes](#ops-notes-from-the-first-real-deployment-qnap-nvidia-t1000).
A profile that failed at 3am can be perfectly fine now, and the only way to know
is to encode something.

### What a profile holds

| Field | Values |
| --- | --- |
| Encoder | Only encoders that actually probed working on this machine are offered. |
| Quality (CQ/CRF) | 1 to 51. Lower is better looking and bigger. The scales are **not** interchangeable between encoders - CRF 21 on libx264 and CQ 21 on NVENC are different pictures and different sizes - so each encoder carries its own recommendation and sane range. |
| Speed vs size | The encoder's own preset vocabulary (`p1`-`p7` for NVENC, `ultrafast`-`veryslow` for the CPU encoders). Stored normalized to the encoder you picked, so a value left behind by a different encoder cannot abort every job. |
| Codec profile | `high`/`main`/`baseline` for H.264, `main`/`main10` for HEVC. |
| Resolution | 0 (source), or a height from 240 to 4320. **Never upscales** - asking for 1080p leaves a 720p file at 720p. |
| Audio | `aac` (re-encoded, plays everywhere) or `copy` (lossless, but MP4 cannot hold DTS or TrueHD). Bitrate 32-640 kbps. Channels: same as source, stereo, or 5.1. |

### A custom profile cannot be saved until it has been tested

Every field above has a way of being individually plausible and jointly
impossible: `main10` on an encoder built without it, a preset from another
encoder family, copy-audio into a container that cannot hold it, a resolution
the card refuses. None of that shows up in a form and all of it shows up in two
seconds of real encoding.

So saving a profile runs a **real encode** first: a two-second clip with video
and 5.1 audio, encoded with the exact argv a real job would build, then ffprobed
to confirm it produced readable video and audio. A profile that fails is
refused with the ffmpeg error rather than stored. The **Test this profile**
button runs the same check without saving and shows you the command it will run.

One exception is handled rather than refused: if `copy` audio fails but AAC
succeeds, the profile saves with a note saying jobs will re-encode the audio -
which is what a real job would do anyway.

### Upgrading does not change your encoding

An install from before the five had exactly one profile, named **Default**,
seeded from the settings that container was already running. **It keeps every
value it had, and it stays active.** It simply stops being ours and becomes one
of your own profiles: editable, deletable, and driving every job exactly as it
did yesterday. The five appear alongside it, so an upgraded install sees six
rows rather than five.

Nothing is re-encoded by upgrading. Nothing is re-qualitied. The five are new
options in a list, not a new setting applied to your library, and the only way
your encoding changes is you activating something else on purpose.

That carried-over profile is tested at boot like everything else, so it finally
gets a verdict it was seeded without - it was your settings written down, never
proved by an encode. If the test fails it is flagged rather than
switched away from - an upgrade that quietly picked a different profile for you
would be the re-quality this whole design refuses.

`QUALITY`, `ENCODER_PRESET`, `ENCODER_PROFILE` and `MAX_HEIGHT` in a compose file
no longer feed anything. They seeded that Default once, on an install that
predates profiles, and that is the whole of what they ever did. See
[the superseded settings](#settings-and-the-env-vars-that-seed-them).

**Neither the active profile nor a shipped one can be deleted.** Activate
something else first for the active one - deleting what every job is using would
leave the worker with no settings at all - and a shipped profile is refused
outright, because it would only come back on the next boot and reseeding it
behind your back is worse than saying no.

## Configuration

Settings are edited in the UI and stored in the database. **A stored value wins
over the environment from then on** - the env var is only the seed for a key
nobody has set yet. The other way round means every `docker run` with a stale
env silently reverts what someone changed in the UI, and they find out when the
library has been re-encoded at the wrong quality. Each field in the UI shows
whether it is saved there, coming from the container, or still the built-in
default.

### Container-level environment

These are read from the environment on every boot and are not editable at
runtime. They describe the container's shape rather than its configuration.

| Var | Default | What it does |
| --- | --- | --- |
| `MEDIA_ROOTS` | `/media` | Colon-separated. Names the volume mounts themselves, which is why it cannot move into the UI: editing it at runtime would point the worker at paths the container cannot see. Nothing outside these roots is ever touched, browsed or queued. |
| `TRANSCODEARR_TOKEN` | (none) | The bootstrap API key. Without it, and with no minted keys, there is no way into a fresh container. It cannot be revoked from the UI. |
| `TRANSCODEARR_RESET_ADMIN` | (unset) | **A recovery switch, not a setting. Do not leave it set.** `1`, `true`, `yes` or `on` deletes the admin account and every session at boot, so a forgotten password can be recovered with an API key. Minted API keys are kept. It acts on every boot, so a container that restarts by itself will delete the replacement account too - remove it and restart once you are back in. Environment-only, because every route that could change a stored setting needs the login you have just lost. See [Locked out](#locked-out-resetting-the-admin-account). |
| `PORT` | `8484` | HTTP listen port. The image's healthcheck reads the same variable, so changing it does not make the container permanently unhealthy. |
| `CONFIG_DIR` | `/config` | Holds `transcodearr.db`. The only path the entrypoint ever chowns. Sources trashed by a pre-0.9 version are still under `/config/trash` and still expire on schedule; nothing new is written there. |
| `TRASH_DIR` | (empty) | Overrides where replaced sources go. Empty means the default: a `.transcodearr-trash` directory under whichever media root holds the source. See [Trash](#trash) - the default is what you want on a NAS. A value that **is** a media root or sits above one (`/media`, `/`) is refused at startup with an error in the log and the default used instead: the trash is pruned by walking it and deleting anything past `trash_keep_days`, so a trash root containing your library is a scheduled delete of your library. |
| `TZ` | (unset, meaning UTC) | An IANA zone name (`America/New_York`). **Set it.** Every clock this container prints is its own, including the one the [convert window](#the-convert-window) is read against, so an unset `TZ` on a NAS that is not UTC runs a window hours away from where you meant it. `tzdata` is in the image and `zoneinfo` resolves real names, so daylight saving is handled - use the zone name, never `EST` or `UTC-5`. |
| `PUID` | `1000` | The uid the daemon and every ffmpeg child run as. |
| `PGID` | `1000` | The gid, likewise. |
| `NVIDIA_DRIVER_CAPABILITIES` | `compute,video,utility` | Set by the image. `compute` is not optional: NVENC initializes through CUDA and dies at `cuInit` without libcuda. |
| `NVIDIA_VISIBLE_DEVICES` | `all` | Set by the image. |

### Settings, and the env vars that seed them

Everything in this table lives in the database once it has been saved in the UI.
The env var **only seeds a first boot** - after that it is ignored for that key,
and the UI will say the value is "stored".

Lists (`paths`, `extensions`, `patterns`) are split on colons, commas or
newlines. Booleans accept `1`, `true`, `yes`, `on` (any case).

| Setting | Env var | Default | Notes |
| --- | --- | --- | --- |
| Watched folders | `WATCH_ROOTS` | (empty) | Folders walked for work. Empty means every media root. Must be absolute and inside `MEDIA_ROOTS`, checked when saved: a watch root outside the mounts cannot produce a job, but it would still send the scanner walking the host filesystem every few minutes. A folder that does not exist is skipped in silence. |
| Scan interval (seconds) | `SCAN_INTERVAL_SECONDS` | `300` | How often the watched folders are walked. Floored at 15 regardless of what you set. |
| Stability window (seconds) | `STABLE_SECONDS` | `120` | A file is only touched once its size has held still for this long. Never based on modified time, which imports preserve from the release. |
| Only convert dot-hidden files | `HIDDEN_ONLY` | `false` | Off, the default, makes every matching file in the watched folders eligible - so point them at one small folder first, and read [What it may convert](#what-it-may-convert). On, only files whose name starts with a dot are eligible, which keeps your media server from ever seeing a file that is about to be replaced; nothing writes that dot for you, so read [Only convert dot-hidden files](#only-convert-dot-hidden-files) first. Turning it **off** in the UI asks you to confirm and names the trash retention window, because off is the widening direction. |
| Convert these extensions | `CONVERT_EXTENSIONS` | `.mkv .avi .m4v .m2ts .mts .vob` | Dot-hidden `.mp4` files are revealed without re-encoding regardless of this list. |
| Never convert files matching | `SKIP_PATTERNS` | (empty) | Case-insensitive substrings matched against the file name. See [Skip rules](#skip-rules). |
| Duration tolerance | `VERIFY_DURATION_TOLERANCE` | `0.015` | How far the output length may drift from the source before the result is rejected, as a fraction: `0.015` is 1.5%. Capped at 0.5, because a tolerance loose enough to accept half a film is not a tolerance. |
| Decode on the GPU too | `HARDWARE_DECODE` | `true` | The encoder was always on the GPU; the decoder was not, and decoding 1080p in software is what actually pins a NAS CPU. Measured on a real episode: 21.3s of CPU became 3.9s and the job ran 45% faster, for a byte-identical file. NVIDIA encoders only; ffmpeg falls back to CPU decoding by itself for anything the card cannot decode. |
| Convert at once | `MAX_CONCURRENT` | `1` | 1 to 8. One at a time suits a NAS: a single set of spindles behind a single network link turns two encodes into two slow ones. Takes effect on the next job, no restart needed. |
| Retry failed files after (hours) | `RETRY_FAILED_AFTER_HOURS` | `6` | How long the watcher leaves a file alone after a job for it failed. `0` retries on the next scan. Queueing a file from the API ignores this. |
| Stall timeout (minutes) | `STALL_TIMEOUT_MINUTES` | `30` | An encode reporting no progress for this long is killed and its job failed. `0` turns the watchdog off, and a stalled job then holds its worker slot for the life of the container. Any other value must be 1 to 1440 - a watchdog shorter than the progress interval would kill healthy encodes. |
| Keep replaced sources (days) | `TRASH_KEEP_DAYS` | `7` | How long a replaced original survives in the trash. Raise it before a large batch. |
| Keep job history (days) | `KEEP_HISTORY_DAYS` | `30` | Done, failed and cancelled rows older than this are deleted on the next scan. `0` keeps every row forever. |
| Start converting on boot | `AUTO_START` | `true` | On, the container boots converting. Off, it boots paused and waits for someone to press Start. Run state is in memory, so **a manual pause does not survive a restart while this is on** - see [Auto start](#auto-start-and-what-a-restart-forgets). |
| Only convert between | `CONVERT_WINDOW` | (empty, meaning always) | One daily range, `HH:MM-HH:MM`. Spans midnight (`22:00-06:00`). **Read in the container's timezone**, which is UTC unless you set `TZ`. Only new work is gated; the encode in flight always finishes and the watcher keeps queueing. See [The convert window](#the-convert-window). |
| Webhook URL | `WEBHOOK_URL` | (empty) | POSTed a JSON summary when a job finishes, done or failed. Must start `http://` or `https://`. See [Job webhook](#job-webhook). |
| Webhook signing secret | `WEBHOOK_SECRET` | (empty) | Optional. When set, each call carries an HMAC-SHA256 signature header. **Secret**: it is masked in the API and the UI and is never in a backup. |
| Encoder CPU priority (nice) | `ENCODE_NICE` | `0` | 0 to 19. See [Throttling ffmpeg](#throttling-ffmpeg). |
| Encode at idle disk priority | `ENCODE_IDLE_IO` | `false` | `ionice -c 3` on the ffmpeg process. |
| Encoder threads | `ENCODE_THREADS` | `0` | 0 to 64. `0` lets ffmpeg take every core. Software encoders only. |
| TLS certificate path | `TLS_CERT` | (empty) | Absolute path to a PEM certificate. Both halves or neither, or the container refuses to start. See [HTTPS](#https). |
| TLS private key path | `TLS_KEY` | (empty) | Absolute path to the matching PEM key. |
| Stay logged in for (days) | `SESSION_DAYS` | `30` | 1 to 365. How long a browser session lasts before the password is asked for again. API keys are not sessions and never expire. |

A setting marked **secret** above comes back from `GET /api/settings` as
`********` rather than its value, so the webhook signing secret is not shipped
to every browser that opens the settings tab. Posting that mask back keeps the
stored value; posting an empty string clears it. That is what makes saving the
whole form safe.

Superseded by [encoding profiles](#encoding-profiles) and hidden in the UI. They
are kept because an install from before profiles seeded its **Default** from
them, and that profile is still driving that library - not because they are
still read by anything that encodes:

| Setting | Env var | Default |
| --- | --- | --- |
| Quality (CQ/CRF) | `QUALITY` | `24` |
| Encoder | `FORCE_ENCODER` | (empty, meaning auto) |
| Speed vs size | `ENCODER_PRESET` | (empty, meaning the encoder's balanced default) |
| Codec profile | `ENCODER_PROFILE` | (empty, likewise) |
| Resolution | `MAX_HEIGHT` | `0` (source) |

`FORCE_ENCODER` is the one with anything left to do: it still decides which
encoder wins the probe, and therefore which of the five a **fresh** install
activates. It does not override the encoder inside a profile - the active
profile is what runs.

A stored value that no longer parses is ignored rather than fatal, and the UI
will show the value as coming from the environment or the default instead, so
the override is visible rather than mysterious.

### Backup and restore

`GET /api/backup` returns your settings, your custom profiles and your arr
connections as one JSON document, and `POST /api/restore` applies it. Buttons
for both are in the System tab.

```
curl -H "Authorization: Bearer $KEY" http://nas:8484/api/backup -o transcodearr-config.json
curl -H "Authorization: Bearer $KEY" -X POST http://nas:8484/api/restore -d @transcodearr-config.json
```

**No secret ever leaves.** Not an arr API key, not a token or session hash, not
the admin password hash, not the webhook signing secret. A backup is a file
people attach to a forum post when they want help with it, and it has to be safe
to do that with.

**It is configuration, not data.** Job history and the trash are not in it, and
nothing in the document describes a media file.

The file is stamped with the format version and the TranscodeArr version that
wrote it, and **a backup written by a newer version is refused** rather than
half-applied. Both checks exist: the format number is the hard gate, and the app
version is compared numerically, because as text `0.10.0` sorts below `0.9.1`
and a string compare would wave through exactly the newest backups.

Four things a restore deliberately does not do, each reported in the `changed`
list it returns so you can see what was skipped:

- **It never touches the profile that is currently active.** That is what every
  future job runs, and rewriting it from an uploaded file would change how a
  library is encoded halfway through without anybody choosing it.
- **Every restored profile comes back untested and cannot be activated** until a
  real test encode passes here. A backup proves somebody once had those
  settings; it does not prove this machine has that GPU, and it is usually a
  different machine being restored onto.
- **Restored arr connections arrive with no API key and disabled**, because no
  key was in the backup and a keyless connection would 401 on every rescan and
  mark itself in error. Type the keys back in and re-enable them.
- **Shipped profiles are skipped.** They are re-seeded from code on every boot.

Ids travel with the document, so restoring the same backup twice **updates**
rather than duplicating - a second copy of every arr connection would rescan
your library twice. Only stored settings are exported, never effective ones: a
value currently coming from the container environment belongs to the
container's shape, and writing it into the database on the far side would pin it
there and make the next `docker run` with a corrected env silently do nothing.

Everything is validated before anything is written, so a bad connection at the
bottom of the file cannot leave the settings applied and half a restore done.

## Skip rules

A file whose name matches a skip rule is **left in its own container rather than
re-encoded**. Arr naming puts the quality in the file name, so `Remux` is usually
the only rule worth writing: it keeps disc-quality copies whole, and a media
server plays MKV perfectly well.

A protected file that is dot-hidden is still un-hidden - the job becomes a
reveal, so the rule saves the encode without leaving the file stuck behind a dot.
A protected file that is already visible has nothing to do, and the job says so
and finishes.

Plain case-insensitive substring matching, not a regex - the thing people
actually want to write is "Remux", and a regex dialect is a way to get that
wrong. The rules are re-checked when a job runs, not only when it is queued, so
a rule added while hundreds of files are already in the queue protects them too.

## Trash

Replaced sources are moved, never deleted. The path is mirrored relative to the
media root that held it, so recovering a file is a move back to where it came
from. The trash is pruned by age on every scan.

**Two files can go in from one job.** When a conversion lands on a name that
already holds a file, that file is displaced into the trash too, and the job
reports it as a warning naming where it went. Both copies of the episode then
outlive the swap by the full retention window. See
[When the name is already taken](#when-the-name-is-already-taken).

### Looking in it, and getting something back

The **Trash** tab lists everything in there with where it came from, its size
and how many days it has left, and does per-row or bulk **Restore** and
**Delete**. Retention lives on that tab too - it is the setting somebody is
actually thinking about while looking at the list.

It pages 100 at a time, and **a selection survives paging**: ticks are held by
path outside the page, so you can gather files across several pages and act on
all of them at once. The header checkbox takes the current page, **Select all**
takes every page, and **Clear selection** empties it. The count shows the total
size selected, and says when the selection includes files you can no longer see.
A selection larger than the 500-file batch cap is sent as several requests
rather than one - the cap is what stops a single accidental request from moving
an entire library - and anything that fails stays selected so it is clear what
was not dealt with.

**Restore can replace what is in the way.** That is the case it exists for: an
arr imports an upgrade, the conversion of it replaces the previous one, and the
upgrade turns out worse. It asks first, naming what it would replace, and the
file it pushes aside is **trashed rather than deleted** - the thing being
displaced by a restore is itself a restore candidate ten minutes later.

Restoring a **source** puts it back in the queue, and the list says so before
you press anything: a dot-hidden `.mkv` is exactly what the watcher finds, so
it is converted and trashed again within a scan interval. Restoring a replaced
**output** - a visible `.mp4` - is not eligible for anything and simply stays.
That second one is what the button is usually for.

A file trashed before 1.0.3 has no recorded origin, so its destination is
derived by reversing the mirroring and the row is marked. That derivation
cannot undo the `.1` a second trashing of the same relative path appends, and
deliberately does not try - a name you can see and correct beats a silent
restore over the wrong file.

Retention is measured from **when a file was trashed**, not from its mtime.
Imported media carries the release's own timestamp (a median of twelve years old
in a real library), so pruning by that inherited date deleted every source older
than the retention window on the very next sweep, minutes after a job reported
"source preserved". The file is restamped on the way in for exactly that reason.

A source goes to a `.transcodearr-trash` directory **under the media root that
holds it**, so the move is a rename on one filesystem and cannot half-finish:

```
media root                /media
source                    /media/Movies/Film (2026)/Film (2026).mkv
goes to    /media/.transcodearr-trash/Movies/Film (2026)/Film (2026).mkv
```

The trash sits at the top of the **media root**, not next to the file, and the
path under it mirrors the path under the root - so recovering a file is moving
it back up the same relative path.

That is a change from the pre-0.9 default of `/config/trash`. On a NAS `/config`
and your media are different mounts, so every replaced source was a full byte
copy onto the config volume, held there for `trash_keep_days` - 127 GB of them
were measured sitting next to the SQLite database on the live deployment.

The dot prefix is not decoration: it keeps the trash invisible to the media
server, and the watcher skips dot-prefixed directories, so a trashed source is
never picked up and converted a second time.

If a destination already exists the new copy is suffixed (`.1`, `.2`) rather
than replacing it. Processing the same relative path twice inside the retention
window would otherwise destroy the older source, which is the one somebody
actually wants back.

`TRASH_DIR` still overrides the location and keeps the same media-root-relative
mirroring, so recovery is still a move back to where the file came from. Put any
override on the same mount as your media, or you are back to copying 40 GB
files. An override with a plain (non-dot) name inside a media root is excluded
from the scan explicitly, since the dot rule cannot cover it.

**An older install's `/config/trash` keeps being pruned**, with the same
`trash_keep_days` against the same trashed-at timestamps, so what is already in
there expires on its original schedule instead of sitting in the config volume
forever. Nothing new is ever written to it.

## Users, groups and permissions

The container starts as root, drops to `PUID:PGID` (default `1000:1000`) with
`setpriv`, and execs the daemon as PID 1 from there. Every file it creates - the
converted MP4, the trashed source - is owned by that uid and gid.

That matters because there was no such drop before: python3 and every ffmpeg
child ran as root, each revealed file landed as `root:root` inside a library an
arr owns as 1000, and that arr then could not upgrade or delete its own media
days later, nowhere near the cause. Match `PUID`/`PGID` to whoever owns your
media, exactly as with the *arrs.

Three things worth knowing:

- **The first start after upgrading chowns `/config` recursively** to
  `PUID:PGID`. That is deliberate - a container that ran as root left
  `transcodearr.db` root-owned, and a database the new uid cannot write is a
  daemon that starts and then fails every job - but it is a one-time visible
  change.
- **The media tree is never chowned. Ever.** It is terabytes we mount
  read-write, not ownership we get to rewrite. If `PUID` cannot write your
  library, jobs fail at rename time rather than at startup.
- `PUID=0` **and** `PGID=0` together mean "stay root", for hosts where nothing
  else can write the mount (an NFS export without `no_root_squash`, a share with
  fixed ownership).

For Intel QSV and AMD VAAPI, `/dev/dri/renderD128` is mode 660 owned by a
render or video group whose gid differs on every host. The entrypoint adds the
dropped user to whatever group owns each device node it finds, because skipping
that does not fail loudly: QSV and VAAPI simply probe as unavailable and every
job quietly re-encodes on the CPU instead. NVIDIA needs none of this, its device
nodes are world-readable.

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

A rescan is never fatal to a job: the media is already correct on disk, and a
conversion is not going to be undone because an arr was unreachable. The result
lands in the job's `rescan` field and, when a connection is failing, on the
connection itself in the UI.

## Job webhook

Set `webhook_url` and every job that reaches `done` or `failed` POSTs a JSON
summary there. It is the same spirit as the arr rescan above, and it lives under
the same rule:

> **A webhook can never fail a job, never block one, and never take the worker
> down.** It is sent after the media is already correct on disk, from a
> background thread, with a 10 second timeout, and every exception in both
> halves is swallowed and logged. A receiver that is down, slow, or wrong is a
> log line and nothing else.

### The payload

```json
{
  "event": "job.done",
  "version": "1.0.0",
  "sent": 1755400000.123,
  "job": {
    "id": "...", "path": "/media/Movies/Film (2026)/.Film (2026).mkv",
    "state": "done", "kind": "transcode",
    "created": 1755399000.0, "started": 1755399010.0, "finished": 1755400000.0,
    "progress": 100, "encoder": "h264_nvenc",
    "warning": "", "error": "", "output": "/media/Movies/Film (2026)/Film (2026).mp4",
    "rescan": "Radarr: rescan requested",
    "src_bytes": 41203847264, "out_bytes": 6120384726
  }
}
```

`event` is `job.done` or `job.failed`. `job` is **exactly** the object
`GET /api/jobs` returns - the same function builds it, so the two cannot drift.

`log_tail` is deliberately not in it, by the same rule that keeps it out of the
job list: it holds the full ffmpeg argv and this container's absolute paths, and
a webhook is a bulk export of those to whatever address is in a settings field.
Call `GET /api/jobs/{id}` with a key if you want it.

Headers are `Content-Type: application/json` and `User-Agent: TranscodeArr`.

### The signature

Set `webhook_secret` and each call carries:

```
X-TranscodeArr-Signature: sha256=<hex hmac-sha256 of the exact request body>
```

Signed over the **exact bytes on the wire**, so a receiver verifies what it was
sent rather than what it re-serialized - re-encoding the JSON before hashing it
is the usual way this check ends up failing on whitespace. Verifying it in
Python:

```python
import hashlib, hmac

def verify(raw_body: bytes, header: str, secret: str) -> bool:
    want = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header)   # constant time, not ==
```

Without a secret the call still goes out, unsigned. Anyone who learns the URL
can then forge a notification, which is fine for a script that only refreshes a
dashboard and not fine for one that moves files.

### The guard on the URL

A webhook URL is operator-supplied egress exactly like an arr base URL, so it
goes through **the same link-local guard**, not a second copy of the rule that
would be the one nobody updates. `169.254.0.0/16` and `fe80::/10` are refused,
including their IPv4-mapped and 6to4 encodings and any hostname that resolves to
them - the cloud metadata service is the one destination with real credential
value and it is never a webhook receiver. Redirects are refused too: a `302`
would replay the POST at an address the guard never got to look at, which would
make the check decorative. A refusal is logged, not raised.

The same honest caveat as the arr connections applies: LAN, Docker-network and
loopback receivers are reachable by design, because that is where they live. See
[Residual SSRF](#residual-ssrf-stated-honestly).

Webhook threads are daemons, so a call in flight is lost if the container stops
in that same second. The job row is already correct and durable; only the
notification is dropped.

## API

Everything is under `/api`, apart from the page itself, `GET /healthz` - which
sits outside the namespace, unauthenticated, because a health check that needs a
key cannot report a missing key - and `GET /metrics`, which is at the path
Prometheus expects. Three routes also still answer at the root; they are the
pre-0.9 spellings and they are still here, see [below](#the-pre-09-route-spellings).

```
Authorization: Bearer <api key, session token or the bootstrap token>
```

**Exactly two routes are reachable without one:** `GET /healthz` and
`POST /api/login`. A session token minted by the login form, a key minted in the
UI and the `TRANSCODEARR_TOKEN` from the environment are interchangeable
everywhere else, so nothing that authenticated with a key before needs to
change.

A missing or wrong token is `401 {"error": "missing or wrong bearer token"}`.
**Every error response in the API is `{"error": "..."}`**, at whatever status
code fits, so a client needs one shape. Malformed JSON on a `POST` or `PUT` is
`400 {"error": "invalid JSON"}` - as is a body that is not a JSON object, or one
over 1 MB. Nothing this API accepts is bigger than that, and a request body is a
trust boundary now that one of them arrives without a token.

Routes are matched **exactly**, after the query string is stripped. `/queue` is
the queue; `/queuegarbage`, `/jobsanything` and `/api/jobs/{id}/anything` are
`404 {"error": "not found"}`.

`TRANSCODEARR_TOKEN=change-me` is refused as a key - a published image makes that
exact string public - so a container set that way is unreachable, logs a warning
naming `openssl rand -hex 24` at startup, and reports `auth_configured: false`.

### The pre-0.9 route spellings

`/jobs`, `/jobs/{id}` and `/queue` still work and are the same handlers as
`/api/jobs`, `/api/jobs/{id}` and `/api/queue`. They are kept because the live
deployment and older clients call them.

**They are still here at 1.0**, which 0.9.1 said they would not be. Removing
them was a promise made before there was a way to know who was calling them, and
the log line added for exactly that purpose has not yet had a release's worth of
production to report from. Breaking a live integration on the release that
promises a frozen API is the wrong trade, so **they go at 2.0** instead. Move to
the `/api` spelling; the container still logs one line per old path it is being
called on, once, so you can see who has not moved.

They differ from the `/api` spellings in exactly one thing, and that has not
changed either.

The one difference is the envelope on a **single** job.

| | `/api/jobs/{id}`, `POST /api/jobs` | `/jobs/{id}`, `POST /jobs` |
| --- | --- | --- |
| Single job | `{"job": {...}}` | the bare job object |
| Lists | `{"jobs": [...]}` | `{"jobs": [...]}` |
| Cancel receipts | `{"cancelled": id}` / `{"canceling": id}` | identical |

### Jobs and queue

| Route | What |
| --- | --- |
| `POST /api/jobs` `{"path": "..."}` | Queue a file. `201 {"job": {...}}`. The retry cooldown never applies here. |
| `GET /api/jobs?state=&limit=&before=` | Job history, newest first. `{"jobs": [...], "total": n}`. |
| `GET /api/jobs/{id}` | One job, plus `log_tail`. `200 {"job": {...}}` or `404 {"error": "no such job"}`. |
| `DELETE /api/jobs/{id}` | Cancel. `200 {"cancelled": id}` if it was still queued; `202 {"canceling": id}` if it was running (the encode is terminated and the source is untouched); `404` if there is no such job; `409 {"error": "job is done"}` if it already finished. |
| `GET /api/queue?limit=` | The live picture, in the order it will actually happen. `limit` defaults to 100, clamped 1-500, and caps the `queued` list only. |
| `POST /api/scan` | Walk the watched folders **now** rather than at the next interval, and report what was there. `200 {"scanned": true, "queued": n, "eligible": n, "settling": n, "cooling": n, "already_queued": n, "skipped_visible": n, "missing_roots": [...], "at": <epoch>}`. `scanned: false` with a `detail` when a scan was already running - it is refused rather than queued behind one, because a library walk is minutes and the caller is holding an HTTP response open. `eligible` counts the files in the watched folders that this configuration would convert; `settling` are the ones whose size has not held still for `stable_seconds` yet; `skipped_visible` are the ones passed over for not being dot-hidden. **This route ignores the retry cooldown** for the same reason `POST /api/jobs` does - somebody pressing it is asking about those files now - so `cooling` is normally 0 here and non-zero only in the watcher's own scans. An empty queue and an empty library look identical from `GET /api/queue`, which is the whole reason this answers with counts instead of just doing the work. |
| `GET /api/trash?limit=&offset=` | What is in the trash. `200 {"entries": [...], "total": n, "shown": n, "offset": n, "limit": n, "bytes": n, "keep_days": n}`, newest first. `limit` defaults to 100 and is clamped to 1-500 - the ceiling is the batch cap below, so a page can always be acted on in one request. `offset` past the end is clamped to the **start of the last page**, not to the end of the list: a bulk delete shortens this list under whoever ran it, and the alternative is a blank table with a working Previous button and no clue why. `total` and `bytes` describe the whole trash, not the page. Paged by offset rather than by a cursor, unlike the job history - the list is rebuilt and re-sorted per request anyway, and every operation is addressed by path, so a row shifting between two page loads cannot make a delete land on the wrong file. Each entry: `path` (where it is now), `original` (where Restore returns it), `origin_known` (false when it was derived rather than recorded - see below), `bytes`, `at`, `job_id`, `occupied` (something already holds `original`), `reconverts` (restoring it would put it straight back in the queue). `occupied` and `reconverts` are computed per request, never stored: the library moves under this view and a stale "free" is what would make Restore quietly replace something. |
| `POST /api/trash/restore` `{"paths": [...], "replace": false}` | Put files back. `200 {"results": [...], "ok": n, "failed": n}` with one result per path, because a bulk call partly succeeding is the normal case and reporting it as one verdict is how somebody concludes a file moved when it did not. Without `replace`, a path whose destination is occupied fails with `occupied: true` and nothing is touched. With it, **the file in the way is moved to the trash, not deleted** - it is itself a restore candidate ten minutes later. At most 500 paths, refused rather than truncated. |
| `POST /api/trash/delete` `{"paths": [...]}` | Delete from the trash now instead of at retention. Same result shape and same cap. |

**`cancelled` and `canceling` really are spelled differently, and neither is a
typo.** `cancelled` is the job STATE - a stored database value and a frozen enum
other software matches on - so that spelling stays whatever it was. The `202` is
not a state, it is a receipt saying the terminate was sent, so it takes the
American spelling the rest of this project uses.

**`POST /api/jobs` accepts a path in somebody else's terms.** The path is tried
as this container's own first; failing that, every **enabled** arr connection's
`arr_path` -> `worker_path` prefix is applied in turn, so Sonarr's
`/tv/Show/ep.mkv` resolves to `/media/TV/Show/ep.mkv` and a webhook payload can
be forwarded unchanged. A translated path is re-checked through the same
containment guard as any other - the mapping is operator-editable, so it is not
trusted - and a disabled connection is not a mapping. The error names the path
the caller actually sent, not the last failed translation.

Refusals are `400` with the reason: `"no path given"`, `"... is outside every
configured media root"`, `".x is not a video extension"`, `"path contains a
control character"`, `"refusing to process a staging file"`.

A duplicate is `409 {"error": "already queued or running for this path", "job":
{...}}`, carrying the job that already exists so that "make sure this is queued,
then track it" is two calls rather than a queue listing matched on path. `"job"`
is `null` in the race where the duplicate finished in between.

`GET /api/queue` returns `running` and `queued` job objects plus `queued_total`,
`seconds_per_job`, `eta_seconds`, `sampled` and `max_concurrent`. The estimate
is measured from real completions and counts transcodes only: a reveal is a
rename that finishes in milliseconds, and averaging those in would promise that
a queue drains in minutes when it actually takes days.

### The job object

The same shape everywhere a job is returned, listed or single:

```
id  path  state  kind  created  started  finished  progress  encoder
warning  error  output  rescan  src_bytes  out_bytes
```

That list is the contract, not the database row. Columns the worker keeps for
its own bookkeeping (`priority`, and anything added later) are not exposed -
shipping the whole row made the SQLite schema itself the public API.

`log_tail` is the exception, and it is **opt-in**: only `GET /api/jobs/{id}`
returns it, because somebody asking for one job is debugging it and the ffmpeg
argv is the answer, whereas sixty of them in a list is a bulk export of absolute
container paths to anyone holding any key. It carries the exact argv that ran,
and on success the trash path the source was preserved at.

`kind` is `transcode` or `reveal`. `state` is one of `queued`, `running`, `done`,
`failed`, `cancelled`. `created`, `started` and `finished` are epoch floats
(`started`/`finished` null until set). `progress` is a percentage that stops at
99 while encoding and becomes 100 when the job is done.

### Paging the history

`GET /api/jobs` takes `limit` (1-200, default 50), `state`, and `before`.

- **`state`** must be one of the five. An unknown value is `400 {"error": "state
  must be one of queued, running, done, failed, cancelled"}`. It used to fall
  through and match nothing, so a typo or a renamed state read exactly like an
  empty history - the same silence this worker exists to remove.
- **`before`** is a cursor: pass the last job's `created` back to get the next
  page. Unparseable is `400 {"error": "before must be a job's created
  timestamp"}`.
- **`total`** counts the `state` filter and ignores the cursor, so it is the size
  of the filtered set rather than of the page.

Paged by cursor rather than by offset because rows are pruned by
`keep_history_days` while new ones arrive constantly, so page 2 of an offset walk
is taken against a different list than page 1 and quietly repeats or skips
whatever moved across the boundary.

An out-of-range `limit` is clamped and an unparseable one falls back to 50,
rather than erroring like the other two: a bad limit still returns the right set
at the default size, while a bad `state` or cursor would return the wrong set
entirely.

### Tracking a job

**There is no event stream.** Either poll `GET /api/jobs/{id}` (or
`GET /api/queue` for the whole picture), or set a [webhook](#job-webhook) and be
told when a job finishes. The UI polls every few seconds and that is the
intended pattern for a client that wants live progress; the queue is a local
SQLite file, so a poll is cheap. The webhook is the right answer for "tell me
when this is done" and carries the finished job object with it.

`queued` and `running` are the non-terminal states. `done`, `failed` and
`cancelled` are terminal, and `finished` is set. A `done` job has `output` (the
visible path) and may have a `warning` - a dropped subtitle track, an audio
stream that could not be copied. `rescan` is filled in shortly after the job is
marked done, so a client that reads the row the instant it turns `done` may see
it empty.

### Settings, profiles, connections

| Route | What |
| --- | --- |
| `GET /api/settings` | Every setting, its current value, where that value came from (`stored`, `env` or `default`), and the spec the UI renders itself from. Each spec carries `secret`, and a set secret's value comes back as `********` rather than itself. |
| `PUT /api/settings` | Partial body of `{key: value}`. Everything is validated before anything is written, so a bad third field cannot leave the form half-saved. `200 {"saved": [keys]}` or `400`. |
| `GET /api/profiles` | Profiles, plus the encoders that actually work on this machine and the options each of them accepts. **Already ordered**: the five shipped ones in encoder-probe order, then your own oldest first. |
| `POST /api/profiles/test` | Run the test encode without saving. `200 {"ok": bool, "detail": "...", "command": "..."}`. |
| `POST /api/profiles` | Create. Runs the test encode first and refuses with `400` if it fails. `201 {"profile": {...}, "detail": "..."}`. |
| `POST`/`PUT` `/api/profiles/{id}` | Update, same rules, `200`. Two spellings of one handler: `POST` is what the bundled UI sends, `PUT` matches `PUT /api/arrs/{id}`. `404 {"error": "no such profile"}` for an unknown id, checked **before** the test encode so a dead id does not cost two seconds of ffmpeg first. `400` for a shipped profile, which cannot be edited. |
| `POST /api/profiles/{id}/test` | Re-run the test encode against one **stored** profile and record the verdict on it. `200 {"profile": {...}, "ok": bool, "detail": "..."}`, or `404`. |
| `POST /api/profiles/retest` | The same for every stored profile. `200 {"profiles": [...]}`. |
| `POST /api/profiles/{id}/activate` | Make it the profile every job uses. `200 {"profile": {...}}`; `404` for an unknown id; `409 {"error": "..."}` when the profile has not passed a test encode on this machine. Two codes because a client retrying the second forever would never succeed and needs to be able to tell them apart. |
| `DELETE /api/profiles/{id}` | `200 {"deleted": id}`. `404` if there is no such profile; `409` if it is the active one or one of the shipped five. The active case succeeds the moment something else is activated, which is what `409` means and `400` does not. |
| `GET /api/encoders` | What each encoder is, whether it probed working, and why. |
| `POST /api/encoders/probe` | Re-run the boot probe now. Hardware changes under a container more often than the container restarts. |
| `GET`/`POST` `/api/arrs`, `PUT`/`DELETE /api/arrs/{id}` | Radarr/Sonarr connections. The API key is never sent back to the browser, and an empty key on edit means "keep the one you have". A `PUT` or `DELETE` on an unknown id is `404 {"error": "no such connection"}`; `400` is a real validation failure. |
| `POST /api/arrs/test` | `200 {"ok": bool, "detail": "..."}` either way - a failed connection is an answer, not an error. Send `{"id": ...}` with no key to test a stored connection. |
| `GET`/`POST` `/api/tokens`, `DELETE /api/tokens/{id}` | API keys. The raw key is returned once, on creation, and only its hash is stored. |
| `GET /api/fs?path=` | Directories under the media roots, for the folder picker. Containment is checked on the resolved path, so a symlink or a `../` cannot walk it out of the mounts. |
| `GET /api/system` | Host snapshot: what the box is doing, how many jobs are converting, the concurrency limit. |

**Every route with "test" in it runs real ffmpeg and answers in seconds, not
milliseconds.** `POST /api/profiles/retest` on five-plus profiles can take a
minute. Give those calls a generous client timeout; that cost is the feature.

`PUT /api/settings` treats `********` on a secret key as "keep what is stored",
so the UI can save the whole form without clearing the webhook secret. An empty
string clears it.

### The profile object

```
id  name  encoder  quality  preset  profile  max_height
audio_codec  audio_bitrate  audio_channels
active  builtin  usable  validated_at  validated_ok  validated_note  created
```

`active` and `builtin` are booleans, `created` and `validated_at` are epoch
floats (`validated_at` is null until something has tested it).

- **`builtin`** is one of the five shipped profiles: read-only, so `POST`/`PUT`
  on it is `400` and `DELETE` is `409`.
- **`validated_ok`** is `null` never tested, `0` tested and failed, `1` works
  here. The first two are opposite answers to "may I use this?", so they are not
  collapsed into one.
- **`usable`** is the derived boolean, `validated_ok == 1`, and it is the one
  every caller should read. It exists so the UI, the activate route and any
  client agree on what "may I use this?" means instead of re-deriving it three
  times and disagreeing about the untested case. **Activation requires it.**
- **`validated_note`** is what actually happened, usually straight from ffmpeg -
  the reason worth showing next to a profile that will not run.

### Login, the admin account and sessions

| Route | What |
| --- | --- |
| `POST /api/login` | **The only POST with no token on it.** `{"username", "password"}` in; `200 {"token": "ts_...", "expires": <epoch>, "username": "..."}` out. The token goes in the **body, not a cookie** - hold it and send it as `Authorization: Bearer`. `401 {"error": "wrong username or password"}` for either half being wrong. `409` when no admin exists yet, naming `POST /api/admin` - render "set a password", not "wrong password". `429` with a `Retry-After` header once the backoff bites. |
| `POST /api/logout` | Revokes the session in the `Authorization` header. `200 {"revoked": "<session id>"}`, or `400` when the bearer token is an API key rather than a browser session. |
| `POST /api/admin` | `{"username", "password", "current_password"}`. `201 {"username"}` when no admin existed and the bootstrap token or any key created the first one; `200` when one was changed. `current_password` is required whenever an admin already exists, **including for a rename**. `400` for an empty username or a password under 8 characters, `403 {"error": "current_password is wrong"}`, `429` with `Retry-After` when backed off. |
| `GET /api/sessions` | `{"sessions": [{"id", "prefix", "created", "expires", "last_used"}...], "admin": "<username>" \| null}`. `prefix` is the first 11 characters, so a client can point at its own row. No hash, ever. |
| `DELETE /api/sessions/{id}` | `200 {"revoked": id}` or `404 {"error": "no such session"}`. Revoking your own is allowed and signs that browser out on its next request. |

No route returns a password hash or a session hash, and nothing logs a password
or a username.

### Run controls

| Route | What |
| --- | --- |
| `GET /api/control` | The run state. |
| `POST /api/control/start` | Start claiming new work. Idempotent - starting a started box is a `200`. |
| `POST /api/control/stop` | Stop claiming new work. **Drains**: the encode in flight finishes, verifies and reveals. Nothing here can touch a running job. |

All three answer `200` with the same object:

```json
{
  "run_state": "running",
  "converting": true,
  "reason": "converting - the window 22:00-06:00 closes in 41m, local time now 05:19 EDT",
  "convert_window": "22:00-06:00",
  "timezone": "America/New_York",
  "local_time": "05:19 EDT",
  "next_change_seconds": 2460,
  "auto_start": true,
  "visible_only_skipped": 0
}
```

- `converting` is the whole answer to "may a worker claim a new job right now" -
  the switch **and** the window, both, so a client never has to combine them.
- `reason` is one human sentence meant to be shown verbatim. It is the same
  string the log prints, by construction.
- `timezone` is the `TZ` environment variable; empty means unset, so the
  container is on UTC. `local_time` carries the zone abbreviation. **Show them
  next to the window** wherever you show the window.
- `next_change_seconds` is `null` while stopped: the window keeps turning, but
  the gate does not follow it again until somebody presses Start.
- `visible_only_skipped` is how many files the last scan skipped for being
  visible, and it is `0` unless `hidden_only` is on **and not one hidden file was
  found anywhere** - that is, unless nothing will ever be queued. It is the
  difference between "nothing to do yet" and "nothing here is eligible at all",
  so it is worth showing rather than an empty queue with no explanation. See [If
  nothing is writing the dot](#if-nothing-is-writing-the-dot).

### Backup, restore and TLS

| Route | What |
| --- | --- |
| `GET /api/backup` | The config document, with `Content-Disposition: attachment; filename="transcodearr-config-YYYYMMDD-HHMMSS.json"`. `{"format": 1, "version", "exported", "settings", "profiles", "arrs"}`. No secrets, no job history. |
| `POST /api/restore` | The body **is** that document, unwrapped, so `curl -d @file` works. `200 {"changed": ["..."]}` - show that list verbatim, it names what the restore deliberately skipped. `400` for anything that is not a backup, a format or version newer than this build, or any invalid value. |
| `POST /api/tls/selfsigned` | `{"host": "nas.local", "days": 3650}`. `201 {"cert", "key", "host", "days", "detail"}` after writing `/config/tls/cert.pem` and `key.pem`. `400` for a bad host or days, `409` if a pair is already there (it never overwrites), `500` carrying openssl's last line. It does **not** save the settings - put the two paths into `tls_cert`/`tls_key` yourself and restart. |

### `GET /healthz`

Unauthenticated, and deliberately outside `/api`. Anonymously it answers `ok`,
`version`, `encoder`, `encoder_reason`, `queued`, `running`, `uptime_seconds`,
`auth_configured`, `admin_configured`, `run_state` and `converting` - which is
what the UI's status line and its login form need before anyone has signed in.

**With a valid bearer token it adds `media_roots`, `watch_roots` and
`hidden_only`, plus every field of the [run control
object](#run-controls)** - the window, the reason, the zone and the local clock.
Those are a map of somebody's filesystem and a description of their schedule,
and this is the one route with no key on it, so they wait for the token.
Whether work is *moving* is operational status like the counts already there;
*why* it is not moving names the window, which is configuration. An absent or
wrong token is not an error here, it just gets the short body.

`auth_configured` is false when the only token is the refused placeholder
`change-me`. Reporting that as configured told an operator their container was
protected while every request using that token was being rejected. It is now
also true when an admin password exists with no key minted - an install
protected by a password used to report itself unprotected.

`admin_configured` says only whether this box has a password, which is what a
page needs to decide between drawing a login form and drawing a token field. One
POST to `/api/login` would establish the same fact.

## Metrics

`GET /metrics`, in the Prometheus text exposition format, with
`Content-Type: text/plain; version=0.0.4; charset=utf-8`.

**It needs the bearer token like every other route**, and that is a deliberate
choice rather than an oversight. Prometheus reads a bearer token natively, so
the secure default costs you one line in a scrape config, and leaking the size
and shape of somebody's media library to whoever finds the port is not worth
avoiding that line.

```yaml
scrape_configs:
  - job_name: transcodearr
    metrics_path: /metrics
    # Or bearer_token_file: /etc/prometheus/transcodearr.token, which keeps the
    # key out of a config file that usually lives in a git repo.
    authorization:
      credentials: ta_your_minted_key_here
    static_configs:
      - targets: ["nas:8484"]
    # Add scheme: https if you turned the built-in TLS on.
```

Mint a key for it in the Access tab rather than handing it the bootstrap token -
that is what named, revocable keys are for.

| Metric | Type | What |
| --- | --- | --- |
| `transcodearr_build_info{version,encoder}` | gauge | Always 1. The running version and the encoder it chose. |
| `transcodearr_queue_depth` | gauge | Jobs waiting to be claimed. |
| `transcodearr_jobs{state}` | gauge | Jobs by state. All five of `queued`, `running`, `done`, `failed`, `cancelled` are always emitted, **including at zero** - a series that only appears once it is non-zero makes an alert written against it fire late, or never. |
| `transcodearr_saved_bytes` | gauge | Bytes reclaimed by the finished jobs still inside the history window. |
| `transcodearr_encode_seconds` | gauge | Mean wall-clock seconds per transcode over the last 20, `0` when unsampled. The same number the UI shows, from the same function. |
| `transcodearr_converting` | gauge | 1 when a worker may claim new work right now - the switch and the window together. |
| `transcodearr_run_state{state}` | gauge | 1 on whichever of `running`/`paused` is in effect. |
| `transcodearr_uptime_seconds` | gauge | Seconds since this process started. |

`saved_bytes` is a **gauge and not a counter**, which is worth knowing before
you write a query against it: `keep_history_days` prunes finished rows, so the
total goes down, and a counter that goes down is read by Prometheus as a process
restart and counted again from zero.

## Security

This API accepts a filesystem path and spawns a process on it, and it replaces
files in your media library. Treat it accordingly.

- **Do not expose it to the internet.** 1.0 added an account model, a login
  backoff and optional TLS, and none of that changes this line. A key or a
  password is one credential in front of an API that takes a filesystem path and
  spawns a process on it; there is no audit log, no second factor and no
  lockout that survives a restart. Bind it to localhost or to the LAN address you
  actually need (`127.0.0.1:8484:8484` in the example compose file), and put it
  behind a reverse proxy if it has to travel further than that.
- **`TRANSCODEARR_TOKEN` is the bootstrap key**, and the only way into a fresh
  container - including for creating the first admin account. It cannot be
  revoked from the UI, so treat it as the root credential: use it once to sign
  in, mint a named key per consumer, and give those out instead. Minted keys are
  `ta_` plus 48 hex characters, shown exactly once at creation, stored only as a
  SHA-256 hash, and revocable individually.
- **The admin password is hashed with `hashlib.scrypt`** and a per-password
  random salt, with the cost parameters stored per row so they can be raised
  later. It is never stored, logged or returned in any form, and no route
  returns a hash of anything. Failed logins back off after three attempts and a
  wrong username is indistinguishable from a wrong password.
- **`TRANSCODEARR_RESET_ADMIN` deletes the admin account at boot**, and is the
  documented way back in after a forgotten password. It is environment-only and
  grants nothing that setting an environment variable on this container did not
  already grant, since that is also where `TRANSCODEARR_TOKEN` is readable and
  the database is writable. Minted API keys survive it. See [Locked
  out](#locked-out-resetting-the-admin-account).
- **Session tokens are `ts_` plus 48 hex characters**, stored only as a hash
  with an expiry and a last-used stamp, revocable one at a time and on logout.
  They carry the same entropy and the same hashing as an API key - a session is
  not a lesser secret just because it expires.
- **The login answers with a bearer token, never a cookie**, which is why this
  service still has no CSRF surface. See [Signing
  in](#it-is-a-bearer-token-not-a-cookie-and-that-is-the-point).
- **TLS is optional and off by default.** Without it, a password crosses your
  network in clear text, which is logged as a warning at boot and again on any
  login that actually arrives over plain HTTP with no `X-Forwarded-Proto`. A
  reverse proxy is the better answer; see [HTTPS](#https).
- **The literal token `change-me` is refused**, wherever it comes from. A
  published image makes any placeholder in its own documentation a public
  credential, so this one is treated as no token at all: the startup log says so,
  `/healthz` reports `auth_configured: false`, and minted keys still work on such
  a container.
- Tokens are compared with `hmac.compare_digest` throughout, as bytes, because a
  plain `==` on a secret leaks its prefix through timing.
- **Paths are an allowlist.** Every job path is resolved with `realpath` and
  must land inside a configured media root, carry a known video extension, and
  not be one of the worker's own staging files. A path translated through an arr
  connection's mapping goes back through that same check - the mapping is
  operator-editable, so a translated path is no more trusted than the one that
  arrived. The folder picker checks containment on the resolved path too.
- Radarr/Sonarr API keys travel outward only. The UI never receives one back,
  and redirects are not followed on those calls - a 302 must not be able to
  replay an `X-Api-Key` header to a third party. The [job
  webhook](#the-guard-on-the-url) uses the same guard and the same
  no-redirects opener, because a webhook URL is operator-supplied egress in
  exactly the same way.
- **A backup carries no secrets**, so it is safe to attach to a forum post: no
  arr API key, no token or session hash, no password hash, no webhook signing
  secret. See [Backup and restore](#backup-and-restore).

### Residual SSRF, stated honestly

Adding an arr connection makes this container issue an HTTP request to an
address you supply, carrying a key you supply. That is not closed, and cannot
be, because of where arrs actually live.

- **LAN, Docker-network and loopback destinations are reachable by design.** A
  real Radarr is on `192.168.x` or `10.x`, on a Docker bridge at `172.16/12` or
  a bare container name, or on `127.0.0.1` when the container is host-networked.
  Blocking private space would break the correct configuration for nearly every
  user. So an authenticated caller can use the connection test to learn whether
  a given host and port on the container's networks answers. **The mitigation
  for that is the API token, not the guard** - which is the real reason not to
  expose this service.
- **Only link-local is refused:** `169.254.0.0/16` and `fe80::/10`, including
  the IPv4-mapped (`::ffff:169.254.169.254`) and 6to4 (`2002:a9fe:a9fe::`)
  encodings, and hostnames that resolve to any of them. That range is the cloud
  metadata service, the one destination with real credential value and the one
  that is never an arr.
- **DNS rebinding is not defended.** The guard resolves the name, and urllib
  resolves it again when it connects. A name that answers safely on the first
  lookup and with metadata on the second would get through. Closing that in pure
  stdlib means pinning the resolved IP through a custom opener, which is a much
  larger change than the attack warrants when the caller must already hold a
  valid token.
- AWS's IPv6 metadata endpoint `fd00:ec2::254` is deliberately **not** blocked:
  it sits inside `fd00::/8`, ordinary unique-local space where people do run
  arrs, so refusing it would be a false positive on legitimate setups.

## Tests

```
python -m unittest discover tests
```

The pure rules (path containment, staging-name arithmetic, verification,
stability, argv construction, the settings precedence rule, the link-local
guard, the convert window's midnight wrap, the throttle prefix), the auth
surface driven over real HTTP on a loopback socket, and the one page's route
names, element ids and setting groups. The incident cases are stated by name.

No container and no ffmpeg required, and the only network is a loopback socket
the suite starts itself. It takes about ten seconds, nearly all of it scrypt
deliberately being slow at hashing test passwords.

CI runs the same suite on pushes to `main` and on every pull request, and in a
separate job builds the Docker image, so a packaging change cannot merge green
while broken.

## Ops notes from the first real deployment (QNAP, NVIDIA T1000)

**One real-world case, not a hardware requirement.** TranscodeArr runs on any
box that can run the container, with or without a GPU. These are kept because
they are things that actually happened, and because the middle three are not
QNAP-specific at all - any long-uptime Linux host with an NVIDIA card can land
on them.

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
- **Converting is itself what refragments memory, so this recurs.** The first
  time it took 101 days of uptime. Under a sustained queue it came back in
  **90 minutes**: 18 jobs, each reading ~2 GB and writing ~1.7 GB, put 50 GB
  into page cache and left 1 GB free on a 64 GB box, and the high-order pools
  went from 1819 free order-9 pages to zero. The kernel was trying and failing
  to fix it - `/proc/vmstat` on that box read `compact_stall 7197`,
  `compact_fail 7048`, `compact_success 149`.

  Since 1.0.6 this container hands those pages back itself: every finished job
  flushes its output and calls `POSIX_FADV_DONTNEED` on the output and on both
  files it moved to the trash, none of which anything reads again. That removes
  the biggest single source of the pressure and needs no privileges.

  It does **not** replace host tuning, and it cannot: `/proc/sys` is mounted
  read-only in a container, so `drop_caches` and `compact_memory` are refused
  even to root inside one. Do not run this image privileged to work around
  that - it parses arbitrary media with ffmpeg, and host kernel write access is
  the wrong thing to hand it. Tune the host instead, as root:

  ```
  sysctl -w vm.min_free_kbytes=1048576      # 1 GB reserve; the default 128 MB
                                            # on a 64 GB box is 0.2%
  sysctl -w vm.watermark_scale_factor=200   # reclaim earlier; default 10
  ```

  Both are runtime-only and reset on reboot; put them wherever your NAS runs
  startup commands to make them stick.
- The boot probe exists precisely because of the above: a listed encoder is
  not a working one, and `/healthz` says which encoder actually won and why.
- After the memory-compaction fix, use **Re-test hardware** in the Encoding tab
  rather than restarting the container. The queue survives either way, but a
  restart fails whatever was mid-encode.
- Follow it with **Re-test all** on the profiles. Re-probing the hardware does
  not revisit a verdict already recorded against a profile, so the GPU profiles
  stay marked "will not run on this machine" - and therefore stay unactivatable -
  until something encodes with them again.

## Reporting a bug, or a security issue

**Three things that are not bugs, with their own fix:** a container that came up
and converts nothing is usually the [convert window](#the-convert-window) read
against a timezone you did not set, a file that fails every time is covered under
[A file that always fails](#a-file-that-always-fails), and a forgotten admin
password is [Locked out](#locked-out-resetting-the-admin-account).

**Bugs and questions:** open an issue at
[github.com/ManageArr/TranscodeArr/issues](https://github.com/ManageArr/TranscodeArr/issues).
Three things make almost any report answerable, and none of them leak anything:

- The startup log, or at least its `encoder:` and `clock:` lines - they name the
  encoder that actually won, the reason each other one lost, and the container's
  timezone against your window.
- `GET /healthz` with your token, which is the version, the run state and the
  queue counts in one line.
- A [config backup](#backup-and-restore) if the question is about settings. It
  carries **no secrets by design**, specifically so it can be attached to a
  public report.

**Security issues: do not open a public issue.** Report them privately as
described in [SECURITY.md](SECURITY.md). That includes anything that would let a
caller reach a path outside the configured media roots, get a job queued or a
process spawned without a valid token, or read back a secret the API is supposed
to mask. What is already known and stated rather than fixed - the residual SSRF
in outbound arr and webhook calls, and the fact that this service must not be
exposed to the internet - is in [Security](#security) above.

## License

MIT. See [LICENSE](LICENSE).
