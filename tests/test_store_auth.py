"""Logins, sessions, the convert window and config backup.

The two that would hurt most if they broke: a login form with no backoff is a
password oracle anyone on the LAN can run at wire speed, and a restore that
marks a profile usable would activate an encoder this machine may not have.
"""

import os
import sqlite3
import sys
import threading
import time
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import core  # noqa: E402
import store  # noqa: E402


def memdb() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(store.SCHEMA)
    return conn


class ConvertWindow(unittest.TestCase):
    def test_a_window_that_spans_midnight_is_the_normal_case(self):
        # Overnight is when a NAS is free, so start > end has to work.
        self.assertEqual(store.parse_window("22:00-06:00"), (1320, 360))
        for hour in (22, 23, 0, 5):
            self.assertTrue(store.in_window("22:00-06:00", datetime(2026, 8, 16, hour, 30)))
        for hour in (6, 12, 21):
            self.assertFalse(store.in_window("22:00-06:00", datetime(2026, 8, 16, hour, 30)))

    def test_a_plain_window_and_an_empty_one(self):
        self.assertTrue(store.in_window("01:00-06:00", datetime(2026, 8, 16, 3, 0)))
        self.assertFalse(store.in_window("01:00-06:00", datetime(2026, 8, 16, 7, 0)))
        self.assertTrue(store.in_window("", datetime(2026, 8, 16, 12, 0)))

    def test_nonsense_is_refused_with_a_readable_reason(self):
        for bad in ("2200-0600", "22:00", "24:00-06:00", "22:00-06:99", "06:00-06:00", "always"):
            with self.assertRaises(ValueError):
                store.parse_window(bad)
        with self.assertRaises(ValueError):
            store.save_settings(memdb(), {"convert_window": "22-6"})

    def test_an_unparseable_window_fails_open_rather_than_wedging_the_queue(self):
        # Unreachable through the API, but a window nobody can parse must not
        # hold the queue shut forever with no error anywhere.
        self.assertTrue(store.in_window("nonsense"))

    def test_the_two_window_readers_cannot_drift_apart(self):
        # There are two: this one runs when the value is saved and drives what
        # the settings page says, core's runs in the claim gate and decides what
        # the worker actually does. If they ever disagreed, the box would convert
        # at an hour the page promises it will not, which is the whole failure
        # this setting exists to prevent.
        for text in ("22:00-06:00", "01:00-06:00", "00:00-12:00", "9:05-21:45", "23:59-00:01"):
            span = store.parse_window(text)
            self.assertEqual(span, core.parse_window(text), text)
            for minute in range(0, 1440, 7):
                at = datetime(2026, 8, 16, minute // 60, minute % 60)
                self.assertEqual(store.in_window(text, at), core.within_window(minute, span),
                                 (text, minute))

    def test_the_one_input_they_read_differently_never_reaches_the_gate(self):
        # core reads equal start and end as a full day; this refuses it outright,
        # at save time, which is what keeps that difference unreachable. Written
        # down because the safety of the pair above rests on it.
        with self.assertRaises(ValueError):
            store.parse_window("06:00-06:00")
        self.assertTrue(core.within_window(12 * 60, core.parse_window("06:00-06:00")))


class NewSettingBounds(unittest.TestCase):
    def bad(self, key, value):
        with self.assertRaises(ValueError, msg=f"{key}={value} was accepted"):
            store.parse_value(store.SPEC_BY_KEY[key], value)

    def test_every_new_number_is_bounded(self):
        self.bad("encode_nice", 20)          # nice(1) tops out at 19
        self.bad("encode_nice", -1)
        self.bad("encode_threads", 65)
        self.bad("session_days", 0)          # a session that expires on issue
        self.bad("session_days", 400)
        self.assertEqual(store.parse_value(store.SPEC_BY_KEY["encode_nice"], "19"), 19)
        self.assertEqual(store.parse_value(store.SPEC_BY_KEY["encode_threads"], "0"), 0)

    def test_paths_and_urls_are_shaped(self):
        self.bad("webhook_url", "example.com/hook")
        self.bad("tls_cert", "cert.pem")
        self.assertEqual(store.parse_value(store.SPEC_BY_KEY["tls_key"], "/config/key.pem"), "/config/key.pem")

    def test_auto_start_defaults_to_running(self):
        self.assertTrue(store.effective({}, {})["auto_start"])
        self.assertFalse(store.effective({}, {"AUTO_START": "false"})["auto_start"])


class Secrets(unittest.TestCase):
    def test_a_secret_is_masked_for_clients_but_real_for_the_daemon(self):
        conn = memdb()
        store.save_settings(conn, {"webhook_secret": "s3cret"})
        values = store.effective(store.read_settings(conn), {})
        self.assertEqual(values["webhook_secret"], "s3cret")
        self.assertEqual(store.redact_secrets(values)["webhook_secret"], store.SECRET_MASK)

    def test_saving_the_mask_back_does_not_overwrite_the_secret(self):
        # The form is sent back whole, and the secret is only ever rendered as
        # the mask - so this is what happens on every unrelated save.
        conn = memdb()
        store.save_settings(conn, {"webhook_secret": "s3cret"})
        store.save_settings(conn, {"webhook_secret": store.SECRET_MASK, "encode_nice": 10})
        values = store.effective(store.read_settings(conn), {})
        self.assertEqual(values["webhook_secret"], "s3cret")
        self.assertEqual(values["encode_nice"], 10)
        # An empty string is still how you clear one.
        store.save_settings(conn, {"webhook_secret": ""})
        self.assertEqual(store.effective(store.read_settings(conn), {})["webhook_secret"], "")


class AdminPassword(unittest.TestCase):
    def setUp(self):
        store._login_failures.clear()

    def test_a_password_round_trips_and_is_never_stored_readable(self):
        conn = memdb()
        store.set_admin(conn, " Lou ", "correct horse battery")
        self.assertTrue(store.admin_exists(conn))
        self.assertEqual(store.admin_username(conn), "Lou")
        self.assertTrue(store.verify_password(conn, "Lou", "correct horse battery"))
        row = conn.execute("SELECT * FROM admin").fetchone()
        self.assertNotIn("correct horse battery", " ".join(str(row[k]) for k in row.keys()))

    def test_the_wrong_password_or_username_is_refused(self):
        conn = memdb()
        store.set_admin(conn, "lou", "correct horse battery")
        self.assertFalse(store.verify_password(conn, "lou", "correct horse batteru"))
        self.assertFalse(store.verify_password(conn, "someone", "correct horse battery"))
        # Case is not a lockout: nobody should lose their box over a capital L.
        self.assertTrue(store.verify_password(conn, "LOU", "correct horse battery"))

    def test_no_admin_means_no_password_works(self):
        self.assertFalse(store.verify_password(memdb(), "lou", "anything"))

    def test_a_short_password_is_refused_with_a_readable_reason(self):
        with self.assertRaises(ValueError) as e:
            store.set_admin(memdb(), "lou", "short")
        self.assertIn(str(store.MIN_PASSWORD_LENGTH), str(e.exception))

    def test_the_cost_travels_with_the_hash_so_it_can_be_raised_later(self):
        conn = memdb()
        store.set_admin(conn, "lou", "correct horse battery")
        conn.execute("UPDATE admin SET n=1024 WHERE id=1")  # as if hashed by an older, cheaper build
        conn.execute(
            "UPDATE admin SET hash=? WHERE id=1",
            (store._scrypt("correct horse battery", bytes.fromhex(
                conn.execute("SELECT salt FROM admin").fetchone()["salt"]), 1024, 8, 1),),
        )
        self.assertTrue(store.verify_password(conn, "lou", "correct horse battery"))

    def test_a_lone_surrogate_password_is_refused_rather_than_crashing(self):
        # JSON can carry one, and a UnicodeEncodeError here would be a 500 an
        # unauthenticated caller could trigger at will.
        conn = memdb()
        store.set_admin(conn, "lou", "correct horse battery")
        self.assertFalse(store.verify_password(conn, "lou", "\ud800abcdefgh"))


class LoginBackoff(unittest.TestCase):
    def setUp(self):
        store._login_failures.clear()

    def test_repeated_guesses_start_costing_time(self):
        conn = memdb()
        store.set_admin(conn, "lou", "correct horse battery")
        for _ in range(store.LOGIN_FREE_ATTEMPTS):
            ok, wait = store.attempt_login(conn, "lou", "nope")
            self.assertFalse(ok)
            self.assertEqual(wait, 0.0)  # fat fingers happen
        ok, wait = store.attempt_login(conn, "lou", "nope")
        self.assertFalse(ok)
        self.assertGreater(wait, 0)
        # And the right password does not get through the backoff either -
        # otherwise the throttle only slows down the guesser who is wrong.
        ok, wait = store.attempt_login(conn, "lou", "correct horse battery")
        self.assertFalse(ok)
        self.assertGreater(wait, 0)

    def test_the_backoff_is_capped_and_clears_on_a_real_login(self):
        conn = memdb()
        store.set_admin(conn, "lou", "correct horse battery")
        store._login_failures["lou"] = (99, 0.0)  # a long night of guessing, expired
        ok, wait = store.attempt_login(conn, "lou", "nope")
        self.assertLessEqual(wait, store.LOGIN_MAX_BACKOFF)
        store._login_failures["lou"] = (99, 0.0)
        ok, wait = store.attempt_login(conn, "lou", "correct horse battery")
        self.assertTrue(ok)
        self.assertEqual(store.login_retry_after(conn, "lou"), 0.0)

    def test_setting_a_password_clears_a_lockout(self):
        conn = memdb()
        store.set_admin(conn, "lou", "correct horse battery")
        store._login_failures["lou"] = (99, time.time() + 300)
        store.set_admin(conn, "lou", "a whole new password")
        self.assertEqual(store.login_retry_after(conn, "lou"), 0.0)

    def test_a_burst_of_concurrent_guesses_only_buys_the_free_attempts(self):
        # Every guess on its own thread is exactly how ThreadingHTTPServer
        # delivers them. Unserialized, all twelve read "no backoff yet" before
        # any of them records a failure: twelve scrypt hashes at 16MB each, from
        # a caller who has proved nothing, and a backoff that saw four of them.
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(store.SCHEMA)
        store.set_admin(conn, "lou", "correct horse battery")
        store._login_failures.clear()
        hashed = []
        real = store.verify_password
        store.verify_password = lambda *a: (hashed.append(1), real(*a))[1]
        try:
            threads = [threading.Thread(target=store.attempt_login, args=(conn, "lou", "nope"))
                       for _ in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            store.verify_password = real
        self.assertLessEqual(len(hashed), store.LOGIN_FREE_ATTEMPTS + 1)
        self.assertGreater(store.login_retry_after(conn, "lou"), 0)
        store._login_failures.clear()

    def test_made_up_usernames_share_one_bucket_and_cannot_lock_the_admin_out(self):
        # Keyed by the name as presented, a guesser cycling a million usernames
        # would grow that dict until the box ran out of memory.
        conn = memdb()
        store.set_admin(conn, "lou", "correct horse battery")
        for n in range(20):
            store.attempt_login(conn, f"admin{n}", "nope")
        self.assertEqual(list(store._login_failures), [store._UNKNOWN_ACCOUNT])
        self.assertEqual(store.login_retry_after(conn, "lou"), 0.0)
        self.assertTrue(store.attempt_login(conn, "lou", "correct horse battery")[0])


class Sessions(unittest.TestCase):
    def test_a_session_authenticates_like_a_key_and_is_not_stored_raw(self):
        conn = memdb()
        raw, row = store.create_session(conn, 30)
        self.assertNotIn("hash", row)
        self.assertNotIn(raw, conn.execute("SELECT hash FROM sessions").fetchone()["hash"])
        self.assertTrue(store.verify_session(conn, raw))
        # The whole point of extending verify_token: the page sends this exactly
        # as it sends an API key.
        self.assertTrue(store.verify_token(conn, raw, ""))

    def test_a_minted_key_and_the_env_token_still_work_alongside_sessions(self):
        # ManageArr authenticates with a minted key; an upgrade must not lock it out.
        conn = memdb()
        key, _ = store.mint_token(conn, "ManageArr")
        store.create_session(conn, 30)
        self.assertTrue(store.verify_token(conn, key, ""))
        self.assertTrue(store.verify_token(conn, "envtoken", "envtoken"))
        self.assertFalse(store.verify_token(conn, "nope", "envtoken"))
        # And the placeholder is still refused, session table or not.
        self.assertFalse(store.verify_token(conn, store.PLACEHOLDER_TOKEN, store.PLACEHOLDER_TOKEN))

    def test_an_expired_session_is_refused_rather_than_renewed(self):
        conn = memdb()
        raw, row = store.create_session(conn, 1)
        conn.execute("UPDATE sessions SET expires=? WHERE id=?", (time.time() - 1, row["id"]))
        self.assertFalse(store.verify_session(conn, raw))
        self.assertFalse(store.verify_token(conn, raw, ""))
        self.assertEqual(store.purge_expired_sessions(conn), 1)
        self.assertEqual(store.list_sessions(conn), [])

    def test_revoking_a_session_ends_it_immediately(self):
        conn = memdb()
        raw, row = store.create_session(conn, 30)
        listed = store.list_sessions(conn)[0]
        self.assertNotIn("hash", listed)
        self.assertTrue(store.revoke_session(conn, row["id"]))
        self.assertFalse(store.verify_session(conn, raw))

    def test_a_session_cannot_outlive_the_cap(self):
        conn = memdb()
        _, row = store.create_session(conn, 99999)
        self.assertLessEqual(row["expires"] - row["created"], store.SESSION_MAX_DAYS * 86400 + 1)


class BackupAndRestore(unittest.TestCase):
    def loaded(self):
        conn = memdb()
        store.save_settings(conn, {"quality": 20, "webhook_secret": "s3cret"})
        store.save_arr(conn, {"name": "Sonarr", "kind": "sonarr", "base_url": "http://s:8989",
                              "api_key": "realkey", "arr_path": "/tv", "worker_path": "/media/TV"})
        store.save_profile(conn, store.clean_profile(
            {"name": "Mine", "encoder": "libx264", "quality": 22}), None, "tested")
        return conn

    def test_no_secret_leaves_in_a_backup(self):
        doc = store.export_config(self.loaded(), "0.9.1")
        flat = str(doc)
        for secret in ("realkey", "s3cret"):
            self.assertNotIn(secret, flat)
        self.assertNotIn("webhook_secret", doc["settings"])
        self.assertNotIn("api_key", doc["arrs"][0])
        self.assertEqual(doc["format"], store.BACKUP_FORMAT)
        self.assertEqual(doc["version"], "0.9.1")

    def test_only_stored_settings_travel_not_the_containers_own(self):
        # Exporting an env-derived value would pin it in the database on the far
        # side, and the next docker run with a corrected env would do nothing.
        conn = memdb()
        store.save_settings(conn, {"quality": 20})
        doc = store.export_config(conn, "0.9.1")
        self.assertEqual(doc["settings"], {"quality": 20})

    def test_a_backup_restores_onto_a_fresh_box(self):
        doc = store.export_config(self.loaded(), "0.9.1")
        fresh = memdb()
        changes = store.import_config(fresh, doc, "0.9.1")
        self.assertEqual(store.effective(store.read_settings(fresh), {})["quality"], 20)
        # The settings a profile carries are the whole reason to keep a backup:
        # coming back with somebody else's quality would re-encode a library at
        # a bitrate nobody chose.
        mine = [p for p in store.list_profiles(fresh) if p["name"] == "Mine"][0]
        self.assertEqual((mine["encoder"], mine["quality"]), ("libx264", 22))
        arr = store.list_arrs(fresh, redact=False)[0]
        self.assertEqual(arr["worker_path"], "/media/TV")
        self.assertEqual(arr["api_key"], "")       # never traveled
        self.assertFalse(arr["enabled"])           # and a keyless arr only 401s
        self.assertTrue(any("API key" in c for c in changes))

    def test_a_restored_profile_is_never_usable_until_it_is_tested_here(self):
        # The backup proves somebody had these settings, not that this machine
        # can run them - it is usually a different machine being restored onto.
        doc = store.export_config(self.loaded(), "0.9.1")
        fresh = memdb()
        store.import_config(fresh, doc, "0.9.1")
        restored = [p for p in store.list_profiles(fresh) if p["name"] == "Mine"][0]
        self.assertFalse(restored["usable"])
        self.assertFalse(restored["active"])
        self.assertIsNone(store.activate_profile(fresh, restored["id"])[0])

    def test_restoring_twice_does_not_duplicate_anything(self):
        doc = store.export_config(self.loaded(), "0.9.1")
        fresh = memdb()
        store.import_config(fresh, doc, "0.9.1")
        store.import_config(fresh, doc, "0.9.1")
        self.assertEqual(len(store.list_arrs(fresh)), 1)
        self.assertEqual(len([p for p in store.list_profiles(fresh) if p["name"] == "Mine"]), 1)

    def test_a_restore_keeps_the_key_and_never_touches_the_profile_in_use(self):
        conn = self.loaded()
        active = [p for p in store.list_profiles(conn) if p["name"] == "Mine"][0]
        store.activate_profile(conn, active["id"])
        doc = store.export_config(conn, "0.9.1")
        doc["profiles"][0]["quality"] = 33
        changes = store.import_config(conn, doc, "0.9.1")
        self.assertEqual(store.get_profile(conn, active["id"])["quality"], 22)
        self.assertTrue(store.get_profile(conn, active["id"])["usable"])
        self.assertTrue(any("in use" in c for c in changes))
        self.assertEqual(store.list_arrs(conn, redact=False)[0]["api_key"], "realkey")

    def test_a_document_from_a_newer_build_is_refused(self):
        conn = memdb()
        for doc in ({"format": store.BACKUP_FORMAT + 1, "version": "0.9.1"},
                    {"format": store.BACKUP_FORMAT, "version": "0.10.0"},
                    {"version": "0.9.1"},
                    "not a document"):
            with self.assertRaises(ValueError):
                store.import_config(conn, doc, "0.9.1")
        # 0.10.0 must read as NEWER than 0.9.1, which is the whole reason the
        # comparison is numeric and not a string compare.
        self.assertGreater(store._version_tuple("0.10.0"), store._version_tuple("0.9.1"))

    def test_a_setting_this_build_does_not_have_is_reported_not_dropped(self):
        conn = memdb()
        changes = store.import_config(
            conn, {"format": 1, "version": "0.9.1", "settings": {"quality": 21, "warp_drive": True}}, "0.9.1")
        self.assertEqual(store.effective(store.read_settings(conn), {})["quality"], 21)
        self.assertTrue(any("warp_drive" in c for c in changes))

    def test_a_bad_value_anywhere_leaves_nothing_applied(self):
        conn = memdb()
        with self.assertRaises(ValueError):
            store.import_config(conn, {"format": 1, "version": "0.9.1", "settings": {"quality": 20},
                                       "arrs": [{"name": "x", "kind": "plex", "base_url": "http://x"}]}, "0.9.1")
        self.assertEqual(store.read_settings(conn), {})


if __name__ == "__main__":
    unittest.main()


class AdminReset(unittest.TestCase):
    """The way back in after a forgotten password.

    It is a recovery path, so the thing worth pinning is that it actually
    recovers: the account is gone, every session with it, and the credential
    other software authenticates with is NOT collateral damage.
    """

    def test_clearing_removes_the_account_and_every_session_but_keeps_api_keys(self):
        conn = memdb()
        store.set_admin(conn, "lou", "a-real-password")
        raw_session, _ = store.create_session(conn, 30)
        raw_key, _ = store.mint_token(conn, "ManageArr")
        self.assertTrue(store.verify_session(conn, raw_session))

        self.assertEqual(store.clear_admin(conn), "lou")

        self.assertFalse(store.admin_exists(conn))
        self.assertIsNone(store.admin_username(conn))
        # A forgotten password and a leaked one look identical from here, so
        # leaving a live session behind would make the reset useless in the
        # second case.
        self.assertFalse(store.verify_session(conn, raw_session))
        # But taking the API keys would turn a password recovery into an
        # outage for whatever else is calling this API.
        self.assertTrue(store.verify_token(conn, raw_key, ""))

    def test_clearing_twice_is_harmless_so_the_flag_can_be_left_set(self):
        # The realistic mistake is leaving TRANSCODEARR_RESET_ADMIN set on a
        # container that restarts by itself. That must not be an error, and it
        # must not resurrect anything - it simply has nothing left to do.
        conn = memdb()
        store.set_admin(conn, "lou", "a-real-password")
        self.assertEqual(store.clear_admin(conn), "lou")
        self.assertIsNone(store.clear_admin(conn))
        self.assertFalse(store.admin_exists(conn))

    def test_the_password_is_gone_rather_than_reset_to_something_guessable(self):
        # Deleting instead of setting a temporary password is the whole design:
        # any temporary value has to be written somewhere it outlives the
        # recovery. Nothing must still authenticate afterwards.
        conn = memdb()
        store.set_admin(conn, "lou", "a-real-password")
        store.clear_admin(conn)
        self.assertFalse(store.verify_password(conn, "lou", "a-real-password"))
        for guess in ("", "admin", "password", "transcodearr", "changeme"):
            self.assertFalse(store.verify_password(conn, "lou", guess))
        self.assertFalse(store.verify_password(conn, "admin", "admin"))

    def test_a_new_account_can_be_created_after_a_reset(self):
        conn = memdb()
        store.set_admin(conn, "lou", "the-forgotten-one")
        store.clear_admin(conn)
        store.set_admin(conn, "lou", "the-new-one")
        self.assertTrue(store.verify_password(conn, "lou", "the-new-one"))
        self.assertFalse(store.verify_password(conn, "lou", "the-forgotten-one"))


class FirstRunKey(unittest.TestCase):
    """The key the container mints for itself when nothing is configured.

    Before it, a fresh container could not be signed into at all until someone
    went away, invented a secret, edited their compose file and came back. The
    properties that make handing them one acceptable are the ones tested here.
    """

    def test_the_generated_key_works_and_only_its_hash_is_kept(self):
        conn = memdb()
        raw, row = store.mint_token(conn, "first run")
        self.assertTrue(store.verify_token(conn, raw, ""))
        stored = conn.execute("SELECT hash FROM tokens").fetchone()["hash"]
        self.assertNotIn(raw, stored)
        self.assertNotIn("hash", row)

    def test_it_is_revocable_like_any_other_key(self):
        # It is printed to a log, so being able to take it out of service once
        # something better exists is the whole reason that is acceptable.
        conn = memdb()
        raw, row = store.mint_token(conn, "first run")
        self.assertTrue(store.revoke_token(conn, row["id"]))
        self.assertFalse(store.verify_token(conn, raw, ""))

    def test_it_is_unguessable(self):
        conn = memdb()
        seen = {store.mint_token(conn, "first run")[0] for _ in range(20)}
        self.assertEqual(len(seen), 20)
        for raw in seen:
            self.assertTrue(raw.startswith("ta_"))
            self.assertGreaterEqual(len(raw), 40)

    def test_the_condition_that_triggers_it_is_only_a_container_nobody_can_reach(self):
        # The guard main() uses. It must stay false once ANY way in exists, or
        # a container would mint a new key into its log on every restart.
        conn = memdb()
        nothing_configured = lambda tok: tok in ("", store.PLACEHOLDER_TOKEN) and not store.list_tokens(conn)
        self.assertTrue(nothing_configured(""))
        self.assertTrue(nothing_configured(store.PLACEHOLDER_TOKEN))
        self.assertFalse(nothing_configured("a-real-bootstrap-token"))
        store.mint_token(conn, "first run")
        self.assertFalse(nothing_configured(""), "a second boot must not mint another key")
