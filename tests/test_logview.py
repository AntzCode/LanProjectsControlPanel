# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 AntzCode
"""The log viewer window, driven for real. Needs GTK 3 for Python and a display: skipped without them, so run it
on a virtual one:  xvfb-run -a dbus-run-session -- python3 -m unittest tests.test_logview"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
try:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, Gio, GLib, Gtk
    HAVE_DISPLAY = Gtk.init_check()[0]
except (ImportError, ValueError):
    HAVE_DISPLAY = False

import lpcp_logs as logs  # noqa: E402

if HAVE_DISPLAY:
    import lpcp_logview as view  # noqa: E402
    WindowClass = view.build_window_class(Gdk, GLib, Gtk)


def pump(seconds=0.25):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while Gtk.events_pending():
            Gtk.main_iteration()
        time.sleep(0.005)


def until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pump(0.05)
        if predicate():
            return True
    return False


def alive(pattern):
    import subprocess
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()


@unittest.skipUnless(HAVE_DISPLAY, "needs GTK 3 for Python and a display (see the module docstring)")
class Window(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application(application_id="com.antzcode.lpcp.test", flags=Gio.ApplicationFlags.NON_UNIQUE)
        cls.app.register()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.project = SimpleNamespace(name="blog", dir=self.dir)
        self.windows = []

    def tearDown(self):
        for w in self.windows:
            w.shutdown()
            w.destroy()
        pump(0.1)

    def window(self, sources, start=None):
        w = WindowClass(self.app, self.project, sources, {"LPCP_PROJECT": "blog"}, start, flush_ms=20)
        self.windows.append(w)
        w.show_all()
        return w

    def feed(self, text, name="app"):
        """A file source, written now; the window follows it."""
        (self.dir / f"{name}.log").write_text(text)
        return logs.LogSource(name, name.title(), file=f"{name}.log")

    def set_filter(self, w, include="", exclude=""):
        w.include.set_text(include)
        w.exclude.set_text(exclude)
        pump(view.FILTER_DELAY_MS / 1000 + 0.25)

    def test_shows_lines_and_colours_levels(self):
        w = self.window([self.feed("GET / 200\n[php:error] boom\nWarning: careful\nplain\n")])
        self.assertTrue(until(lambda: len(w.shown) == 4))
        self.assertEqual(w.shown[1], "[php:error] boom")
        text = w.text
        line = lambda n: (text.get_iter_at_line(n), text.get_iter_at_line_offset(n, 3))
        self.assertTrue(line(1)[0].has_tag(w.tags["error"]))
        self.assertTrue(line(2)[0].has_tag(w.tags["warn"]))
        self.assertFalse(line(0)[0].has_tag(w.tags["error"]) or line(3)[0].has_tag(w.tags["warn"]))

    def test_filter_include_exclude_and_case(self):
        w = self.window([self.feed("GET /a 200\nGET /b 404\nPOST /a 500\nget /c 200\n")])
        self.assertTrue(until(lambda: len(w.shown) == 4))
        self.set_filter(w, include="/a")
        self.assertEqual(w.shown, ["GET /a 200", "POST /a 500"])
        self.assertEqual(w.text.get_text(*w.text.get_bounds(), False), "GET /a 200\nPOST /a 500\n")
        self.set_filter(w, include="/a", exclude="POST")
        self.assertEqual(w.shown, ["GET /a 200"])
        self.set_filter(w, include="GET")
        self.assertEqual(w.shown, ["GET /a 200", "GET /b 404", "get /c 200"])      # case ignored by default
        w.match_case.set_active(True)
        pump(view.FILTER_DELAY_MS / 1000 + 0.25)
        self.assertEqual(w.shown, ["GET /a 200", "GET /b 404"])
        self.set_filter(w)
        self.assertEqual(len(w.shown), 4)

    def test_new_lines_are_filtered_as_they_arrive(self):
        src = self.feed("a 1\nb 2\n")
        w = self.window([src])
        self.set_filter(w, include="^a")
        with (self.dir / "app.log").open("a") as f:
            f.write("b 3\na 4\n")
        self.assertTrue(until(lambda: w.shown == ["a 1", "a 4"]))
        self.assertEqual(len(w.buffer), 4)

    def test_a_bad_pattern_keeps_the_last_good_filter(self):
        w = self.window([self.feed("one\ntwo\n")])
        self.assertTrue(until(lambda: len(w.shown) == 2))
        self.set_filter(w, include="one")
        self.assertEqual(w.shown, ["one"])
        self.set_filter(w, include="(")
        self.assertEqual(w.shown, ["one"])
        self.assertIn("error", w.include.get_style_context().list_classes())
        self.assertNotIn("error", w.exclude.get_style_context().list_classes())
        self.set_filter(w, include="two")
        self.assertEqual(w.shown, ["two"])
        self.assertNotIn("error", w.include.get_style_context().list_classes())

    def test_pause_freezes_the_display_and_resume_catches_up(self):
        w = self.window([self.feed("one\ntwo\n")])
        self.assertTrue(until(lambda: len(w.shown) == 2))
        w.pause.set_active(True)
        with (self.dir / "app.log").open("a") as f:
            f.write("three\nfour\n")
        self.assertTrue(until(lambda: len(w.buffer) == 4))
        self.assertEqual(w.shown, ["one", "two"])                        # frozen
        self.assertIn("PAUSED: 2 new", w.status.get_text())
        self.set_filter(w, include="o")                                  # filtering while paused uses the snapshot
        self.assertEqual(w.shown, ["one", "two"])
        w.pause.set_active(False)
        pump(0.1)
        self.assertEqual(w.shown, ["one", "two", "four"])               # 'three' has no 'o'... and 'four' does
        self.assertNotIn("PAUSED", w.status.get_text())

    def test_find_marks_counts_and_steps(self):
        w = self.window([self.feed("one two\ntwo\nnone\nTWO two\n")])
        self.assertTrue(until(lambda: len(w.shown) == 4))
        w.find.set_text("two")
        pump(0.4)
        self.assertEqual(len(w.matches), 4)
        self.assertEqual(w.find_count.get_text(), "1 of 4")
        w.step(1)
        w.step(1)
        self.assertEqual(w.find_count.get_text(), "3 of 4")
        a, b = w.iters(*w.matches[w.current])
        self.assertEqual(w.text.get_text(a, b, False), "TWO")
        self.assertTrue(a.has_tag(w.tags["current"]))
        w.step(1)
        w.step(1)                                                        # wraps
        self.assertEqual(w.find_count.get_text(), "1 of 4")
        w.step(-1)
        self.assertEqual(w.find_count.get_text(), "4 of 4")
        w.match_case.set_active(True)
        pump(0.4)
        self.assertEqual(len(w.matches), 3)
        w.find.set_text("zzz")
        pump(0.4)
        self.assertEqual(w.find_count.get_text(), "no matches")
        w.find.set_text("(")
        pump(0.4)
        self.assertEqual(w.find_count.get_text(), "bad pattern")
        w.find.set_text("")
        pump(0.4)
        self.assertEqual(w.find_count.get_text(), "")
        begin, end = w.text.get_bounds()
        self.assertFalse(begin.has_tag(w.tags["match"]))

    def test_find_marks_lines_that_arrive_later(self):
        w = self.window([self.feed("needle 1\nhay\n")])
        self.assertTrue(until(lambda: len(w.shown) == 2))
        w.find.set_text("needle")
        pump(0.4)
        self.assertEqual(len(w.matches), 1)
        with (self.dir / "app.log").open("a") as f:
            f.write("hay\nneedle 2\n")
        self.assertTrue(until(lambda: len(w.matches) == 2))
        self.assertEqual(w.matches[1][0], 3)

    def test_follow_stops_when_scrolling_up_and_resumes_at_the_end(self):
        w = self.window([self.feed("".join(f"line {i}\n" for i in range(400)))])
        w.set_default_size(600, 200)
        self.assertTrue(until(lambda: len(w.shown) == 400))
        pump(0.3)
        self.assertTrue(w.follow.get_active())
        adj = w.adjustment
        self.assertGreaterEqual(adj.get_value(), adj.get_upper() - adj.get_page_size() - 2)
        adj.set_value(0)
        pump(0.1)
        self.assertFalse(w.follow.get_active())
        with (w.dir if False else self.dir / "app.log").open("a") as f:
            f.write("late\n")
        self.assertTrue(until(lambda: len(w.shown) == 401))
        self.assertEqual(adj.get_value(), 0)                             # still where the user left it
        adj.set_value(adj.get_upper() - adj.get_page_size())
        pump(0.1)
        self.assertTrue(w.follow.get_active())

    def test_the_window_keeps_only_the_newest_lines(self):
        old = logs.MAX_LINES
        logs.MAX_LINES = 50
        try:
            w = self.window([logs.LogSource("c", "C", cmd="seq 1 120")])
            self.assertTrue(until(lambda: w.stream.ended.is_set() and w.stream.pending() == 0 and len(w.shown) == 50))
        finally:
            logs.MAX_LINES = old
        self.assertEqual(w.shown[0], "71")
        self.assertEqual(w.shown[-1], "120")
        self.assertEqual(w.text.get_line_count() - 1, 50)

    def test_switching_source_ends_the_old_one(self):
        a = self.feed("from a\n", "a")
        b = self.feed("from b\n", "b")
        w = self.window([a, b])
        self.assertTrue(until(lambda: w.shown == ["from a"]))
        pid = w.stream._proc.pid
        w.source_box.set_active_id("b")
        self.assertTrue(until(lambda: w.shown == ["from b"]))
        self.assertFalse(os.path.exists(f"/proc/{pid}") and open(f"/proc/{pid}/stat").read().split()[2] != "Z")

    def test_starting_on_a_named_source(self):
        w = self.window([self.feed("x\n", "a"), self.feed("y\n", "b")], start="b")
        self.assertTrue(until(lambda: w.shown == ["y"]))

    def test_a_command_that_ends_is_reported(self):
        w = self.window([logs.LogSource("c", "C", cmd="echo hi; exit 3")])
        self.assertTrue(until(lambda: "ended (exit 3)" in w.status.get_text()))
        self.assertEqual(w.shown, ["hi"])

    def test_closing_the_window_ends_every_process(self):
        marker = f"lpcp-view-{os.getpid()}"
        w = self.window([logs.LogSource("c", "C", cmd=f"sleep 400 & echo {marker}; exec sleep 401")])
        self.assertTrue(until(lambda: w.shown == [marker]))
        self.assertTrue(alive("sleep 40[01]"))
        w.destroy()
        self.assertTrue(until(lambda: not alive("sleep 40[01]"), 5), "the log command was left running")

    def test_clear_copy_and_save(self):
        w = self.window([self.feed("keep\ndrop\n")])
        self.assertTrue(until(lambda: len(w.shown) == 2))
        self.set_filter(w, include="keep")
        target = self.dir / "saved.log"
        w.write_shown(target)
        self.assertEqual(target.read_text(), "keep\n")
        w.copy()
        self.assertEqual(Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_text(), "keep\n")
        w.clear()
        self.assertEqual((w.shown, len(w.buffer)), ([], 0))

    def test_application_id_is_per_project(self):
        self.assertEqual(view.application_id("my-blog"), "com.antzcode.lpcp.logs.p_my_blog")
        self.assertEqual(view.application_id("3d"), "com.antzcode.lpcp.logs.p_3d")


if __name__ == "__main__":
    unittest.main()
