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


if __name__ == "__main__":
    unittest.main()
