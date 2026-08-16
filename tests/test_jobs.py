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

    def test_a_file_already_at_the_visible_name_stops_the_job(self):
        source = self.write(".Movie (2026).mkv", "the source")
        target = self.write("Movie (2026).mp4", "somebody else's copy")
        row = self.run_job(self.claim(source))
        self.assertEqual(row["state"], "failed")
        self.assertIn("already exists", row["error"])
        self.assertEqual(self.read(target), "somebody else's copy")
        self.assertEqual(self.read(source), "the source")

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
        self.assertIn("already exists", row["error"])
        self.assertEqual(self.read(visible), "an import that landed mid-job")
        self.assertEqual(self.read(source), "the hidden copy")


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
