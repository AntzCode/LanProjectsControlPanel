# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 AntzCode
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import lpcp_logs as logs  # noqa: E402


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def collect(stream, count, timeout=5.0):
    got = []
    wait_for(lambda: len(got.extend(stream.drain()) or got) >= count, timeout)
    return got


class Parse(unittest.TestCase):
    def test_commands_and_files(self):
        got = logs.parse_log_sources('[{"id":"app","title":" App ","cmd":"docker compose logs -f"},'
                                     '{"id":"apache-error","title":"Apache","file":"data/logs/error.log"}]')
        self.assertEqual(got, [logs.LogSource("app", "App", cmd="docker compose logs -f"),
                               logs.LogSource("apache-error", "Apache", file="data/logs/error.log")])
        self.assertEqual([s.kind for s in got], ["cmd", "file"])

    def test_invalid(self):
        for text in ("nope", "{}", "[]", "[1]",
                     '[{"title":"T","cmd":"x"}]',                                   # no id
                     '[{"id":"A b","title":"T","cmd":"x"}]',                        # bad id
                     '[{"id":"a","cmd":"x"}]',                                      # no title
                     '[{"id":"a","title":"T"}]',                                    # neither cmd nor file
                     '[{"id":"a","title":"T","cmd":"x","file":"y"}]',               # both
                     '[{"id":"a","title":"T","cmd":""}]',                           # empty
                     '[{"id":"a","title":"T","cmd":"x"},{"id":"a","title":"U","cmd":"y"}]'):  # duplicate id
            with self.subTest(text=text), self.assertRaises(logs.LogError):
                logs.parse_log_sources(text)

    def test_argv(self):
        project = Path("/srv/blog")
        argv, shell = logs.source_argv(logs.LogSource("a", "A", cmd="docker compose logs -f"), project)
        self.assertEqual((argv, shell), ("docker compose logs -f", True))
        argv, shell = logs.source_argv(logs.LogSource("e", "E", file="data/e.log"), project, tail=50)
        self.assertEqual((argv, shell), (["tail", "-n", "50", "-F", "--", "/srv/blog/data/e.log"], False))
        argv, _ = logs.source_argv(logs.LogSource("e", "E", file="/var/log/x.log"), project)
        self.assertEqual(argv[-1], "/var/log/x.log")
        argv, _ = logs.source_argv(logs.LogSource("e", "E", file="~/x.log"), project)
        self.assertEqual(argv[-1], str(Path("~/x.log").expanduser()))


class Lines(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(logs.clean(b"plain\r\n"), "plain")
        self.assertEqual(logs.clean(b"\x1b[31mred\x1b[0m text\n"), "red text")
        self.assertEqual(logs.clean(b"bad \xff byte\n"), "bad � byte")
        self.assertEqual(logs.clean(b"progress\r50%\r100%\n"), "progress50%100%")

    def test_buffer_cap(self):
        buf = logs.LineBuffer(maxlen=3)
        buf.extend(["a", "b"])
        buf.extend(["c", "d", "e"])
        self.assertEqual(list(buf.lines), ["c", "d", "e"])
        self.assertEqual(buf.dropped, 2)
        buf.clear()
        self.assertEqual((len(buf), buf.dropped), (0, 0))

    def test_level(self):
        self.assertEqual(logs.level("[php:error] boom"), "error")
        self.assertEqual(logs.level("PHP Fatal error: x"), "error")
        self.assertEqual(logs.level("Warning: x"), "warn")
        self.assertIsNone(logs.level("GET / 200"))
        self.assertIsNone(logs.level("terrorist"))      # whole words only


class Filtering(unittest.TestCase):
    LINES = ["GET /a 200", "GET /b 404", "POST /a 500", "get /c 200"]

    def shown(self, **kw):
        flt = logs.LineFilter(**kw)
        return [line for line in self.LINES if flt(line)]

    def test_include_exclude(self):
        self.assertEqual(self.shown(), self.LINES)
        self.assertEqual(self.shown(include="/a"), ["GET /a 200", "POST /a 500"])
        self.assertEqual(self.shown(exclude=" 200"), ["GET /b 404", "POST /a 500"])
        self.assertEqual(self.shown(include="/a", exclude="POST"), ["GET /a 200"])
        self.assertEqual(self.shown(include=r"^(GET|POST) /a"), ["GET /a 200", "POST /a 500"])

    def test_case(self):
        self.assertEqual(self.shown(include="GET"), ["GET /a 200", "GET /b 404"])
        self.assertEqual(self.shown(include="GET", ignore_case=True), ["GET /a 200", "GET /b 404", "get /c 200"])

    def test_bad_pattern(self):
        with self.assertRaises(logs.LogError) as e:
            logs.LineFilter(include="(")
        self.assertIn("include", str(e.exception))
        with self.assertRaises(logs.LogError) as e:
            logs.LineFilter(exclude="[")
        self.assertIn("exclude", str(e.exception))

    def test_find(self):
        lines = ["one two", "two", "none", "TWO two"]
        self.assertEqual(logs.find_matches(lines, "two"), [(0, 4, 7), (1, 0, 3), (3, 0, 3), (3, 4, 7)])
        self.assertEqual(logs.find_matches(lines, "two", ignore_case=False), [(0, 4, 7), (1, 0, 3), (3, 4, 7)])
        self.assertEqual(logs.find_matches(lines, ""), [])
        self.assertEqual(logs.find_matches(lines, "x*"), [])           # only empty matches
        with self.assertRaises(logs.LogError):
            logs.find_matches(lines, "(")

    def test_find_is_capped(self):
        self.assertEqual(len(logs.find_matches(["a"] * (logs.MAX_MATCHES + 10), "a")), logs.MAX_MATCHES)


class Streams(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.streams = []

    def tearDown(self):
        for s in self.streams:
            s.stop()

    def open(self, source, **kw):
        s = logs.Stream(source, self.dir, **kw)
        self.streams.append(s)
        return s

    def test_command_runs_in_the_project_with_its_environment(self):
        s = self.open(logs.LogSource("c", "C", cmd="echo $LPCP_PROJECT; pwd; echo oops >&2"),
                      env={"LPCP_PROJECT": "blog"})
        got = collect(s, 3)
        self.assertEqual(sorted(got), sorted(["blog", os.path.realpath(self.dir), "oops"]))
        self.assertTrue(s.ended.wait(5))
        self.assertEqual(s.returncode, 0)

    def test_command_exit_code(self):
        s = self.open(logs.LogSource("c", "C", cmd="echo hi; exit 3"))
        self.assertTrue(s.ended.wait(5))
        self.assertEqual((s.drain(), s.returncode), (["hi"], 3))

    def test_follow_filters_until_the_stream_ends(self):
        s = self.open(logs.LogSource("c", "C", cmd="printf 'a 1\\nb 2\\na 3\\n'"))
        out = []
        rc = logs.follow(s, logs.LineFilter(include="^a"), out.append)
        self.assertEqual((out, rc), (["a 1", "a 3"], 0))

    def test_file_shows_the_tail_then_follows(self):
        path = self.dir / "e.log"
        path.write_text("".join(f"old {i}\n" for i in range(10)))
        s = self.open(logs.LogSource("e", "E", file="e.log"), tail=3)
        self.assertEqual(collect(s, 3), ["old 7", "old 8", "old 9"])
        with path.open("a") as f:
            f.write("new 1\nnew 2\n")
        self.assertEqual(collect(s, 2), ["new 1", "new 2"])

    def test_file_that_appears_later(self):
        s = self.open(logs.LogSource("e", "E", file="later.log"))
        time.sleep(0.3)
        (self.dir / "later.log").write_text("hello\n")
        got = collect(s, 3)                         # tail's "cannot open" and "has appeared" notices come first
        self.assertEqual(got[-1], "hello")
        self.assertIn("cannot open", got[0])

    def test_big_file_is_not_read_whole(self):
        path = self.dir / "big.log"
        with path.open("w") as f:
            for i in range(200000):
                f.write(f"line {i} " + "x" * 40 + "\n")
        s = self.open(logs.LogSource("b", "B", file="big.log"), tail=5)
        got = collect(s, 5)
        self.assertEqual(got[-1], "line 199999 " + "x" * 40)
        time.sleep(0.2)
        self.assertEqual(s.pending(), 0)            # the file's other 199995 lines were never read

    def test_stop_ends_the_whole_process_group(self):
        marker = f"lpcp-test-{os.getpid()}-{time.monotonic_ns()}"
        s = self.open(logs.LogSource("c", "C", cmd=f"sleep 300 & echo {marker}; exec sleep 301"))
        self.assertEqual(collect(s, 1), [marker])
        def alive():
            return subprocess.run(["pgrep", "-f", "sleep 30[01]"], capture_output=True).stdout.strip()
        self.assertTrue(alive())
        started = time.monotonic()
        s.stop()
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(s.ended.is_set())
        self.assertTrue(wait_for(lambda: not alive(), 3), "a child of the source was left running")
        s.stop()                                    # again: harmless

    def test_stop_a_following_tail(self):
        (self.dir / "t.log").write_text("x\n")
        s = self.open(logs.LogSource("t", "T", file="t.log"))
        collect(s, 1)
        pid = s._proc.pid
        s.stop()
        self.assertTrue(wait_for(lambda: not Path(f"/proc/{pid}").exists(), 3))

    def test_a_command_that_cannot_start(self):
        s = logs.Stream(logs.LogSource("c", "C", cmd="true"), self.dir / "missing")
        self.streams.append(s)
        self.assertTrue(s.ended.is_set())
        self.assertEqual(s.returncode, 127)
        self.assertTrue(s.error)

    def test_a_slow_reader_loses_nothing(self):
        s = self.open(logs.LogSource("c", "C", cmd="seq 1 5000"))
        self.assertTrue(s.ended.wait(5))
        got = []
        done = threading.Event()
        def consume():
            while s.pending():
                got.extend(s.drain(100))
            done.set()
        threading.Thread(target=consume).start()
        self.assertTrue(done.wait(5))
        self.assertEqual(got, [str(i) for i in range(1, 5001)])


if __name__ == "__main__":
    unittest.main()
