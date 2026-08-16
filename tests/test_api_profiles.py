"""The profile routes over real HTTP, in the shape the bundled UI reads them.

The rest of the suite tests store.py's rules directly. This one exists because
the UI and the daemon are written apart and can drift apart: a renamed route or
a field the browser reads and the server stopped sending breaks the Encoding tab
silently, and CI's only other guard is "every module imports cleanly". Every
assertion here is something app/web.py actually reads.

ffmpeg is stubbed - validate_profile is tested by the encoder itself, and a real
test encode per case would put minutes into a suite that runs in a second.
"""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

# main reads CONFIG_DIR at import time to place its database.
_TMP = tempfile.mkdtemp(prefix="transcodearr-api-")
os.environ["CONFIG_DIR"] = _TMP
os.environ.setdefault("TRANSCODEARR_TOKEN", "test-token")

import core  # noqa: E402
import main  # noqa: E402
import store  # noqa: E402

TOKEN = "test-token"
# Which encoders the stubbed test encode says yes to. h264_qsv is the failure
# every case needs: a profile that exists, is shipped, and cannot run here.
WORKS = {e: e != "h264_qsv" for e in core.ENCODER_ORDER}


class ProfileApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        main.init_db()
        cls._real_validate = main.validate_profile
        main.validate_profile = lambda fields: (WORKS.get(fields["encoder"], False), "stub verdict")
        main.TOKEN = TOKEN
        main.ENCODER = "h264_nvenc"
        main.ENCODER_PROBES = [
            {"name": e, "available": WORKS[e], "reason": "stub",
             "codec": core.ENCODER_INFO[e]["codec"], "hardware": core.ENCODER_INFO[e]["hardware"],
             "recommended_quality": core.ENCODER_INFO[e]["recommended"],
             "sane_range": core.ENCODER_INFO[e]["sane"], "summary": core.ENCODER_INFO[e]["summary"],
             "detail": core.ENCODER_INFO[e]["detail"]}
            for e in core.ENCODER_ORDER
        ]
        # Port 0 so a developer already running the container on 8484 does not
        # have the suite fail on a bound address.
        cls.srv = main.ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        main.validate_profile = cls._real_validate

    def setUp(self):
        conn = main.db()
        conn.execute("DELETE FROM profiles")
        conn.commit()
        store.ensure_shipped_profiles(conn)
        main.test_stored_profiles(store.list_profiles(conn))
        self.shipped = {p["encoder"]: p["id"] for p in store.list_profiles(conn)}
        store.activate_profile(conn, self.shipped["h264_nvenc"])

    def call(self, method, path, body=None):
        req = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body or {}).encode() if method in ("POST", "PUT") else None,
            headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def test_the_list_carries_every_field_the_page_renders(self):
        status, body = self.call("GET", "/api/profiles")
        self.assertEqual(status, 200)
        for key in ("profiles", "available_encoders", "resolutions", "audio_codecs", "audio_channels"):
            self.assertIn(key, body)
        p = body["profiles"][0]
        for key in ("id", "name", "encoder", "quality", "preset", "profile", "max_height",
                    "audio_codec", "audio_bitrate", "audio_channels", "active", "builtin",
                    "usable", "validated_at", "validated_ok", "validated_note", "created"):
            self.assertIn(key, p)
        # The card branches on these; a 0/1 here renders "0" somewhere.
        for key in ("active", "builtin", "usable"):
            self.assertIsInstance(p[key], bool, key)
        # The form's encoder select is built from this, so it needs the options
        # each encoder accepts, not just its name.
        for key in ("name", "codec", "hardware", "recommended_quality", "sane_range",
                    "summary", "presets", "profiles", "default_preset", "default_profile"):
            self.assertIn(key, body["available_encoders"][0])
        self.assertNotIn("h264_qsv", [e["name"] for e in body["available_encoders"]])

    def test_the_server_orders_the_list_so_the_page_does_not_have_to(self):
        # web.py splits shipped from custom and never re-sorts.
        _, body = self.call("GET", "/api/profiles")
        self.assertEqual([p["encoder"] for p in body["profiles"]], core.ENCODER_ORDER)

    def test_never_tested_and_tested_and_failed_are_different_answers(self):
        # A plain falsiness check on validated_ok collapses these two, and the
        # untested profile then looks as broken as the one ffmpeg refused.
        conn = main.db()
        conn.execute("UPDATE profiles SET validated_at=NULL, validated_ok=NULL WHERE id=?",
                     (self.shipped["libx265"],))
        conn.commit()
        _, body = self.call("GET", "/api/profiles")
        by_enc = {p["encoder"]: p for p in body["profiles"]}
        self.assertIsNone(by_enc["libx265"]["validated_ok"])
        self.assertEqual(by_enc["h264_qsv"]["validated_ok"], 0)
        self.assertEqual(by_enc["h264_nvenc"]["validated_ok"], 1)
        self.assertEqual([by_enc[e]["usable"] for e in ("libx265", "h264_qsv", "h264_nvenc")],
                         [False, False, True])

    def test_a_shipped_profile_cannot_be_edited_through_any_verb(self):
        # The UI hides the Edit button, which is not a guard - only the daemon is.
        pid = self.shipped["h264_nvenc"]
        body = {"name": "hacked", "encoder": "h264_nvenc", "quality": 45, "preset": "p1",
                "profile": "high", "max_height": 480, "audio_codec": "aac",
                "audio_bitrate": 128, "audio_channels": 2}
        for method in ("POST", "PUT"):
            status, _ = self.call(method, "/api/profiles/" + pid, body)
            self.assertEqual(status, 400, method)
        self.assertEqual(store.get_profile(main.db(), pid)["quality"],
                         core.recommended_quality("h264_nvenc"))

    def test_a_shipped_profile_cannot_be_deleted(self):
        pid = self.shipped["hevc_nvenc"]
        status, _ = self.call("DELETE", "/api/profiles/" + pid)
        self.assertEqual(status, 409)
        self.assertIsNotNone(store.get_profile(main.db(), pid))

    def test_a_profile_that_does_not_work_here_cannot_be_activated(self):
        status, body = self.call("POST", "/api/profiles/%s/activate" % self.shipped["h264_qsv"])
        # 409 not 404: the id is real, the machine is the problem, and the page
        # prints body.error straight from this.
        self.assertEqual(status, 409)
        self.assertIn("error", body)
        self.assertEqual(store.active_profile(main.db())["encoder"], "h264_nvenc")
        status, _ = self.call("POST", "/api/profiles/00000000-0000-0000-0000-000000000000/activate")
        self.assertEqual(status, 404)

    def test_testing_one_stored_profile_records_and_returns_the_verdict(self):
        status, body = self.call("POST", "/api/profiles/%s/test" % self.shipped["h264_qsv"])
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"profile", "ok", "detail"})
        self.assertIs(body["ok"], False)
        self.assertFalse(store.get_profile(main.db(), self.shipped["h264_qsv"])["usable"])
        status, _ = self.call("POST", "/api/profiles/00000000-0000-0000-0000-000000000000/test")
        self.assertEqual(status, 404)

    def test_retesting_everything_answers_with_the_whole_list(self):
        # The page re-reads /api/profiles afterwards, but the body is the API's
        # contract for anyone who does not.
        status, body = self.call("POST", "/api/profiles/retest")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["profiles"]), len(core.ENCODER_ORDER))

    def test_a_custom_profile_round_trips_and_the_active_one_is_not_deletable(self):
        body = {"name": "Mine", "encoder": "libx264", "quality": 21, "preset": "slow",
                "profile": "high", "max_height": 1080, "audio_codec": "aac",
                "audio_bitrate": 192, "audio_channels": 2}
        status, created = self.call("POST", "/api/profiles", body)
        self.assertEqual(status, 201)
        self.assertTrue(created["profile"]["usable"])
        pid = created["profile"]["id"]
        self.assertEqual(self.call("POST", "/api/profiles/%s/activate" % pid)[0], 200)
        self.assertEqual(self.call("DELETE", "/api/profiles/" + pid)[0], 409)
        self.call("POST", "/api/profiles/%s/activate" % self.shipped["h264_nvenc"])
        self.assertEqual(self.call("DELETE", "/api/profiles/" + pid)[0], 200)

    def test_the_dry_run_returns_the_command_it_would_have_run(self):
        status, body = self.call("POST", "/api/profiles/test", {
            "name": "x", "encoder": "libx264", "quality": 21, "preset": "slow", "profile": "high",
            "max_height": 0, "audio_codec": "aac", "audio_bitrate": 192, "audio_channels": 2})
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"ok", "detail", "command"})
        self.assertIn("libx264", body["command"])
        self.assertEqual(len(store.list_profiles(main.db())), len(core.ENCODER_ORDER))


if __name__ == "__main__":
    unittest.main()
