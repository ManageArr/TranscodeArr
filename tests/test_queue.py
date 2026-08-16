"""The queue view must list work in the order the worker will actually do it.

Worth its own test because the two orderings live in different functions: the
worker takes `ORDER BY created` and the job history shows `ORDER BY created
DESC`. Showing the history's order on the queue page would be a page that is
confidently, exactly backwards.
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

# main reads CONFIG_DIR at import time to place its database.
_TMP = tempfile.mkdtemp(prefix="transcodearr-test-")
os.environ["CONFIG_DIR"] = _TMP
os.environ["TRASH_DIR"] = os.path.join(_TMP, "trash")

import main  # noqa: E402


class QueueOrder(unittest.TestCase):
    def setUp(self):
        main.init_db()
        conn = main.db()
        conn.execute("DELETE FROM jobs")
        conn.commit()

    def add(self, name, created, state="queued", kind="transcode", **extra):
        conn = main.db()
        cols = {"id": name, "path": f"/media/TV/{name}.mkv", "state": state, "kind": kind, "created": created, **extra}
        conn.execute(
            f"INSERT INTO jobs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            tuple(cols.values()),
        )
        conn.commit()

    def test_queued_work_is_listed_oldest_first(self):
        self.add("third", 300)
        self.add("first", 100)
        self.add("second", 200)
        view = main._queue_view(50)
        self.assertEqual([j["id"] for j in view["queued"]], ["first", "second", "third"])

    def test_the_first_row_is_the_one_the_worker_takes_next(self):
        # The contract, asserted against the worker's own query rather than a
        # copy of it - so the two cannot drift apart silently.
        self.add("later", 500)
        self.add("sooner", 400)
        view = main._queue_view(50)
        worker_pick = main.db().execute(
            "SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1"
        ).fetchone()
        self.assertEqual(view["queued"][0]["id"], worker_pick["id"])

    def test_running_work_is_separated_from_waiting_work(self):
        self.add("busy", 100, state="running", started=100.0)
        self.add("waiting", 200)
        view = main._queue_view(50)
        self.assertEqual([j["id"] for j in view["running"]], ["busy"])
        self.assertEqual([j["id"] for j in view["queued"]], ["waiting"])

    def test_finished_and_cancelled_work_never_appears_as_pending(self):
        for state in ("done", "failed", "cancelled"):
            self.add(state, 100, state=state)
        view = main._queue_view(50)
        self.assertEqual(view["queued"], [])
        self.assertEqual(view["queued_total"], 0)

    def test_the_total_counts_everything_waiting_not_just_the_page(self):
        for i in range(30):
            self.add(f"j{i}", i)
        view = main._queue_view(10)
        self.assertEqual(len(view["queued"]), 10)
        self.assertEqual(view["queued_total"], 30)


class Throughput(unittest.TestCase):
    def setUp(self):
        main.init_db()
        conn = main.db()
        conn.execute("DELETE FROM jobs")
        conn.commit()

    def add_done(self, name, seconds, kind="transcode"):
        QueueOrder.add(self, name, 0, state="done", kind=kind, started=1000.0, finished=1000.0 + seconds)

    def test_an_estimate_needs_real_completions(self):
        self.assertIsNone(main._queue_view(10)["seconds_per_job"])

    def test_reveals_are_excluded_from_the_average(self):
        # A reveal is a rename that finishes in milliseconds. Averaging those in
        # would promise a queue drains in minutes when it takes days.
        self.add_done("rename", 0.05, kind="reveal")
        self.add_done("encode", 300)
        self.assertEqual(main._queue_view(10)["seconds_per_job"], 300)

    def test_the_estimate_covers_everything_waiting(self):
        self.add_done("encode", 100)
        for i in range(5):
            QueueOrder.add(self, f"q{i}", i)
        view = main._queue_view(10)
        self.assertEqual(view["eta_seconds"], 500)


class Priority(unittest.TestCase):
    """Files that need no conversion should not wait days behind ones that do."""

    def setUp(self):
        main.init_db()
        conn = main.db()
        conn.execute("DELETE FROM jobs")
        conn.commit()

    def test_a_reveal_jumps_ahead_of_older_transcodes(self):
        QueueOrder.add(self, "old-transcode", 100, kind="transcode", priority=0)
        QueueOrder.add(self, "older-transcode", 50, kind="transcode", priority=0)
        QueueOrder.add(self, "new-reveal", 999, kind="reveal", priority=main.REVEAL_PRIORITY)
        view = main._queue_view(10)
        self.assertEqual(view["queued"][0]["id"], "new-reveal")

    def test_equal_priority_still_runs_oldest_first(self):
        QueueOrder.add(self, "second", 200)
        QueueOrder.add(self, "first", 100)
        self.assertEqual([j["id"] for j in main._queue_view(10)["queued"]], ["first", "second"])

    def test_reveals_among_themselves_stay_in_order(self):
        for i, name in enumerate(["r1", "r2", "r3"]):
            QueueOrder.add(self, name, 100 + i, kind="reveal", priority=main.REVEAL_PRIORITY)
        self.assertEqual([j["id"] for j in main._queue_view(10)["queued"]], ["r1", "r2", "r3"])

    def test_the_view_agrees_with_what_the_worker_takes(self):
        QueueOrder.add(self, "transcode", 1, kind="transcode", priority=0)
        QueueOrder.add(self, "reveal", 2, kind="reveal", priority=main.REVEAL_PRIORITY)
        pick = main.db().execute(
            "SELECT * FROM jobs WHERE state='queued' ORDER BY priority, created LIMIT 1").fetchone()
        self.assertEqual(main._queue_view(10)["queued"][0]["id"], pick["id"])

    def test_the_estimate_ignores_reveals_it_will_not_spend_time_on(self):
        QueueOrder.add(self, "done1", 0, state="done", kind="transcode", started=0.0, finished=100.0)
        QueueOrder.add(self, "t1", 1, kind="transcode")
        QueueOrder.add(self, "t2", 2, kind="transcode")
        for i in range(20):
            QueueOrder.add(self, f"rev{i}", 10 + i, kind="reveal", priority=main.REVEAL_PRIORITY)
        view = main._queue_view(50)
        self.assertEqual(view["queued_total"], 22)
        self.assertEqual(view["eta_seconds"], 200)  # two transcodes, not twenty-two jobs


class Concurrency(unittest.TestCase):
    def test_the_limit_is_bounded_to_something_a_disk_can_survive(self):
        spec = main.store.SPEC_BY_KEY["max_concurrent"]
        self.assertEqual(main.store.parse_value(spec, "3"), 3)
        for bad in ("0", "9", "-1"):
            with self.assertRaises(ValueError):
                main.store.parse_value(spec, bad)

    def test_canceling_a_job_that_is_not_running_says_so(self):
        self.assertFalse(main.cancel_running("nope"))

    def test_cancel_targets_one_job_not_whichever_happens_to_be_running(self):
        # With a single global flag, canceling job A stopped whatever was
        # running - which with more than one worker is usually job B.
        with main._jobs_lock:
            main._running.clear()
            main._running["a"] = {"cancel": False, "proc": None}
            main._running["b"] = {"cancel": False, "proc": None}
        self.assertTrue(main.cancel_running("a"))
        self.assertTrue(main._cancelled("a"))
        self.assertFalse(main._cancelled("b"))
        with main._jobs_lock:
            main._running.clear()


class Trash(unittest.TestCase):
    """The promise the whole project rests on: a replaced source outlives its
    replacement. It did not - and no test covered trash() at all."""

    def setUp(self):
        main.init_db()
        self.media = os.path.join(_TMP, "media")
        os.makedirs(os.path.join(self.media, "TV"), exist_ok=True)
        main.MEDIA_ROOTS = [self.media]

    def make_old_file(self, name, years=12):
        path = os.path.join(self.media, "TV", name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("x")
        old = time.time() - years * 365 * 86400
        os.utime(path, (old, old))
        return path

    def test_a_trashed_source_is_stamped_with_when_it_was_trashed(self):
        # shutil.move preserves mtime, and imported media carries the release's
        # own timestamp - a median of twelve years old. Pruning on that date
        # deleted every source minutes after the job said "source preserved".
        dest = main.trash(self.make_old_file("Old Release.mkv"))
        self.assertTrue(os.path.exists(dest))
        self.assertLess(time.time() - os.path.getmtime(dest), 60,
                        "trashed file still carries the release's own timestamp")

    def test_prune_keeps_a_source_trashed_inside_the_retention_window(self):
        dest = main.trash(self.make_old_file("Keep Me.mkv"))
        main.store.save_settings(main.db(), {"trash_keep_days": 7})
        main.prune_trash()
        self.assertTrue(os.path.exists(dest), "a source was deleted inside its retention window")

    def test_prune_still_removes_what_is_genuinely_old(self):
        # The pruning has to keep working, or the trash eats the disk.
        dest = main.trash(self.make_old_file("Expired.mkv"))
        long_ago = time.time() - 30 * 86400
        os.utime(dest, (long_ago, long_ago))
        main.store.save_settings(main.db(), {"trash_keep_days": 7})
        main.prune_trash()
        self.assertFalse(os.path.exists(dest))


if __name__ == "__main__":
    unittest.main()
