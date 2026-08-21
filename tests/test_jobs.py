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
        def fake(job_id, source, names, src_probe):
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

    def attempt(self, plan, subtitle_streams=1, cancelled=False):
        job_id = str(uuid.uuid4())
        names = core.plan_names(os.path.join(self.media, "Movies", ".Movie.mkv"))
        fake = FakeFfmpeg(plan)
        with main._jobs_lock:
            main._running[job_id] = {"cancel": cancelled, "proc": None}
        self.addCleanup(main._running.clear)
        probe = core.Probe(3600.0, 1, 1, subtitle_streams)
        with mock.patch.object(main.subprocess, "Popen", fake):
            return fake, main.run_encode(job_id, names.source, names, probe)

    def test_a_subtitle_failure_retries_without_subtitles(self):
        fake, (ok, warning, error) = self.attempt([(1, "Error: mov_text encoder not found"), (0, "")])
        self.assertTrue(ok, error)
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("mov_text", fake.calls[0])
        self.assertIn("-sn", fake.calls[1])
        self.assertNotIn("mov_text", fake.calls[1])
        # A dropped subtitle track is recorded, never silently lost.
        self.assertIn("subtitle", warning)

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


if __name__ == "__main__":
    unittest.main()
