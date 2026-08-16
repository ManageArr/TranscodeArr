"""Settings precedence, API keys and arr path mapping.

The precedence rule is the one worth pinning: get it backwards and every
container recreate silently reverts what someone changed in the UI.
"""

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import arrs  # noqa: E402
import core  # noqa: E402
import store  # noqa: E402


def memdb() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(store.SCHEMA)
    return conn


class Precedence(unittest.TestCase):
    def test_default_when_nothing_is_set(self):
        self.assertEqual(store.effective({}, {})["quality"], 24)

    def test_env_seeds_a_key_nobody_has_set(self):
        self.assertEqual(store.effective({}, {"QUALITY": "20"})["quality"], 20)

    def test_a_stored_value_outranks_the_environment(self):
        # The whole point: a container recreated with a stale env must not undo
        # what someone chose in the UI.
        values = store.effective({"quality": "18"}, {"QUALITY": "20"})
        self.assertEqual(values["quality"], 18)

    def test_the_ui_can_say_where_each_value_came_from(self):
        src = store.sources({"quality": "18"}, {"TRASH_KEEP_DAYS": "30"})
        self.assertEqual(src["quality"], "stored")
        self.assertEqual(src["trash_keep_days"], "env")
        self.assertEqual(src["stable_seconds"], "default")

    def test_a_corrupt_row_falls_through_instead_of_taking_the_daemon_down(self):
        values = store.effective({"quality": "not json"}, {"QUALITY": "20"})
        self.assertEqual(values["quality"], 20)

    def test_a_nonsense_env_value_falls_back_to_the_default(self):
        self.assertEqual(store.effective({}, {"QUALITY": "high"})["quality"], 24)


class Parsing(unittest.TestCase):
    def spec(self, key):
        return store.SPEC_BY_KEY[key]

    def test_extensions_are_normalised_with_a_leading_dot(self):
        self.assertEqual(store.parse_value(self.spec("convert_extensions"), "mkv, .AVI"), [".mkv", ".avi"])

    def test_paths_accept_colon_and_newline_separated_input(self):
        self.assertEqual(
            store.parse_value(self.spec("watch_roots"), "/media/TV:/media/Movies"),
            ["/media/TV", "/media/Movies"],
        )

    def test_a_relative_watch_root_is_refused(self):
        with self.assertRaises(ValueError):
            store.parse_value(self.spec("watch_roots"), "media/TV")

    def test_a_negative_interval_is_refused(self):
        with self.assertRaises(ValueError):
            store.parse_value(self.spec("scan_interval_seconds"), "-5")

    def test_saving_an_unknown_key_is_refused(self):
        with self.assertRaises(ValueError):
            store.save_settings(memdb(), {"rm_rf": "true"})

    def test_saving_then_resetting_returns_a_key_to_the_environment(self):
        conn = memdb()
        store.save_settings(conn, {"quality": 18})
        self.assertEqual(store.effective(store.read_settings(conn), {"QUALITY": "20"})["quality"], 18)
        store.reset_setting(conn, "quality")
        self.assertEqual(store.effective(store.read_settings(conn), {"QUALITY": "20"})["quality"], 20)


class ToleranceIsBounded(unittest.TestCase):
    """The field that can switch verification off by accident."""

    def spec(self):
        return store.SPEC_BY_KEY["verify_duration_tolerance"]

    def test_a_sane_fraction_is_accepted(self):
        self.assertAlmostEqual(store.parse_value(self.spec(), "0.015"), 0.015)

    def test_fifteen_meaning_fifteen_percent_is_refused(self):
        # The realistic mistake: the label says "tolerance" and the help says
        # "0.015 is 1.5%", so someone types 15. At 15.0 a three-second file
        # passes as a two-hour film - the exact incident this repo exists for.
        with self.assertRaises(ValueError):
            store.parse_value(self.spec(), "15")

    def test_infinity_and_nan_are_refused(self):
        for bad in ("inf", "nan", "-1", "0"):
            with self.assertRaises(ValueError):
                store.parse_value(self.spec(), bad)

    def test_a_bad_stored_value_falls_back_rather_than_disabling_verification(self):
        values = store.effective({"verify_duration_tolerance": '"15"'}, {})
        self.assertEqual(values["verify_duration_tolerance"], 0.015)

    def test_the_ui_does_not_claim_a_rejected_row_is_in_force(self):
        # Saying "saved here" beside a value that came from somewhere else is
        # how someone spends an hour editing a field that does nothing.
        rows = {"verify_duration_tolerance": '"15"'}
        self.assertEqual(store.sources(rows, {})["verify_duration_tolerance"], "default")


class WatchRootContainment(unittest.TestCase):
    def test_a_root_outside_the_media_mounts_is_refused(self):
        spec = store.SPEC_BY_KEY["watch_roots"]
        with self.assertRaises(ValueError):
            store.parse_value(spec, ["/etc"], roots=["/media"])

    def test_a_sibling_sharing_a_prefix_is_not_inside(self):
        # /media must not match /mediafoo.
        self.assertFalse(store.is_within_any("/mediafoo", ["/media"]))

    def test_roots_inside_the_mounts_are_accepted(self):
        spec = store.SPEC_BY_KEY["watch_roots"]
        self.assertEqual(store.parse_value(spec, ["/media/TV"], roots=["/media"]), ["/media/TV"])


class Tokens(unittest.TestCase):
    def test_a_minted_key_authenticates_and_is_not_stored_raw(self):
        conn = memdb()
        raw, row = store.mint_token(conn, "ManageArr")
        self.assertTrue(store.verify_token(conn, raw, ""))
        stored = conn.execute("SELECT hash FROM tokens").fetchone()["hash"]
        self.assertNotIn(raw, stored)
        self.assertNotIn("hash", row)

    def test_a_wrong_key_is_refused_and_an_empty_one_never_passes(self):
        conn = memdb()
        store.mint_token(conn, "k")
        self.assertFalse(store.verify_token(conn, "ta_wrong", ""))
        self.assertFalse(store.verify_token(conn, "", ""))
        # An empty env token must not turn an empty presented token into a pass.
        self.assertFalse(store.verify_token(conn, "", ""))

    def test_the_container_env_token_still_works_as_a_bootstrap_key(self):
        # Otherwise upgrading locks out ManageArr, which holds the env token.
        self.assertTrue(store.verify_token(memdb(), "envtoken", "envtoken"))

    def test_a_non_ascii_token_is_refused_rather_than_raising(self):
        # Headers are decoded latin-1, and compare_digest raises TypeError on a
        # non-ASCII str - which killed the request with no response at all.
        conn = memdb()
        self.assertFalse(store.verify_token(conn, "tokén", "envtoken"))
        self.assertFalse(store.verify_token(conn, "\udcff", "envtoken"))

    def test_nothing_is_written_when_any_field_is_invalid(self):
        # A bad third field must not leave the first two applied.
        conn = memdb()
        with self.assertRaises(ValueError):
            store.save_settings(conn, {"quality": 20, "scan_interval_seconds": "-1"})
        self.assertEqual(store.read_settings(conn), {})

    def test_a_revoked_key_stops_working_immediately(self):
        conn = memdb()
        raw, row = store.mint_token(conn, "k")
        self.assertTrue(store.revoke_token(conn, row["id"]))
        self.assertFalse(store.verify_token(conn, raw, ""))


class Arrs(unittest.TestCase):
    def test_a_connection_round_trips_and_hides_its_key_from_the_browser(self):
        conn = memdb()
        row = store.save_arr(conn, {"name": "Sonarr", "kind": "sonarr", "base_url": "http://s:8989/",
                                    "api_key": "secret", "arr_path": "/tv", "worker_path": "/media/TV"})
        self.assertEqual(row["base_url"], "http://s:8989")  # trailing slash trimmed
        listed = store.list_arrs(conn)[0]
        self.assertEqual(listed["api_key"], "********")
        self.assertEqual(store.get_arr(conn, row["id"])["api_key"], "secret")

    def test_editing_without_a_key_keeps_the_existing_one(self):
        # The form never receives the real key, so it cannot send it back.
        conn = memdb()
        row = store.save_arr(conn, {"name": "S", "kind": "sonarr", "base_url": "http://s", "api_key": "secret"})
        store.save_arr(conn, {"name": "Renamed", "kind": "sonarr", "base_url": "http://s", "api_key": ""}, row["id"])
        self.assertEqual(store.get_arr(conn, row["id"])["api_key"], "secret")

    def test_a_bad_kind_or_url_is_refused(self):
        conn = memdb()
        for body in ({"kind": "plex", "base_url": "http://x", "api_key": "k"},
                     {"kind": "radarr", "base_url": "radarr:7878", "api_key": "k"},
                     {"kind": "radarr", "base_url": "http://x", "api_key": ""}):
            with self.assertRaises(ValueError):
                store.save_arr(conn, body)


class PathMapping(unittest.TestCase):
    def test_a_container_path_translates_to_what_the_arr_calls_it(self):
        # The pair verified on the real NAS: Sonarr says /tv, the worker /media/TV.
        self.assertEqual(
            arrs.to_arr_path("/media/TV/Show/S01E01.mp4", "/media/TV", "/tv"),
            "/tv/Show/S01E01.mp4",
        )

    def test_a_file_outside_this_connection_root_is_not_claimed(self):
        # How one arr ignores another's libraries instead of guessing.
        self.assertIsNone(arrs.to_arr_path("/media/Movies/x.mp4", "/media/TV", "/tv"))
        # A sibling directory sharing a prefix must not match either.
        self.assertIsNone(arrs.to_arr_path("/media/TV-Anime/x.mp4", "/media/TV", "/tv"))

    def test_an_unmapped_connection_claims_nothing(self):
        self.assertIsNone(arrs.to_arr_path("/media/TV/x.mp4", "", "/tv"))

    def test_the_longest_matching_folder_wins(self):
        items = [{"id": 1, "path": "/tv/Show"}, {"id": 2, "path": "/tv/Show Extras"}]
        self.assertEqual(arrs.find_item(items, "/tv/Show Extras/S01E01.mp4")["id"], 2)
        self.assertEqual(arrs.find_item(items, "/tv/Show/S01E01.mp4")["id"], 1)

    def test_no_match_is_none_rather_than_a_guess(self):
        self.assertIsNone(arrs.find_item([{"id": 1, "path": "/tv/Show"}], "/tv/Other/S01E01.mp4"))


class ArrRequests(unittest.TestCase):
    def test_a_user_agent_is_always_sent(self):
        # urllib's default "Python-urllib/3.x" gets a flat 403 from Cloudflare
        # and several reverse proxies, which reads exactly like a rejected API
        # key. Cost an hour against a real proxied Sonarr.
        self.assertTrue(arrs.USER_AGENT)
        self.assertNotIn("urllib", arrs.USER_AGENT.lower())


class SkipRules(unittest.TestCase):
    def test_a_remux_can_be_protected_by_name(self):
        self.assertTrue(core.matches_skip("Film - Bluray-1080p Remux.mkv", ["remux"]))
        self.assertFalse(core.matches_skip("Film - WEBDL-1080p.mkv", ["remux"]))

    def test_an_empty_rule_list_protects_nothing(self):
        self.assertFalse(core.matches_skip("anything.mkv", []))
        self.assertFalse(core.matches_skip("anything.mkv", ["  "]))

    def test_a_protected_file_keeps_its_own_container(self):
        # Revealing with the default .mp4 target would rename an mkv to .mp4
        # without converting it - a file whose extension lies about its bytes.
        names = core.plan_names("/m/.Film - Remux.mkv", ".mkv")
        self.assertTrue(names.reveal_only)
        self.assertEqual(names.visible, os.path.join("/m", "Film - Remux.mkv"))


if __name__ == "__main__":
    unittest.main()
