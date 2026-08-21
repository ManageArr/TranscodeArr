"""The 1.0 HTTP surface: login, run controls, TLS settings, metrics, backup.

Driven over real HTTP rather than by calling the handler, because the properties
worth protecting here are wire properties: which route answers without a token,
what a status code means, which header comes back. A test that called the
methods directly would pass while the routing table said something else.

Two rules this file exists to keep true: exactly one POST is reachable without a
bearer token, and no response anywhere carries a hash or a password.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

# main reads CONFIG_DIR at import time to place its database.
_TMP = tempfile.mkdtemp(prefix="transcodearr-auth-")
os.environ["CONFIG_DIR"] = _TMP
os.environ.setdefault("TRANSCODEARR_TOKEN", "test-token")

import main  # noqa: E402
import store  # noqa: E402

TOKEN = "test-token"
PASSWORD = "correct horse battery"


class ApiCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        main.init_db()
        main.TOKEN = TOKEN
        # Port 0 so a developer already running the container on 8484 does not
        # have the suite fail on a bound address.
        cls.srv = main.ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        conn = main.db()
        # tokens included: a minted key left behind makes /healthz report a later
        # test's tokenless container as protected, which is a real assertion in
        # this file and would fail for a reason nothing in it names.
        for table in ("admin", "sessions", "settings", "jobs", "arrs", "tokens"):
            conn.execute("DELETE FROM " + table)
        conn.commit()
        # The backoff is a process-wide dict, so one test's wrong passwords
        # would lock out the next test's correct one.
        store._login_failures.clear()
        # Both are module globals a test can move. Restored so this file leaves
        # the gate exactly where it found it - test_run_state counts log lines
        # and a leaked "already announced" state silently eats one.
        self.addCleanup(setattr, main, "RUN_STATE", main.RUN_STATE)
        self.addCleanup(setattr, main, "_announced", main._announced)
        main.RUN_STATE = "running"

    def call(self, method, path, body=None, token=TOKEN, raw_body=None):
        """(status, parsed body, headers). token=None sends no Authorization."""
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = None
        if raw_body is not None:
            data = raw_body
        elif method in ("POST", "PUT"):
            data = json.dumps(body or {}).encode()
        req = urllib.request.Request(self.base + path, method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, self._parse(r), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, self._parse(e), dict(e.headers)

    @staticmethod
    def _parse(response):
        payload = response.read()
        if (response.headers.get("Content-Type") or "").startswith("application/json"):
            return json.loads(payload or b"null")
        return payload.decode()

    def make_admin(self, username="lou", password=PASSWORD):
        store.set_admin(main.db(), username, password)

    def login(self, username="lou", password=PASSWORD):
        return self.call("POST", "/api/login", {"username": username, "password": password}, token=None)


class Login(ApiCase):
    def test_login_is_the_only_post_reachable_without_a_token(self):
        self.make_admin()
        status, body, _ = self.login()
        self.assertEqual(status, 200)
        self.assertTrue(body["token"].startswith("ts_"))
        # Every other write still needs one, including the new ones.
        for path in ("/api/control/start", "/api/control/stop", "/api/admin", "/api/restore",
                     "/api/logout", "/api/tls/selfsigned"):
            self.assertEqual(self.call("POST", path, {}, token=None)[0], 401, path)
        for path in ("/api/sessions", "/api/backup", "/api/control", "/metrics"):
            self.assertEqual(self.call("GET", path, token=None)[0], 401, path)

    def test_with_no_admin_the_form_says_so_instead_of_wrong_password(self):
        status, body, _ = self.login()
        self.assertEqual(status, 409)
        self.assertIn("POST /api/admin", body["error"])

    def test_a_wrong_password_and_a_wrong_username_answer_identically(self):
        self.make_admin()
        wrong_pw = self.login(password="not it at all")
        wrong_user = self.login(username="nobody")
        self.assertEqual(wrong_pw[0], 401)
        self.assertEqual(wrong_pw[1], wrong_user[1])

    def test_failed_logins_back_off_and_say_how_long(self):
        self.make_admin()
        for _ in range(store.LOGIN_FREE_ATTEMPTS):
            self.assertEqual(self.login(password="wrong one")[0], 401)
        status, body, headers = self.login(password="wrong one")
        self.assertEqual(status, 429)
        self.assertIn("Retry-After", headers)
        self.assertGreaterEqual(int(headers["Retry-After"]), 1)
        # The point of the backoff: the right password waits too, or a guesser
        # simply keeps guessing between the refusals.
        self.assertEqual(self.login()[0], 429)

    def test_a_session_token_works_like_an_api_key_and_logout_ends_it(self):
        self.make_admin()
        session = self.login()[1]["token"]
        self.assertEqual(self.call("GET", "/api/sessions", token=session)[0], 200)
        self.assertEqual(self.call("POST", "/api/logout", {}, token=session)[0], 200)
        self.assertEqual(self.call("GET", "/api/sessions", token=session)[0], 401)

    def test_logout_refuses_to_revoke_an_api_key(self):
        # ManageArr authenticates with a minted key; a logout button that
        # revoked it would take the integration down.
        self.make_admin()
        status, body, _ = self.call("POST", "/api/logout", {})
        self.assertEqual(status, 400)
        self.assertIn("API key", body["error"])

    def test_no_route_here_ever_returns_a_hash(self):
        self.make_admin()
        session = self.login()[1]
        bodies = [session,
                  self.call("GET", "/api/sessions")[1],
                  self.call("POST", "/api/admin", {"username": "lou", "password": "another one",
                                                   "current_password": PASSWORD})[1]]
        for body in bodies:
            text = json.dumps(body)
            self.assertNotIn("hash", text)
            self.assertNotIn(PASSWORD, text)

    def test_a_minted_key_still_works_after_every_session_event(self):
        # ManageArr holds a minted key and nothing else. Logins, an expiry, a
        # revoke, a password change and a logout all touch the same verify_token
        # it authenticates through, and any one of them taking the key down with
        # it breaks the integration with no error anywhere near the cause.
        key, _row = store.mint_token(main.db(), "ManageArr")
        self.make_admin()
        session = self.login()[1]["token"]
        conn = main.db()
        expired_raw, expired = store.create_session(conn, 1)
        conn.execute("UPDATE sessions SET expires=? WHERE id=?", (time.time() - 1, expired["id"]))
        conn.commit()
        doomed_raw, doomed = store.create_session(conn, 30)
        self.assertEqual(self.call("DELETE", "/api/sessions/" + doomed["id"])[0], 200)
        self.assertEqual(self.call("POST", "/api/admin", {"username": "lou", "password": "a new password",
                                                          "current_password": PASSWORD})[0], 200)
        self.assertEqual(self.call("POST", "/api/logout", {}, token=session)[0], 200)
        # Every one of those tokens is dead, for four different reasons...
        for dead in (session, expired_raw, doomed_raw, "ta_not_a_real_key"):
            self.assertEqual(self.call("GET", "/api/sessions", token=dead)[0], 401, dead[:11])
        # ...and the key ManageArr authenticates with is not one of them.
        self.assertEqual(self.call("GET", "/api/settings", token=key)[0], 200)
        self.assertEqual(self.call("GET", "/api/control", token=key)[0], 200)

    def test_sessions_are_listed_and_revoked_one_at_a_time(self):
        self.make_admin()
        first, second = self.login()[1]["token"], self.login()[1]["token"]
        status, body, _ = self.call("GET", "/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["sessions"]), 2)
        self.assertEqual(body["admin"], "lou")
        victim = store.list_sessions(main.db())[0]["id"]
        self.assertEqual(self.call("DELETE", "/api/sessions/" + victim)[0], 200)
        self.assertEqual(self.call("DELETE", "/api/sessions/" + victim)[0], 404)
        # Exactly one of the two died.
        alive = [t for t in (first, second) if self.call("GET", "/api/sessions", token=t)[0] == 200]
        self.assertEqual(len(alive), 1)


class Admin(ApiCase):
    def test_the_first_admin_is_created_with_the_bootstrap_token(self):
        status, body, _ = self.call("POST", "/api/admin", {"username": "lou", "password": PASSWORD})
        self.assertEqual(status, 201)
        self.assertEqual(body, {"username": "lou"})
        self.assertTrue(store.admin_exists(main.db()))

    def test_changing_the_password_needs_the_current_one(self):
        self.make_admin()
        status, body, _ = self.call("POST", "/api/admin", {"username": "lou", "password": "a new password"})
        self.assertEqual(status, 403)
        self.assertIn("current_password", body["error"])
        self.assertTrue(store.verify_password(main.db(), "lou", PASSWORD))
        status, _, _ = self.call("POST", "/api/admin", {"username": "lou", "password": "a new password",
                                                        "current_password": PASSWORD})
        self.assertEqual(status, 200)
        self.assertTrue(store.verify_password(main.db(), "lou", "a new password"))

    def test_a_short_password_is_refused_with_the_reason(self):
        status, body, _ = self.call("POST", "/api/admin", {"username": "lou", "password": "short"})
        self.assertEqual(status, 400)
        self.assertIn(str(store.MIN_PASSWORD_LENGTH), body["error"])


class RunControls(ApiCase):
    def at(self, minutes):
        p = mock.patch.object(main, "local_clock",
                              lambda: (minutes, "%02d:%02d TEST" % divmod(minutes, 60)))
        p.start()
        self.addCleanup(p.stop)

    def test_stop_and_start_report_the_window_and_the_local_clock(self):
        store.save_settings(main.db(), {"convert_window": "22:00-06:00"})
        self.at(18 * 60 + 30)
        status, body, _ = self.call("POST", "/api/control/stop")
        self.assertEqual(status, 200)
        self.assertEqual(body["run_state"], "paused")
        self.assertFalse(body["converting"])
        self.assertIn("Start", body["reason"])
        # Stopped means nothing changes on its own, whatever the window does.
        self.assertIsNone(body["next_change_seconds"])

        status, body, _ = self.call("POST", "/api/control/start")
        self.assertEqual(body["run_state"], "running")
        # Started, but the window is shut - and the reason has to say which of
        # the two it is, with the clock beside it or a TZ mismatch stays silent.
        self.assertFalse(body["converting"])
        self.assertEqual(body["convert_window"], "22:00-06:00")
        self.assertIn("18:30 TEST", body["reason"])
        self.assertEqual(body["local_time"], "18:30 TEST")
        self.assertEqual(body["next_change_seconds"], 3 * 3600 + 1800)
        self.assertEqual(self.call("GET", "/api/control")[1]["run_state"], "running")

    def test_healthz_keeps_its_split(self):
        store.save_settings(main.db(), {"convert_window": "22:00-06:00"})
        self.make_admin()
        status, anon, _ = self.call("GET", "/healthz", token=None)
        self.assertEqual(status, 200)
        # Enough to see that work is not moving...
        self.assertEqual(anon["run_state"], "running")
        self.assertIn("converting", anon)
        self.assertTrue(anon["admin_configured"])
        self.assertTrue(anon["auth_configured"])
        # ...and nothing about how this machine is configured.
        for key in ("convert_window", "reason", "media_roots", "local_time", "timezone"):
            self.assertNotIn(key, anon)
        authed = self.call("GET", "/healthz")[1]
        for key in ("convert_window", "reason", "media_roots", "local_time", "timezone", "auto_start"):
            self.assertIn(key, authed)

    def test_a_password_with_no_token_minted_still_counts_as_protected(self):
        # An install locked with a password and no API key used to report
        # auth_configured false, which reads as "anyone can queue jobs here".
        main.TOKEN = ""
        self.addCleanup(setattr, main, "TOKEN", TOKEN)
        self.assertFalse(self.call("GET", "/healthz", token=None)[1]["auth_configured"])
        self.make_admin()
        self.assertTrue(self.call("GET", "/healthz", token=None)[1]["auth_configured"])


class TheQueueShowsWhatItIsWaitingFor(ApiCase):
    """A file asked-about is not in the queue and has no job - it would simply
    vanish from view after failing, which is the silence this worker exists to
    remove."""

    def test_the_queue_carries_the_replacements_it_is_waiting_on(self):
        conn = main.db()
        conn.execute("DELETE FROM replacements")
        conn.execute(
            "INSERT INTO replacements (path, arr_id, arr_name, kind, item_id, episode_id, release, at, note) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("/media/TV/Show/.Show - S01E01.mkv", "a1", "Sonarr", "sonarr", 126, 10984,
             "Show.S01.1080p-GRP", time.time(), "blocklisted it"))
        conn.commit()
        main._REPLACEMENTS = (0.0, [])            # the cache must not hide a new row
        self.addCleanup(setattr, main, "_REPLACEMENTS", (0.0, []))
        with mock.patch.object(main.store, "list_arrs", lambda conn, redact=True: []):
            status, body, _headers = self.call("GET", "/api/queue")
        self.assertEqual(status, 200)
        [waiting] = body["awaiting_replacement"]
        self.assertEqual(waiting["name"], ".Show - S01E01.mkv")
        self.assertEqual(waiting["release"], "Show.S01.1080p-GRP")
        # No arr reachable, so no download to report - and that is a state, not
        # an error: searching, nothing found, or already imported all look the
        # same from here.
        self.assertIsNone(waiting["download"])

    def test_a_replacement_stops_being_shown_once_something_converts(self):
        conn = main.db()
        conn.execute("DELETE FROM replacements")
        conn.execute("DELETE FROM jobs")
        path = "/media/TV/Show/.Show - S01E02.mkv"
        conn.execute(
            "INSERT INTO replacements (path, arr_id, arr_name, kind, item_id, episode_id, release, at, note) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (path, "a1", "Sonarr", "sonarr", 126, 1, "r", time.time() - 100, "n"))
        conn.execute("INSERT INTO jobs (id, path, state, kind, created) VALUES (?,?,?,?,?)",
                     ("j1", path, "done", "transcode", time.time()))
        conn.commit()
        main._REPLACEMENTS = (0.0, [])
        self.addCleanup(setattr, main, "_REPLACEMENTS", (0.0, []))
        with mock.patch.object(main.store, "list_arrs", lambda conn, redact=True: []):
            self.assertEqual(main.replacements_view(), [])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM replacements").fetchone()[0], 0)


class NothingIsCacheable(ApiCase):
    """The UI is one file that changes every release and carried no validator.

    A Trash tab shipped, deployed and verified on the running container, and
    was simply not on screen: the browser had a copy from before the update,
    nothing to revalidate it against, and no reason to ask. The API bodies
    matter for the same reason from the other side - they are a live view of a
    queue, where a cached answer is a lie with a timestamp on it.
    """

    def test_the_page_says_not_to_cache_it(self):
        _status, _body, headers = self.call("GET", "/", token=None)
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_so_does_every_api_answer(self):
        for path in ("/healthz", "/api/queue", "/api/settings", "/api/trash"):
            _status, _body, headers = self.call("GET", path)
            self.assertEqual(headers.get("Cache-Control"), "no-store", path)

    def test_a_caller_that_sets_its_own_still_wins(self):
        # The backoff sends Retry-After with its own headers dict; that path
        # must keep working, and must not end up with two Cache-Control lines.
        self.make_admin()
        for _ in range(12):
            status, _body, headers = self.call("POST", "/api/login",
                                               {"username": "lou", "password": "wrong"}, token=None)
            if status == 429:
                break
        self.assertEqual(status, 429)
        self.assertIn("Retry-After", headers)
        self.assertEqual(headers.get("Cache-Control"), "no-store")


class Metrics(ApiCase):
    def test_the_format_is_the_one_prometheus_parses(self):
        conn = main.db()
        conn.execute("INSERT INTO jobs (id, path, state, kind, created, src_bytes, out_bytes) "
                     "VALUES ('a','/media/x.mkv','done','transcode',1,1000,400)")
        conn.execute("INSERT INTO jobs (id, path, state, kind, created) "
                     "VALUES ('b','/media/y.mkv','queued','transcode',2)")
        conn.commit()
        status, body, headers = self.call("GET", "/metrics")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/plain; version=0.0.4"))
        self.assertIn("transcodearr_queue_depth 1", body)
        self.assertIn("transcodearr_saved_bytes 600", body)
        self.assertIn('transcodearr_run_state{state="running"} 1', body)
        self.assertIn('transcodearr_run_state{state="paused"} 0', body)
        # Every state present even at zero, or an alert written against one of
        # them never fires. And every series carries its HELP and TYPE.
        for state in main.JOB_STATES:
            self.assertIn('transcodearr_jobs{state="%s"} ' % state, body)
        names = {line.split()[2] for line in body.splitlines() if line.startswith("# HELP")}
        for name in names:
            self.assertIn("# TYPE %s " % name, body)
        self.assertTrue(body.endswith("\n"))

    def test_an_ugly_version_string_cannot_break_the_scrape(self):
        self.addCleanup(setattr, main, "VERSION", main.VERSION)
        main.VERSION = 'we"ird'
        line = [l for l in self.call("GET", "/metrics")[1].splitlines()
                if l.startswith("transcodearr_build_info")][0]
        self.assertIn(r'version="we\"ird"', line)


class BackupAndRestore(ApiCase):
    def seed(self):
        store.save_settings(main.db(), {"webhook_secret": "shhh", "convert_window": "01:00-05:00"})
        store.save_arr(main.db(), {"name": "R", "kind": "radarr", "base_url": "http://radarr:7878",
                                   "api_key": "arr-secret"})

    def test_the_document_names_its_file_and_carries_no_secrets(self):
        self.seed()
        status, doc, headers = self.call("GET", "/api/backup")
        self.assertEqual(status, 200)
        self.assertIn("attachment; filename=", headers["Content-Disposition"])
        self.assertIn(".json", headers["Content-Disposition"])
        self.assertEqual(doc["format"], store.BACKUP_FORMAT)
        self.assertEqual(doc["version"], main.VERSION)
        self.assertEqual(doc["settings"]["convert_window"], "01:00-05:00")
        text = json.dumps(doc)
        self.assertNotIn("shhh", text)
        self.assertNotIn("arr-secret", text)
        self.assertNotIn("api_key", text)
        self.assertNotIn("webhook_secret", text)

    def test_a_restore_says_exactly_what_it_changed(self):
        self.seed()
        doc = self.call("GET", "/api/backup")[1]
        store.save_settings(main.db(), {"convert_window": ""})
        status, body, _ = self.call("POST", "/api/restore", doc)
        self.assertEqual(status, 200)
        self.assertTrue(any("convert_window" in line for line in body["changed"]))
        self.assertEqual(main.cfg()["convert_window"], "01:00-05:00")

    def test_a_backup_from_a_newer_build_is_refused_with_its_reason(self):
        doc = self.call("GET", "/api/backup")[1]
        doc["version"] = "99.0.0"
        status, body, _ = self.call("POST", "/api/restore", doc)
        self.assertEqual(status, 400)
        self.assertIn("99.0.0", body["error"])
        self.assertEqual(self.call("POST", "/api/restore", {"nope": True})[0], 400)


class Settings(ApiCase):
    def test_a_secret_is_masked_and_flagged_for_the_form(self):
        store.save_settings(main.db(), {"webhook_secret": "shhh"})
        status, body, _ = self.call("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(body["values"]["webhook_secret"], store.SECRET_MASK)
        specs = {s["key"]: s for s in body["specs"]}
        self.assertTrue(specs["webhook_secret"]["secret"])
        self.assertFalse(specs["convert_window"]["secret"])
        # And the mask coming back from a whole-form save keeps the real value.
        self.assertEqual(self.call("PUT", "/api/settings", {"webhook_secret": store.SECRET_MASK})[0], 200)
        self.assertEqual(main.cfg()["webhook_secret"], "shhh")


class Tls(ApiCase):
    def test_no_certificate_configured_serves_plain_http_as_before(self):
        self.assertIsNone(main.tls_context())

    def test_half_a_certificate_stops_the_container_rather_than_downgrading(self):
        store.save_settings(main.db(), {"tls_cert": "/config/tls/cert.pem"})
        with self.assertRaises(SystemExit):
            main.tls_context()

    def test_a_certificate_that_cannot_be_read_stops_the_container(self):
        # The failure this refuses to have: HTTPS configured, HTTP served, and
        # the admin password crossing the network in clear text anyway.
        store.save_settings(main.db(), {"tls_cert": "/config/tls/nope.pem",
                                        "tls_key": "/config/tls/nope.key"})
        with self.assertRaises(SystemExit):
            main.tls_context()

    def test_a_generated_pair_is_usable_and_never_overwritten(self):
        status, body, _ = self.call("POST", "/api/tls/selfsigned", {"host": "nas.local", "days": 30})
        if status == 500 and "openssl" in body.get("error", ""):
            self.skipTest("no openssl on this machine - the image has one")
        self.assertEqual(status, 201)
        self.assertTrue(os.path.exists(body["cert"]) and os.path.exists(body["key"]))
        # The whole point: the pair this route writes is one the server can
        # actually serve. Fed through cfg() rather than saved, because the
        # settings validator wants a container-absolute path and this suite runs
        # on the developer's machine too.
        settings = dict(main.cfg(), tls_cert=body["cert"], tls_key=body["key"])
        with mock.patch.object(main, "cfg", lambda: settings):
            self.assertIsNotNone(main.tls_context())
        # A real certificate could just as easily be sitting at that path.
        self.assertEqual(self.call("POST", "/api/tls/selfsigned", {"host": "nas.local"})[0], 409)
        os.unlink(body["cert"])
        os.unlink(body["key"])


if __name__ == "__main__":
    unittest.main()
