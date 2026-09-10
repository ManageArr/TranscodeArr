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

`1.7.5` is the release to run. Everything in the sections below is in it and is
still true; those sections are kept because the reasoning behind each rule is
the point of this file, and the patch releases changed little of it.

## [1.7.5] - 2026-09-10

Everything here comes from one change made outside this worker: the download
client was given a download queue. Sonarr and Radarr now hold a queue row per
GRAB rather than per active download, so a queue that used to be dozens of rows
is thousands, and a grab can wait days for a slot before a byte moves. Three
things in here quietly assumed neither of those.

### Fixed

- **A replacement that is downloading no longer says "searching".** The queue
  was read as a fixed first page of 200 rows of the WHOLE queue. Sonarr's is
  1,916 rows long, and two replacements the operator had just chosen by hand
  sat at rows 872 and 949 - so nothing was found for either and the page called
  them searching while both were downloading normally. It now asks for the one
  series or film by id, which is not paged, and returns two rows instead of two
  hundred.

- **A download waiting for a free slot is no longer called "downloading".**
  `trackedDownloadState` describes what the ARR is doing and reads
  "downloading" for a torrent the client has not started; with a queue turned
  on that was 1,489 of 1,890 rows. The client's own word now wins when it is
  the one holding things up, and the row says it is waiting for a slot. A
  percentage that has not moved since yesterday looks exactly like a dead
  download until you know that is why.

- **A replacement is no longer forgotten while its download is still coming.**
  The give-up clock runs from when the replacement was asked for, and at 14
  days that was comfortably longer than any grab took to start. Behind a client
  queue it is not: the row would be deleted with the download still queued, and
  nothing would be waiting when it landed. Rows are now only given up on when
  the arr has actually answered and has no download for them.

- **An unreachable arr no longer reads as an empty queue.** Reading the queue
  returned the same "no download" answer whether the arr said there was none or
  could not be reached at all - which, with the change above, would have been a
  reason to delete the row. It raises now, and the two callers say which
  happened. Forcing an import against an offline arr used to report "that
  download is not sitting waiting to be imported", which was not true.

### Unchanged, and worth knowing

A replacement swapped for a different release is already followed: the waiting
row is keyed on the episode or film and the path, never on the release it
blocklisted. So when an arr retires a stalled download and grabs something
else, the row tracks the new one without being told, and still clears the same
way - when a different file appears at that path and converts.

## [1.7.4] - 2026-09-08

### Fixed

- **A restart no longer parks whatever was mid-encode for six hours.** The boot
  reconcile fails an interrupted job with "interrupted by restart", and the
  retry cooldown charged that the full failure wait - so every container
  restart, which means every update, sent the file it was converting to the
  back of a six-hour queue. The cooldown is a verdict on the FILE: a process
  killed while holding one says nothing about whether it converts, so an
  interruption now retries on the next scan. The sentence is a shared constant
  rather than two string literals that can drift apart, which is checked.

- **A file the arr names is no longer mistaken for a reason it gives.** Queue
  status messages arrive as `{title, messages}`, where the title is the FILE
  being discussed. When an entry carried no messages the title was being read
  as a rejection reason - and those strings are exactly what decides whether a
  refused import may be overridden, so a release name was capable of refusing a
  force with a filename as the explanation. Only real messages count now.

- **The Queue page no longer keeps saying an import is refused after the
  worker has forced it.** The replacements view caches for 20 seconds, and the
  automatic path was not clearing it.

- Bounded the in-memory record of how long each download has been stuck, so a
  row dismissed while its download is parked cannot leave an entry behind for
  the life of the process.

### Note for anyone running the QNAP host tuning from the ops notes

A `host-tuning.sh` written from these notes read the order-9 free page count
as `awk '/Normal/{print $11}'`. **That is order-6.** `/proc/buddyinfo` lines
begin `Node 0, zone Normal`, so order *k* is field `$(5+k)` and order-9 is
`$14`. Order-6 sits in the thousands where order-9 sits in the hundreds, so the
`-lt 200` test never fired and the compaction half of the `cuInit` mitigation
had never once run on the box it was written for.

Fix the field number only. Rewriting the line with double quotes lets the SHELL
expand `$14` before awk sees it, which yields a small number, passes the test on
every run, and drops the entire page cache every five minutes.

## [1.7.3] - 2026-09-08

### Fixed

- **A replacement served out the retry cooldown earned by the file it
  replaced.** The cooldown is keyed on the PATH, and a replacement inherits the
  path of the file it replaces - so a brand new download that has never failed
  at anything sat waiting for the six hours the unreadable one had earned.

  Seen live on Malcolm in the Middle S07E05. The bad file failed verification
  at 18:52. Sonarr imported the replacement at 00:10, this worker saw it at
  00:17, cleared the wait, logged **"the replacement arrived, converting it"** -
  and then declined to queue it, 32 minutes short of the old file's cooldown.
  Nothing was broken enough to notice: the row vanished off the Queue page as
  it should, and the file simply did not convert for another half hour.

  The state a path can be in is now three answers rather than two - `waiting`,
  `arrived`, `none` - because the caller has to treat "the replacement is here"
  differently from "nobody is waiting on this". Only `arrived` skips the
  cooldown, so a file that merely failed still serves it, which is the whole
  reason the cooldown exists.

  The log line no longer promises anything either. It says a different file has
  arrived and stops there; announcing "converting it" and then handing back to a
  caller that silently declined is how this hid for a day.

## [1.7.2] - 2026-09-08

### Fixed

- **The import grace period was measured from the wrong event, so in `auto`
  mode there was effectively no grace at all.** It ran from the moment the
  REPLACEMENT WAS ASKED FOR. Those rows are days old on a real library - the
  ones this was built for had been waiting up to five days - so the ten minutes
  had always long since elapsed, and the first watcher pass after a download
  completed would have forced the import immediately.

  Caught on the live box the first time a replacement actually landed. Sonarr
  imported it **by itself**, about four minutes after the download completed,
  with no rejection and nothing wrong. In `auto` mode this worker would have
  raced that with a forced import of the same file. `manual` being the default
  is the only reason it did not.

  The grace now runs from the first moment a download is SEEN finished and not
  imported, which is the only honest clock for it. Held in memory rather than
  on the row: losing it to a restart merely restarts the grace, and the
  alternative is a column that has to be cleared correctly on every exit.

- **A refusal and a pause are no longer treated as the same thing.**
  `importPending` is what both look like. When the arr has said WHY, it has
  decided, and waiting ten minutes to act on a sentence it already wrote adds
  nothing - so an explicit rejection is actionable at once. When it has said
  nothing it is usually still working, which is exactly what happened live, so
  silence gets the grace before anything here steps in front of it.

  One consequence worth knowing: the Force import button no longer appears the
  instant a download completes. It waits for the arr to either explain itself
  or run out of time, which is the same rule the automatic path follows - the
  button and the watcher can no longer disagree about whether a download has
  had its grace, because only one function decides.

## [1.7.1] - 2026-09-07

### Fixed

- **1.7.0 served a page whose script would not parse, so nothing on it
  rendered.** The header sat on "connecting..." and every tab was empty. The
  cause was one `confirm()` in the new Force import button whose `\n` escapes
  were written into the source as REAL newlines, which is an unterminated
  string literal.

  There is no worse failure mode available to this page. A syntax error in it
  does not fail a build, does not log anything, and does not put an error on
  screen - the boot script simply never runs, so a working container with
  healthy endpoints looks completely dead to the only interface most people
  ever use. `/healthz`, `/api/settings` and every route answered correctly
  throughout.

  **Nothing checked that the page was valid JavaScript**, which is why this
  shipped. The page is a thousand lines of hand-written script inside a Python
  string, compiled by nothing, and every existing check on it - the ids, the
  routes, the entities, the dashes - reads it as text. It is now parsed with a
  real parser rather than a regex approximating one, borrowing `node --check`
  where there is a node to borrow and skipping where there is not. CI has one.
  Verified against this exact defect: reintroduce it and the test fails with
  the browser's own error.

## [1.7.0] - 2026-09-07

### Added

- **Force the import when the arr will not do it.** A replacement can download
  perfectly and still never land. The arr decides an import by QUALITY, and the
  file being replaced is almost always the same quality - it is unreadable, not
  low resolution - so the arr refuses with "Not an upgrade for existing episode
  file(s)" and parks the finished download indefinitely. It is not wrong. It
  cannot tell that the file it is protecting will not play.

  That is the same blind spot that made 1.6.0 necessary, one step later in the
  chain: 1.6.0 was needed because blocklisting never triggers a search, and this
  is needed because winning the search still does not finish the job.

  One new setting under Rules, **When the arr will not import it**
  (`REPLACEMENT_IMPORT`):

  | Mode | What it may do |
  | --- | --- |
  | `off` | Leaves it parked, and says on the row that it is parked. |
  | `manual` (default) | Adds a Force import button once the download really is stuck. |
  | `auto` | Does it unprompted, 10 minutes after the arr gives up. |

  The grace period exists so the arr always gets first refusal: it retries its
  own import well inside ten minutes, so anything still parked after that is
  parked on a decision rather than on a timer.

  **The gate is the rejection reason, and it is the same distinction the release
  list already makes.** A refusal about the file we ALREADY HAVE - "not an
  upgrade", "existing file meets cutoff" - is ours to overrule, because that
  file is the whole problem. A refusal about the DOWNLOAD - a sample, a file the
  arr cannot parse, an episode it cannot match - is refused for a good reason
  and stays refused. Both arrive through the same field, and forcing the second
  kind would import something broken over something merely unplayable. A mixed
  set of reasons is treated as the serious half.

  Two more things it will not do: it only ever acts on a path this worker is
  already waiting on a replacement for, so it is not a way to import something
  nobody asked about; and inside a season pack it imports only the file matching
  the episode the row is about, because importing the wrong one replaces an
  episode nobody was talking about.

  It runs on the watcher's existing pass rather than a timer of its own - the
  same loop that notices the new file afterwards - and after the scan, so the
  next pass is the one that finds what landed, once the stability window has had
  a chance to prove the file finished being written.

  **This is the one action in the chain that destroys a file.** The arr replaces
  the existing file as part of importing, and with no Recycle Bin configured it
  deletes it rather than keeping a copy. That is why the default is a button and
  not automatic, and why the confirmation says so in as many words.

## [1.6.2] - 2026-09-07

### Fixed

- **A replacement search no longer times out at twenty seconds.** Every call to
  an arr shared one 20-second budget, which is right for a status ping and
  wrong for the one call that is not a request to the arr at all: an
  interactive search is a request to every indexer the arr has, and it takes as
  long as the slowest of them. Measured here: 4s warm, **28s cold**, with one
  indexer behind FlareSolverr configured for 90s on its own. A cold search
  failed at 21s with "the read operation timed out" while Sonarr was still
  waiting perfectly happily.

  Searches now get their own budget, set high enough that the **arr** is always
  the one to give up first. It already has a per-indexer timeout and returns
  whatever answered, and a partial list of real releases beats an error about a
  limit this worker invented. A timeout that does happen now says how long it
  waited instead of "the read operation timed out", and the failure is written
  to the container log - it used to leave a red box on the page and the log
  completely silent about the one call here that depends on somebody else's
  indexers.

- **Find a replacement worked from the Queue and did nothing from History.**
  The results panel was a card inside the Queue tab, and tab sections are
  `display:none` unless their tab is on - so the same button in the job history
  opened it inside a hidden section. It looked like a button that did nothing
  at all. It is now a modal outside every section, which is also what was asked
  for: it opens over the page rather than pushing the queue down, closes on the
  button, the backdrop or Escape, and scrolls its own list instead of the page.

  It stacks above the sticky header and the scroll arrow but still under the
  sign-in screen, so a session that expires while it is open is covered by the
  login form rather than leaving a live Grab button floating over it.

## [1.6.1] - 2026-09-07

### Changed

- **The header and the tabs hold still while the page scrolls.** Both, not just
  the header: sticking the header alone leaves the tabs sliding away underneath
  it, which reads as a rendering fault rather than a layout choice. The gap
  under the tabs moved from the tab strip's own margin onto the sticky wrapper
  as padding, because a margin sits OUTSIDE the sticky box and left a
  transparent band for text to scroll through.

  The bar sits below the sign-in screen in stacking order, deliberately. That
  screen covers the page because everything behind it needed a token to render
  at all, and a Sign out button floating over it belongs to a session nobody
  has yet.

- **A back-to-top arrow, once there is a top to go back to.** Bottom right,
  and only after the page has scrolled about a screen - a control that is
  always there is one that covers a corner of every short page for no reason.
  Under the sign-in screen for the same reason the top bar is.

### Fixed

- **Row action buttons are no longer ragged.** In a dense table the actions
  cell got whatever width the other columns left over, so "Find a replacement"
  wrapped mid-phrase and sat above a "Dismiss" of a completely different width.
  The cell is now sized to the widest button in it and both fill that width,
  which also fixes the Grab column in the release list and the Cancel column in
  the job history.

## [1.6.0] - 2026-09-07

### Added

- **Find a replacement, from here.** Any job that failed now offers a search
  against the arr that owns the file - the same interactive search the arr's
  own page runs - and lists what the indexers have, in the order the arr
  ranked them. Pick one, press Grab, and the arr downloads it and imports it
  over the bad file.

  This exists because blocklisting a release does not, on its own, bring you a
  new file, and 1.5.2 made that impossible to miss by finally leaving those
  files alone. Nothing here deletes media, so after a verification failure the
  unreadable file is still on disk and the arr still counts the episode as
  having one: `hasFile: true`, `qualityCutoffNotMet: false` - satisfied.
  Blocklisting stops that release coming back; it does not make the arr want
  anything. `Redownload Failed` does not apply either, because that is about a
  download that never imported and this one imported cleanly weeks ago. On a
  real library seven files waited this way, the oldest five days, with no
  `grabbed` event ever following the blocklist.

  **Expect every result to say rejected**, usually `Existing file meets
  cutoff`. That is the arr correctly refusing to replace a file it cannot know
  is unplayable, and overriding it is the whole point - so the rejections are
  shown in full rather than filtered out. The distinction that matters is
  shown too: a rejection about the file you already have is the one to
  override, and one about the release itself - `Not enough seeders: 0` - is
  not.

  One new setting under Rules, **Finding a replacement**
  (`REPLACEMENT_SEARCH`), decides how far that goes:

  | Mode | What it may do |
  | --- | --- |
  | `manual` (default) | Lists releases. Grabs only the one you click. |
  | `best` | Adds a Grab best button: the arr's top-ranked release, skipping any refused for a reason about the release itself. |
  | `auto` | Makes that same pick by itself whenever a source is blocklisted. |

  `auto` is the only setting in this project that spends bandwidth on its own
  judgement, so it is not the default, and even there it never takes a release
  rejected for a reason about the release - "best" would then mean "least
  bad", and a seedless torrent grabbed unattended is a queue slot held open
  for days and a replacement that never arrives.

  The ordering is the arr's own `releaseWeight` and is deliberately not
  re-scored here: this list is read beside the same search on the arr's page,
  and a worker that invented its own ranking would disagree with it and have
  no way to explain why.

  Grabbing records nothing on this side and needs no follow-up. The arr
  imports over the file, the next scan sees a different file at that path,
  and that is already the signal 1.5.2 watches for - so the wait clears itself
  and the new file converts.

### Fixed

- **A scan no longer calls a replacement wait a retry cooldown.** Files held
  back by 1.5.2 were counted as "held by the retry cooldown", which sends the
  reader off to wait for a clock that is never going to run out. A cooldown
  lapses on its own; this ends when a different file lands or when somebody
  dismisses it, so the scan reports it separately and says so.

## [1.5.2] - 2026-09-07

### Fixed

- **A file waiting on a replacement is no longer converted again every six
  hours.** Asking an arr to replace an unreadable file did not make that file
  ineligible, so the watcher re-queued it the moment the retry cooldown lapsed
  and spent a whole GPU encode arriving at the identical verification failure.
  It then declined to ask for a replacement a second time - correctly - and
  waited to do the whole thing again. On the live box seven files had done this
  **58 times**, one of them repeatedly for five days.

  Blocklisting a release is a statement that this file is known bad. Converting
  it again cannot produce a different answer, so `enqueue` now refuses while a
  replacement is outstanding.

  The refusal is keyed on **file identity** - inode, size and mtime, the same
  `file_identity` the reveal already uses to tell a file apart from the one
  that replaced it - and not on a timestamp. An arr import preserves the
  release's mtime, so "has it changed since we asked" is not a question the
  clock can answer here; a source on this library carries an mtime from 1989.
  When a genuinely different file turns up at that path the replacement has
  landed, the wait is cleared and it converts, which is the entire point of
  having asked.

  This is deliberately **not** overridable by `force`. The retry cooldown is
  about timing and a person pressing "Check for files to convert" may override
  timing; this is about a file already known to be unreadable. Dismiss, which
  the Queue page has had since 1.5.0, is the door out, and `POST /jobs` for
  such a file now says so instead of answering "already queued" about a job
  that does not exist.

  Rows written before this release carry no recorded identity. They are not
  backfilled at boot - that would mean a `stat()` per row against a share that
  may not be mounted yet - so the first check records what is on disk then,
  which for a file still waiting is by definition the one complained about.

## [1.5.1] - 2026-09-07

### Fixed

- **A job that lost one database race stopped being an encode that never
  ends.** On the live box a conversion read "converting" for two days and
  fourteen hours while nothing was encoding it, and one of the eight worker
  threads had been dead for exactly as long. Both were the same lost write.

  `run_encode` recorded progress on every line ffmpeg emitted - twice a second,
  committed each time, about 4,800 writes across a forty-minute episode to
  publish a hundred distinct numbers, times eight workers, beside the watcher
  and every HTTP thread. Eventually one of those writes lost its race and
  raised `database is locked`. `sqlite3` opens the transaction implicitly, so a
  statement that raises leaves it **open**, and nothing rolled it back.

  Everything after that followed from the one open transaction. The crash
  handler ran `finish("failed")` on the same poisoned connection, so it failed
  the same way and the row was never marked terminal - which is the job that
  read "converting" for two and a half days. The watcher counts such a file as
  already pending, so it is never queued again; the boot reconcile was the only
  thing that would ever have cleared it. The stale read then pinned the WAL: a
  checkpoint could reclaim 755 of 96,271 frames, so a 4.5 MB database was
  carrying a 396 MB write-ahead log that had not checkpointed since the minute
  of the crash, and every reader was paying for it. And the worker itself was
  finished: a stale snapshot cannot be upgraded to a write, SQLite answers
  `SQLITE_BUSY` immediately rather than waiting out `busy_timeout`, so that
  thread failed to claim once every five seconds, 19,843 times, and would have
  gone on failing for the life of the container.

  Four things changed, none of them large:

  - Progress is written only when the integer percent actually moves, which is
    the difference between ~4,800 writes an encode and ~100.
  - A progress write that fails now rolls back and is dropped. A percentage for
    the UI is not worth a finished encode.
  - The crash handler rolls back **before** it does anything else, so the
    `finish("failed")` that follows can land.
  - `worker_loop` is the one place that knows a job is over however it ended.
    If the row still says `running` when `process` returns, it is marked failed
    there. A job stuck running forever is now structurally impossible rather
    than merely unlikely.

  The watcher guard rolls back for the same reason: it is the other thread that
  outlives every job, and `scan_once` holds a transaction open per directory on
  purpose, so a walk that dies mid-directory is exactly the shape that strands
  one.

  Nothing about this was visible from outside. `/healthz` answered `ok` for the
  whole two and a half days, and the seven surviving workers kept converting,
  which is what made a dead thread and a phantom job look like a slow queue.

## [1.5.0] - 2026-09-01

### Added

- **Subtitles can be extracted beside the file instead of squeezed into it.**
  One new setting under Rules, `EXTRACT_SUBTITLES`, **off by default** because
  it changes what an existing library looks like on disk. On, every text
  subtitle in the source is written next to the finished video as
  `<stem>.<language>[.forced][.n].srt` / `.ass`, which is the naming Jellyfin,
  Plex and Emby all read, and the encode runs with `-sn` so no track is carried
  twice.

  **Recommended for anime.** MP4 can only hold text subtitles as `mov_text`,
  and converting an ASS track into `mov_text` keeps the words while throwing
  away the styles, fonts, positioning and colors - which for anime is the sign
  typesetting and the karaoke. That loss was permanent, because the styled
  original was in the source the job had just moved to the trash. Extracting to
  `.ass` copies the stream out untouched.

  It fixes the other half too. MP4 cannot carry the image subtitles a Blu-ray
  remux has at all, so those conversions used to walk down the fallback ladder
  and finish having dropped every track. A source on the live box carried five
  `subrip` tracks - eng, dut, fre, ger, spa - into a conversion that could only
  degrade them.

  The dot convention applies to a subtitle exactly as it does to the video: a
  sidecar is written hidden, revealed one moment ahead of the film, and unlinked
  on every exit that is not a reveal - so a media server can never scan a
  half-written subtitle, one named for a file that is about to be replaced, or
  the film without its subtitles. Past the moment the source moves to the trash
  that reverses: the hidden sidecars are then the only copy of those tracks, so
  a job that dies after it reveals them rather than unlinking or keeping them.
  Keeping them was not enough - the watcher then picks the staged `.mp4` up as a
  reveal job and unhides the film without them, leaving the subtitles invisible
  for good. A subtitle already at a name being revealed
  onto is displaced into the trash rather than overwritten, and only when it is
  the same file that was there when the job started: one that arrived while the
  job ran - a Bazarr download - is left alone, and the job says which happened.
  The replaced file's own subtitles go to the trash with it, because a subtitle
  left behind reattaches to whatever takes that name next.

  An extraction that fails, fails the job, and leaves the source where it is for
  the retry. The encode is built with `-sn` BECAUSE these tracks are going to
  sidecars, so a swallowed extraction failure shipped a file with no subtitles
  anywhere and then trashed the only copy that had them. A sidecar ffmpeg wrote
  and that cannot be read back afterwards - `ESTALE` or `EIO`, a NAS - counts as
  a failure for the same reason: that track is in neither the mp4 nor a file
  anybody can open. A source with no text subtitles at all is still a success -
  there was nothing to write. Extraction is a full demux of the source, so the
  two guards that protect the staging and visible names, and the cancel flag,
  are all re-asked on the far side of it, and the visible-name guard is asked
  once more immediately before the reveal.

  A hidden sidecar name that is already taken is cleared rather than refused
  forever: the leftover goes to the trash with the retention a displaced file
  gets, never an overwrite and never an unlink. Refusing was a permanent poison:
  nothing else ever cleared those files, so one transient error failed every
  later conversion of that filename identically. A sidecar belonging to a job
  still in flight is still refused, because two sources can plan the same
  targets and several jobs run at once.

  **Image subtitles are not extracted**, and that is a decision rather than a
  gap. PGS and VOBSUB are pictures; nothing turns a bitmap into a `.srt` except
  OCR, and this worker is not going to run OCR unattended over a library and
  write the guesses out as subtitles. Extracting them as `.sup` with `-c:s
  copy` was considered and left out - almost nothing plays a `.sup` sidecar, so
  it would put files in the library that look like subtitles and are not. The
  job names the codecs that stayed behind, and they are still in the source,
  which is in the trash for the retention window.

  **Bazarr is not integrated**, for reasons written out under
  [Bazarr](README.md#bazarr) in the README. Its rescan endpoint
  (`PATCH /api/series?seriesid=<id>&action=scan-disk`) is real, but it is
  addressed by a Sonarr/Radarr id this worker does not have, it re-probes a
  whole series synchronously per call, and it cannot answer inside the ten
  second timeout every other outbound call here holds to.

- **A "Waiting on a replacement" row can be dismissed.** New button on each row
  of the card, and `DELETE /api/replacements?path=` behind it. It deletes this
  worker's row and nothing else - the release stays blocklisted, and a download
  the arr has already started belongs to the arr. Until now the card had no
  control on it at all: the only thing that cleared a row was that exact path
  converting later, so a file whose library moved out from under it sat there
  saying "searching" with nothing to press.

- **The card says which item it is waiting for.** Each row now shows the folder
  the file was in, which connection was asked, the whole blocklisted release
  rather than the first 44 characters of it, and the arr's own sentence about
  the request - which is where the series or film title is. Finding the item in
  Sonarr or Radarr no longer means going back through the logs.

### Fixed

- **The reveal no longer replaces a file that arrived while the source was
  being trashed.** The guard that protects the visible name was asked before
  the encode and again after it, and then the reveal ran without asking anyone.
  Two slow things sit in that gap: an fsync of the whole freshly written output,
  which is seconds to minutes over NFS, and the move of the source into the
  trash, which is a full byte copy whenever the trash is on another filesystem.
  An arr importing an upgrade inside that window landed on exactly the name
  about to be revealed onto, and `os.replace` destroyed it without a word - and
  unlike a source, a clobbered file there never reaches the trash.

  Worse, the code already knew. `displace()` returns nothing both when the name
  was free and when it positively identified a stranger there and refused to
  move it, and the reveal read that second answer as permission to overwrite.

  The guard is now re-asked immediately before the reveal. Because that is past
  the point of no return - the source is already in the trash - refusing means
  the job fails with the converted file left at its hidden staging name, and the
  error names that path and the trashed source both, so finishing it by hand is
  a rename rather than an investigation. The same rule now covers the reveal of
  a skipped or already-`.mp4` file, which had the same gap around its own
  `displace()`. This one is not new in `1.5.0`: it has been there since the
  guard was introduced.

- **A waiting row whose world moved underneath it no longer waits forever.** A
  row was cleared by a later successful conversion OF THAT EXACT PATH and by
  nothing else. That is the honest signal, and it is also unreachable once the
  path can never convert again - a media root deleted, the connection deleted,
  the file moved to another library and converted there. The live box had such
  a row from a deleted `/media/Temp` root and a deleted "Sonarr (Temp TV)"
  connection, permanently "searching" for an episode that had converted
  perfectly well somewhere else.

  Two rules now clear those, both deliberately narrow. The arr connection is
  gone, so nothing can report on that download or deliver it. Or the file is
  missing from where it was AND a file of that exact name has since converted
  successfully somewhere else. Both halves of the second one are required: a
  missing path on its own is an unmounted share, and a same-named conversion on
  its own is a second copy of the episode in another library while this one is
  still genuinely waiting. Anything that might still be waiting is left exactly
  where it is - that is what the Dismiss button above is for.

## [1.4.0] - 2026-08-27

### Added

- **Jellyfin is told about every finalized file.** Three settings under
  Notifications: `JELLYFIN_URL`, `JELLYFIN_API_KEY` and `JELLYFIN_PATH_MAP`
  (`worker_prefix=jellyfin_prefix` pairs, longest prefix wins, matched only on
  a path component boundary). After the arr rescan, each finished job POSTs
  `/Library/Media/Updated` for the visible path, on a thread, with the
  webhook's egress guard and ten-second timeout, and notes the outcome on the
  job. A library on a mount Jellyfin cannot watch (NFS, SMB) otherwise learns
  about a conversion only at its next scheduled scan. Off until a URL is set.
- **An encoder that is not there is retried, not walked down the ladder.**
  `cuInit` refusals, `CUDA_ERROR_*`, a driver library that will not load: the
  same command is started again `ENCODER_RETRY_ATTEMPTS` times (default 3),
  `ENCODER_RETRY_SECONDS` apart (default 20), with one diagnostics line from
  `nvidia-smi` and `/dev/nvidia*` before the first retry. When they run out the
  job fails as `encoder unavailable: ...` and the watcher tries the file again
  after `ENCODER_RETRY_COOLDOWN_MINUTES` (default 15) instead of the six-hour
  wait for a file that cannot convert. Every rung of the fallback ladder keeps
  the video encoder, so the ladder never helped here: on a real box eighteen
  jobs failed inside three seconds each, minutes apart from encodes that went
  through.

### Fixed

- **A probe that never answered is no longer a verdict on the file.** An
  `ffprobe` that timed out or could not run was reported as "found no video
  stream", which the replacement rule believes, so a slow or flapping share
  could get a good release blocklisted. Both probes now fail the job as
  `source probe failed (timeout or I/O error)` / `output probe failed (timeout
  or I/O error)`, neither of which asks an arr for anything.

## [1.3.1] - 2026-08-21

### Fixed

- **Lowering trash retention now says what it is about to delete.** Retention
  is applied on the next scan, minutes later, permanently - and `0` here is
  **not** "keep forever": it empties the trash completely. The setting directly
  below it, Keep job history, reads `0` as keep every row forever. Two adjacent
  retention settings where zero means opposite things is a trap, and the one
  that springs it destroys the only copy that makes a conversion undoable.

  Lowering the value now confirms, naming the file count and total size at
  risk, and `0` gets its own wording saying plainly that it is not "forever".
  Both help texts now name the other's meaning of zero. Nothing about what the
  values *do* changed - an empty field was already refused rather than read as
  zero, which was checked rather than assumed.

## [1.3.0] - 2026-08-21

### Added

- **The Queue shows what it is waiting for.** A file handed to an arr for
  replacement has no job to show - it failed, and the next job cannot exist
  until a new file lands - so it vanished from the UI at exactly the moment
  somebody would want to know what was happening to it. A **Waiting on a
  replacement** card now lists each one with the release that was blocklisted,
  when it was asked, and what the arr's own download queue says: searching,
  downloading with a percentage, or the arr's error message.

  A row clears when a later job for that path converts. That is the only
  signal that means the replacement actually arrived and worked - the arr's
  queue going quiet looks identical whether the download finished, never
  started, or was rejected on import. Anything nobody delivers is dropped
  after 14 days.

  The arr's ids are recorded when the request is made rather than re-derived
  per poll, which would be three API calls per waiting file per refresh, and
  the whole view is cached for 20 seconds however many browser tabs are open.
  `trackedDownloadState` is what gets shown, not `status`: the latter still
  says "downloading" for something stalled, waiting to import, or about to be
  retried.

## [1.2.0] - 2026-08-21

### Added

- **Blocklisting a season pack is now a choice, not a consequence.** 1.1.0
  blocklisted whatever the grab was, which meant one unreadable episode could
  retire the release the rest of a season came from. **...even when it came
  from a season pack** (`REPLACE_BAD_SOURCE_PACKS`, off) decides that.

  The arr is asked rather than the title parsed. A grab records its own
  `releaseType` - `SingleEpisode`, `MultiEpisode` or `SeasonPack` - and that is
  what the switch is applied to:

  | | Off (default) | On |
  | --- | --- | --- |
  | Single episode | blocklisted | blocklisted |
  | Season pack or multi-episode | left alone, reported on the job | blocklisted |
  | No release type recorded | left alone | blocklisted |

  Off, a pack-sourced bad file is reported and left for a person - safe, but
  the arr keeps offering the same pack. On is the only thing that stops that.
  Blocklisting a pack deletes nothing and does not touch episodes already
  imported from it; it stops the release being grabbed again.

  An unclassified release is refused with the broad case rather than waved
  through as a single episode: unknown is not the same as safe when the
  consequence is retiring somebody's release.

## [1.1.0] - 2026-08-21

### Added

- **Sonarr and Radarr can be asked to replace a file that is simply bad.**
  Some sources are unreadable in part: one converts to 96% and fails
  `duration mismatch: source 2724s, output 2624s` every retry forever, because
  the verification is right and the file has a hundred seconds ffmpeg cannot
  get through. Turn on **Ask Sonarr/Radarr to replace an unreadable file**
  (`REPLACE_BAD_SOURCE`, off by default) and the arr that owns it is asked to
  mark the grab as **failed**, which blocklists the release and starts its own
  search.

  Blocklisting is the point rather than a detail. Deleting the file and
  searching lets the arr hand back the identical release, which converts to the
  identical failure, round and round - that loop is the whole reason this needs
  the arr rather than a retry.

  What it will not do matters more than what it does, because it is the only
  thing here that spends bandwidth and retires somebody's release:

  - **Never for a failure of the machine.** Only a verification failure naming
    the source qualifies. A GPU that stopped initializing, a share that went
    away, a name already taken - all of those clear on their own, and one
    evening on a real box produced dozens of `CUDA_ERROR_NOT_INITIALIZED`
    failures that would each have retired a release and started a download.
    A test asserts that exact text asks nobody.
  - **Never deletes your media.** The unreadable file stays where it is; the
    arr replaces it if its own search finds something.
  - **Once per file.** A replacement that is also unreadable is not something
    another download fixes, so it stops for a person to look.

  Verified against the live Sonarr 4.0.19 this was built for, read-only, before
  any of it was written: the episode-file lookup by path, the episode, the grab
  in history, and that `POST /api/v3/history/failed/{id}` is a real route.

  One consequence to know before switching it on: when the grab was a **season
  pack**, the pack is blocklisted. That is correct - the pack contains the
  unreadable episode - but one bad episode can retire the release the rest of
  the season came from.

## [1.0.7] - 2026-08-21

### Fixed

- **A job that FAILED kept its whole source in page cache.** 1.0.6 handed pages
  back on the success path only, and `process()` has eight other exits - a
  failed verification, a source that changed mid-encode, an encoder that would
  not start. Every one of them has already read the entire source before it
  returns, and every one of them returned without giving it back.

  Failures are exactly when this matters. A batch that fails fails fast, so it
  fails often, and the fragmentation this whole mechanism exists to avoid is
  what makes encoders stop starting in the first place - a GPU outage would
  have filled the cache faster than a working queue and made itself worse.
  Found by measuring 1.0.6 on a real box, where the file chosen to test it
  happened to fail verification and released nothing.

  The release moved into a `finally`, so it covers every exit including the
  crash handler, and is a no-op on the success path where the source has
  already moved to the trash under another name.

## [1.0.6] - 2026-08-21

### Added

- **Every finished job hands its page cache back.** Converting is what
  refragments kernel memory, so the `cuInit` failure in the notes below is not
  a one-off: the first occurrence took 101 days of uptime, and under a
  sustained queue it returned in **90 minutes**. Eighteen jobs, each reading
  ~2 GB and writing ~1.7 GB, put 50 GB into page cache and left 1 GB free on a
  64 GB box; free order-9 pages went from 1819 to zero and every encode began
  failing with `CUDA_ERROR_NOT_INITIALIZED`. The kernel was already trying -
  that box read `compact_stall 7197`, `compact_fail 7048`,
  `compact_success 149`.

  None of those pages are ever read again: the source is in the trash and the
  output is streamed once by a media server that reads ahead anyway. A job now
  flushes its output and calls `POSIX_FADV_DONTNEED` on it and on both files it
  moved to the trash. No privileges, no setting, and it removes the single
  biggest source of the pressure.

  The flush is ordered deliberately - the output is fsynced **before** the
  source is trashed. `POSIX_FADV_DONTNEED` skips dirty pages so it was needed
  anyway, but it also closes a real gap: until that returns the verified output
  exists only in the page cache of a NAS, and the next step moves the one other
  copy of that episode.

  **This does not replace host tuning and cannot.** `/proc/sys` is mounted
  read-only in a container, so `drop_caches` and `compact_memory` are refused
  even to root inside one - verified on the box this was found on. The README
  now carries the two sysctls that prevent it (`vm.min_free_kbytes`,
  `vm.watermark_scale_factor`) and says plainly not to run this image
  privileged to get around the restriction: it parses arbitrary media with
  ffmpeg, and host kernel write access is the wrong thing to hand it.

## [1.0.5] - 2026-08-21

### Added

- **The Trash tab pages, and a selection survives paging.** 1.0.3 listed the
  first 500 files and stopped, which on a real library of 633 meant the rest
  were unreachable and "mass delete" was a phrase rather than a feature. It
  pages 100 at a time now, with the totals still describing the whole trash
  rather than the page - that count is what somebody reads before pressing
  Select all.

  Ticks are held **by path, outside the page**, so files gathered across
  several pages can be acted on together. Without that, selection resets on
  every Next and a bulk action can only ever mean "this page". The header
  checkbox takes the current page, **Select all** walks every page, and
  **Clear selection** empties it.

  A selection larger than the 500-file batch cap is sent as several requests
  rather than raising the cap: the cap is what stops one accidental request
  from moving an entire library. Progress is reported per batch, and anything
  that **fails stays selected**, so a partial failure leaves a selection
  holding exactly what was not dealt with.

  Two edge cases the tests found rather than the UI. The sort now breaks ties
  on path - files trashed by one job share a timestamp, and an undefined order
  under a pager puts one row on two pages and another on none. And an `offset`
  past the end clamps to the **start of the last page** rather than the end of
  the list, because a bulk delete shortens the list under whoever ran it and
  the alternative is a blank table with a working Previous button.

## [1.0.4] - 2026-08-21

Both of these were found by 1.0.3's own new tab failing to appear and then
failing to save, on a real box, within ten minutes of shipping.

### Fixed

- **Nothing this server sends was cacheable and nothing said so.** The UI is a
  single 80KB file that changes every release, and it went out with no
  `Cache-Control`, no `ETag` and no `Last-Modified` - so a browser had nothing
  to revalidate against and no reason to ask. 1.0.3's Trash tab was built,
  shipped, deployed and confirmed present in the served HTML, and was still not
  on screen: the browser had the previous version and only a hard reload moved
  it. That is every future UI change, not one tab.

  Every response now carries `Cache-Control: no-store`. The API bodies need it
  for the opposite reason: they are a live view of a queue, and a cached answer
  there is a lie with a timestamp on it. A caller that sets its own header - the
  login backoff, with its `Retry-After` - still wins.

- **Saving the trash retention answered "Not Found".** The new control sent
  `POST /api/settings`; that route has a `GET` and a `PUT` and no `POST`, so it
  fell through `do_POST` to the catch-all 404 and reported it at somebody who
  had just typed a number into a box.

  `test_web_page.py` exists to catch exactly this - "a route the page calls
  that the server never had" - and it passed, because it only asked whether the
  path appears anywhere in `main.py`. It does; under two other verbs. It now
  splits `main.py` by handler and checks the **(method, route)** pair, which
  fails on the bug as written.

## [1.0.3] - 2026-08-20

### Added

- **A Trash tab: see what is in there, restore it, or delete it early.** The
  trash has always been the thing that makes replacing an original safe, and
  until now the only way to look in it was a shell. It lists every file with
  where it came from, its size, and how many days it has left, and it does
  per-row and bulk Restore and Delete.

  **Restore can replace what is in the way**, which is the case it exists for:
  an arr imports an "upgrade", 1.0.2 replaces the old conversion with it, and
  the upgrade turns out worse. That needs `replace`, it confirms first - naming
  the files and saying plainly that they are what the media server is serving
  right now - and **the file it pushes aside is trashed, not deleted**. The
  thing being displaced by a restore is itself a restore candidate ten minutes
  later; deleting it would make undoing the undo impossible. It costs one more
  file in the trash and buys back the whole decision.

  Restoring a **source** is reported as putting it back in the queue, because
  it does: a dot-hidden `.mkv` is exactly what the watcher exists to find, so
  it is converted again and trashed again. Restoring a replaced **output** - a
  visible `.mp4`, the case above - is not eligible for anything and simply
  stays. The listing marks which is which before anything is pressed.

  Both operations take paths from an HTTP body, so containment is checked
  against the **real** path: a symlink inside the trash pointing at the library
  would otherwise turn Delete into an arbitrary unlink. A bulk call is capped
  and refused rather than truncated, because a partial success reported as
  success is how somebody concludes a file was deleted when it was not.

- **A `trash` table recording where each trashed file came from.** The
  mirroring is reversible by arithmetic right up until two files land on one
  relative path inside the retention window - `trash()` then appends `.1`, and
  nothing can tell that suffix apart from a file genuinely named `Movie.1.mkv`.
  Restore puts media back; its destination is not something to guess. Files
  already in the trash from before this release still list and still restore,
  from the derived origin, and are marked as derived.

- **Retention is on the Trash tab.** `trash_keep_days` was only in the General
  settings form, which is not where somebody is standing when they think about
  how long this is kept. Same setting, same 7-day default, adjustable from the
  view it governs.

### Changed

- **"Check for files to convert" now ignores the retry cooldown.** It reported
  `Nothing new to convert` about 23 files it could see perfectly well and was
  simply declining to mention - they were inside the six-hour cooldown after a
  failure. The cooldown exists to stop the WATCHER re-running a permanently
  failing file every interval; a person pressing the button is asking about
  those files now, which is the same reason `POST /api/jobs` has always ignored
  it. The interval's own scan still honours it.

  The answer also breaks down what it did not queue: how many were already
  queued or running, how many were held by the cooldown, how many are still
  settling, how many were skipped for not being dot-hidden. "Nothing new" and
  "23 files are waiting on something you can override" are different answers.

### Fixed

- **A conversion that grew read `-21% smaller`.** Re-encoding an
  already-converted file can land bigger, and the double negative made the
  reader unpick a minus sign to learn the file grew. It says `21% larger` now,
  and `same size` at zero.

## [1.0.2] - 2026-08-20

### Changed

- **A conversion that lands on a name already taken now replaces it, instead of
  failing forever.** An arr upgrades an episode, imports the new release behind
  a dot, and the previous conversion is still at the visible name - so the job
  failed with `target already exists` and kept failing, because nothing on disk
  changed between attempts. A real library had 26 files doing that, and they
  would have done it until somebody deleted one by hand.

  The displaced file goes to the **trash**, never under `os.replace`: unlike a
  source - which is only ever replaced by a verified encode of itself - it has
  no other copy anywhere, so it gets the same retention every replaced source
  gets, mirrored under the same relative path. A job that replaced something
  says so in its warning and records both safety copies.

  **The mid-run race is still refused, and that has not weakened.** The check
  runs before the encode and again immediately before the replace, and the
  second compares inode, size and mtime against the first: only the exact file
  the job decided to displace may be displaced. Anything else arrived while the
  encode ran - an arr importing an upgrade lands on precisely this name - and
  it is newer than the source this job converted. That failure now reads `was
  written by something else while this job ran`, deliberately not the old
  sentence, because they are different events and used to be indistinguishable.
  The hidden staging name is never displaced at all: a file there is somebody's
  pending reveal, not a stale output.

### Added

- **`POST /api/scan`, and a "Check for files to convert" button on the Queue
  tab.** Walks the watched folders now instead of at the next interval, and
  answers with what it found: how many were queued, how many are eligible, how
  many are still settling inside `stable_seconds`, how many were skipped for
  not being dot-hidden, and any watched folder that does not exist in the
  container. It reports rather than just doing the work because an empty queue
  looks identical whether there was nothing to convert or the configuration is
  wrong, and waiting out a five-minute interval to discover which is the
  silence this worker exists to remove. One walk at a time - a scan requested
  while the interval's own is running is refused with a reason rather than
  queued behind minutes of stat-ing.

### Fixed

- **`/healthz` reported a version that was not running, and no update could
  move it.** The version was read from `TRANSCODEARR_VERSION` with the constant
  in `main.py` as a fallback - two sources of truth for one fact, and the
  environment one is the half that can go stale. Updating the live QNAP
  container to 1.0.1 left the **old** container's explicit
  `TRANSCODEARR_VERSION=1.0.0` in place: Container Station rebuilds a container
  from the environment it recorded at create, and an explicit env beats the new
  image's `ENV`. So the container ran 1.0.1 code and answered `1.0.0`, on the
  one field somebody reads when they are already debugging the wrong build -
  and it would have survived every future update down that path.

  `main.VERSION` is now a plain constant compiled into the image, and the image
  no longer sets `TRANSCODEARR_VERSION` at all. Nothing outside the image can
  reach it. The release workflow still refuses a tag that disagrees with the
  constant, the packaging test still refuses a Dockerfile that disagrees, and a
  new test refuses either file reintroducing the environment variable.

## [1.0.1] - 2026-08-20

A patch release from five days on a real library: 2609 jobs, 714 files, 643
converted, and 44 that could never convert no matter how many times they were
retried. Nothing about the API, the settings or the storage format changes.

### Fixed

- **A 10-bit source into an 8-bit profile failed forever, and the fallback
  ladder could not save it.** H.264 NVENC cannot encode 10 bits. Handed a
  `yuv420p10le` source it answers `10 bit encode not supported` / `No capable
  devices found` and exits `-22 (Invalid argument)` with `frame= 0`, and ffmpeg
  does not rescue it on its own: NVENC advertises **one shared pixel-format
  list for H.264 and HEVC**, so `p010` is accepted during format negotiation
  and only refused at encoder init. No rung of the fallback ladder changes the
  encoder or the picture, so every retry failed identically. On the library
  this was found in, 41 files - whole seasons that an arr had upgraded from
  8-bit to 10-bit HEVC releases - had been retrying every six hours for five
  days, and 44% of the entire job history was those retries.

  A profile is a promise about bit depth, and nothing was keeping it. Now
  `output_pix_fmt` narrows the picture with `-pix_fmt yuv420p` when the source
  carries more bits than the chosen profile can, for every encoder rather than
  the one that was noticed. `main10` is left alone, because that profile
  exists to keep those bits.

  **8-bit sources are byte-identical to 1.0.0**, which is the point of doing
  this from the probe rather than always: a pixel conversion is a CPU filter,
  and a CPU filter cannot read frames that stayed in GPU memory, so the flag
  costs the fully-on-GPU decode path wherever it appears. Measured on the
  T1000 this was found on, against a 1080p 10-bit HEVC episode: hand the frames
  back to system RAM and narrow them there, 2.5x realtime; keep them on the GPU
  with `scale_cuda=format=nv12`, 0.6x. `scale_cuda` also breaks outright the
  moment ffmpeg falls back to software decoding, which is the case this has to
  survive, so the slower-looking option is the only correct one.

- **Every ffmpeg failure was recorded as the same sentence.** The stored error
  was the last 400 characters of the last 8 stderr lines, and ffmpeg spends
  those lines restating one errno through every layer on the way out. Both of
  the failures above reduce to `-22 (Invalid argument)` there, so a card that
  cannot encode 10-bit and a driver that never initialised were indistinguishable
  in the job list - and the line that named the cause had been cut before the
  truncation even ran. `error_summary` now keeps the lines that say something:
  the indented input dump is dropped, the restatements and the stats epilogue
  are dropped, and address pointers are stripped so one fault reads as one
  fault instead of a different string on every attempt. The fallback ladder is
  gated on the same text it shows, because a rung that retries on words nobody
  is ever shown is a rung nobody can explain from the job list.

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
