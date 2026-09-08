"""What a job does to the bytes on disk. Real files in a temp dir, no ffmpeg.

The rest of the suite states the rules as values. This one exercises the three
calls that can actually destroy media - os.replace, shutil.move and os.unlink -
because the incident behind this repo was not a wrong decision, it was a
correct decision applied to a file that had changed underneath it.

Nothing here mocks the filesystem: the rules are about what is left on disk
afterwards, and a fake filesystem would only prove the test agrees with itself.
ffprobe and ffmpeg are the exceptions - they are external processes, and the
container is not what is under test.
"""

import os
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

# main reads CONFIG_DIR and TRASH_DIR at import time to place its database.
_TMP = tempfile.mkdtemp(prefix="transcodearr-jobs-")
os.environ["CONFIG_DIR"] = _TMP
os.environ["TRASH_DIR"] = os.path.join(_TMP, "trash")

import core  # noqa: E402
import main  # noqa: E402
import store  # noqa: E402

WHOLE = core.Probe(duration=3600.0, video_streams=1, audio_streams=1, subtitle_streams=0)


class JobCase(unittest.TestCase):
    """One temp media root and one temp trash per test, both real directories."""

    def setUp(self):
        main.init_db()
        conn = main.db()
        conn.execute("DELETE FROM jobs")
        conn.commit()
        self.media = tempfile.mkdtemp(prefix="media-", dir=_TMP)
        os.makedirs(os.path.join(self.media, "Movies"))
        self.trash = tempfile.mkdtemp(prefix="trash-", dir=_TMP)
        self.set_global("MEDIA_ROOTS", [self.media])
        self.set_global("TRASH_DIR", self.trash)

    def set_global(self, name, value):
        self.addCleanup(setattr, main, name, getattr(main, name))
        setattr(main, name, value)

    def write(self, name, content, folder="Movies"):
        path = os.path.join(self.media, folder, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def claim(self, path, kind="transcode"):
        """A row in the state worker_loop leaves it in before calling process."""
        job_id = str(uuid.uuid4())
        conn = main.db()
        conn.execute(
            "INSERT INTO jobs (id, path, state, kind, created, started) VALUES (?,?,?,?,?,?)",
            (job_id, path, "running", kind, time.time(), time.time()),
        )
        conn.commit()
        return {"id": job_id, "path": path, "kind": kind}

    def row(self, job_id):
        return main.db().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def encoder_that_writes(self, content, then=None):
        """A stand-in for run_encode that produces a plausible .part file."""
        def fake(job_id, source, names, src_probe, extract_subtitles=False):
            with open(names.part, "w", encoding="utf-8") as f:
                f.write(content)
            if then is not None:
                then(names)
            return True, "", ""
        return fake

    def run_job(self, job, run_encode=None):
        patches = [mock.patch.object(main, "ffprobe", lambda path: WHOLE)]
        if run_encode is not None:
            patches.append(mock.patch.object(main, "run_encode", run_encode))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        main.process(job)
        return self.row(job["id"])


class NothingIsOverwritten(JobCase):
    """os.replace destroys the destination silently and atomically. Every path
    into it needs a reason to believe the destination is not somebody's media."""

    def test_a_file_already_at_the_visible_name_is_displaced_into_the_trash(self):
        # This used to fail the job. 26 files in a real library sat on that
        # refusal for days: an arr had re-imported the episode, the previous
        # conversion still held the visible name, and nothing on disk changed
        # between attempts so every retry failed identically forever.
        source = self.write(".Movie (2026).mkv", "the re-imported source")
        target = self.write("Movie (2026).mp4", "the previous conversion")
        row = self.run_job(self.claim(source), self.encoder_that_writes("the new conversion"))
        self.assertEqual(row["state"], "done", row["error"])
        self.assertEqual(self.read(target), "the new conversion")
        # Displaced, never destroyed: both the source and the file it replaced
        # are recoverable for the whole retention window.
        self.assertEqual(self.read(os.path.join(self.trash, "Movies", ".Movie (2026).mkv")),
                         "the re-imported source")
        self.assertEqual(self.read(os.path.join(self.trash, "Movies", "Movie (2026).mp4")),
                         "the previous conversion")

    def test_replacing_a_file_is_reported_and_not_done_quietly(self):
        # Somebody may have been about to watch what this replaced. The reason
        # it is safe is that the old file still exists, which only helps a
        # person who is told where it went.
        self.write("Movie.mp4", "the previous conversion")
        source = self.write(".Movie.mkv", "the re-imported source")
        row = self.run_job(self.claim(source), self.encoder_that_writes("the new conversion"))
        self.assertEqual(row["state"], "done", row["error"])
        self.assertIn("replaced the file already at this name", row["warning"])
        self.assertIn("replaced file preserved at", row["log_tail"])
        self.assertIn("source preserved at", row["log_tail"])

    def test_an_untouched_target_leaves_no_replacement_note(self):
        source = self.write(".Movie.mkv", "the original")
        row = self.run_job(self.claim(source), self.encoder_that_writes("the encode"))
        self.assertEqual(row["state"], "done", row["error"])
        self.assertIsNone(row["warning"])
        self.assertNotIn("replaced", row["log_tail"])

    def test_a_file_already_at_the_hidden_staging_name_stops_the_job(self):
        # .Movie.mp4 is where the verified encode lands before the reveal, and
        # it is also exactly what a previous run of the same file leaves behind.
        source = self.write(".Movie.mkv", "the source")
        taken = self.write(".Movie.mp4", "a finished encode nobody revealed")
        row = self.run_job(self.claim(source))
        self.assertEqual(row["state"], "failed")
        self.assertIn("staging name is taken", row["error"])
        self.assertEqual(self.read(taken), "a finished encode nobody revealed")
        self.assertEqual(self.read(source), "the source")

    def test_a_reveal_is_not_blocked_by_its_own_source(self):
        # A reveal's hidden_final IS the source: .Movie.mp4 becomes Movie.mp4.
        # A staging guard that does not exempt that case refuses every reveal,
        # and the file stays behind its dot forever with no error anywhere -
        # the silent failure mode this worker exists to remove.
        source = self.write(".Movie.mp4", "already the right container")
        row = self.run_job(self.claim(source, kind="reveal"))
        self.assertEqual(row["state"], "done", row["error"])
        self.assertEqual(self.read(os.path.join(self.media, "Movies", "Movie.mp4")),
                         "already the right container")
        self.assertFalse(os.path.exists(source))

    def test_a_finished_encode_leaves_the_source_in_the_trash(self):
        source = self.write(".Movie.mkv", "the original")
        row = self.run_job(self.claim(source), self.encoder_that_writes("the encode"))
        self.assertEqual(row["state"], "done", row["error"])
        self.assertEqual(self.read(os.path.join(self.media, "Movies", "Movie.mp4")), "the encode")
        self.assertEqual(self.read(os.path.join(self.trash, "Movies", ".Movie.mkv")), "the original")
        # The staging names are both gone: a stranded hidden_final is what the
        # next run of the same file would refuse to overwrite.
        self.assertFalse(os.path.exists(os.path.join(self.media, "Movies", ".Movie.mp4")))
        self.assertFalse(os.path.exists(os.path.join(self.media, "Movies", ".Movie.tapart.mp4")))

    def test_a_source_replaced_during_the_encode_beats_the_encode(self):
        # An arr upgrading a file mid-run means this encode is of bytes that no
        # longer exist. The encode is the disposable one, always.
        source = self.write(".Movie.mkv", "the original")

        def upgraded(names):
            with open(names.source, "w", encoding="utf-8") as f:
                f.write("a better import that landed mid-encode")

        row = self.run_job(self.claim(source), self.encoder_that_writes("the encode", upgraded))
        self.assertEqual(row["state"], "failed")
        self.assertIn("source changed", row["error"])
        self.assertEqual(self.read(source), "a better import that landed mid-encode")
        self.assertFalse(os.path.exists(os.path.join(self.media, "Movies", "Movie.mp4")))
        self.assertFalse(os.path.exists(os.path.join(self.media, "Movies", ".Movie.tapart.mp4")))
        self.assertFalse(os.path.exists(os.path.join(self.trash, "Movies", ".Movie.mkv")))


class TheGuardIsReAskedNotRemembered(JobCase):
    """occupied() answers about NOW, and every os.replace has to ask it again.

    The pre-flight answer is stale the moment anything slow runs after it -
    an encode, or just an ffprobe of a 4K remux over SMB - and an arr finishing
    an import inside that window lands on exactly the name about to be written.
    Unlike the source, what os.replace clobbers never reaches the trash.
    """

    def test_a_reveal_does_not_clobber_a_file_that_arrived_during_the_probe(self):
        source = self.write(".Movie.mp4", "the hidden copy")
        visible = os.path.join(self.media, "Movies", "Movie.mp4")

        def probe_then_import(_path):
            with open(visible, "w", encoding="utf-8") as f:
                f.write("an import that landed mid-job")
            return WHOLE

        job = self.claim(source, kind="reveal")
        with mock.patch.object(main, "ffprobe", probe_then_import):
            main.process(job)
        row = self.row(job["id"])
        self.assertEqual(row["state"], "failed")
        # Not "already exists" any more: a file that was already there IS
        # displaced now. What is refused is a file that arrived after the job
        # looked - and the message has to say which of the two happened.
        self.assertIn("written by something else while this job ran", row["error"])
        self.assertEqual(self.read(visible), "an import that landed mid-job")
        self.assertEqual(self.read(source), "the hidden copy")
        # And it is not sitting in the trash either - refusing means untouched.
        self.assertFalse(os.path.exists(os.path.join(self.trash, "Movies", "Movie.mp4")))

    def test_a_target_swapped_during_the_encode_is_refused_not_displaced(self):
        # The pre-flight saw a file it was willing to displace. A DIFFERENT one
        # is there by the time the encode finishes, which is an arr importing an
        # upgrade - newer than the source this job converted.
        source = self.write(".Movie.mkv", "the source")
        visible = self.write("Movie.mp4", "the previous conversion")

        def upgraded_target(names):
            with open(names.visible, "w", encoding="utf-8") as f:
                f.write("an upgrade that landed mid-encode")

        row = self.run_job(self.claim(source), self.encoder_that_writes("the encode", upgraded_target))
        self.assertEqual(row["state"], "failed")
        self.assertIn("written by something else while this job ran", row["error"])
        self.assertEqual(self.read(visible), "an upgrade that landed mid-encode")
        self.assertEqual(self.read(source), "the source")
        self.assertFalse(os.path.exists(os.path.join(self.trash, "Movies", "Movie.mp4")))

    def test_a_file_that_arrives_while_the_source_is_trashed_is_not_clobbered(self):
        # The last guard, and the one the reveal never had. Between the check
        # after the encode and the reveal itself sit an fsync of the whole
        # output and a trash move that is a byte copy across filesystems -
        # minutes, on the NFS mount this runs against - and displace() answers
        # "" both for "nothing was there" and for "a stranger is there and I
        # refuse to move it", so the reveal read its own refusal as consent.
        source = self.write(".Movie.mkv", "the source")
        visible = os.path.join(self.media, "Movies", "Movie.mp4")
        real = main.release_page_cache

        def import_during_the_flush(*paths):
            if paths and paths[0].endswith(os.path.join("Movies", ".Movie.mp4")):
                with open(visible, "w", encoding="utf-8") as f:
                    f.write("an import that landed while the source was being trashed")
            return real(*paths)

        with mock.patch.object(main, "release_page_cache", import_during_the_flush):
            row = self.run_job(self.claim(source), self.encoder_that_writes("the encode"))
        self.assertEqual(row["state"], "failed")
        self.assertIn("written by something else while this job ran", row["error"])
        self.assertEqual(self.read(visible), "an import that landed while the source was being trashed")
        self.assertFalse(os.path.exists(os.path.join(self.trash, "Movies", "Movie.mp4")))
        # The source is already gone by then, so the error has to say where both
        # copies are: the output stays staged rather than being thrown away.
        self.assertEqual(self.read(os.path.join(self.media, "Movies", ".Movie.mp4")), "the encode")
        self.assertEqual(self.read(os.path.join(self.trash, "Movies", ".Movie.mkv")), "the source")
        self.assertIn(os.path.join("Movies", ".Movie.mp4"), row["error"])
        self.assertIn(os.path.join("Movies", ".Movie.mkv"), row["error"])


class PageCacheIsHandedBack(JobCase):
    """A conversion reads a whole source and writes a whole output, and neither
    is read again. Left resident they filled 50 GB of a 64 GB box in 90 minutes
    and starved the NVIDIA driver of the high-order pages cuInit needs."""

    def test_a_finished_job_releases_the_files_it_will_not_read_again(self):
        source = self.write(".Movie.mkv", "the original")
        seen = []
        real = main.release_page_cache

        def spy(*paths):
            seen.append([p for p in paths if p])
            return real(*paths)

        with mock.patch.object(main, "release_page_cache", spy):
            row = self.run_job(self.claim(source), self.encoder_that_writes("the encode"))
        self.assertEqual(row["state"], "done", row["error"])
        released = [p for call in seen for p in call]
        visible = os.path.join(self.media, "Movies", "Movie.mp4")
        trashed = os.path.join(self.trash, "Movies", ".Movie.mkv")
        # The output, flushed before the source was trashed, and the source.
        self.assertIn(trashed, released)
        self.assertTrue(any(p in (visible, os.path.join(self.media, "Movies", ".Movie.mp4"))
                            for p in released), released)

    def test_the_output_is_flushed_before_the_source_is_trashed(self):
        # Ordering, not decoration: until the output is on disk, trashing the
        # source leaves one copy of the episode in the page cache of a NAS.
        source = self.write(".Movie.mkv", "the original")
        order = []
        real_release, real_trash = main.release_page_cache, main.trash

        with mock.patch.object(main, "release_page_cache",
                               lambda *p: (order.append("release"), real_release(*p))[1]), \
             mock.patch.object(main, "trash",
                               lambda *a, **k: (order.append("trash"), real_trash(*a, **k))[1]):
            row = self.run_job(self.claim(source), self.encoder_that_writes("the encode"))
        self.assertEqual(row["state"], "done", row["error"])
        self.assertEqual(order[:2], ["release", "trash"], order)

    def test_a_job_that_fails_verification_still_hands_its_source_back(self):
        source = self.write(".Movie.mkv", "the original")
        seen = []
        short = core.Probe(duration=10.0, video_streams=1, audio_streams=1, subtitle_streams=0)

        def probe(path):
            return short if path.endswith(core.PART_MARKER + ".mp4") else WHOLE

        with mock.patch.object(main, "release_page_cache", lambda *p: seen.append([x for x in p if x])),              mock.patch.object(main, "ffprobe", probe),              mock.patch.object(main, "run_encode", self.encoder_that_writes("a short encode")):
            main.process(self.claim(source))
        self.assertIn([source], seen, "a failed job kept its source in page cache")

    def test_releasing_never_damages_or_removes_the_file(self):
        # It is an advisory call about cache, not about content. If this ever
        # became destructive it would do so silently and on every job.
        path = self.write("Keep.mp4", "every byte of this matters")
        self.assertEqual(main.release_page_cache(path), 1 if hasattr(os, "posix_fadvise") else 0)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(self.read(path), "every byte of this matters")

    def test_it_shrugs_at_paths_that_are_not_there(self):
        # Called with a trashed path and a displaced path, and the second is ""
        # for most jobs. None of that is worth an exception after the media is
        # already correct on disk.
        self.assertEqual(main.release_page_cache("", None, "/no/such/file.mkv"), 0)


class AskingAnArrForAReplacement(JobCase):
    """Off by default, once per file, and never for a failure of the machine.

    This is the only feature here that spends somebody's bandwidth and retires
    a release, so what it must NOT do matters more than what it does.
    """

    def setUp(self):
        super().setUp()
        self.asked = []

    def ask(self, error, replace=True, packs=False):
        """Drive request_replacement with a fake arr linked; record the calls."""
        source = self.write(".Movie.mkv", "the source")
        rows = [{"id": "a1", "name": "Sonarr", "kind": "sonarr", "enabled": 1,
                 "base_url": "http://s", "api_key": "k", "arr_path": "/tv", "worker_path": self.media}]
        settings = dict(main.cfg())
        settings["replace_bad_source"] = replace
        settings["replace_bad_source_packs"] = packs
        asked = self.asked

        class FakeArr:
            def __init__(self, _row):
                pass

            def replace_bad_file(self, path, allow_packs=False):
                asked.append((path, allow_packs))
                return (True, "Sonarr: blocklisted Some.Release-GRP and asked for a replacement",
                        {"arr_id": "a1", "arr_name": "Sonarr", "kind": "sonarr",
                         "item_id": 126, "episode_id": 10984, "release": "Some.Release-GRP"})

        with mock.patch.object(main.store, "list_arrs", lambda conn, redact=True: rows),                 mock.patch.object(main, "_client_for", FakeArr),                 mock.patch.object(main, "cfg", lambda: settings):
            main.request_replacement("job1234", source, error)
        return source

    SHORT = "output failed verification: duration mismatch: source 2724s, output 2624s (3.7% off)"

    def test_a_short_output_asks_the_arr_to_blocklist_and_replace(self):
        source = self.ask(self.SHORT)
        self.assertEqual(self.asked, [(source, False)])

    def test_the_season_pack_choice_is_passed_through_not_decided_here(self):
        # main.py must not second-guess the operator: it forwards the answer
        # and the arr applies it against what the grab actually was.
        source = self.ask(self.SHORT, packs=True)
        self.assertEqual(self.asked, [(source, True)])

    def test_a_dead_gpu_asks_nobody(self):
        # The expensive false positive. This exact text failed dozens of jobs
        # on a real box in one evening; every one would have retired a release
        # and started a download.
        self.ask("ffmpeg exited 171: [h264_nvenc] cuInit(0) failed -> CUDA_ERROR_NOT_INITIALIZED")
        self.assertEqual(self.asked, [])

    def test_switched_off_asks_nobody(self):
        self.ask(self.SHORT, replace=False)
        self.assertEqual(self.asked, [])

    def test_it_asks_once_per_file_and_not_again(self):
        # A replacement that is also unreadable means something another
        # download will not fix. Stop, and let a person look.
        source = self.ask(self.SHORT)
        self.assertEqual(len(self.asked), 1)
        conn = main.db()
        conn.execute("INSERT INTO jobs (id, path, state, kind, created, rescan) VALUES (?,?,?,?,?,?)",
                     ("old", source, "failed", "transcode", time.time(),
                      main.REPLACEMENT_MARK + " Sonarr: blocklisted it"))
        conn.commit()
        self.assertTrue(main.already_asked_for_replacement(source))
        self.ask(self.SHORT)
        self.assertEqual(len(self.asked), 1, "it asked a second time for the same file")

    def test_the_request_is_recorded_on_the_job_that_triggered_it(self):
        conn = main.db()
        source = self.write(".Movie.mkv", "the source")
        conn.execute("INSERT INTO jobs (id, path, state, kind, created) VALUES (?,?,?,?,?)",
                     ("job1234", source, "failed", "transcode", time.time()))
        conn.commit()
        self.ask(self.SHORT)
        row = self.row("job1234")
        self.assertTrue(str(row["rescan"] or "").startswith(main.REPLACEMENT_MARK), row["rescan"])
        self.assertIn("blocklisted", row["rescan"])


class TheTrashCanBeUndone(JobCase):
    """Restore and Delete act on media, and take their paths from a client."""

    def trashed_one(self, name=".Movie.mkv", content="the original"):
        """Run a real conversion so the trash holds a real, recorded file."""
        source = self.write(name, content)
        row = self.run_job(self.claim(source), self.encoder_that_writes("the encode"))
        self.assertEqual(row["state"], "done", row["error"])
        listing = main.list_trash()
        self.assertEqual(len(listing["entries"]), 1, listing)
        return source, listing["entries"][0]

    def test_the_listing_knows_where_a_file_came_from(self):
        source, entry = self.trashed_one()
        self.assertEqual(entry["original"], source)
        self.assertTrue(entry["origin_known"])
        self.assertFalse(entry["occupied"])          # the .mkv name is free again
        self.assertTrue(entry["reconverts"])         # a hidden .mkv would be re-queued

    def test_restore_puts_it_back_and_forgets_it(self):
        source, entry = self.trashed_one()
        [result] = main.restore_from_trash([entry["path"]])
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.read(source), "the original")
        self.assertFalse(os.path.exists(entry["path"]))
        self.assertEqual(main.list_trash()["entries"], [])

    def test_restore_refuses_an_occupied_name_until_told_to_replace(self):
        source, entry = self.trashed_one()
        self.write(".Movie.mkv", "something else arrived here")
        [result] = main.restore_from_trash([entry["path"]])
        self.assertFalse(result["ok"])
        self.assertTrue(result["occupied"])
        self.assertEqual(self.read(source), "something else arrived here")
        self.assertTrue(os.path.exists(entry["path"]))

    def test_replacing_trashes_what_was_in_the_way_rather_than_deleting_it(self):
        # The scenario this exists for is an upgrade that turned out worse, so
        # the file being pushed aside is itself a restore candidate ten minutes
        # later. Deleting it would make undoing the undo impossible.
        source, entry = self.trashed_one()
        self.write(".Movie.mkv", "the upgrade nobody wanted")
        [result] = main.restore_from_trash([entry["path"]], replace=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.read(source), "the original")
        self.assertTrue(result["displaced"], "the replaced file was destroyed, not trashed")
        self.assertEqual(self.read(result["displaced"]), "the upgrade nobody wanted")

    def test_delete_removes_it_for_good(self):
        _source, entry = self.trashed_one()
        [result] = main.delete_from_trash([entry["path"]])
        self.assertTrue(result["ok"], result)
        self.assertFalse(os.path.exists(entry["path"]))
        self.assertEqual(main.list_trash()["entries"], [])

    def test_neither_will_touch_a_path_outside_the_trash(self):
        # Both take paths straight from an HTTP body. Without containment,
        # Delete is an arbitrary unlink and Restore is an arbitrary move.
        keep = self.write("Precious.mp4", "somebody's media")
        outside = [keep, os.path.join(self.media, "Movies", "..", "..", "etc", "passwd"),
                   os.path.join(self.trash, "..", os.path.basename(keep))]
        for path in outside:
            [d] = main.delete_from_trash([path])
            [r] = main.restore_from_trash([path], replace=True)
            self.assertFalse(d["ok"], path)
            self.assertFalse(r["ok"], path)
        self.assertEqual(self.read(keep), "somebody's media")

    def test_a_symlink_in_the_trash_cannot_reach_the_library(self):
        # Containment is checked on the REAL path. A link inside the trash
        # pointing at the library would otherwise pass a string check and let
        # Delete unlink whatever it names.
        keep = self.write("Precious.mp4", "somebody's media")
        link = os.path.join(self.trash, "innocent.mp4")
        os.makedirs(self.trash, exist_ok=True)
        try:
            os.symlink(keep, link)
        except (OSError, NotImplementedError):
            self.skipTest("this platform will not make symlinks without privileges")
        [result] = main.delete_from_trash([link])
        self.assertFalse(result["ok"], "a symlink walked out of the trash")
        self.assertEqual(self.read(keep), "somebody's media")

    def fill_trash(self, n):
        """n files in the trash, trashed in a known order."""
        for i in range(n):
            source = self.write(f".Film {i:02d}.mkv", f"original {i}")
            row = self.run_job(self.claim(source), self.encoder_that_writes(f"encode {i}"))
            self.assertEqual(row["state"], "done", row["error"])
        return main.list_trash(limit=1000)["total"]

    def test_paging_walks_every_file_exactly_once(self):
        # The bug a pager has when its sort has ties: one row on two pages and
        # another on none. Files trashed in one burst share a timestamp to the
        # resolution that matters, so path breaks the tie.
        total = self.fill_trash(7)
        self.assertEqual(total, 7)
        seen = []
        for offset in (0, 3, 6):
            page = main.list_trash(limit=3, offset=offset)
            self.assertEqual(page["total"], 7)
            self.assertEqual(page["offset"], offset)
            seen += [e["path"] for e in page["entries"]]
        self.assertEqual(len(seen), 7)
        self.assertEqual(len(set(seen)), 7, "a file appeared on two pages")

    def test_a_page_past_the_end_shows_the_last_page_not_a_blank_one(self):
        # A bulk delete shortens the list under whoever ran it. Clamping to the
        # end of the list leaves a blank table and a working Previous button.
        self.fill_trash(5)
        page = main.list_trash(limit=2, offset=999)
        self.assertEqual(page["offset"], 4)
        self.assertEqual(page["shown"], 1)
        self.assertEqual(page["total"], 5)

    def test_an_empty_trash_pages_without_complaining(self):
        page = main.list_trash(limit=10, offset=40)
        self.assertEqual((page["total"], page["shown"], page["offset"]), (0, 0, 0))

    def test_the_totals_describe_the_whole_trash_not_the_page(self):
        # The header count is what somebody reads before pressing Select all.
        self.fill_trash(4)
        page = main.list_trash(limit=1, offset=0)
        self.assertEqual(page["shown"], 1)
        self.assertEqual(page["total"], 4)
        self.assertGreater(page["bytes"], page["entries"][0]["bytes"])

    def test_a_bulk_call_reports_each_path_separately(self):
        _s1, e1 = self.trashed_one(".One.mkv", "one")
        source2 = self.write(".Two.mkv", "two")
        self.run_job(self.claim(source2), self.encoder_that_writes("encoded two"))
        paths = [e["path"] for e in main.list_trash()["entries"]] + ["/not/in/the/trash.mkv"]
        results = main.delete_from_trash(paths)
        self.assertEqual([r["ok"] for r in results], [True, True, False])
        self.assertEqual(main.list_trash()["entries"], [])


class RetryCooldown(JobCase):
    """The cooldown exists to stop the WATCHER looping on a file that always
    fails. Applied to an API caller it becomes 'come back in six hours' for a
    file they can see, which is the silence this worker exists to remove."""

    def recently_failed(self, path):
        main.db().execute(
            "INSERT INTO jobs (id, path, state, kind, created, finished) VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4()), path, "failed", "transcode", time.time(), time.time()),
        )
        main.db().commit()

    def test_the_watcher_backs_off_but_an_explicit_enqueue_does_not(self):
        path = self.write(".Movie.mkv", "the source")
        self.recently_failed(path)
        self.assertIsNone(main.enqueue(path, "transcode"), "the watcher ignored the cooldown")
        self.assertIsNotNone(main.enqueue(path, "transcode", force=True),
                             "an explicit enqueue was refused by the cooldown")

    def test_a_missing_card_earns_minutes_not_hours(self):
        path = self.write(".Movie.mkv", "the source")
        main.db().execute(
            "INSERT INTO jobs (id, path, state, kind, created, finished, error) VALUES (?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), path, "failed", "transcode", time.time(), time.time() - 20 * 60,
             core.ENCODER_UNAVAILABLE + ": [h264_nvenc] cuInit(0) failed"))
        main.db().commit()
        # Twenty minutes ago: inside the six-hour wait, past the fifteen-minute one.
        self.assertIsNotNone(main.enqueue(path, "transcode"), "the watcher waited hours for a card that was back")


class BootCleanup(JobCase):
    """Anything left 'running' died with the previous process, and the restart
    has to clean up after it without touching anything it did not create."""

    def test_an_interrupted_encode_loses_its_part_and_keeps_its_source(self):
        source = self.write(".Movie.mkv", "the original")
        part = self.write(".Movie.tapart.mp4", "half an encode")
        job = self.claim(source)
        main.init_db()
        self.assertFalse(os.path.exists(part), "a half-written .part survived the restart")
        self.assertEqual(self.read(source), "the original")
        self.assertEqual(self.row(job["id"])["state"], "failed")

    def test_an_interrupted_extraction_loses_its_hidden_sidecars(self):
        # Same rule as the .part beside them: the source is still there, so
        # nothing in them is lost and the retry re-extracts them. Left behind
        # they are invisible to the media server and ineligible for the watcher,
        # so nothing cleans them up - one per attempt, forever.
        source = self.write(".Movie.mkv", "the original")
        part = self.write(".Movie.tapart.mp4", "half an encode")
        stale = self.write(".Movie.eng.srt", "extracted before the process died")
        self.claim(source)
        main.init_db()
        self.assertFalse(os.path.exists(stale), "a hidden sidecar survived the restart")
        self.assertFalse(os.path.exists(part))
        self.assertEqual(self.read(source), "the original")

    def test_hidden_sidecars_survive_a_restart_once_the_source_is_trashed(self):
        # Died between the trash and the reveal: the source is gone, so these
        # are the only copy of those tracks - the same reason hidden_final is
        # left alone here, and the same guard decides both.
        source = os.path.join(self.media, "Movies", ".Movie.mkv")
        os.makedirs(os.path.dirname(source), exist_ok=True)
        hidden_final = self.write(".Movie.mp4", "the finished encode")
        kept = self.write(".Movie.eng.srt", "the only copy of the english track")
        self.claim(source)
        main.init_db()
        self.assertTrue(os.path.exists(kept), "boot cleanup deleted the only copy of a subtitle")
        self.assertTrue(os.path.exists(hidden_final))

    def test_an_interrupted_reveal_does_not_delete_the_file_it_was_revealing(self):
        # For a reveal, hidden_final IS the source and there is no trash copy,
        # because nothing was replaced. A boot sweep of stranded hidden_final
        # files that does not exempt the source deletes the only copy of the
        # film, and the job history says 'interrupted by restart'.
        source = self.write(".Movie.mp4", "the only copy")
        job = self.claim(source, kind="reveal")
        main.init_db()
        self.assertTrue(os.path.exists(source), "boot cleanup deleted a reveal's source")
        self.assertEqual(self.read(source), "the only copy")
        self.assertEqual(self.row(job["id"])["state"], "failed")


class TrashDestination(JobCase):
    """Where a replaced source goes. It is a safety copy, so the destination
    has to be unambiguous - two sources must never resolve to one file."""

    def test_the_path_is_mirrored_relative_to_the_root_that_holds_it(self):
        # Mirrored so a recovery is a move back, and per-root so the second
        # mount is not filed as if it were the first.
        second = tempfile.mkdtemp(prefix="media2-", dir=_TMP)
        self.set_global("MEDIA_ROOTS", [self.media, second])
        source = os.path.join(second, "TV", "Show", "S01E01.mkv")
        os.makedirs(os.path.dirname(source))
        with open(source, "w", encoding="utf-8") as f:
            f.write("an episode")
        dest = main.trash(source)
        self.assertEqual(dest, os.path.join(self.trash, "TV", "Show", "S01E01.mkv"))
        self.assertEqual(self.read(dest), "an episode")

    def test_two_files_with_the_same_name_in_different_folders_both_survive(self):
        # The reason the relative path is mirrored rather than flattened to a
        # basename: "video.mkv" and "Movie.mkv" are extremely common names, and
        # a flat trash would file the second one on top of the first.
        first = self.write("video.mkv", "the 2001 film", folder="Movies/Film (2001)")
        second = self.write("video.mkv", "the 2019 remake", folder="Movies/Film (2019)")
        a, b = main.trash(first), main.trash(second)
        self.assertNotEqual(a, b)
        self.assertEqual(self.read(a), "the 2001 film")
        self.assertEqual(self.read(b), "the 2019 remake")


class SubtitlesBesideTheFile(JobCase):
    """Extraction writes new files into somebody's library, so it obeys the same
    dot convention the video does.

    The failure it exists to prevent is not a lost subtitle - it is a media
    server scanning a half-written .srt, or one named for a file that is about
    to be replaced, and remembering it. Jellyfin caches an external subtitle it
    has seen; correcting that afterwards means editing the library by hand.
    """

    SUBS = (core.SubtitleStream("subrip", "eng", False),
            core.SubtitleStream("ass", "jpn", False))
    CUE = "1\n00:00:01,000 --> 00:00:02,000\nhello\n"

    def extractor(self, content=CUE, returncode=0):
        """Stands in for the extraction pass, writing every output path it is
        handed. build_extract_args puts each one straight after its -c:s value."""
        calls = []

        def run(args, **_kwargs):
            calls.append(args)
            for i in range(2, len(args)):
                if args[i - 2] == "-c:s":
                    with open(args[i], "w", encoding="utf-8") as f:
                        f.write(content)
            return mock.Mock(returncode=returncode, stderr="[srt] nothing to write\n")

        run.calls = calls
        return run

    def convert(self, source, subtitles=SUBS, extract=True, extractor=None, encode=None):
        probe = core.Probe(3600.0, 1, 1, len(subtitles), subtitles=tuple(subtitles))
        # Kept on the case so a test can toggle the setting from inside the
        # encode, which is the whole point of one of them.
        self.settings = dict(main.cfg())
        self.settings["extract_subtitles"] = extract
        job = self.claim(source)
        self.job_id = job["id"]
        self.ffmpeg = extractor or self.extractor()
        with mock.patch.object(main, "ffprobe", lambda path: probe), \
                mock.patch.object(main, "cfg", lambda: self.settings), \
                mock.patch.object(main, "run_encode",
                                  encode or self.encoder_that_writes("the encode")), \
                mock.patch.object(main.subprocess, "run", self.ffmpeg):
            main.process(job)
        return self.row(job["id"])

    def beside(self, *names):
        """What is actually on disk next to the film, hidden files included."""
        folder = os.path.join(self.media, "Movies")
        found = sorted(os.listdir(folder))
        return [n for n in found if not names or n in names]

    def test_the_subtitles_become_visible_in_the_same_breath_as_the_film(self):
        source = self.write(".Dark - S01E01.mkv", "the source")
        row = self.convert(source)
        self.assertEqual(row["state"], "done", row["error"])
        self.assertEqual(self.beside(),
                         ["Dark - S01E01.eng.srt", "Dark - S01E01.jpn.ass", "Dark - S01E01.mp4"])
        self.assertEqual(self.read(os.path.join(self.media, "Movies", "Dark - S01E01.eng.srt")), self.CUE)
        self.assertIn("extracted 2 subtitle files", row["warning"])

    def test_the_film_is_never_visible_before_its_subtitles(self):
        # This used to assert the window rather than close it: the video was
        # revealed first, so a scan landing in between cached the film with no
        # subtitles and correcting that afterwards means editing the library by
        # hand. The subtitles go first now - one beside a film that is not there
        # yet is offered to nobody.
        source = self.write(".Dark.mkv", "the source")
        seen = []

        def watch(sidecars, expected):
            seen.append(sorted(os.listdir(os.path.join(self.media, "Movies"))))
            return main_reveal(sidecars, expected)

        main_reveal = main.reveal_sidecars
        with mock.patch.object(main, "reveal_sidecars", watch):
            row = self.convert(source)
        self.assertEqual(row["state"], "done", row["error"])
        # Every subtitle written, and the film itself still hidden.
        self.assertEqual(seen[0], [".Dark.eng.srt", ".Dark.jpn.ass", ".Dark.mp4"])
        self.assertEqual(self.beside(), ["Dark.eng.srt", "Dark.jpn.ass", "Dark.mp4"])

    def test_ass_is_extracted_as_ass_so_the_styling_survives(self):
        # The anime case, and the reason the default is not "everything to srt".
        # Karaoke, sign typesetting and positioning live in the ASS itself; the
        # mov_text an mp4 would force keeps none of it, and the styled original
        # goes to the trash with the source.
        source = self.write(".Anime - S01E01.mkv", "the source")
        self.assertEqual(self.convert(source)["state"], "done")
        [command] = self.ffmpeg.calls
        self.assertEqual(command[command.index("0:s:1") + 1: command.index("0:s:1") + 3], ["-c:s", "copy"])
        self.assertIn("Anime - S01E01.jpn.ass", self.beside())

    def test_nothing_is_left_hidden_when_the_job_dies_after_extracting(self):
        # A hidden .srt is invisible to the media server and ineligible for the
        # watcher, so nothing breaks loudly - which is exactly why it would
        # accumulate, one per attempt, for a file that fails every six hours.
        source = self.write(".Dark.mkv", "the source")

        def boom(path, job_id=None):
            raise OSError("the share went away between the encode and the swap")

        with mock.patch.object(main, "trash", boom):
            row = self.convert(source)
        self.assertEqual(row["state"], "failed")
        self.assertIn("share went away", row["error"])
        self.assertEqual([n for n in self.beside() if n.endswith((".srt", ".ass"))], [])

    def test_a_failed_encode_never_gets_as_far_as_writing_a_subtitle(self):
        source = self.write(".Dark.mkv", "the source")
        row = self.convert(source, encode=lambda *a: (False, "", "ffmpeg exited 1: No space left on device"))
        self.assertEqual(row["state"], "failed")
        self.assertEqual(self.ffmpeg.calls, [])
        self.assertEqual([n for n in self.beside() if n.endswith((".srt", ".ass"))], [])

    def test_a_subtitle_that_will_not_come_out_fails_the_conversion(self):
        # It used to be a warning on a job that reported done - but the mp4 was
        # built with -sn BECAUSE these tracks were going to sidecars, so that
        # shipped a file with no subtitles anywhere and trashed the only copy
        # that had them. Failing leaves the source alone for the next attempt.
        source = self.write(".Dark.mkv", "the source")
        row = self.convert(source, extractor=self.extractor(returncode=1))
        self.assertEqual(row["state"], "failed")
        self.assertIn("subtitles could not be extracted", row["error"])
        self.assertEqual(self.read(source), "the source")
        self.assertEqual(self.beside(), [".Dark.mkv"])

    def test_an_ffmpeg_that_says_zero_and_writes_nothing_fails_the_job_too(self):
        # Never trust exit 0 is the rule this repo exists for. The tracks are
        # not in the mp4 either - it was built with -sn - so a job that reported
        # done here would have lost them with the trashed source.
        def wrote_nothing(args, **_kwargs):
            self.ffmpeg.calls.append(args)
            return mock.Mock(returncode=0, stderr="")

        wrote_nothing.calls = []
        source = self.write(".Dark.mkv", "the source")
        row = self.convert(source, extractor=wrote_nothing)
        self.assertEqual(row["state"], "failed")
        self.assertIn("nothing was written for", row["error"])
        self.assertEqual(self.read(source), "the source")
        self.assertEqual(self.beside(), [".Dark.mkv"])

    def test_a_source_with_no_text_subtitles_at_all_still_succeeds(self):
        # The other half of the rule above: nothing came out because there was
        # nothing in there, which is not a failure of anything.
        source = self.write(".Dark.mkv", "the source")
        row = self.convert(source, subtitles=())
        self.assertEqual(row["state"], "done", row["error"])
        self.assertEqual(self.beside(), ["Dark.mp4"])
        self.assertEqual(self.ffmpeg.calls, [])

    def test_a_hidden_sidecar_name_already_taken_is_never_overwritten(self):
        # `ffmpeg -y` overwrites whatever is at these names, and the video's own
        # staging name is explicitly guarded against exactly that. A hidden .srt
        # sitting here is another job's work in flight or somebody else's file.
        # It is moved aside rather than overwritten - and rather than refused
        # forever, which is what it used to be: nothing ever cleared the file,
        # so one transient error poisoned that filename's conversions for good.
        source = self.write(".Dark.mkv", "the source")
        held = self.write(".Dark.eng.srt", "a leftover from an earlier attempt")
        row = self.convert(source)
        self.assertEqual(row["state"], "done", row["error"])
        self.assertFalse(os.path.exists(held), "the leftover was left to poison the name again")
        self.assertEqual(self.read(os.path.join(self.trash, "Movies", ".Dark.eng.srt")),
                         "a leftover from an earlier attempt")
        self.assertIn("leftover hidden subtitle", row["warning"])
        # And the extraction ran, at a name that is now free.
        self.assertEqual(self.read(os.path.join(self.media, "Movies", "Dark.eng.srt")), self.CUE)

    def test_a_hidden_sidecar_of_a_job_still_in_flight_is_left_alone(self):
        # The one thing the self-heal above must never clear. Two sources plan
        # the same targets - ".Dark.mkv" and "Dark.avi" both become "Dark.mp4" -
        # and up to WORKER_POOL jobs run at once, so a hidden .srt at one of
        # these names can be another job's extraction in progress.
        source = self.write(".Dark.mkv", "the source")
        held = self.write(".Dark.eng.srt", "the other job's extraction, in progress")
        self.claim(os.path.join(self.media, "Movies", "Dark.avi"))   # still 'running'
        row = self.convert(source)
        self.assertEqual(row["state"], "failed")
        self.assertIn("taken by a job in flight", row["error"])
        self.assertEqual(self.read(held), "the other job's extraction, in progress")
        self.assertEqual(self.read(source), "the source")
        self.assertEqual(self.ffmpeg.calls, [], "ffmpeg ran at a name it was not allowed to write")

    def test_a_sidecar_that_cannot_be_read_back_is_not_reported_as_extracted(self):
        # ESTALE or EIO on a NAS: ffmpeg wrote the file and we cannot read it.
        # It landed in neither list, so the loud branch never fired, the job
        # reported success and trashed the source - and the mp4 was built with
        # -sn, so that track was neither embedded nor on disk. A directory is
        # the portable way to make open() raise here.
        source = self.write(".Dark.mkv", "the source")
        hidden = os.path.join(self.media, "Movies", ".Dark.eng.srt")

        def wrote_then_unreadable(args, **kwargs):
            result = base(args, **kwargs)
            os.unlink(hidden)
            os.mkdir(hidden)
            return result

        base = self.extractor()
        row = self.convert(source, extractor=wrote_then_unreadable)
        self.assertEqual(row["state"], "failed")
        self.assertIn(".Dark.eng.srt", row["error"])
        self.assertEqual(self.read(source), "the source")
        self.assertFalse(os.path.exists(os.path.join(self.media, "Movies", "Dark.mp4")),
                         "a job shipped an mp4 built with -sn and no subtitle beside it")
        # Every planned sidecar is cleaned up, readable or not.
        self.assertFalse(os.path.exists(os.path.join(self.media, "Movies", ".Dark.jpn.ass")))

    def test_a_subtitle_that_could_not_be_revealed_says_where_it_is(self):
        # It is not unlinked: the source is in the trash by then, so this hidden
        # file is the only copy of that track outside it. What it must not be is
        # unreferenced - the note names it, and the staging guard clears it on
        # the next conversion of that name, when the source is back on disk.
        source = self.write(".Dark.mkv", "the source")
        real = os.replace

        def flaky(src, dst):
            if str(dst).endswith("Dark.jpn.ass"):
                raise OSError("read-only file system")
            return real(src, dst)

        with mock.patch.object(main.os, "replace", flaky):
            row = self.convert(source)
        self.assertEqual(row["state"], "done", row["error"])
        self.assertIn(".Dark.jpn.ass", row["warning"], "the leftover is named nowhere")
        self.assertTrue(os.path.exists(os.path.join(self.media, "Movies", ".Dark.jpn.ass")))

    def test_the_setting_is_read_once_and_never_again_after_the_encode(self):
        # Two reads separated by a whole encode: run_encode built the mp4 with
        # -sn because extraction was on, and the second read - hours later -
        # found it off and skipped the extraction. Every subtitle gone, and the
        # job said done.
        source = self.write(".Dark.mkv", "the source")

        def encode_then_toggle(job_id, src, names, probe, extract_subtitles):
            self.assertTrue(extract_subtitles, "run_encode was not told what the caller decided")
            with open(names.part, "w", encoding="utf-8") as f:
                f.write("an mp4 built with -sn")
            self.settings["extract_subtitles"] = False   # turned off mid-encode
            return True, "", ""

        row = self.convert(source, encode=encode_then_toggle)
        self.assertEqual(row["state"], "done", row["error"])
        self.assertEqual(self.beside(), ["Dark.eng.srt", "Dark.jpn.ass", "Dark.mp4"])

    def test_a_file_that_arrives_during_the_extraction_is_not_destroyed(self):
        # The worst of them. Extraction is a full demux of the source and runs
        # for up to SUBTITLE_TIMEOUT over a NAS, and it sat between the guard
        # and the two os.replace calls - so an arr importing the upgrade inside
        # that window had its file destroyed, with no trash copy, by a job that
        # then reported done.
        source = self.write(".Dark.mkv", "the source")

        def extract_then_import(args, **kwargs):
            result = base(args, **kwargs)
            self.write("Dark.mp4", "the upgrade an arr just imported")
            return result

        base = self.extractor()
        row = self.convert(source, extractor=extract_then_import)
        self.assertEqual(row["state"], "failed")
        self.assertIn("written by something else", row["error"])
        self.assertEqual(self.read(os.path.join(self.media, "Movies", "Dark.mp4")),
                         "the upgrade an arr just imported")
        self.assertEqual(self.read(source), "the source")
        # Nothing half-done left behind: no .part, and no hidden sidecars.
        self.assertEqual(self.beside(), [".Dark.mkv", "Dark.mp4"])

    def test_a_staging_name_taken_during_the_extraction_is_not_overwritten(self):
        # The same window, the other name: a hidden import waiting for its own
        # reveal job is not a stale output, and displacing it eats somebody
        # else's pending work.
        source = self.write(".Dark.mkv", "the source")

        def extract_then_import(args, **kwargs):
            result = base(args, **kwargs)
            self.write(".Dark.mp4", "a hidden import waiting for its reveal")
            return result

        base = self.extractor()
        row = self.convert(source, extractor=extract_then_import)
        self.assertEqual(row["state"], "failed")
        self.assertIn("staging name is taken", row["error"])
        self.assertEqual(self.read(os.path.join(self.media, "Movies", ".Dark.mp4")),
                         "a hidden import waiting for its reveal")
        self.assertEqual(self.beside(), [".Dark.mkv", ".Dark.mp4"])

    def test_cancel_is_honoured_after_the_extraction(self):
        # Cancel was a no-op for the whole extraction window, and the job then
        # trashed the source and replaced the file anyway.
        source = self.write(".Dark.mkv", "the source")

        def extract_then_cancel(args, **kwargs):
            result = base(args, **kwargs)
            with main._jobs_lock:
                main._running[self.job_id] = {"cancel": True, "proc": None}
            return result

        base = self.extractor()
        self.addCleanup(main._running.clear)
        row = self.convert(source, extractor=extract_then_cancel)
        self.assertEqual(row["state"], "cancelled")
        self.assertEqual(self.read(source), "the source")
        self.assertEqual(self.beside(), [".Dark.mkv"])

    def test_the_subtitles_are_revealed_once_the_source_is_in_the_trash(self):
        # After the trash, these hidden files are the only copy of those tracks
        # anywhere. The crash handler used to unlink them on exactly this path,
        # then merely kept them - hidden, and referenced by nothing. The watcher
        # re-queues the staged .Dark.mp4 as a reveal job and unhides the film
        # WITHOUT them, so keeping them stranded them invisible for good.
        source = self.write(".Dark.mkv", "the source")

        def boom(visible, expected):
            raise OSError("the share went away between the trash and the reveal")

        with mock.patch.object(main, "displace", boom):
            row = self.convert(source)
        self.assertEqual(row["state"], "failed")
        self.assertEqual([n for n in self.beside() if n.endswith((".srt", ".ass"))],
                         ["Dark.eng.srt", "Dark.jpn.ass"])
        self.assertEqual(self.read(os.path.join(self.media, "Movies", "Dark.eng.srt")), self.CUE)
        # The film is still hidden, waiting for its own reveal job - a subtitle
        # beside a film that is not there yet is offered to nobody.
        self.assertIn(".Dark.mp4", self.beside())

    def test_a_subtitle_that_arrived_while_the_job_ran_is_left_alone(self):
        # Bazarr downloading one mid-encode is the ordinary case. It used to be
        # trashed with no identity check and named nowhere, so it was gone for
        # good after trash_keep_days and nobody knew to restore it.
        source = self.write(".Dark.mkv", "the source")

        def extract_then_bazarr(args, **kwargs):
            result = base(args, **kwargs)
            self.write("Dark.eng.srt", "the one Bazarr just downloaded")
            return result

        base = self.extractor()
        row = self.convert(source, extractor=extract_then_bazarr)
        self.assertEqual(row["state"], "done", row["error"])
        self.assertEqual(self.read(os.path.join(self.media, "Movies", "Dark.eng.srt")),
                         "the one Bazarr just downloaded")
        self.assertFalse(os.path.exists(os.path.join(self.trash, "Movies", "Dark.eng.srt")))
        self.assertIn("Dark.eng.srt was written by something else", row["warning"])

    def test_the_count_is_what_landed_not_what_was_written(self):
        # The count was computed before any reveal happened, so the job claimed
        # files nobody has. A reveal that fails is reported now, not swallowed.
        source = self.write(".Dark.mkv", "the source")
        real = os.replace

        def flaky(src, dst):
            if str(dst).endswith(".jpn.ass"):
                raise OSError("read-only file system")
            return real(src, dst)

        with mock.patch.object(main.os, "replace", flaky):
            row = self.convert(source)
        self.assertEqual(row["state"], "done", row["error"])
        self.assertIn("extracted 1 subtitle file", row["warning"])
        self.assertIn("could not be revealed", row["warning"])
        self.assertIn("Dark.eng.srt", self.beside())

    def test_the_replaced_files_subtitles_go_to_the_trash_with_it(self):
        # A subtitle left behind is not an orphan - it reattaches to whatever
        # takes that name next. The previous conversion's German track beside a
        # new file with no German in it is worse than a missing subtitle,
        # because nothing about it looks wrong.
        self.write("Dark.mp4", "the previous conversion")
        stale = self.write("Dark.ger.srt", "the previous conversion's german track")
        # A different film whose name merely starts the same way. The sweep is
        # a prefix rule, so this is the direction that would quietly move
        # somebody else's subtitle.
        neighbour = self.write("Dark Knight.eng.srt", "another film's subtitle")
        source = self.write(".Dark.mkv", "the re-imported source")
        row = self.convert(source, subtitles=(core.SubtitleStream("subrip", "eng", False),))
        self.assertEqual(row["state"], "done", row["error"])
        self.assertFalse(os.path.exists(stale), "a stale subtitle reattached to the new film")
        self.assertEqual(self.read(os.path.join(self.trash, "Movies", "Dark.ger.srt")),
                         "the previous conversion's german track")
        self.assertIn("Dark.ger.srt went with it", row["warning"])
        self.assertEqual(self.read(neighbour), "another film's subtitle")
        self.assertEqual(self.beside(), ["Dark Knight.eng.srt", "Dark.eng.srt", "Dark.mp4"])

    ASS_HEADER = "[Script Info]\nScriptType: v4.00+\n\n[Events]\nFormat: Layer, Start, End, Text\n"

    def test_a_track_with_no_events_is_dropped_even_though_its_file_is_not_empty(self):
        # "size > 0" never fired for an .ass: the muxer always writes the
        # [Script Info] and [Events] header, so an empty track shipped as a
        # subtitle the media server offers and that plays nothing.
        source = self.write(".Dark.mkv", "the source")
        row = self.convert(source, extractor=self.extractor(content=self.ASS_HEADER))
        self.assertEqual(row["state"], "done", row["error"])
        self.assertEqual(self.beside(), ["Dark.mp4"])
        self.assertIn("extracted 0 subtitle files", row["warning"])

    def test_an_empty_track_is_dropped_rather_than_offered(self):
        # ffmpeg writes a header for a stream with no cues in it. Revealed, that
        # is a subtitle Jellyfin offers and that plays nothing, which reads as a
        # broken file rather than as an absent track.
        source = self.write(".Dark.mkv", "the source")
        row = self.convert(source, extractor=self.extractor(content=""))
        self.assertEqual(row["state"], "done", row["error"])
        self.assertEqual(self.beside(), ["Dark.mp4"])
        self.assertIn("extracted 0 subtitle files", row["warning"])

    def test_image_subtitles_are_named_on_the_job_rather_than_dropped_in_silence(self):
        # A Blu-ray remux whose only subtitles are PGS finishes with no sidecars
        # at all. That is the format's answer - a bitmap is not a text file and
        # OCR is not something this worker runs unattended over a library - so
        # the job says which codecs stayed behind instead of leaving somebody to
        # work out why the subtitles they could see in the source are gone.
        source = self.write(".Film.mkv", "the source")
        row = self.convert(source, subtitles=(core.SubtitleStream("hdmv_pgs_subtitle", "eng", False),))
        self.assertEqual(row["state"], "done", row["error"])
        self.assertIn("image subtitles cannot become text files", row["warning"])
        self.assertIn("hdmv_pgs_subtitle", row["warning"])
        self.assertEqual(self.ffmpeg.calls, [], "ffmpeg was run for a track it cannot extract")
        # The source keeps them, and the source is in the trash for the window.
        self.assertTrue(os.path.exists(os.path.join(self.trash, "Movies", ".Film.mkv")))

    def test_it_is_off_by_default_and_changes_nothing(self):
        # The default has to stay off: this alters what an existing library
        # looks like on disk, and nobody upgrading asked for that.
        self.assertFalse(store.SPEC_BY_KEY["extract_subtitles"].default)
        self.assertFalse(main.cfg()["extract_subtitles"])
        source = self.write(".Dark.mkv", "the source")
        row = self.convert(source, extract=False)
        self.assertEqual(row["state"], "done", row["error"])
        self.assertEqual(self.beside(), ["Dark.mp4"])
        self.assertEqual(self.ffmpeg.calls, [])

    def test_a_subtitle_already_at_that_name_is_displaced_not_destroyed(self):
        # Very likely the previous conversion's own sidecar - but it could just
        # as easily be one Bazarr downloaded or somebody typed, and this worker
        # does not unlink a file it did not write. Same trash, same retention.
        self.write("Dark.eng.srt", "the one Bazarr downloaded")
        source = self.write(".Dark.mkv", "the source")
        row = self.convert(source)
        self.assertEqual(row["state"], "done", row["error"])
        self.assertEqual(self.read(os.path.join(self.media, "Movies", "Dark.eng.srt")), self.CUE)
        self.assertEqual(self.read(os.path.join(self.trash, "Movies", "Dark.eng.srt")),
                         "the one Bazarr downloaded")

    def test_a_reveal_only_job_is_left_alone(self):
        # A hidden .mp4 is somebody's import waiting to be unhidden. Nothing is
        # re-encoded, nothing is trashed, and there is no conversion to attach a
        # subtitle to.
        source = self.write(".Dark.mp4", "already the right container")
        row = self.convert(source)
        self.assertEqual(row["state"], "done", row["error"])
        self.assertEqual(self.beside(), ["Dark.mp4"])
        self.assertEqual(self.ffmpeg.calls, [])


class FakeFfmpeg:
    """Enough of Popen for run_encode: records argv, returns a chosen exit code.

    The last entry of the plan repeats, so a test that expects one attempt
    fails on the assertion about attempt count rather than on an IndexError.
    """

    def __init__(self, plan):
        self.plan = list(plan)
        self.calls = []

    def __call__(self, args, **_kwargs):
        self.calls.append(args)
        code, stderr = self.plan[min(len(self.calls) - 1, len(self.plan) - 1)]
        return FakeProc(code, stderr)


class FakeProc:
    def __init__(self, returncode, stderr_line):
        self.returncode = returncode
        self.stdout = iter(["out_time_us=1000000\n"])
        self.stderr = iter([stderr_line])

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


class FallbackLadder(JobCase):
    """MP4 cannot carry every stream a source has, so one failure is not a
    failed job - but a retry loop that cannot tell the difference between
    'this stream does not fit' and 'someone pressed cancel' is worse."""

    def attempt(self, plan, subtitle_streams=1, cancelled=False, extract=False):
        job_id = str(uuid.uuid4())
        names = core.plan_names(os.path.join(self.media, "Movies", ".Movie.mkv"))
        fake = FakeFfmpeg(plan)
        with main._jobs_lock:
            main._running[job_id] = {"cancel": cancelled, "proc": None}
        self.addCleanup(main._running.clear)
        probe = core.Probe(3600.0, 1, 1, subtitle_streams)
        with mock.patch.object(main.subprocess, "Popen", fake):
            return fake, main.run_encode(job_id, names.source, names, probe, extract)

    def test_a_subtitle_failure_retries_without_subtitles(self):
        fake, (ok, warning, error) = self.attempt([(1, "Error: mov_text encoder not found"), (0, "")])
        self.assertTrue(ok, error)
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("mov_text", fake.calls[0])
        self.assertIn("-sn", fake.calls[1])
        self.assertNotIn("mov_text", fake.calls[1])
        # A dropped subtitle track is recorded, never silently lost.
        self.assertIn("subtitle", warning)

    def test_extraction_takes_the_subtitles_out_of_the_mp4_and_off_the_ladder(self):
        # Carrying the same tracks inside the file as well would offer every
        # language twice in Jellyfin, with the mov_text copy - stripped of the
        # styling the sidecar exists to keep - as one of the two. And the rung
        # below the first would then only ever repeat it.
        # Passed in rather than read here: process() decides it once, before
        # the encode, and uses that same answer again afterwards.
        fake, (ok, warning, error) = self.attempt([(0, "")], subtitle_streams=5, extract=True)
        self.assertTrue(ok, error)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("-sn", fake.calls[0])
        self.assertNotIn("mov_text", fake.calls[0])
        self.assertEqual(warning, "")   # nothing was dropped - they went to sidecars

    def test_an_unrelated_failure_is_reported_rather_than_retried(self):
        # Retrying a full disk or an unreadable share just burns the queue.
        fake, (ok, _warning, error) = self.attempt([(1, "No space left on device")])
        self.assertFalse(ok)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("exited 1", error)
        self.assertIn("No space left", error)

    def test_a_cancelled_job_is_not_retried(self):
        # ffmpeg exits nonzero when it is terminated, and the message it leaves
        # behind can look exactly like a stream that would not fit. Restarting
        # an encode somebody stopped is the one retry that is never wanted.
        fake, (ok, _warning, error) = self.attempt([(255, "Error: mov_text encoder not found")], cancelled=True)
        self.assertFalse(ok)
        self.assertEqual(error, "cancelled")
        self.assertEqual(len(fake.calls), 1)

    CUINIT = ("[h264_nvenc @ 0x55d0] dl_fn->cuda_dl->cuInit(0) failed -> "
              "CUDA_ERROR_NOT_INITIALIZED: initialization error")

    def quick_retries(self, attempts):
        """The encoder retry loop with no wait and no nvidia-smi call."""
        main.store.save_settings(main.db(), {"encoder_retry_attempts": attempts, "encoder_retry_seconds": 0})
        self.addCleanup(lambda: (main.db().execute("DELETE FROM settings"), main.db().commit()))
        p = mock.patch.object(main, "log_encoder_diagnostics", lambda job_id: None)
        p.start()
        self.addCleanup(p.stop)

    def test_a_missing_card_retries_the_same_command_not_the_next_rung(self):
        # "cuda" is a fallback word, so before this a cuInit refusal walked all
        # four rungs in three seconds - none of them changes the video encoder.
        self.quick_retries(3)
        fake, (ok, warning, error) = self.attempt([(171, self.CUINIT), (171, self.CUINIT), (0, "")])
        self.assertTrue(ok, error)
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(fake.calls[0], fake.calls[1])
        self.assertEqual(fake.calls[1], fake.calls[2])
        self.assertEqual(warning, "")   # nothing was dropped to get there

    def test_a_card_that_stays_missing_fails_by_name_after_the_retries(self):
        self.quick_retries(2)
        fake, (ok, _warning, error) = self.attempt([(171, self.CUINIT)])
        self.assertFalse(ok)
        self.assertEqual(len(fake.calls), 3)   # the first try and two retries, no rungs
        self.assertTrue(error.startswith(core.ENCODER_UNAVAILABLE + ": "), error)
        self.assertIn("cuInit", error)
        self.assertFalse(core.is_bad_source_failure(error))

    def test_the_diagnostics_line_is_logged_once_before_the_first_retry(self):
        self.quick_retries(2)
        seen = []
        with mock.patch.object(main, "log_encoder_diagnostics", seen.append):
            self.attempt([(171, self.CUINIT)])
        self.assertEqual(len(seen), 1)

    def test_the_diagnostics_never_raise_on_a_box_without_a_card(self):
        # No nvidia-smi and no /dev/nvidia* is a warning line, not a crash
        # between two encode attempts on the worker.
        with mock.patch.object(main.subprocess, "run", side_effect=OSError("no such tool")), \
                self.assertLogs(main.log, level="WARNING") as logged:
            main.log_encoder_diagnostics("abcdef12")
        self.assertIn("nvidia-smi", logged.output[0])
        self.assertIn("no such tool", logged.output[0])


class ProbeThatNeverAnswered(JobCase):
    """ffprobe timing out on a slow share is not ffprobe finding no video
    stream - and the second sentence is one the arr replacement rule acts on."""

    def test_ffprobe_tells_a_timeout_apart_from_an_unreadable_file(self):
        with mock.patch.object(main.subprocess, "run",
                               side_effect=main.subprocess.TimeoutExpired("ffprobe", 120)):
            with self.assertRaises(main.ProbeUnavailable):
                main.ffprobe("/media/Film.mkv")
        with mock.patch.object(main.subprocess, "run", side_effect=OSError("stale file handle")):
            with self.assertRaises(main.ProbeUnavailable):
                main.ffprobe("/media/Film.mkv")
        with mock.patch.object(main.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="")):
            self.assertIsNone(main.ffprobe("/media/Film.mkv"))

    def test_a_source_probe_that_hangs_fails_the_job_without_blaming_the_file(self):
        source = self.write(".Movie.mkv", "the source")
        job = self.claim(source)

        def hung(path):
            raise main.ProbeUnavailable("Command 'ffprobe' timed out after 120 seconds")

        with mock.patch.object(main, "ffprobe", hung):
            main.process(job)
        row = self.row(job["id"])
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["error"], "source probe failed (timeout or I/O error) - will retry")
        self.assertFalse(core.is_bad_source_failure(row["error"]))
        self.assertEqual(self.read(source), "the source")

    def test_an_output_probe_that_hangs_fails_the_job_and_keeps_the_source(self):
        source = self.write(".Movie.mkv", "the source")
        job = self.claim(source)

        def probe(path):
            if path == source:
                return WHOLE
            raise main.ProbeUnavailable("stale file handle")

        with mock.patch.object(main, "ffprobe", probe), \
                mock.patch.object(main, "run_encode", self.encoder_that_writes("the encode")):
            main.process(job)
        row = self.row(job["id"])
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["error"], "output probe failed (timeout or I/O error)")
        self.assertFalse(core.is_bad_source_failure(row["error"]))
        self.assertEqual(self.read(source), "the source")
        self.assertFalse(os.path.exists(core.plan_names(source).part))


class AFileWaitingOnAReplacement(JobCase):
    """The loop that made this rule: asking an arr for a replacement did not
    stop the watcher re-queueing the unreadable file, so every retry cooldown
    spent a whole GPU encode reaching the identical verification failure. Seven
    files on the live box did that 58 times, one of them for five days.
    """

    def setUp(self):
        super().setUp()
        conn = main.db()
        conn.execute("DELETE FROM replacements")
        conn.commit()

    def waiting_on(self, path, identity="sample"):
        """A replacements row for `path`, stamped with what is on disk now."""
        conn = main.db()
        mark = main.identity_mark(main.file_identity(path)) if identity == "sample" else identity
        conn.execute(
            "INSERT OR REPLACE INTO replacements (path, arr_id, arr_name, kind, item_id, "
            "episode_id, release, at, note, bad_identity) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (path, "a1", "Sonarr", "sonarr", 1, 2, "Some.Release-GRP", time.time(), "blocklisted", mark))
        conn.commit()

    def test_the_unreadable_file_is_not_queued_again(self):
        source = self.write(".Bad.mkv", "unreadable")
        self.waiting_on(source)
        self.assertIsNone(main.enqueue(source, "transcode"))
        self.assertEqual(main.db().execute(
            "SELECT COUNT(*) FROM jobs WHERE path=?", (source,)).fetchone()[0], 0)

    def test_pressing_check_for_files_does_not_override_it(self):
        # force exists to skip the retry COOLDOWN, which is about timing. This
        # is about a file already known to be unreadable, and asking sooner
        # cannot change that answer.
        source = self.write(".Bad.mkv", "unreadable")
        self.waiting_on(source)
        self.assertIsNone(main.enqueue(source, "transcode", force=True))

    def test_the_replacement_arriving_clears_the_wait_and_converts_it(self):
        source = self.write(".Bad.mkv", "unreadable")
        self.waiting_on(source)
        # A different file at the same path is the only signal that means the
        # replacement landed. Not mtime: an arr import preserves the release's.
        os.unlink(source)
        source = self.write(".Bad.mkv", "a whole different download entirely")
        job = main.enqueue(source, "transcode")
        self.assertIsNotNone(job, "the replacement was refused as though it were the bad file")
        self.assertEqual(main.db().execute(
            "SELECT COUNT(*) FROM replacements WHERE path=?", (source,)).fetchone()[0], 0,
            "the wait outlived the replacement it was waiting for")

    def test_a_row_from_before_the_column_existed_still_blocks_and_is_stamped(self):
        source = self.write(".Bad.mkv", "unreadable")
        self.waiting_on(source, identity=None)
        self.assertIsNone(main.enqueue(source, "transcode"))
        self.assertEqual(
            main.db().execute("SELECT bad_identity FROM replacements WHERE path=?", (source,)).fetchone()[0],
            main.identity_mark(main.file_identity(source)))


    def test_an_arrived_replacement_does_not_serve_out_its_predecessors_cooldown(self):
        """Live on 2026-09-08, Malcolm in the Middle S07E05.

        The bad file failed at 18:52. Sonarr imported the replacement at 00:10,
        TranscodeArr saw it at 00:17 and logged "the replacement arrived,
        converting it" - and then declined to queue it, because the path was
        32 minutes short of the six-hour retry cooldown the OLD file had earned.
        A replacement inherits the path of the file it replaces; it has never
        failed at anything.
        """
        source = self.write(".Bad.mkv", "unreadable")
        self.waiting_on(source)
        # A job for this path that failed moments ago: cooldown fully in force.
        conn = main.db()
        conn.execute(
            "INSERT INTO jobs (id, path, state, kind, created, finished, error) VALUES (?,?,?,?,?,?,?)",
            ("older", source, "failed", "transcode", time.time() - 60, time.time() - 60,
             "output failed verification: duration mismatch"))
        conn.commit()

        # Still the bad file: refused, and the cooldown is not even reached.
        self.assertIsNone(main.enqueue(source, "transcode"))

        # The replacement lands at the same path.
        os.unlink(source)
        source = self.write(".Bad.mkv", "a completely different download")
        job = main.enqueue(source, "transcode")
        self.assertIsNotNone(job, "the replacement was held by the cooldown its predecessor earned")
        self.assertEqual(job["state"], "queued")

    def test_a_file_that_merely_failed_still_serves_its_cooldown(self):
        # The other half: without a replacement having arrived, the cooldown is
        # exactly what stops the watcher re-running a permanently failing file.
        source = self.write(".JustBad.mkv", "unreadable")
        conn = main.db()
        conn.execute(
            "INSERT INTO jobs (id, path, state, kind, created, finished, error) VALUES (?,?,?,?,?,?,?)",
            ("recent", source, "failed", "transcode", time.time() - 60, time.time() - 60, "boom"))
        conn.commit()
        self.assertIsNone(main.enqueue(source, "transcode"),
                          "the retry cooldown stopped working")

    def test_the_state_is_reported_as_arrived_exactly_once(self):
        # It deletes the row on the way past, so the second look is "none" - and
        # "none" must not re-open the cooldown for a job already queued.
        source = self.write(".Once.mkv", "unreadable")
        self.waiting_on(source)
        os.unlink(source)
        source = self.write(".Once.mkv", "the replacement")
        conn = main.db()
        self.assertEqual(main.replacement_state(conn, source), "arrived")
        self.assertEqual(main.replacement_state(conn, source), "none")

    def test_a_file_nobody_is_waiting_on_is_untouched(self):
        source = self.write(".Fine.mkv", "convert me")
        self.assertIsNotNone(main.enqueue(source, "transcode"))

if __name__ == "__main__":
    unittest.main()
