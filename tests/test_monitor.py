# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 AntzCode
"""The panel's logic (no GTK, no display, no docker: docker's output is faked)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import lpcp  # noqa: E402
import lpcp_monitor as mon  # noqa: E402


class Parsing(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(mon.parse_bytes("20.71MiB"), 20.71 * 1024 ** 2)
        self.assertEqual(mon.parse_bytes("1.5GiB"), 1.5 * 1024 ** 3)
        self.assertEqual(mon.parse_bytes("512kB"), 512e3)
        self.assertEqual(mon.parse_bytes("0B"), 0)
        self.assertEqual(mon.parse_bytes("--"), 0)
        self.assertEqual(mon.parse_bytes(""), 0)

    def test_cpu(self):
        self.assertEqual(mon.parse_cpu("12.34%"), 12.34)
        self.assertEqual(mon.parse_cpu("--"), 0)

    def test_published(self):
        ports = "0.0.0.0:443->443/tcp, [::]:443->443/tcp, 127.0.0.2:8080->80/tcp, 5432/tcp, 0.0.0.0:53->53/udp"
        self.assertEqual(mon.parse_published(ports),
                         {("0.0.0.0", 443), ("[::]", 443), ("127.0.0.2", 8080), ("0.0.0.0", 53)})
        self.assertEqual(mon.parse_published(""), set())
        # docker collapses adjacent ports (seen with Docker 29)
        self.assertEqual(mon.parse_published("80/tcp, 127.0.0.9:3000-3002->3000-3002/tcp, 127.0.0.9:3005->3005/tcp"),
                         {("127.0.0.9", 3000), ("127.0.0.9", 3001), ("127.0.0.9", 3002), ("127.0.0.9", 3005)})

    def test_ps_and_stats(self):
        ps = mon.parse_ps("0123456789abcdef\t/srv/a\t127.0.0.2:80->80/tcp\nfedcba987654\t\t\n")
        self.assertEqual([(c.id, c.workdir, c.published) for c in ps],
                         [("0123456789ab", "/srv/a", {("127.0.0.2", 80)}), ("fedcba987654", "", set())])
        stats = mon.parse_stats("0123456789ab\t1.50%\t100MiB / 62.76GiB\nbad line\n")
        self.assertEqual(stats, {"0123456789ab": (1.5, 100 * 1024 ** 2)})

    def test_belongs(self):
        self.assertTrue(mon.belongs("/srv/a", Path("/srv/a")))
        self.assertTrue(mon.belongs("/srv/a/infra", Path("/srv/a")))   # compose file in a subfolder
        self.assertFalse(mon.belongs("/srv/ab", Path("/srv/a")))
        self.assertFalse(mon.belongs("", Path("/srv/a")))

    def test_web_addresses(self):
        c = [mon.Container("x", "/a", {("0.0.0.0", 443), ("[::]", 443), ("127.0.0.1", 5432)})]
        self.assertEqual(mon.web_addresses(c, 443, "192.168.1.5"), ["192.168.1.5"])
        self.assertEqual(mon.web_addresses(c, 443, None), ["127.0.0.1"])
        self.assertEqual(mon.web_addresses(c, 80, "127.0.0.2"), [])

    def test_state(self):
        self.assertEqual(mon.derive_state(True, True, None)[0], "green")
        self.assertEqual(mon.derive_state(True, True, "start failed")[0], "green")   # running wins
        self.assertEqual(mon.derive_state(True, False, None)[0], "red")
        self.assertEqual(mon.derive_state(False, False, "start failed"), ("red", "start failed"))
        self.assertEqual(mon.derive_state(False, False, None)[0], "off")

    def test_failure_line(self):
        lines = ["p1: starting on 127.0.0.2 ...", "Error response from daemon: boom", "p1: start command failed"]
        self.assertEqual(mon.failure_line("p1", lines), "start command failed")
        self.assertEqual(mon.failure_line("p1", ["error: not registered: p1 (see ./dev ls)"]),
                         "error: not registered: p1 (see ./dev ls)")
        self.assertEqual(mon.failure_line("p1", ["something odd"]), "something odd")
        self.assertEqual(mon.failure_line("p1", []), "failed")


class Snapshot(unittest.TestCase):
    """Monitor.snapshot() against a temporary registry and faked docker output."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for d in ("web", "lan", "idle"):
            (root / d).mkdir()
        (root / "projects.toml").write_text(f"""
[projects.web]
dir = "{root / 'web'}"
bind = "127.0.0.2"
[projects.lan]
dir = "{root / 'lan'}"
bind = "192.168.1.5"
port = 443
proxy = false
[projects.idle]
dir = "{root / 'idle'}"
bind = "127.0.0.3"
""")
        self.root = root
        env = mock.patch.dict(os.environ, {"LPCP_HOME": str(root)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_snapshot(self, ps: str, stats: str, answering: set, failures=None):
        def docker(args, timeout=15):
            return ps if args[0] == "ps" else stats
        with mock.patch.object(mon, "docker", docker), \
                mock.patch.object(lpcp, "listening", lambda port, host="127.0.0.1": (host, port) in answering):
            return mon.Monitor(failures).snapshot()

    def test_states_and_usage(self):
        r = self.root
        ps = (f"aaaaaaaaaaaa\t{r / 'web'}\t127.0.0.2:80->80/tcp, 127.0.0.2:5432->5432/tcp\n"
              f"bbbbbbbbbbbb\t{r / 'web'}\t\n"
              f"cccccccccccc\t{r / 'lan'}\t0.0.0.0:443->443/tcp, [::]:443->443/tcp\n"
              f"dddddddddddd\t{r / 'idle' / 'tests'}\t127.0.0.1:5433->5432/tcp\n"
              f"eeeeeeeeeeee\t/elsewhere\t0.0.0.0:80->80/tcp\n")
        stats = ("aaaaaaaaaaaa\t10.25%\t1GiB / 62GiB\nbbbbbbbbbbbb\t2.00%\t512MiB / 62GiB\n"
                 "cccccccccccc\t0.50%\t100MB / 62GB\ndddddddddddd\t1.00%\t50MB / 62GB\n")
        snap = self.run_snapshot(ps, stats, answering={("127.0.0.2", 80)})
        rows = {row.name: row for row in snap.rows}
        self.assertIsNone(snap.error)
        self.assertTrue(snap.stats)
        web, lan, idle = rows["web"], rows["lan"], rows["idle"]
        self.assertEqual((web.state, web.action, web.containers), ("green", "down", 2))
        self.assertAlmostEqual(web.cpu, 12.25)
        self.assertAlmostEqual(web.mem_gb, (1024 ** 3 + 512 * 1024 ** 2) / 1e9)
        # Published on 0.0.0.0 but nothing answers on its address: red, and a click stops it.
        self.assertEqual((lan.state, lan.action), ("red", "down"))
        # Only a test container from a subfolder runs: off (with usage), and a click starts it.
        self.assertEqual((idle.state, idle.action, idle.containers), ("off", "up", 1))
        self.assertIn("1 other container", idle.why)
        self.assertAlmostEqual(idle.mem_gb, 0.05)

    def test_nothing_running(self):
        snap = self.run_snapshot("", "", answering=set())
        self.assertEqual([(r.state, r.action, r.cpu, r.mem_gb) for r in snap.rows], [("off", "up", None, None)] * 3)

    def test_failure_shows_red_until_running(self):
        failures = {"idle": "start failed: port 80 is held", "web": "start failed: old"}
        r = self.root
        snap = self.run_snapshot(f"aaaaaaaaaaaa\t{r / 'web'}\t127.0.0.2:80->80/tcp\n", "",
                                 answering={("127.0.0.2", 80)}, failures=failures)
        rows = {row.name: row for row in snap.rows}
        self.assertEqual((rows["idle"].state, rows["idle"].why, rows["idle"].action),
                         ("red", "start failed: port 80 is held", "up"))
        self.assertEqual(rows["web"].state, "green")
        self.assertNotIn("web", failures)          # cleared once it runs

    def test_registry_error_is_reported(self):
        (self.root / "projects.toml").write_text("[settings]\nport_range = [1, 2]\n")
        snap = self.run_snapshot("", "", answering=set())
        self.assertEqual(snap.rows, [])
        self.assertIn("projects.toml", snap.error)

    def test_docker_unreachable(self):
        def docker(args, timeout=15):
            raise lpcp.LpcpError("Cannot connect to the Docker daemon")
        with mock.patch.object(mon, "docker", docker):
            snap = mon.Monitor().snapshot()
        self.assertIn("Docker is not reachable", snap.error)
        self.assertEqual({r.state for r in snap.rows}, {"off"})


class Panic(unittest.TestCase):
    """Ctrl+Alt+click on the tray icon kills the registered project using the most CPU."""

    def row(self, name, cpu, ids):
        return mon.Row(name, "green" if ids else "off", "", "down", None, "", cpu=cpu, ids=ids)

    def test_target(self):
        rows = [self.row("a", 30.0, ["a1"]), self.row("b", 180.5, ["b1", "b2"]), self.row("c", None, [])]
        self.assertEqual(mon.panic_target(rows).name, "b")
        self.assertIsNone(mon.panic_target([self.row("a", 4.9, ["a1"])]))   # nothing busy: kill nothing
        self.assertIsNone(mon.panic_target([self.row("c", None, [])]))

    def run_panic(self, rows):
        killed = []

        def docker(args, timeout=15):
            killed.append(args)
            return ""
        monitor = mock.Mock()
        monitor.snapshot.return_value = mon.Snapshot(rows=rows, stats=True)
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"LPCP_HOME": home}), \
                mock.patch.object(mon, "docker", docker):
            message = mon.panic(monitor)
            logs = {f.stem: f.read_text() for f in Path(home, ".state/logs").glob("*.log")}
        monitor.snapshot.assert_called_once_with(stats=True)      # fresh CPU figures, not the last refresh's
        return message, killed, logs

    def test_kills_the_busiest(self):
        message, killed, logs = self.run_panic([self.row("a", 30.0, ["a1"]), self.row("b", 180.5, ["b1", "b2"])])
        self.assertEqual(killed, [["kill", "b1", "b2"]])
        self.assertIn("killed b (180% CPU, 2 containers; all: a 30.0%, b 180.5%)", message)
        self.assertIn("./dev up b", message)
        self.assertEqual(sorted(logs), ["b", "panic"])            # the panic log and the killed project's log
        self.assertIn(message, logs["panic"])
        self.assertIn(message, logs["b"])

    def test_nothing_busy(self):
        message, killed, logs = self.run_panic([self.row("a", 1.2, ["a1"])])
        self.assertEqual(killed, [])
        self.assertIn("nothing killed", message)
        self.assertIn("(a 1.2%)", message)
        self.assertEqual(sorted(logs), ["panic"])                 # logged even when nothing was killed
        self.assertIn(message, logs["panic"])


class DownNamed(unittest.TestCase):
    def test_named_site_is_stopped_even_when_nothing_answers(self):
        reg = lpcp.parse_registry('[projects.a]\ndir = "/tmp"\nbind = "127.0.0.2"\n'
                                  '[projects.b]\ndir = "/tmp"\nbind = "127.0.0.3"\n')
        ran = []
        with mock.patch.object(lpcp, "listening", lambda *a, **k: False), \
                mock.patch.object(lpcp, "run_project", lambda p, *a: ran.append(p.name) or 0), \
                mock.patch.object(lpcp, "sync_proxy", lambda *a, **k: "stopped"), \
                mock.patch.object(lpcp, "load_addresses", dict):
            self.assertEqual(lpcp.cmd_down(reg, SimpleNamespace(sites=["a"])), 0)
            self.assertEqual(ran, ["a"])
            lpcp.cmd_down(reg, SimpleNamespace(sites=[]))     # all: only running ones
            self.assertEqual(ran, ["a"])

    def test_named_site_without_an_address(self):
        """An auto project never started by lpcp (started by hand) has no address; stopping it must still work."""
        with tempfile.TemporaryDirectory() as home:
            reg = lpcp.parse_registry(f'[projects.solo]\ndir = "{home}"\ndown = "true"\n')
            with mock.patch.dict(os.environ, {"LPCP_HOME": home}), \
                    mock.patch.object(lpcp, "sync_proxy", lambda *a, **k: "stopped"):
                self.assertEqual(lpcp.cmd_down(reg, SimpleNamespace(sites=["solo"])), 0)


class Worker(unittest.TestCase):
    def test_survives_an_exception(self):
        import threading
        done = threading.Event()
        actions = mon.Actions(on_change=done.set)
        calls = []

        def run(name, verb):
            calls.append(name)
            if name == "bad":
                raise OSError("disk full")
            return True, "started"
        actions.run = run
        actions.request("bad", "up")
        self.assertTrue(done.wait(5))
        self.assertEqual(actions.failures, {"bad": "start failed: disk full"})
        done.clear()
        actions.request("good", "up")
        self.assertTrue(done.wait(5))
        self.assertEqual((calls, actions.busy), (["bad", "good"], {}))


class Placement(unittest.TestCase):
    """Where a launch puts the window: by the panel launcher the pointer is on."""

    SIZE = (452, 120)
    PRIMARY = (0, 0, 1920, 1200)          # a 34 px top panel: work area known
    PRIMARY_WA = (0, 34, 1920, 1166)
    SECOND = (1920, 0, 1920, 1080)        # second monitor: GTK reports no work area (= geometry)

    def place(self, pointer, geometry=PRIMARY, workarea=PRIMARY_WA):
        import lpcp_ui
        return lpcp_ui.place(pointer, geometry, workarea, self.SIZE)

    def test_top_panel(self):
        self.assertEqual(self.place((700, 17)), ("NORTH_WEST", 700 - 226, 34))   # centred under the icon
        self.assertEqual(self.place((40, 17)), ("NORTH_WEST", 0, 34))            # kept on screen
        self.assertEqual(self.place((1910, 17)), ("NORTH_WEST", 1920 - 452, 34))

    def test_other_edges(self):
        geometry = (0, 0, 1920, 1200)
        self.assertEqual(self.place((700, 1190), geometry, (0, 0, 1920, 1176)), ("SOUTH_WEST", 474, 1176))
        self.assertEqual(self.place((10, 500), geometry, (24, 0, 1896, 1200)), ("NORTH_WEST", 24, 440))
        self.assertEqual(self.place((1910, 500), geometry, (0, 0, 1896, 1200)), ("NORTH_EAST", 1896, 440))

    def test_launched_from_a_menu_or_terminal(self):
        self.assertIsNone(self.place((700, 400)))
        self.assertIsNone(self.place((2500, 400), self.SECOND, self.SECOND))

    def test_monitor_without_a_work_area(self):
        # pointer mid-panel at y=17: a panel 35 px thick is assumed
        self.assertEqual(self.place((2500, 17), self.SECOND, self.SECOND), ("NORTH_WEST", 2500 - 226, 35))
        self.assertEqual(self.place((3830, 17), self.SECOND, self.SECOND), ("NORTH_WEST", 3840 - 452, 35))


class TrayAnchor(unittest.TestCase):
    """A tray click reports the panel's inner edge at the icon and the panel's side (as Mint's applet does)."""

    MONITOR = (1920, 0, 1920, 1080)
    SIZE = (452, 120)

    def anchor(self, point, side):
        import lpcp_ui
        return lpcp_ui.anchor(point, side, self.MONITOR, self.SIZE)

    def test_sides(self):
        self.assertEqual(self.anchor((2500, 34), "top"), ("NORTH_WEST", 2500, 34))          # drops from the icon
        self.assertEqual(self.anchor((3800, 34), "top"), ("NORTH_WEST", 3840 - 452, 34))    # kept on the monitor
        self.assertEqual(self.anchor((2500, 1046), "bottom"), ("SOUTH_WEST", 2500, 1046))
        self.assertEqual(self.anchor((1954, 500), "left"), ("NORTH_WEST", 1954, 500))
        self.assertEqual(self.anchor((3806, 1050), "right"), ("NORTH_EAST", 3806, 1080 - 120))

    def test_panel_positions(self):
        import lpcp_ui
        self.assertEqual([lpcp_ui.SIDES[n] for n in range(4)], ["left", "right", "top", "bottom"])


class Launcher(unittest.TestCase):
    def test_install_with_and_without_tray(self):
        import lpcp_ui
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": f"{tmp}/data", "XDG_CONFIG_HOME": f"{tmp}/config",
                                             "LPCP_HOME": ""}):
            menu, autostart = Path(tmp, "data/applications/lpcp.desktop"), Path(tmp, "config/autostart/lpcp.desktop")
            lpcp_ui.install_launcher(tray=True)
            self.assertIn(" ui --tray\n", menu.read_text())
            self.assertEqual(autostart.read_text(), menu.read_text())
            lpcp_ui.install_launcher(tray=False)
            self.assertIn(" ui\n", menu.read_text())
            self.assertFalse(autostart.exists())        # back to a plain launcher: no icon at login

    def test_desktop_entry(self):
        import lpcp_ui  # imports without GTK: GTK loads only when the window opens
        with mock.patch.dict(os.environ, {"LPCP_HOME": ""}):
            entry = lpcp_ui.desktop_entry()
        self.assertIn(f'Exec="{lpcp_ui.REPO / "dev"}" ui\n', entry)
        self.assertIn(f"Icon={lpcp_ui.ICON}\n", entry)
        self.assertIn("StartupWMClass=lpcp\n", entry)
        self.assertTrue(lpcp_ui.ICON.is_file())


if __name__ == "__main__":
    unittest.main()
