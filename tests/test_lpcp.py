# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 AntzCode
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import lpcp  # noqa: E402

REGISTRY = """
[settings]
address_range = ["127.0.0.2", "127.0.0.4"]

[projects.nan]
dir = "~/nan"

[projects.blog]
dir = "/srv/blog"
host = "journal.localhost"
bind = "127.0.0.50"
port = 3000
up = "BIND={bind} docker compose up -d"

[projects.st]
dir = "/srv/st"
bind = "lan"
port = 443
proxy = false
url = "https://dev.scripturetips.com"
"""


class Registry(unittest.TestCase):
    def test_parse(self):
        reg = lpcp.parse_registry(REGISTRY)
        nan, blog, st = reg.projects["nan"], reg.projects["blog"], reg.projects["st"]
        self.assertEqual((nan.host, nan.bind, nan.port, nan.proxy), ("nan.localhost", "auto", 80, True))
        self.assertEqual(nan.up, "./dev up --bind={bind}")
        self.assertEqual(nan.dir, Path("~/nan").expanduser())
        self.assertEqual((blog.host, blog.bind, blog.port), ("journal.localhost", "127.0.0.50", 3000))
        self.assertEqual((st.bind, st.proxy, st.url), ("lan", False, "https://dev.scripturetips.com"))
        self.assertEqual(reg.settings["bind"], "127.0.0.1")

    def test_rejects_bad_input(self):
        bad = [
            '[projects.Bad_Name]\ndir="/x"',
            '[projects.x]\nhost="x"',
            '[settings]\nnope=1',
            '[projects.x]\ndir="/x"\nbind="0.0.0.0"',
            '[projects.x]\ndir="/x"\nbind="somewhere"',
            '[projects.x]\ndir="/x"\nport=0',
            '[projects.x]\ndir="/x"\nproxy="no"',
            '[projects.x]\ndir="/x"\ntypo=1',
            '[settings]\naddress_range=["10.0.0.2","10.0.0.9"]',
            '[settings]\naddress_range=["127.0.0.9","127.0.0.2"]',
        ]
        for text in bad:
            with self.assertRaises(lpcp.LpcpError, msg=text):
                lpcp.parse_registry(text)

    def test_old_registries_get_a_clear_error(self):
        with self.assertRaisesRegex(lpcp.LpcpError, "address_range"):
            lpcp.parse_registry('[settings]\nport_range=[8100, 8999]')
        with self.assertRaisesRegex(lpcp.LpcpError, r"--bind=\{bind\}"):
            lpcp.parse_registry('[projects.x]\ndir="/x"\nup="./dev up -p {port}"')

    def test_add_then_remove_round_trips(self):
        text = lpcp.append_project("", "nan", "/p/nan")
        text = lpcp.append_project(text, "st", "/p/st", {"bind": "lan", "port": 443, "proxy": False,
                                                         "url": "https://x.example", "host": None})
        reg = lpcp.parse_registry(text)
        self.assertEqual(list(reg.projects), ["nan", "st"])
        st = reg.projects["st"]
        self.assertEqual((st.bind, st.port, st.proxy, st.url), ("lan", 443, False, "https://x.example"))
        self.assertNotIn("host", text)
        text = lpcp.remove_project(text, "nan")
        self.assertEqual(list(lpcp.parse_registry(text).projects), ["st"])
        with self.assertRaises(lpcp.LpcpError):
            lpcp.remove_project(text, "nan")
        with self.assertRaises(lpcp.LpcpError):
            lpcp.append_project(text, "st", "/x")

    def test_remove_keeps_neighbours_and_settings(self):
        reg = lpcp.parse_registry(lpcp.remove_project(REGISTRY, "nan"))
        self.assertEqual(list(reg.projects), ["blog", "st"])
        self.assertEqual(reg.settings["address_range"], ["127.0.0.2", "127.0.0.4"])


class Addresses(unittest.TestCase):
    def setUp(self):
        self.reg = lpcp.parse_registry(REGISTRY)

    def address(self, name, addresses, **kw):
        kw.setdefault("in_use", lambda port, ip: False)
        kw.setdefault("lan", lambda: "192.168.1.20")
        return lpcp.project_address(self.reg.projects[name], self.reg, addresses, **kw)

    def test_fixed_and_lan(self):
        self.assertEqual(self.address("blog", {}), "127.0.0.50")
        self.assertEqual(self.address("st", {}), "192.168.1.20")

    def test_assigned_once_and_remembered(self):
        addresses = {}
        self.assertEqual(self.address("nan", addresses), "127.0.0.2")
        self.assertEqual(addresses, {"nan": "127.0.0.2"})
        self.assertEqual(self.address("nan", addresses, in_use=lambda port, ip: True), "127.0.0.2")

    def test_skips_taken_and_busy_addresses(self):
        self.reg.projects["other"] = lpcp.Project("other", Path("/o"), "other.localhost")
        addresses = {"other": "127.0.0.2"}
        self.assertEqual(self.address("nan", addresses, in_use=lambda port, ip: ip == "127.0.0.3"), "127.0.0.4")

    def test_never_the_proxy_or_a_fixed_address(self):
        self.reg.settings["address_range"] = ["127.0.0.1", "127.0.0.2"]
        self.reg.projects["fixed"] = lpcp.Project("fixed", Path("/f"), "f.localhost", bind="127.0.0.2")
        with self.assertRaises(lpcp.LpcpError):
            self.address("nan", {})

    def test_not_assigned_when_only_looking(self):
        self.assertIsNone(self.address("nan", {}, assign=False))

    def test_exhausted(self):
        with self.assertRaises(lpcp.LpcpError):
            self.address("nan", {}, in_use=lambda port, ip: True)


class Rendering(unittest.TestCase):
    def setUp(self):
        self.reg = lpcp.parse_registry(REGISTRY)

    def test_fill_replaces_tokens_only(self):
        p = self.reg.projects["nan"]
        out = lpcp.fill("go --bind={bind} {host} {url} {name} ${X} {y}", p, "127.0.0.2", self.reg.settings)
        self.assertEqual(out, "go --bind=127.0.0.2 nan.localhost https://nan.localhost nan ${X} {y}")

    def test_urls(self):
        s = self.reg.settings
        self.assertEqual(lpcp.site_url(self.reg.projects["nan"], s, "127.0.0.2"), "https://nan.localhost")
        self.assertEqual(lpcp.site_url(self.reg.projects["st"], s, "192.168.1.20"),
                         "https://dev.scripturetips.com")
        direct = lpcp.Project("d", Path("/d"), "d.localhost", port=3000, proxy=False)
        self.assertEqual(lpcp.site_url(direct, s, "127.0.0.3"), "http://127.0.0.3:3000")
        s = dict(s, https_port=18443)
        self.assertEqual(lpcp.site_url(self.reg.projects["nan"], s), "https://nan.localhost:18443")
        s = dict(s, https=False)
        self.assertEqual(lpcp.site_url(self.reg.projects["nan"], s), "http://nan.localhost")

    def test_project_env(self):
        env = lpcp.project_env(self.reg.projects["nan"], "127.0.0.2", self.reg.settings)
        self.assertEqual(env, {"LPCP_PROJECT": "nan", "LPCP_BIND": "127.0.0.2", "LPCP_HOST": "nan.localhost",
                               "LPCP_URL": "https://nan.localhost"})

    def test_caddyfile(self):
        s = dict(lpcp.DEFAULTS, http_port=18080, https_port=18443)
        out = lpcp.render_caddyfile(s, [("b.localhost", "127.0.0.3:80"), ("a.localhost", "127.0.0.2:3000")])
        self.assertIn("\thttp_port 18080\n\thttps_port 18443\n", out)
        self.assertIn("\tdefault_bind 127.0.0.1\n", out)
        self.assertLess(out.index("a.localhost {"), out.index("b.localhost {"))
        self.assertIn("a.localhost {\n\ttls internal\n\treverse_proxy 127.0.0.2:3000\n}", out)

    def test_caddyfile_plain_http_and_empty(self):
        out = lpcp.render_caddyfile(dict(lpcp.DEFAULTS, https=False), [("a.localhost", "127.0.0.2:80")])
        self.assertIn("http://a.localhost {\n\treverse_proxy 127.0.0.2:80\n}", out)
        self.assertNotIn("tls internal", out)
        self.assertNotIn("reverse_proxy", lpcp.render_caddyfile(dict(lpcp.DEFAULTS), []))

    def test_lan_address_is_not_loopback(self):
        try:
            ip = lpcp.lan_address()
        except lpcp.LpcpError:
            self.skipTest("no default route on this machine")
        self.assertFalse(ip.startswith("127."))


class AddressInUse(unittest.TestCase):
    """A new project skips an address where anything listens, not only its own web port."""

    SS = ("LISTEN 0 4096 127.0.0.2:443 0.0.0.0:*\n"
          "LISTEN 0 4096 127.0.0.20:80 0.0.0.0:*\n"
          "LISTEN 0 4096 [::1]:631 [::]:*\n")

    def check(self, ip):
        from unittest import mock
        ss = mock.Mock(stdout=self.SS)
        with mock.patch.object(lpcp, "listening", lambda port, host="127.0.0.1": False), \
                mock.patch.object(lpcp.subprocess, "run", lambda *a, **k: ss):
            return lpcp.address_in_use(80, ip)

    def test_any_listener_takes_the_address(self):
        self.assertTrue(self.check("127.0.0.2"))      # its 443 is taken, though 80 is free
        self.assertFalse(self.check("127.0.0.3"))
        self.assertFalse(self.check("127.0.0.200"))   # no prefix matches


class Launchers(unittest.TestCase):
    """A project's launch buttons come from its `./dev config.launchers`; without them lpcp opens its URL."""

    def test_parse(self):
        got = lpcp.parse_launchers('[{"app":"site","title":"Open dev.scripture.tips in the browser"},'
                                   '{"app":"data","title":" Open the data browser "}]')
        self.assertEqual(got, [lpcp.Launcher("site", "Open dev.scripture.tips in the browser"),
                               lpcp.Launcher("data", "Open the data browser")])
        # The default may leave out "app": `./dev launch` without --app opens it.
        self.assertEqual(lpcp.parse_launchers('[{"title":"Open it"}]'), [lpcp.Launcher(None, "Open it")])

    def test_class(self):
        """A launcher's "class" picks its icon: web unless it says otherwise."""
        got = lpcp.parse_launchers('[{"app":"site","title":"S"},{"app":"mail","title":"M","class":"mail"},'
                                   '{"app":"data","title":"D","class":"db"}]')
        self.assertEqual([lch.kind for lch in got], ["web", "mail", "db"])
        self.assertEqual(lpcp.LAUNCHER_CLASSES, ("web", "mail", "db", "logs", "config", "tool"))
        # A well-formed class this lpcp doesn't know is kept (the panel draws it as a tool), not an error.
        (newer,) = lpcp.parse_launchers('[{"app":"x","title":"X","class":"metrics"}]')
        self.assertEqual(newer.kind, "metrics")

    def test_class_rejects(self):
        for bad in ('"Web"', '"a b"', '""', "3", "null", "[]"):
            with self.subTest(bad=bad), self.assertRaises(lpcp.LpcpError):
                lpcp.parse_launchers('[{"app":"site","title":"S","class":%s}]' % bad)

    def test_parse_rejects(self):
        for text in ("", "nope", "{}", "[]", '["x"]', '[{"app":"site"}]', '[{"app":"site","title":" "}]',
                     '[{"app":"Site","title":"x"}]', '[{"app":"a b","title":"x"}]',
                     '[{"app":"a","title":"x"},{"app":"a","title":"y"}]',
                     '[{"app":"a","title":"x"},{"title":"y"}]'):
            with self.subTest(text=text), self.assertRaises(lpcp.LpcpError):
                lpcp.parse_launchers(text)

    def project(self, script: str | None):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        if script is not None:
            (d / "dev").write_text("#!/usr/bin/env bash\n" + script)
            (d / "dev").chmod(0o755)
        return lpcp.Project("st", d, "st.localhost", url="https://dev.example.test")

    def test_from_the_project(self):
        p = self.project('[[ "$1" == config.launchers ]] || exit 2\n'
                         'printf \'[{"app":"site","title":"Open %s"}]\\n\' "$LPCP_URL"\n')
        self.assertEqual(lpcp.project_launchers(p, dict(lpcp.DEFAULTS), "192.168.1.5"),
                         [lpcp.Launcher("site", "Open https://dev.example.test")])   # LPCP_* are set

    def test_builtin_when_not_provided(self):
        settings = dict(lpcp.DEFAULTS)
        for script in (None, 'echo "usage: ./dev up | down" >&2; exit 1\n'):
            with self.subTest(script=script):
                (only,) = lpcp.project_launchers(self.project(script), settings)
                self.assertTrue(only.builtin)
                self.assertEqual((only.app, only.title), (None, "Open dev.example.test in the browser"))

    def test_invalid_output_is_an_error(self):
        with self.assertRaises(lpcp.LpcpError):
            lpcp.project_launchers(self.project("echo not json\n"), dict(lpcp.DEFAULTS))

    def test_launch(self):
        from unittest import mock
        p = self.project('case "$1" in\n'
                         '  config.launchers) echo \'[{"app":"site","title":"S"},{"app":"data","title":"D"}]\' ;;\n'
                         '  launch) echo "$*|$LPCP_URL" > launched ;;\n'
                         'esac\n')
        reg = lpcp.Registry(projects={"st": p})
        with mock.patch.object(lpcp, "load_addresses", lambda: {}):
            self.assertEqual(lpcp.launch(p, reg, "data"), 0)
            self.assertEqual((p.dir / "launched").read_text(), "launch --app=data|https://dev.example.test\n")
            self.assertEqual(lpcp.launch(p, reg), 0)
            self.assertEqual((p.dir / "launched").read_text(), "launch|https://dev.example.test\n")
            with self.assertRaises(lpcp.LpcpError):
                lpcp.launch(p, reg, "nope")

    def test_launch_builtin_opens_the_url(self):
        from unittest import mock
        p = self.project(None)
        reg = lpcp.Registry(projects={"st": p})
        with mock.patch.object(lpcp, "load_addresses", lambda: {}), \
                mock.patch.object(lpcp, "open_url") as opened:
            self.assertEqual(lpcp.launch(p, reg), 0)
            opened.assert_called_once_with("https://dev.example.test")
            with self.assertRaises(lpcp.LpcpError):
                lpcp.launch(p, reg, "data")      # no launchers: no apps to choose from


class LogSources(unittest.TestCase):
    """A project's logs come from its `./dev config.logs`; ./dev logs reads them with --list, --source, --grep."""

    def project(self, script):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        if script is not None:
            (d / "dev").write_text("#!/usr/bin/env bash\n" + script)
            (d / "dev").chmod(0o755)
        return lpcp.Project("blog", d, "blog.localhost")

    SCRIPT = ('[[ "$1" == config.logs ]] || exit 2\n'
              'cat <<\'EOF\'\n'
              '[{"id":"app","title":"App","cmd":"printf \'GET /a 200\\\\nPOST /b 500\\\\nget /c 200\\\\n\'"},\n'
              ' {"id":"err","title":"Errors","file":"error.log"}]\n'
              'EOF\n')

    def test_from_the_project(self):
        p = self.project(self.SCRIPT)
        sources = lpcp.project_log_sources(p, dict(lpcp.DEFAULTS), "127.0.0.2")
        self.assertEqual([(s.id, s.kind) for s in sources], [("app", "cmd"), ("err", "file")])
        self.assertEqual(lpcp.log_launcher(sources),
                         lpcp.Launcher(None, "View logs (app, err)", kind="logs", viewer=True))

    def test_none_when_not_provided(self):
        for script in (None, 'echo "usage: ./dev up" >&2; exit 1\n', "exit 2\n"):
            with self.subTest(script=script):
                self.assertEqual(lpcp.project_log_sources(self.project(script), dict(lpcp.DEFAULTS)), [])

    def test_invalid_output_is_an_error(self):
        for out in ("not json", "[]", '[{"id":"a","title":"A"}]'):
            with self.subTest(out=out), self.assertRaises(lpcp.LpcpError):
                lpcp.project_log_sources(self.project(f"echo '{out}'\n"), dict(lpcp.DEFAULTS))

    def test_pick(self):
        p = self.project(None)
        sources = lpcp.lpcp_logs.parse_log_sources('[{"id":"a","title":"A","cmd":"x"},{"id":"b","title":"B","cmd":"y"}]')
        self.assertEqual(lpcp.pick_log_source(p, sources, None).id, "a")
        self.assertEqual(lpcp.pick_log_source(p, sources, "b").id, "b")
        with self.assertRaises(lpcp.LpcpError) as e:
            lpcp.pick_log_source(p, sources, "c")
        self.assertIn("choose from: a, b", str(e.exception))

    def run_logs(self, p, **opts):
        import contextlib
        import io
        from types import SimpleNamespace
        from unittest import mock
        args = SimpleNamespace(site="blog", list=False, source=None, grep=None, exclude=None, ignore_case=False)
        vars(args).update(opts)
        reg = lpcp.Registry(projects={"blog": p})
        out = io.StringIO()
        with mock.patch.object(lpcp, "load_addresses", lambda: {}), contextlib.redirect_stdout(out):
            rc = lpcp.cmd_logs(reg, args)
        return rc, out.getvalue()

    def test_logs_list_and_filter(self):
        p = self.project(self.SCRIPT)
        rc, out = self.run_logs(p, list=True)
        self.assertEqual(rc, 0)
        self.assertIn("app  cmd   App", out)
        self.assertIn("err  file  Errors  (error.log)", out)
        self.assertEqual(self.run_logs(p, source="app")[1], "GET /a 200\nPOST /b 500\nget /c 200\n")
        self.assertEqual(self.run_logs(p, grep="/a|/b", exclude="POST")[1], "GET /a 200\n")
        self.assertEqual(self.run_logs(p, grep="GET", ignore_case=True)[1], "GET /a 200\nget /c 200\n")
        self.assertEqual(self.run_logs(p, grep="GET")[1], "GET /a 200\n")
        with self.assertRaises(lpcp.LpcpError):
            self.run_logs(p, source="nope")
        with self.assertRaises(lpcp.LpcpError):
            self.run_logs(p, grep="(")

    def test_logs_without_sources(self):
        with self.assertRaises(lpcp.LpcpError):
            self.run_logs(self.project(None), list=True)

    def test_plain_logs_is_still_the_projects_own_command(self):
        from unittest import mock
        p = self.project(None)
        with mock.patch.object(lpcp, "run_project", return_value=0) as run:
            self.assertEqual(self.run_logs(p)[0], 0)
        self.assertEqual(run.call_args.args[1], p.logs)


if __name__ == "__main__":
    unittest.main()
