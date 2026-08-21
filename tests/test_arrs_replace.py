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
        with mock.patch.object(client, "_owning_item", lambda f: (SERIES, "/tv/Criminal Minds/x.mkv")), \
             mock.patch.object(client, "_grab_history_id",
                               lambda item, path: (history_id, "Criminal.Minds.S07.1080p-iVy", release_type)), \
             mock.patch.object(arrs, "_request", lambda *a, **k: (posted.append(a[1]), (None, None))[1]):
            handled, message = client.replace_bad_file(FILE, allow_packs=allow_packs)
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


if __name__ == "__main__":
    unittest.main()
