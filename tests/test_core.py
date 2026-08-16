"""The rules that keep media safe, stated exactly. python -m unittest discover tests"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import core  # noqa: E402


class ValidatePath(unittest.TestCase):
    ROOTS = ["/media"]

    def check(self, raw, realpath=lambda p: p):
        return core.validate_path(raw, self.ROOTS, realpath=realpath)

    def test_accepts_a_video_inside_the_root(self):
        ok, resolved = self.check("/media/Movies/.Movie (2026).mkv")
        self.assertTrue(ok)
        self.assertEqual(resolved, "/media/Movies/.Movie (2026).mkv")

    def test_refuses_everything_outside_the_root(self):
        for raw in ["/etc/passwd.mkv", "/media2/x.mkv", "/m.mkv", "/"]:
            self.assertFalse(self.check(raw)[0], raw)

    def test_refuses_a_symlink_that_points_out(self):
        # The path LOOKS inside; realpath says otherwise. Resolution must come
        # before containment or a symlink walks straight out of the root.
        ok, why = self.check("/media/link.mkv", realpath=lambda p: "/etc/target.mkv")
        self.assertFalse(ok)
        self.assertIn("outside", why)

    def test_refuses_non_video_and_staging_files(self):
        self.assertFalse(self.check("/media/notes.txt")[0])
        self.assertFalse(self.check("/media/x" + core.PART_MARKER + ".mp4")[0])

    def test_refuses_control_characters_and_empty(self):
        self.assertFalse(self.check("/media/x\n.mkv")[0])
        self.assertFalse(self.check("")[0])
        self.assertFalse(core.validate_path("/media/x.mkv", [])[0])  # no roots = refuse


class PlanNames(unittest.TestCase):
    # Expected paths built with os.path.join so the same assertions hold on
    # the Linux container and a Windows dev box.
    def j(self, name):
        return os.path.join("/m", name)

    def test_hidden_mkv_stages_hidden_and_reveals_visible(self):
        n = core.plan_names(self.j(".Movie (2026).mkv"))
        self.assertTrue(n.hidden)
        self.assertEqual(n.part, self.j(".Movie (2026).tapart.mp4"))
        self.assertEqual(n.hidden_final, self.j(".Movie (2026).mp4"))
        self.assertEqual(n.visible, self.j("Movie (2026).mp4"))
        self.assertFalse(n.reveal_only)

    def test_hidden_mp4_needs_only_the_reveal(self):
        n = core.plan_names(self.j(".Movie.mp4"))
        self.assertTrue(n.reveal_only)
        self.assertEqual(n.visible, self.j("Movie.mp4"))

    def test_visible_mkv_still_stages_hidden(self):
        # Even for an unhidden source, the partial file must never be visible.
        n = core.plan_names(self.j("Movie.mkv"))
        self.assertFalse(n.hidden)
        self.assertEqual(n.part, self.j(".Movie.tapart.mp4"))
        self.assertEqual(n.visible, self.j("Movie.mp4"))


class VerifyOutput(unittest.TestCase):
    """The check whose absence truncated a real library."""

    def probe(self, duration, v=1, a=1, s=0):
        return core.Probe(duration=duration, video_streams=v, audio_streams=a, subtitle_streams=s)

    def test_accepts_a_faithful_encode(self):
        ok, why = core.verify_output(self.probe(5460), self.probe(5459))
        self.assertTrue(ok, why)

    def test_rejects_the_minions_case(self):
        # 91 minutes in, 38 minutes out, exit code 0. The incident, exactly.
        ok, why = core.verify_output(self.probe(91 * 60), self.probe(38.65 * 60))
        self.assertFalse(ok)
        self.assertIn("duration mismatch", why)

    def test_rejects_video_less_and_audio_less_output(self):
        self.assertFalse(core.verify_output(self.probe(100), self.probe(100, v=0))[0])
        self.assertFalse(core.verify_output(self.probe(100, a=2), self.probe(100, a=0))[0])

    def test_refuses_to_replace_when_duration_is_unreadable(self):
        # Unknown is not the same as fine - the source stays.
        self.assertFalse(core.verify_output(self.probe(None), self.probe(100))[0])
        self.assertFalse(core.verify_output(self.probe(100), self.probe(None))[0])
        self.assertFalse(core.verify_output(self.probe(0), self.probe(0))[0])

    def test_tolerance_is_a_knob(self):
        src, out = self.probe(1000), self.probe(985)
        self.assertFalse(core.verify_output(src, out, tolerance=0.01)[0])
        self.assertTrue(core.verify_output(src, out, tolerance=0.02)[0])


class Stability(unittest.TestCase):
    """Size across a real interval - never mtime, which imports preserve."""

    def test_stable_only_when_size_held_for_the_window(self):
        self.assertTrue(core.is_stable(1000, 1000, 121, 120))
        self.assertFalse(core.is_stable(1000, 1000, 60, 120))   # not long enough
        self.assertFalse(core.is_stable(1000, 2000, 300, 120))  # still growing
        self.assertFalse(core.is_stable(None, 1000, 999, 120))  # never seen before


class FfmpegArgs(unittest.TestCase):
    def test_paths_are_argv_entries_never_shell(self):
        args = core.build_ffmpeg_args(core.DEFAULT_TEMPLATES["libx264"], "/m/a b'; rm.mkv", "/m/.a.tapart.mp4", 24, True)
        self.assertIn("/m/a b'; rm.mkv", args)          # intact, unquoted, harmless as argv
        self.assertEqual(args[0], "ffmpeg")
        self.assertIn("mov_text", args)

    def test_subtitle_fallback_drops_subs_explicitly(self):
        args = core.build_ffmpeg_args(core.DEFAULT_TEMPLATES["h264_nvenc"], "/m/x.mkv", "/m/.x.tapart.mp4", 24, False)
        self.assertIn("-sn", args)
        self.assertNotIn("mov_text", args)

    def test_progress_parsing(self):
        self.assertEqual(core.parse_progress("out_time_us=30000000", 60.0), 50)
        self.assertIsNone(core.parse_progress("speed=2.5x", 60.0))
        self.assertEqual(core.parse_progress("out_time_us=999999999999", 60.0), 99)  # capped


if __name__ == "__main__":
    unittest.main()
