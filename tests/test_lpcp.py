# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 AntzCode
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import lpcp  # noqa: E402

REGISTRY = """
[settings]
domain = "test"
port_range = [8100, 8103]

[projects.nan]
dir = "~/nan"

[projects.blog]
dir = "/srv/blog"
host = "journal.test"
port = 9000
up = "WEB_PORT={port} docker compose up -d"
"""


class Registry(unittest.TestCase):
    def test_parse(self):
        reg = lpcp.parse_registry(REGISTRY)
        nan, blog = reg.projects["nan"], reg.projects["blog"]
        self.assertEqual(nan.host, "nan.test")
        self.assertEqual(nan.up, "./dev up -p {port}")
        self.assertEqual(nan.dir, Path("~/nan").expanduser())
        self.assertEqual((blog.host, blog.port), ("journal.test", 9000))
        self.assertEqual(blog.up, "WEB_PORT={port} docker compose up -d")
        self.assertEqual(reg.settings["https_port"], 443)

    def test_rejects_bad_input(self):
        for bad in ('[projects.Bad_Name]\ndir="/x"', '[projects.x]\nhost="x"', '[settings]\nnope=1'):
            with self.assertRaises(lpcp.LpcpError, msg=bad):
                lpcp.parse_registry(bad)

    def test_add_then_remove_round_trips(self):
        text = lpcp.append_project("", "nan", "/p/nan", None, None, None)
        text = lpcp.append_project(text, "blog", "/p/blog", "journal.test", "docker compose up -d", None)
        reg = lpcp.parse_registry(text)
        self.assertEqual(list(reg.projects), ["nan", "blog"])
        self.assertEqual(reg.projects["blog"].up, "docker compose up -d")
        text = lpcp.remove_project(text, "nan")
        self.assertEqual(list(lpcp.parse_registry(text).projects), ["blog"])
        with self.assertRaises(lpcp.LpcpError):
            lpcp.remove_project(text, "nan")
        with self.assertRaises(lpcp.LpcpError):
            lpcp.append_project(text, "blog", "/x", None, None, None)

    def test_remove_keeps_neighbours_and_settings(self):
        text = lpcp.remove_project(REGISTRY, "nan")
        reg = lpcp.parse_registry(text)
        self.assertEqual(list(reg.projects), ["blog"])
        self.assertEqual(reg.settings["domain"], "test")


class Ports(unittest.TestCase):
    def setUp(self):
        self.reg = lpcp.parse_registry(REGISTRY)

    def test_fixed_port_wins(self):
        self.assertEqual(lpcp.assign_port(self.reg.projects["blog"], self.reg, {}), 9000)

    def test_assigned_once_and_remembered(self):
        ports = {}
        first = lpcp.assign_port(self.reg.projects["nan"], self.reg, ports, in_use=lambda p: False)
        self.assertEqual((first, ports), (8100, {"nan": 8100}))
        self.assertEqual(lpcp.assign_port(self.reg.projects["nan"], self.reg, ports, in_use=lambda p: True), 8100)

    def test_skips_taken_and_busy_ports(self):
        self.reg.projects["other"] = lpcp.Project("other", Path("/o"), "other.test")
        ports = {"other": 8100}
        port = lpcp.assign_port(self.reg.projects["nan"], self.reg, ports, in_use=lambda p: p == 8101)
        self.assertEqual(port, 8102)

    def test_exhausted(self):
        with self.assertRaises(lpcp.LpcpError):
            lpcp.assign_port(self.reg.projects["nan"], self.reg, {}, in_use=lambda p: True)


class Rendering(unittest.TestCase):
    def test_fill_replaces_tokens_only(self):
        reg = lpcp.parse_registry(REGISTRY)
        p = reg.projects["nan"]
        out = lpcp.fill("go -p {port} {host} {url} {name} ${X} {y}", p, 8101, reg.settings)
        self.assertEqual(out, "go -p 8101 nan.test https://nan.test nan ${X} {y}")

    def test_urls_show_non_default_ports(self):
        reg = lpcp.parse_registry('[settings]\nhttps_port = 18443\n[projects.a]\ndir = "/a"')
        self.assertEqual(lpcp.site_url(reg.projects["a"], reg.settings), "https://a.localhost:18443")
        reg.settings["https"] = False
        reg.settings["http_port"] = 80
        self.assertEqual(lpcp.site_url(reg.projects["a"], reg.settings), "http://a.localhost")

    def test_project_env(self):
        reg = lpcp.parse_registry(REGISTRY)
        env = lpcp.project_env(reg.projects["nan"], 8101, reg.settings)
        self.assertEqual(env, {"LPCP_PROJECT": "nan", "LPCP_PORT": "8101", "LPCP_HOST": "nan.test",
                               "LPCP_URL": "https://nan.test"})

    def test_caddyfile(self):
        s = dict(lpcp.DEFAULTS, http_port=18080, https_port=18443)
        out = lpcp.render_caddyfile(s, [("b.localhost", 8102), ("a.localhost", 8101)])
        self.assertIn("\thttp_port 18080\n\thttps_port 18443\n", out)
        self.assertIn("\tdefault_bind 127.0.0.1\n", out)
        self.assertLess(out.index("a.localhost {"), out.index("b.localhost {"))
        self.assertIn("a.localhost {\n\ttls internal\n\treverse_proxy 127.0.0.1:8101\n}", out)

    def test_caddyfile_plain_http_and_empty(self):
        out = lpcp.render_caddyfile(dict(lpcp.DEFAULTS, https=False), [("a.localhost", 8101)])
        self.assertIn("http://a.localhost {\n\treverse_proxy 127.0.0.1:8101\n}", out)
        self.assertNotIn("tls internal", out)
        self.assertNotIn("reverse_proxy", lpcp.render_caddyfile(dict(lpcp.DEFAULTS), []))


if __name__ == "__main__":
    unittest.main()
