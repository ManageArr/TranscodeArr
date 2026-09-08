"""Which releases this worker is willing to blocklist.

Blocklisting retires a release. When the grab was a season pack, that is the
release the rest of a season came from - so the choice belongs to the operator,
and the values here (SingleEpisode, MultiEpisode, SeasonPack) are the ones a
real Sonarr 4.0.19 was observed recording.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import arrs  # noqa: E402

SERIES = {"id": 126, "title": "Criminal Minds", "path": "/tv/Criminal Minds"}
FILE = "/media/TV/Criminal Minds/Season 07/.Criminal Minds - S07E10.mkv"


class BlocklistScope(unittest.TestCase):
    def client(self):
        return arrs.ArrClient({"id": "a", "name": "Sonarr", "kind": "sonarr",
                               "base_url": "http://s", "api_key": "k",
                               "arr_path": "/tv", "worker_path": "/media/TV"})

    def run_replace(self, release_type, allow_packs, history_id=33448):
        """The client with its lookups stubbed, so only the gate is under test."""
        client = self.client()
        posted = []
        grab = None if history_id is None else {
            "id": history_id, "release": "Criminal.Minds.S07.1080p-iVy",
            "type": release_type, "episode_id": 10984,
        }
        stub_grab = lambda item, path: (grab, "" if grab else "no grab in this arr's history")  # noqa: E731
        stub_post = lambda *a, **k: (posted.append(a[1]), (None, None))[1]  # noqa: E731
        with mock.patch.object(client, "_owning_item", lambda f: (SERIES, "/tv/Criminal Minds/x.mkv")):
            with mock.patch.object(client, "_grab", stub_grab):
                with mock.patch.object(arrs, "_request", stub_post):
                    handled, message, found = client.replace_bad_file(FILE, allow_packs=allow_packs)
        self.found = found
        return handled, message, posted

    def test_a_single_episode_release_is_blocklisted_without_asking(self):
        handled, message, posted = self.run_replace("SingleEpisode", allow_packs=False)
        self.assertTrue(handled)
        self.assertIn("blocklisted", message)
        self.assertEqual(len(posted), 1)
        self.assertIn("/api/v3/history/failed/33448", posted[0])

    def test_a_season_pack_is_refused_unless_allowed(self):
        handled, message, posted = self.run_replace("SeasonPack", allow_packs=False)
        self.assertTrue(handled)
        self.assertIn("not blocklisting", message)
        self.assertIn("SeasonPack", message)
        self.assertEqual(posted, [], "it blocklisted a season pack nobody allowed")

    def test_a_season_pack_is_blocklisted_when_allowed(self):
        handled, message, posted = self.run_replace("SeasonPack", allow_packs=True)
        self.assertTrue(handled)
        self.assertIn("blocklisted", message)
        self.assertIn("SeasonPack", message)
        self.assertEqual(len(posted), 1)

    def test_a_multi_episode_release_counts_as_broad_too(self):
        # It is not a season pack, but it still covers episodes this file is
        # not, and retiring it costs them.
        _handled, message, posted = self.run_replace("MultiEpisode", allow_packs=False)
        self.assertIn("not blocklisting", message)
        self.assertEqual(posted, [])

    def test_an_unclassified_release_is_refused_rather_than_assumed_safe(self):
        _handled, message, posted = self.run_replace("", allow_packs=False)
        self.assertIn("did not classify", message)
        self.assertEqual(posted, [], "unknown was treated as safe")

    def test_no_grab_means_nothing_to_blocklist(self):
        _handled, message, posted = self.run_replace("SingleEpisode", allow_packs=True, history_id=None)
        self.assertEqual(posted, [])
        self.assertTrue(message)


class FindingWhatToSearchFor(unittest.TestCase):
    """Resolving item and episode from the PATH, not from the grab history.

    A job can fail verification on a file whose grab the arr has long since
    pruned, or that was imported by hand and never grabbed at all, and those
    are exactly the files somebody wants a replacement for.
    """

    def client(self, kind="sonarr"):
        return arrs.ArrClient({"id": "a", "name": "Sonarr" if kind == "sonarr" else "Radarr", "kind": kind,
                               "base_url": "http://s", "api_key": "k",
                               "arr_path": "/tv", "worker_path": "/media/TV"})

    def test_a_sonarr_file_resolves_to_its_episode(self):
        client = self.client()
        episodes = [
            {"id": 1, "seasonNumber": 7, "episodeNumber": 13, "episodeFile": {"path": "/tv/Show/other.mkv"}},
            {"id": 16914, "seasonNumber": 7, "episodeNumber": 14,
             "episodeFile": {"path": "/tv/Show/Season 07/.Show - S07E14.mkv"}},
            {"id": 3, "seasonNumber": 7, "episodeNumber": 15, "episodeFile": None},
        ]
        with mock.patch.object(client, "_owning_item",
                               lambda f: (SERIES, "/tv/Show/Season 07/.Show - S07E14.mkv")), \
                mock.patch.object(arrs, "_request", lambda *a, **k: (episodes, None)):
            target, why = client.find_target("/media/TV/Show/Season 07/.Show - S07E14.mkv")
        self.assertEqual(why, "")
        self.assertEqual(target["episode_id"], 16914)
        self.assertEqual(target["item_id"], SERIES["id"])
        self.assertIn("S07E14", target["title"])

    def test_a_file_no_episode_claims_is_reported_not_guessed(self):
        client = self.client()
        with mock.patch.object(client, "_owning_item", lambda f: (SERIES, "/tv/Show/nobody.mkv")), \
                mock.patch.object(arrs, "_request", lambda *a, **k: ([], None)):
            target, why = client.find_target("/media/TV/Show/nobody.mkv")
        self.assertIsNone(target)
        self.assertIn("no episode", why)

    def test_a_radarr_file_needs_no_episode(self):
        client = self.client("radarr")
        movie = {"id": 44, "title": "Heat"}
        with mock.patch.object(client, "_owning_item", lambda f: (movie, "/movies/Heat/Heat.mkv")):
            target, why = client.find_target("/media/TV/Heat/Heat.mkv")
        self.assertEqual((target["item_id"], target["episode_id"], why), (44, None, ""))


class GrabbingOne(unittest.TestCase):
    def client(self):
        return arrs.ArrClient({"id": "a", "name": "Sonarr", "kind": "sonarr", "base_url": "http://s",
                               "api_key": "k", "arr_path": "/tv", "worker_path": "/media/TV"})

    def test_a_grab_posts_the_guid_and_indexer_the_arr_gave_us(self):
        sent = []

        def fake(method, url, key, body=None):
            sent.append((method, url, body))
            return None, None

        with mock.patch.object(arrs, "_request", fake):
            ok, error = self.client().grab_release("magnet:?xt=urn:btih:abc", 18)
        self.assertTrue(ok)
        self.assertEqual(error, "")
        self.assertEqual(sent[0][0], "POST")
        self.assertTrue(sent[0][1].endswith("/api/v3/release"))
        self.assertEqual(sent[0][2], {"guid": "magnet:?xt=urn:btih:abc", "indexerId": 18})

    def test_an_arr_that_refuses_is_reported_not_swallowed(self):
        with mock.patch.object(arrs, "_request", lambda *a, **k: (None, "500 Internal Server Error")):
            ok, error = self.client().grab_release("g", 1)
        self.assertFalse(ok)
        self.assertIn("500", error)

    def test_the_release_view_keeps_every_rejection_verbatim(self):
        # Truncating or summarising these would leave somebody clicking Grab
        # with no idea what they are overriding.
        raw = {"guid": "g", "indexerId": 18, "indexer": "EZTV", "title": "Show.S07E14",
               "size": 2534030704, "age": 2190, "seeders": 3, "protocol": "torrent",
               "quality": {"quality": {"name": "WEBDL-1080p"}}, "rejected": True,
               "rejections": ["Existing file meets cutoff: WEB 1080p", "Not enough seeders: 0"],
               "downloadAllowed": True, "releaseWeight": 0}
        view = arrs._release_view(raw)
        self.assertEqual(view["rejections"], raw["rejections"])
        self.assertEqual(view["quality"], "WEBDL-1080p")
        self.assertEqual(view["indexer_id"], 18)
        self.assertTrue(view["download_allowed"])

    def test_a_release_missing_half_its_fields_does_not_explode(self):
        view = arrs._release_view({})
        self.assertEqual((view["guid"], view["rejections"], view["quality"]), ("", [], ""))
        self.assertTrue(view["download_allowed"], "absent downloadAllowed must not read as refused")


if __name__ == "__main__":
    unittest.main()
