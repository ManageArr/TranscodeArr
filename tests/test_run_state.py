"""The claim gate, the throttle and the webhook - the daemon half of 1.0.

The rule these all exist around: nothing here may ever reach a job that is
already encoding. A window that closes drains, a Stop drains, and a webhook that
cannot be delivered is a log line and not a failed conversion.
"""

import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

# main reads CONFIG_DIR at import time to place its database.
_TMP = tempfile.mkdtemp(prefix="transcodearr-run-")
os.environ.setdefault("CONFIG_DIR", _TMP)
os.environ.setdefault("TRASH_DIR", os.path.join(_TMP, "trash"))

import core  # noqa: E402
import main  # noqa: E402
import store  # noqa: E402


class LoopStop(BaseException):
    """Ends worker_loop at the sleep it would take next.

    Not an Exception on purpose: worker_loop catches Exception so that one lost
    database race cannot kill the only worker thread, and a test sentinel it
    swallowed would spin the loop forever instead of failing.
    """


class _StopAtSleep:
    """main's `time`, with a sleep that ends the loop after one pass."""

    def __getattr__(self, name):
        return getattr(time, name)

    def sleep(self, _seconds):
        raise LoopStop


class RunStateCase(unittest.TestCase):
    def setUp(self):
        main.init_db()
        conn = main.db()
        conn.execute("DELETE FROM jobs")
        conn.execute("DELETE FROM settings")
        conn.commit()
        self.addCleanup(setattr, main, "RUN_STATE", main.RUN_STATE)
        self.addCleanup(setattr, main, "_announced", main._announced)

    def at(self, minutes):
        """Pin the container's local clock to one minute of the day."""
        p = mock.patch.object(main, "local_clock", lambda: (minutes, "%02d:%02d TEST" % divmod(minutes, 60)))
        p.start()
        self.addCleanup(p.stop)

    def worker_pass(self):
        """Run the real worker_loop until it would sleep. Returns what it claimed.

        The loop itself rather than a copy of its query, because the gate is a
        line inside it and a test that re-implemented the claim would keep
        passing after somebody moved that line.
        """
        claimed = []
        with mock.patch.object(main, "time", _StopAtSleep()), \
                mock.patch.object(main, "process", claimed.append):
            with self.assertRaises(LoopStop):
                main.worker_loop()
        return claimed

    def state(self, job_id):
        return main.db().execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()[0]


class TheGate(RunStateCase):
    def test_a_manual_stop_refuses_new_work_and_says_so(self):
        allowed, why = main.set_run_state(False)
        self.assertFalse(allowed)
        self.assertIn("Start", why)
        # The one thing an operator must not have to guess at: a growing queue
        # on a stopped box is the design, not a fault.
        self.assertIn("queueing", why)
        self.assertTrue(main.set_run_state(True)[0])

    def test_a_window_that_spans_midnight_is_open_across_midnight(self):
        store.save_settings(main.db(), {"convert_window": "22:00-06:00"})
        for minute, expected in ((22 * 60, True), (23 * 60 + 59, True), (0, True),
                                 (5 * 60 + 59, True), (6 * 60, False), (14 * 60, False)):
            self.at(minute)
            self.assertEqual(main.may_claim()[0], expected, minute)

    def test_a_shut_window_counts_down_and_shows_the_local_time(self):
        store.save_settings(main.db(), {"convert_window": "22:00-06:00"})
        self.at(18 * 60 + 30)
        allowed, why = main.may_claim()
        self.assertFalse(allowed)
        self.assertIn("22:00-06:00", why)
        self.assertIn("3h 30m", why)
        # The zone travels with the time or the four-hour error stays invisible.
        self.assertIn("18:30 TEST", why)

    def test_a_stop_beats_an_open_window(self):
        store.save_settings(main.db(), {"convert_window": "22:00-06:00"})
        self.at(23 * 60)
        main.set_run_state(False)
        self.assertFalse(main.may_claim()[0])

    def test_no_window_means_always(self):
        self.at(3 * 60)
        self.assertEqual(main.may_claim(), (True, "converting"))

    def test_a_transition_is_logged_once_and_not_once_per_loop(self):
        store.save_settings(main.db(), {"convert_window": "22:00-06:00"})
        self.at(18 * 60)
        with self.assertLogs(main.log, level="INFO") as logged:
            for _ in range(5):
                main.may_claim()
            self.at(22 * 60)
            for _ in range(5):
                main.may_claim()
        # Two events, ten calls. A line per call is what buries the events.
        self.assertEqual(len(logged.output), 2, logged.output)
        self.assertIn("outside the convert window", logged.output[0])
        self.assertIn("converting", logged.output[1])


class RecordingProc:
    """Enough of Popen to record the call that pausing must never make."""

    def __init__(self):
        self.signals = []

    def terminate(self):
        self.signals.append("terminate")

    def kill(self):
        self.signals.append("kill")


class Drain(RunStateCase):
    """Pausing gates the CLAIM. A 40GB remux at 90% is never thrown away.

    This is the class that exists to fail when somebody "optimizes" Stop into
    stopping: terminating the encode wastes hours of work, leaves a .part file
    for the boot sweep to clean up, and buys nothing on a box that will be
    started again in the morning. The same is true of a window closing, which is
    the one that would happen unattended at 06:00 every day.
    """

    def setUp(self):
        super().setUp()
        with main._jobs_lock:
            main._running.clear()
        self.addCleanup(main._running.clear)

    def encoding(self, job_id="in-flight"):
        """A job in exactly the state worker_loop leaves it in while ffmpeg runs."""
        conn = main.db()
        conn.execute("INSERT INTO jobs (id, path, state, kind, created, started) VALUES (?,?,?,?,?,?)",
                     (job_id, "/media/Movies/.Remux.mkv", "running", "transcode", time.time(), time.time()))
        conn.commit()
        proc = RecordingProc()
        with main._jobs_lock:
            main._running[job_id] = {"cancel": False, "proc": proc}
        return job_id, proc

    def queued(self, job_id="waiting"):
        conn = main.db()
        conn.execute("INSERT INTO jobs (id, path, state, kind, created) VALUES (?,?,?,?,?)",
                     (job_id, "/media/Movies/.Next.mkv", "queued", "transcode", time.time()))
        conn.commit()
        return job_id

    def test_a_manual_stop_never_signals_the_encode_it_finds_running(self):
        job_id, proc = self.encoding()
        allowed, _why = main.set_run_state(False)
        self.assertFalse(allowed)
        self.assertEqual(proc.signals, [], "pausing killed an encode that was already running")
        # The cancel flag is what run_encode reads to decide the ladder is over
        # and the attempt was deliberate. Setting it here would fail the job.
        self.assertFalse(main._running[job_id]["cancel"])
        self.assertEqual(self.state(job_id), "running")

    def test_a_window_closing_mid_encode_does_the_same_nothing(self):
        # 06:00 arrives with nobody watching, which is exactly why this one has
        # to drain rather than stop.
        store.save_settings(main.db(), {"convert_window": "22:00-06:00"})
        job_id, proc = self.encoding()
        self.at(6 * 60)
        self.assertFalse(main.may_claim()[0])
        self.assertEqual(proc.signals, [])
        self.assertEqual(self.state(job_id), "running")

    def test_the_gate_refuses_a_claim_rather_than_reaching_for_cancel(self):
        # The straight-line way to break this is to wire Stop to cancel_running,
        # which is right there and does terminate the process.
        def never(job_id):
            raise AssertionError("pausing canceled a job that was already encoding")

        job_id, _proc = self.encoding()
        with mock.patch.object(main, "cancel_running", never):
            main.set_run_state(False)
            self.assertEqual(self.worker_pass(), [])
        self.assertEqual(self.state(job_id), "running")

    def test_a_paused_worker_claims_nothing_and_the_queue_waits_intact(self):
        job_id = self.queued()
        main.set_run_state(False)
        self.assertEqual(self.worker_pass(), [], "a paused worker took a job")
        self.assertEqual(self.state(job_id), "queued")
        self.assertEqual(main._running, {})
        # Waiting, not lost: the same worker takes it the moment somebody starts.
        main.set_run_state(True)
        claimed = self.worker_pass()
        self.assertEqual([j["id"] for j in claimed], [job_id])
        self.assertEqual(self.state(job_id), "running")

    def test_a_shut_window_holds_the_claim_and_the_open_one_releases_it(self):
        store.save_settings(main.db(), {"convert_window": "22:00-06:00"})
        job_id = self.queued()
        self.at(12 * 60)
        self.assertEqual(self.worker_pass(), [])
        self.assertEqual(self.state(job_id), "queued")
        self.at(23 * 60)
        self.assertEqual([j["id"] for j in self.worker_pass()], [job_id])


class WatcherKeepsQueueing(RunStateCase):
    """A stopped box with a climbing queue is this working.

    It is also the single most alarming thing this feature does, so it gets a
    test that fails if the watcher is ever put behind the same switch as the
    worker. Queueing costs one SQLite row, and doing it while the window is shut
    is what makes the queue ready the minute the window opens instead of a whole
    scan interval later.
    """

    def setUp(self):
        super().setUp()
        conn = main.db()
        conn.execute("DELETE FROM seen")
        conn.commit()
        self.media = tempfile.mkdtemp(prefix="watch-", dir=_TMP)
        os.makedirs(os.path.join(self.media, "Movies"))
        self.addCleanup(setattr, main, "MEDIA_ROOTS", main.MEDIA_ROOTS)
        main.MEDIA_ROOTS = [self.media]

    def settled_file(self, name):
        """A real file the watcher has already seen at this size, long enough ago."""
        path = os.path.join(self.media, "Movies", name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("a film")
        conn = main.db()
        conn.execute("INSERT OR REPLACE INTO seen (path, size, at) VALUES (?,?,?)",
                     (path, os.path.getsize(path), time.time() - 3600))
        conn.commit()
        return path

    def queued_for(self, path):
        return main.db().execute("SELECT state FROM jobs WHERE path=?", (path,)).fetchone()

    def test_the_queue_is_still_built_while_the_box_is_stopped(self):
        path = self.settled_file(".Movie (2026).mkv")
        main.set_run_state(False)
        main.scan_once()
        row = self.queued_for(path)
        self.assertIsNotNone(row, "the watcher stopped queueing while the box was paused")
        self.assertEqual(row["state"], "queued")
        # Both halves of the sentence in the UI: queued, and not being worked on.
        self.assertFalse(main.may_claim()[0])

    def test_a_shut_window_does_not_stop_the_watcher_either(self):
        store.save_settings(main.db(), {"convert_window": "22:00-06:00"})
        self.at(12 * 60)
        path = self.settled_file(".Another (2026).mkv")
        main.scan_once()
        self.assertIsNotNone(self.queued_for(path), "the watcher stopped queueing outside the window")

    def test_a_normal_library_converts_out_of_the_box(self):
        """The stranger's first boot: ordinary visible files, and it just works.

        This is the default the 1.0 rename bought. Before it, hidden_only's
        predecessor defaulted the other way, so a stranger who pointed the
        container at a normal library got a healthy container that scanned
        forever and converted nothing, with no error anywhere saying why.
        """
        visible = self.settled_file("Movie (2026).mkv")
        main.scan_once()
        self.assertIsNotNone(self.queued_for(visible),
                             "a visible file must be eligible by default")

    def test_hidden_only_says_why_it_skipped_instead_of_scanning_in_silence(self):
        """The other mode, and the silence it must not fall back into.

        Turning hidden_only on when nothing writes the dot is the one way back
        into "converts nothing and never says why", so the count is what lets
        the page explain an empty queue.
        """
        store.save_settings(main.db(), {"hidden_only": True})
        self.addCleanup(setattr, main, "VISIBLE_ONLY_SKIPPED", main.VISIBLE_ONLY_SKIPPED)
        main.VISIBLE_ONLY_SKIPPED = 0
        visible = self.settled_file("Movie (2026).mkv")
        self.settled_file("poster.jpg")  # not a candidate, so not counted
        main.scan_once()
        self.assertIsNone(self.queued_for(visible), "hidden_only must exclude a visible file")
        self.assertEqual(main.VISIBLE_ONLY_SKIPPED, 1)
        self.assertEqual(main._run_state_view()["visible_only_skipped"], 1)

        # One hidden file means the dot convention IS in use here, so the same
        # visible files are the filter doing its job rather than a dead install.
        self.settled_file(".Another (2026).mkv")
        main.scan_once()
        self.assertEqual(main.VISIBLE_ONLY_SKIPPED, 0)


class Throttle(RunStateCase):
    def test_the_throttle_wraps_the_command_that_gets_recorded(self):
        store.save_settings(main.db(), {"encode_nice": 10, "encode_idle_io": True, "encode_threads": 3})
        job_id = str(uuid.uuid4())
        conn = main.db()
        conn.execute("INSERT INTO jobs (id, path, state, kind, created) VALUES (?,?,?,?,?)",
                     (job_id, "/media/x.mkv", "running", "transcode", time.time()))
        conn.commit()
        with main._jobs_lock:
            main._running[job_id] = {"cancel": False, "proc": None}
        self.addCleanup(main._running.clear)
        names = core.plan_names("/media/Movies/.Movie.mkv")
        seen = []

        class Proc:
            returncode = 0
            stdout = iter(())
            stderr = iter(())

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        def fake_popen(args, **_kwargs):
            seen.append(args)
            return Proc()

        with mock.patch.object(main, "encoding_profile",
                               lambda: {"encoder": "libx265", "quality": 24, "preset": "", "profile": "",
                                        "max_height": 0, "audio_codec": "aac", "audio_bitrate": 192,
                                        "audio_channels": 2}), \
                mock.patch.object(main.subprocess, "Popen", fake_popen):
            ok, _warning, _error = main.run_encode(job_id, names.source, names, core.Probe(60.0, 1, 1, 0))
        self.assertTrue(ok)
        self.assertEqual(seen[0][:7], ["ionice", "-t", "-c", "3", "nice", "-n", "10"])
        self.assertEqual(seen[0][7], "ffmpeg")
        self.assertIn("-threads", seen[0])
        # log_tail's whole job is to say what actually ran.
        recorded = conn.execute("SELECT log_tail FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        self.assertEqual(recorded, " ".join(seen[0]))


class Webhook(RunStateCase):
    def job(self, state):
        job_id = str(uuid.uuid4())
        conn = main.db()
        conn.execute("INSERT INTO jobs (id, path, state, kind, created, output) VALUES (?,?,?,?,?,?)",
                     (job_id, "/media/Movies/Film.mkv", state, "transcode", time.time(),
                      "/media/Movies/Film.mp4"))
        conn.commit()
        return job_id

    def fire(self, state, wait=5.0):
        sent, delivered = [], threading.Event()

        def record(url, secret, payload, job_id):
            sent.append((url, secret, payload, job_id))
            delivered.set()

        job_id = self.job(state)
        with mock.patch.object(main, "_post_webhook", record):
            main.webhook_after(job_id)
            delivered.wait(wait)
        return sent

    def test_nothing_is_sent_when_no_url_is_configured(self):
        # A short wait, because webhook_after returns before it starts a thread
        # when there is no URL - waiting the full timeout for a delivery that is
        # never coming put five seconds into a suite that runs in one.
        self.assertEqual(self.fire("done", wait=0.2), [])

    def test_the_payload_is_the_public_job_shape(self):
        store.save_settings(main.db(), {"webhook_url": "https://example.invalid/hook"})
        [(url, _secret, payload, _job_id)] = self.fire("failed")
        self.assertEqual(url, "https://example.invalid/hook")
        self.assertEqual(payload["event"], "job.failed")
        self.assertEqual(payload["version"], main.VERSION)
        self.assertEqual(payload["job"]["output"], "/media/Movies/Film.mp4")
        # log_tail holds the ffmpeg argv and absolute container paths.
        self.assertNotIn("log_tail", payload["job"])

    def test_a_receiver_that_explodes_never_reaches_the_job(self):
        store.save_settings(main.db(), {"webhook_url": "https://example.invalid/hook"})
        job_id = self.job("done")
        with mock.patch.object(main, "cfg", side_effect=RuntimeError("boom")):
            main.webhook_after(job_id)  # must not raise
        self.assertEqual(main.db().execute(
            "SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()[0], "done")

    def test_the_signature_covers_the_bytes_that_were_sent(self):
        import hashlib
        import hmac
        posted = []

        class FakeOpener:
            def open(self, req, timeout=None):
                posted.append(req)
                return mock.MagicMock(__enter__=lambda s: s, __exit__=lambda *a: False)

        with mock.patch.object(main.arr_client, "_opener", FakeOpener()), \
                mock.patch.object(main.arr_client, "blocked_reason", lambda url: None):
            main._post_webhook("https://example.invalid/hook", "s3cret", {"event": "job.done"}, "abcdef12")
        [req] = posted
        expected = hmac.new(b"s3cret", req.data, hashlib.sha256).hexdigest()
        self.assertEqual(req.get_header("X-transcodearr-signature"), f"sha256={expected}")

    def test_an_unreachable_receiver_is_a_log_line_and_never_a_failed_job(self):
        # The real failure, not a mocked one: a receiver that has moved, or a
        # container that is down. The media is already correct on disk by the
        # time this runs, so nothing here may undo that or raise into the worker.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = probe.getsockname()[1]
        url = "http://127.0.0.1:%d/hook" % dead_port
        store.save_settings(main.db(), {"webhook_url": url})
        job_id = self.job("done")
        with self.assertLogs(main.log, level="WARNING") as logged:
            main._post_webhook(url, "s3cret", {"event": "job.done"}, job_id)  # must not raise
        self.assertIn("failed", logged.output[0])
        # And the job it was announcing is exactly as it was: this is the call
        # webhook_after hands to a daemon thread, so a refusal here is the whole
        # blast radius of a receiver that has moved.
        self.assertEqual(main.db().execute(
            "SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()[0], "done")

    def test_a_link_local_receiver_is_refused(self):
        posted = []

        class FakeOpener:
            def open(self, req, timeout=None):
                posted.append(req)

        with mock.patch.object(main.arr_client, "_opener", FakeOpener()):
            main._post_webhook("http://169.254.169.254/latest/meta-data", "", {}, "abcdef12")
        self.assertEqual(posted, [])


if __name__ == "__main__":
    unittest.main()
