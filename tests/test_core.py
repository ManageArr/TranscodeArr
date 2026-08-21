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

    def test_accepts_disc_container_extensions(self):
        # A real library had a .m2ts. An extension missing from the set is
        # refused by the API and skipped by the watcher, so the file stays
        # hidden forever and nothing ever reports it.
        for name in ["/media/Movies/.Film.m2ts", "/media/Movies/.Film.mts", "/media/Movies/.Film.vob"]:
            self.assertTrue(self.check(name)[0], name)

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


class TrashOverride(unittest.TestCase):
    """prune_trash unlinks everything past the retention window under every
    trash root, so a trash root that contains the library deletes the library."""

    ROOTS = ["/media", "/media2"]

    def test_a_media_root_itself_is_refused(self):
        self.assertTrue(core.trash_override_is_unsafe("/media", self.ROOTS))

    def test_a_parent_of_a_media_root_is_refused(self):
        self.assertTrue(core.trash_override_is_unsafe("/", self.ROOTS))

    def test_a_folder_inside_a_media_root_is_the_supported_case(self):
        self.assertFalse(core.trash_override_is_unsafe("/media/trash", self.ROOTS))

    def test_a_folder_off_the_media_mounts_is_allowed(self):
        self.assertFalse(core.trash_override_is_unsafe("/config/trash", self.ROOTS))

    def test_no_override_means_the_default(self):
        self.assertFalse(core.trash_override_is_unsafe("", self.ROOTS))


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


class ConvertWindow(unittest.TestCase):
    """When the box is allowed to start converting.

    Spanning midnight is the case this has to get right, because "overnight" is
    the only window anybody actually types. The naive start <= now < end holds
    22:00-06:00 shut for all twenty-four hours while the queue grows forever,
    and nothing on screen looks wrong while it happens.
    """

    def test_a_window_that_spans_midnight_is_open_across_midnight(self):
        window = core.parse_window("22:00-06:00")
        self.assertEqual(window, (1320, 360))
        for hour, minute, expected in ((23, 30, True), (2, 0, True), (12, 0, False), (6, 30, False)):
            self.assertEqual(core.within_window(hour * 60 + minute, window), expected, (hour, minute))

    def test_a_plain_window_is_still_a_plain_window(self):
        window = core.parse_window("01:00-06:00")
        self.assertTrue(core.within_window(3 * 60, window))
        self.assertFalse(core.within_window(23 * 60, window))
        self.assertFalse(core.within_window(0, window))

    def test_the_start_is_inclusive_and_the_end_is_exclusive(self):
        # Both ends inclusive would run one minute into the morning every day,
        # and both exclusive loses the minute the window is meant to open on.
        window = core.parse_window("22:00-06:00")
        self.assertFalse(core.within_window(1319, window))
        self.assertTrue(core.within_window(1320, window))
        self.assertTrue(core.within_window(359, window))
        self.assertFalse(core.within_window(360, window))

    def test_a_typo_raises_instead_of_quietly_meaning_always(self):
        # The expensive direction to be wrong in: the one setting whose job is
        # "do not encode while I am watching television" becoming a box that
        # encodes at 8pm and never says why.
        for bad in ("2200-0600", "22:00", "22:00-", "24:00-06:00", "22:00-06:99",
                    "10pm-6am", "22:00 06:00", "22:00_06:00", "always"):
            with self.assertRaises(ValueError, msg=f"{bad!r} was accepted"):
                core.parse_window(bad)

    def test_the_refusal_is_written_to_be_shown_to_a_person(self):
        with self.assertRaises(ValueError) as raised:
            core.parse_window("10pm to 6am")
        self.assertIn("22:00-06:00", str(raised.exception))

    def test_empty_is_the_only_way_to_say_always(self):
        for text in ("", "   ", "\t"):
            self.assertIsNone(core.parse_window(text))
        for minute in (0, 13 * 60, 1439):
            self.assertTrue(core.within_window(minute, None))

    def test_equal_ends_are_a_full_day_and_never_a_way_to_say_never(self):
        # Empty already means always, so a second reading of "never" would leave
        # no way to say "from 02:00 round to 02:00" - and never belongs to the
        # Stop button, which says so on screen.
        window = core.parse_window("02:00-02:00")
        for minute in range(0, 1440, 37):
            self.assertTrue(core.within_window(minute, window), minute)

    def test_the_countdown_points_at_the_next_edge(self):
        window = core.parse_window("22:00-06:00")
        self.assertEqual(core.next_window_change(18 * 60 + 30, window), 3 * 60 + 30)  # until it opens
        self.assertEqual(core.next_window_change(23 * 60, window), 7 * 60)            # until it closes

    def test_zero_is_reserved_for_a_window_that_will_never_change(self):
        # The UI reads 0 as "nothing is scheduled". One minute of the day that
        # answered 0 by accident would print "opens in 0m" at exactly that
        # minute, every day, and read as a stuck countdown.
        self.assertEqual(core.next_window_change(0, None), 0)
        self.assertEqual(core.next_window_change(0, core.parse_window("02:00-02:00")), 0)
        for text in ("22:00-06:00", "01:00-06:00", "00:00-23:59", "23:59-00:01"):
            window = core.parse_window(text)
            for minute in range(1440):
                left = core.next_window_change(minute, window)
                self.assertTrue(1 <= left <= 1440, (text, minute, left))


class Throttling(unittest.TestCase):
    """Yielding to the media server this box exists to feed."""

    def test_asking_for_neither_wraps_nothing(self):
        self.assertEqual(core.throttle_prefix(0, False), [])

    def test_ionice_gets_t_so_a_refused_class_still_execs_ffmpeg(self):
        # Without -t, ionice exits nonzero on a scheduler with no idle class and
        # every job dies at spawn with a message that looks nothing like an
        # encoding error - switching on a throttle would stop the queue dead.
        self.assertEqual(core.throttle_prefix(0, True), ["ionice", "-t", "-c", "3"])

    def test_niceness_is_only_ever_nicer(self):
        # A negative value asks for MORE CPU than the media server, which is the
        # opposite of the setting's whole reason for existing.
        self.assertEqual(core.throttle_prefix(-5, False), [])
        self.assertEqual(core.throttle_prefix(None, False), [])
        self.assertEqual(core.throttle_prefix(99, False), ["nice", "-n", "19"])
        self.assertEqual(core.throttle_prefix(10, False), ["nice", "-n", "10"])

    def test_both_together_leave_ffmpeg_at_the_end(self):
        # Each of these execs the next in place, so the PID Popen holds is still
        # ffmpeg - which is what keeps cancel and the stall watchdog working.
        self.assertEqual(core.throttle_prefix(10, True),
                         ["ionice", "-t", "-c", "3", "nice", "-n", "10"])

    def test_only_the_software_encoders_take_a_thread_cap(self):
        # On NVENC and QSV the work is on the chip; -threads caps a few
        # coordination threads and buys nothing, so it would be a knob that only
        # appears to do something.
        for encoder in core.ENCODER_ORDER:
            expected = [] if core.ENCODER_INFO[encoder]["hardware"] else ["-threads", "2"]
            self.assertEqual(core.thread_args(encoder, 2), expected, encoder)

    def test_no_cap_asked_for_means_no_flag_at_all(self):
        for count in (0, None, -1):
            self.assertEqual(core.thread_args("libx264", count), [], count)
        self.assertEqual(core.thread_args("av1_qsv", 4), [])  # an encoder we do not ship

    def test_the_thread_cap_lands_between_the_input_and_the_output(self):
        # Before -i it caps the DECODER and the encoder still takes every core,
        # which is a throttle that reads as switched on and does nothing.
        args = core.build_ffmpeg_args(core.DEFAULT_TEMPLATES["libx265"], "/m/x.mkv", "/m/.x.tapart.mp4",
                                      24, False, threads=core.thread_args("libx265", 3))
        self.assertGreater(args.index("-threads"), args.index("-i"))
        self.assertEqual(args[args.index("-threads") + 1], "3")
        self.assertEqual(args[-1], "/m/.x.tapart.mp4")

    def test_an_uncapped_job_gets_exactly_the_command_it_always_did(self):
        template = core.DEFAULT_TEMPLATES["libx265"]
        base = core.build_ffmpeg_args(template, "/m/x.mkv", "/m/.x.tapart.mp4", 24, False)
        for threads in (None, [], core.thread_args("h264_nvenc", 4)):
            self.assertEqual(
                core.build_ffmpeg_args(template, "/m/x.mkv", "/m/.x.tapart.mp4", 24, False, threads=threads),
                base)


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


# Both blocks are verbatim ffmpeg 7.1 output from the deployment where this was
# found, indentation included. They are the two failures a person most needs
# told apart, and the recorded error used to be the same text for both.
TEN_BIT = """\
        _STATISTICS_TAGS: BPS DURATION NUMBER_OF_FRAMES NUMBER_OF_BYTES
Stream mapping:
  Stream #0:0 -> #0:0 (hevc (native) -> h264 (h264_nvenc))
  Stream #0:1 -> #0:1 (eac3 (native) -> aac (native))
[h264_nvenc @ 0x5644eb5e12c0] 10 bit encode not supported
[h264_nvenc @ 0x5644eb5e12c0] No capable devices found
[vost#0:0/h264_nvenc @ 0x5644eb5f4fc0] Error while opening encoder - maybe incorrect parameters such as bit_rate, rate, width or height.
[vf#0:0 @ 0x5644eb5f8dc0] Error sending frames to consumers: Generic error in an external library
[vf#0:0 @ 0x5644eb5f8dc0] Task finished with error code: -542398533 (Generic error in an external library)
[vf#0:0 @ 0x5644eb5f8dc0] Terminating thread with return code -542398533 (Generic error in an external library)
[vost#0:0/h264_nvenc @ 0x5644eb5f4fc0] Could not open encoder before EOF
[vost#0:0/h264_nvenc @ 0x5644eb5f4fc0] Task finished with error code: -22 (Invalid argument)
[vost#0:0/h264_nvenc @ 0x5644eb5f4fc0] Terminating thread with return code -22 (Invalid argument)
[out#0/mp4 @ 0x5644eb617b00] Nothing was written into output file, because at least one of its streams received no packets.
frame=    0 fps=0.0 q=0.0 Lsize=       0KiB time=N/A bitrate=N/A speed=N/A
[aac @ 0x5644ebdfaa80] Qavg: 64525.016
[aac @ 0x5644ebec53c0] Qavg: 64525.016
Conversion failed!""".split("\n")

NO_CUDA = """\
[AVHWDeviceContext @ 0x561634917880] cu->cuInit(0) failed -> CUDA_ERROR_NOT_INITIALIZED: initialization error
[vist#0:0/hevc @ 0x56163491bb00] [dec:hevc @ 0x5616348e2a40] Hardware device setup failed for decoder: Generic error in an external library
Error opening output file /tmp/out.mp4.
Error opening output files: Generic error in an external library""".split("\n")


class ErrorSummary(unittest.TestCase):
    """What a failed job is allowed to say. The old tail said nothing."""

    def test_it_keeps_the_line_that_names_the_cause(self):
        self.assertIn("10 bit encode not supported", core.error_summary(TEN_BIT))
        self.assertIn("CUDA_ERROR_NOT_INITIALIZED", core.error_summary(NO_CUDA))

    def test_the_two_failures_no_longer_read_the_same(self):
        # Both end in "-22 (Invalid argument)". Keeping the raw tail is what
        # made forty files failing for one reason look like forty reasons.
        self.assertNotEqual(core.error_summary(TEN_BIT), core.error_summary(NO_CUDA))

    def test_restatements_and_stats_are_dropped(self):
        summary = core.error_summary(TEN_BIT)
        for noise in ("Terminating thread", "Task finished", "Qavg", "frame=", "Conversion failed!"):
            self.assertNotIn(noise, summary, noise)

    def test_the_input_dump_is_dropped(self):
        # Indented lines are the metadata ffmpeg prints under Input #0. They
        # are what pushed the real diagnostics out of the window.
        self.assertNotIn("_STATISTICS_TAGS", core.error_summary(TEN_BIT))

    def test_pointers_are_stripped_so_one_fault_reads_as_one_fault(self):
        summary = core.error_summary(TEN_BIT)
        self.assertNotIn("0x5644eb", summary)
        self.assertIn("[h264_nvenc]", summary)

    def test_it_stays_within_its_width(self):
        self.assertLessEqual(len(core.error_summary(TEN_BIT * 10, width=200)), 200)

    def test_output_that_is_nothing_but_noise_still_says_something(self):
        # An empty error field is the silence this worker exists to remove.
        self.assertTrue(core.error_summary(["frame=    1 fps=0.0", "Conversion failed!"]).strip())


if __name__ == "__main__":
    unittest.main()
