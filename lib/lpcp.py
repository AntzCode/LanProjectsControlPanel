# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 AntzCode
"""LanProjectsControlPanel: run several docker projects side by side.

Each registered project is started with its own `./dev up -p <port>` on a port this tool assigns, and
a Caddy proxy (in Docker, host networking) routes `<name>.localhost` to that port. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DEFAULTS = {
    "domain": "localhost",      # sites are <name>.<domain>
    "https": True,              # serve https (Caddy's local CA); false = plain http
    "http_port": 80,
    "https_port": 443,
    "bind": "127.0.0.1",
    "admin": "localhost:2019",  # Caddy's admin endpoint (used for reloads)
    "port_range": [8100, 8999],
    "wait": 60,                 # seconds to wait for a started project's port to open
    "caddy_image": "caddy:2.11.9",
    "container": "lpcp-caddy",
}


class LpcpError(Exception):
    pass


def home() -> Path:
    """Directory holding projects.toml and .state/ (the repo itself unless LPCP_HOME is set)."""
    return Path(os.environ.get("LPCP_HOME") or Path(__file__).resolve().parent.parent)


# --------------------------------------------------------------------------- registry

@dataclass
class Project:
    name: str
    dir: Path
    host: str
    port: int | None = None     # fixed in projects.toml; otherwise assigned and remembered in .state/
    up: str = "./dev up -p {port}"
    down: str = "./dev down"
    logs: str = "./dev logs"


@dataclass
class Registry:
    settings: dict = field(default_factory=lambda: dict(DEFAULTS))
    projects: dict[str, Project] = field(default_factory=dict)


def parse_registry(text: str) -> Registry:
    data = tomllib.loads(text)
    settings = dict(DEFAULTS)
    unknown = set(data.get("settings", {})) - set(DEFAULTS)
    if unknown:
        raise LpcpError(f"unknown setting(s) in projects.toml: {', '.join(sorted(unknown))}")
    settings.update(data.get("settings", {}))
    reg = Registry(settings=settings)
    for name, p in data.get("projects", {}).items():
        if not NAME_RE.match(name):
            raise LpcpError(f"project name '{name}' must be lowercase letters, digits and dashes")
        if "dir" not in p:
            raise LpcpError(f"project '{name}' has no dir")
        reg.projects[name] = Project(
            name=name,
            dir=Path(p["dir"]).expanduser(),
            host=p.get("host", f"{name}.{settings['domain']}"),
            port=p.get("port"),
            **{k: p[k] for k in ("up", "down", "logs") if k in p},
        )
    return reg


def registry_path() -> Path:
    return home() / "projects.toml"


def load_registry() -> Registry:
    path = registry_path()
    return parse_registry(path.read_text(encoding="utf-8")) if path.exists() else Registry()


def append_project(text: str, name: str, directory: str, host: str | None, up: str | None,
                   down: str | None) -> str:
    """Add a [projects.<name>] block to the registry text."""
    if not NAME_RE.match(name):
        raise LpcpError(f"project name '{name}' must be lowercase letters, digits and dashes")
    if f"[projects.{name}]" in text:
        raise LpcpError(f"'{name}' is already registered")
    block = [f"[projects.{name}]", f"dir = {json.dumps(directory)}"]
    for key, value in (("host", host), ("up", up), ("down", down)):
        if value:
            block.append(f"{key} = {json.dumps(value)}")
    if text and not text.endswith("\n"):
        text += "\n"
    return text + ("\n" if text.strip() else "") + "\n".join(block) + "\n"


def remove_project(text: str, name: str) -> str:
    """Drop the [projects.<name>] block (up to the next table header) from the registry text."""
    lines = text.splitlines(keepends=True)
    out, skipping, found = [], False, False
    for line in lines:
        if line.strip() == f"[projects.{name}]":
            skipping = found = True
            continue
        if skipping and line.lstrip().startswith("["):
            skipping = False
        if not skipping:
            out.append(line)
    if not found:
        raise LpcpError(f"'{name}' is not registered")
    return re.sub(r"\n{3,}", "\n\n", "".join(out))


# --------------------------------------------------------------------------- state and ports

def state_dir() -> Path:
    d = home() / ".state"
    d.mkdir(exist_ok=True)
    return d


def load_ports() -> dict[str, int]:
    f = state_dir() / "ports.json"
    return json.loads(f.read_text()) if f.exists() else {}


def save_ports(ports: dict[str, int]) -> None:
    (state_dir() / "ports.json").write_text(json.dumps(ports, indent=2, sort_keys=True) + "\n")


def listening(port: int, host: str = "127.0.0.1") -> bool:
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def assign_port(project: Project, reg: Registry, ports: dict[str, int], in_use=listening) -> int:
    """The project's port: fixed in the registry, else remembered from before, else the first free one."""
    if project.port:
        return project.port
    if project.name in ports:
        return ports[project.name]
    taken = set(ports.values()) | {p.port for p in reg.projects.values() if p.port}
    lo, hi = reg.settings["port_range"]
    for port in range(lo, hi + 1):
        if port not in taken and not in_use(port):
            ports[project.name] = port
            return port
    raise LpcpError(f"no free port left in {lo}-{hi}")


def site_url(project: Project, settings: dict) -> str:
    https = settings["https"]
    port = settings["https_port"] if https else settings["http_port"]
    default = 443 if https else 80
    return f"{'https' if https else 'http'}://{project.host}" + ("" if port == default else f":{port}")


def project_env(project: Project, port: int, settings: dict) -> dict[str, str]:
    return {"LPCP_PROJECT": project.name, "LPCP_PORT": str(port), "LPCP_HOST": project.host,
            "LPCP_URL": site_url(project, settings)}


def fill(template: str, project: Project, port: int, settings: dict) -> str:
    """Substitute {port} {host} {url} {name}. Plain replacement, so other braces pass through."""
    for token, value in (("{port}", str(port)), ("{host}", project.host),
                         ("{url}", site_url(project, settings)), ("{name}", project.name)):
        template = template.replace(token, value)
    return template


# --------------------------------------------------------------------------- proxy

def render_caddyfile(settings: dict, sites: list[tuple[str, int]]) -> str:
    """Caddyfile for the given (host, port) sites. Caddy runs with host networking."""
    https = settings["https"]
    lines = [
        "# Generated by LanProjectsControlPanel. Do not edit: `./dev up` rewrites it.",
        "{",
        f"\tadmin {settings['admin']}",
        f"\tdefault_bind {settings['bind']}",
        f"\thttp_port {settings['http_port']}",
        f"\thttps_port {settings['https_port']}",
        "}",
    ]
    for host, port in sorted(sites):
        lines += ["", f"{host if https else 'http://' + host} {{"]
        if https:
            lines.append("\ttls internal")
        lines += [f"\treverse_proxy 127.0.0.1:{port}", "}"]
    return "\n".join(lines) + "\n"


def sh(cmd: list[str], check=True, quiet=True, **kw) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(cmd, text=True, capture_output=quiet, **kw)
    except FileNotFoundError:
        raise LpcpError(f"'{cmd[0]}' not found") from None
    if check and r.returncode != 0:
        raise LpcpError(f"{' '.join(cmd[:3])} failed: {(r.stderr or r.stdout or '').strip()}")
    return r


def container_state(name: str) -> str | None:
    r = sh(["docker", "inspect", "-f", "{{.State.Status}}", name], check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def port_holder(port: int) -> str:
    """Best-effort description of what listens on `port`."""
    r = sh(["docker", "ps", "--format", "{{.Names}}|{{.Ports}}"], check=False)
    for line in r.stdout.splitlines():
        name, _, ports = line.partition("|")
        if re.search(rf"[:\]]{port}->", ports):
            return f"container {name}"
    r = sh(["ss", "-Hltnp", f"sport = :{port}"], check=False)
    m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', r.stdout)
    return f"process {m.group(1)} (pid {m.group(2)})" if m else "another process"


def write_caddyfile(settings: dict, sites: list[tuple[str, int]]) -> Path:
    d = state_dir() / "caddy"          # a directory is mounted, so rewriting the file is always seen
    d.mkdir(exist_ok=True)
    f = d / "Caddyfile"
    f.write_text(render_caddyfile(settings, sites), encoding="utf-8")
    return f


def proxy_start(settings: dict) -> None:
    c = settings["container"]
    for key in ("http_port", "https_port"):
        if listening(settings[key], settings["bind"]):
            raise LpcpError(
                f"cannot start the proxy: {settings['bind']}:{settings[key]} is held by "
                f"{port_holder(settings[key])}. Stop it, or change http_port/https_port/bind in projects.toml.")
    sh(["docker", "rm", "-f", c], check=False)
    sh(["docker", "run", "-d", "--name", c, "--restart", "unless-stopped", "--network", "host",
        "-v", f"{state_dir() / 'caddy'}:/etc/caddy:ro", "-v", "lpcp-caddy-data:/data",
        "-v", "lpcp-caddy-config:/config", settings["caddy_image"]])
    for _ in range(50):
        if listening(settings["https_port" if settings["https"] else "http_port"], settings["bind"]):
            return
        time.sleep(0.2)
    logs = sh(["docker", "logs", "--tail", "15", c], check=False)
    raise LpcpError("the proxy did not start listening:\n" + (logs.stdout + logs.stderr).strip())


def proxy_stop(settings: dict) -> None:
    sh(["docker", "rm", "-f", settings["container"]], check=False)


def proxy_reload(settings: dict) -> None:
    sh(["docker", "exec", settings["container"], "caddy", "reload", "--config",
        "/etc/caddy/Caddyfile", "--adapter", "caddyfile"])


def active_sites(reg: Registry, ports: dict[str, int], include: set[str] = frozenset()):
    """Sites to route: assigned projects whose port is open, plus those just started."""
    sites = []
    for p in reg.projects.values():
        port = p.port or ports.get(p.name)
        if port and (p.name in include or listening(port)):
            sites.append((p.host, port))
    return sites


def sync_proxy(reg: Registry, ports: dict[str, int], include: set[str] = frozenset()) -> str:
    """Make the proxy match what is running. It lives only while a site needs it (it holds 80/443)."""
    s = reg.settings
    sites = active_sites(reg, ports, include)
    write_caddyfile(s, sites)
    state = container_state(s["container"])
    if not sites:
        if state:
            proxy_stop(s)
        return "stopped"
    if state == "running":
        proxy_reload(s)
        return "reloaded"
    proxy_start(s)
    return "started"


# --------------------------------------------------------------------------- commands

def select(reg: Registry, names: list[str]) -> list[Project]:
    if not reg.projects:
        raise LpcpError("no projects registered: ./dev add <name> <dir>")
    if not names:
        return list(reg.projects.values())
    missing = [n for n in names if n not in reg.projects]
    if missing:
        raise LpcpError(f"not registered: {', '.join(missing)} (see ./dev ls)")
    return [reg.projects[n] for n in names]


def run_project(project: Project, template: str, port: int, reg: Registry) -> int:
    if not project.dir.is_dir():
        raise LpcpError(f"{project.name}: {project.dir} does not exist")
    env = {**os.environ, **project_env(project, port, reg.settings)}
    cmd = fill(template, project, port, reg.settings)
    return subprocess.run(["bash", "-c", cmd], cwd=project.dir, env=env).returncode


def cmd_up(reg: Registry, args) -> int:
    projects, ports = select(reg, args.sites), load_ports()
    started, failed = set(), []
    for p in projects:
        port = assign_port(p, reg, ports)
        save_ports(ports)
        if listening(port):
            print(f"{p.name}: already running on port {port}")
            started.add(p.name)
            continue
        print(f"{p.name}: starting on port {port} ...")
        if run_project(p, p.up, port, reg) != 0:
            failed.append(p.name)
            print(f"{p.name}: start command failed", file=sys.stderr)
            continue
        deadline = time.time() + reg.settings["wait"]
        while not listening(port) and time.time() < deadline:
            time.sleep(0.5)
        if not listening(port):
            print(f"{p.name}: nothing is listening on port {port} yet (is its web service published there?)",
                  file=sys.stderr)
        started.add(p.name)
    status = None
    if started:
        try:
            status = sync_proxy(reg, ports, started)
        except LpcpError as e:
            print(f"proxy: {e}", file=sys.stderr)
            failed.append("proxy")
    for p in projects:
        if p.name in started:
            via = site_url(p, reg.settings) if status in ("started", "reloaded") else None
            print(f"{p.name}: {via or 'http://127.0.0.1:' + str(assign_port(p, reg, ports))}")
    return 1 if failed else 0


def cmd_down(reg: Registry, args) -> int:
    projects, ports = select(reg, args.sites), load_ports()
    failed = []
    for p in projects:
        port = p.port or ports.get(p.name)
        if not port or not listening(port):
            print(f"{p.name}: not running")
            continue
        print(f"{p.name}: stopping ...")
        if run_project(p, p.down, port, reg) != 0:
            failed.append(p.name)
    try:
        sync_proxy(reg, ports)
    except LpcpError as e:
        print(f"proxy: {e}", file=sys.stderr)
        failed.append("proxy")
    return 1 if failed else 0


def cmd_restart(reg: Registry, args) -> int:
    rc = cmd_down(reg, args)
    return cmd_up(reg, args) or rc


def cmd_ls(reg: Registry, args) -> int:
    ports = load_ports()
    rows = [("NAME", "URL", "PORT", "STATE")]
    for p in reg.projects.values():
        port = p.port or ports.get(p.name)
        rows.append((p.name, site_url(p, reg.settings), str(port or "-"),
                     "running" if port and listening(port) else "stopped"))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    if not reg.projects:
        print("(no projects registered: ./dev add <name> <dir>)")
    return 0


def cmd_logs(reg: Registry, args) -> int:
    (p,) = select(reg, [args.site])
    return run_project(p, p.logs, p.port or load_ports().get(p.name) or 0, reg)


def cmd_open(reg: Registry, args) -> int:
    (p,) = select(reg, [args.site])
    sh(["xdg-open", site_url(p, reg.settings)])
    return 0


def cmd_add(reg: Registry, args) -> int:
    directory = Path(args.dir).expanduser().resolve()
    if not directory.is_dir():
        raise LpcpError(f"{directory} is not a directory")
    path = registry_path()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(append_project(text, args.name, str(directory), args.host, args.up, args.down),
                    encoding="utf-8")
    print(f"added {args.name} -> {directory}")
    if not (directory / "dev").exists() and not args.up:
        print(f"note: {directory} has no ./dev; give it one that accepts -p PORT, or re-add with --up '...'",
              file=sys.stderr)
    return 0


def cmd_rm(reg: Registry, args) -> int:
    path = registry_path()
    path.write_text(remove_project(path.read_text(encoding="utf-8"), args.name), encoding="utf-8")
    ports = load_ports()
    if ports.pop(args.name, None) is not None:
        save_ports(ports)
    print(f"removed {args.name} (its containers are untouched; `./dev down {args.name}` first to stop them)")
    return 0


def cmd_proxy(reg: Registry, args) -> int:
    s, ports = reg.settings, load_ports()
    if args.action == "status":
        state = container_state(s["container"])
        print(f"proxy container {s['container']}: {state or 'not created'}")
        for host, port in active_sites(reg, ports):
            print(f"  {host} -> 127.0.0.1:{port}")
        return 0
    if args.action == "down":
        proxy_stop(s)
        print("proxy stopped")
        return 0
    if args.action == "up":
        write_caddyfile(s, active_sites(reg, ports))
        if container_state(s["container"]) == "running":
            proxy_reload(s)
        else:
            proxy_start(s)
        print("proxy running")
        return 0
    print(f"proxy {sync_proxy(reg, ports)}")        # reload
    return 0


def cmd_trust(reg: Registry, args) -> int:
    s = reg.settings
    if container_state(s["container"]) != "running":
        raise LpcpError("the proxy isn't running; `./dev up` a site first")
    out = state_dir() / "lpcp-root.crt"
    sh(["docker", "cp", f"{s['container']}:/data/caddy/pki/authorities/local/root.crt", str(out)])
    print(f"Caddy's local CA certificate: {out}")
    print(f"  curl --cacert {out} https://<site>.{s['domain']}   # verifies without trusting it system-wide")
    print("  To stop browser warnings, import that file into the browser's or the OS's trusted authorities.")
    return 0


def cmd_doctor(reg: Registry, args) -> int:
    s, ports, problems = reg.settings, load_ports(), 0

    def report(ok: bool, text: str) -> None:
        nonlocal problems
        problems += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {text}")

    report(shutil.which("docker") is not None and sh(["docker", "info"], check=False).returncode == 0,
           "docker is installed and reachable")
    report(registry_path().exists(), f"registry {registry_path()} exists")
    ours = container_state(s["container"]) == "running"
    for key in ("http_port", "https_port"):
        port = s[key]
        if not listening(port, s["bind"]):
            report(True, f"{s['bind']}:{port} is free")
        elif ours:
            report(True, f"{s['bind']}:{port} is held by our proxy")
        else:
            report(False, f"{s['bind']}:{port} is held by {port_holder(port)}")
    for p in reg.projects.values():
        label = f"project {p.name}"
        report(p.dir.is_dir(), f"{label}: {p.dir} exists")
        if p.dir.is_dir() and p.up.startswith("./dev"):
            report(os.access(p.dir / "dev", os.X_OK), f"{label}: ./dev is executable")
        port = p.port or ports.get(p.name)
        if port and listening(port):
            report(True, f"{label}: listening on port {port}")
    return 1 if problems else 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="./dev", description="Run several docker projects side by side "
                                 "behind one hostname proxy.")
    sub = ap.add_subparsers(dest="command", required=True)
    for name, helptext in (("up", "start sites (all registered when none are named)"),
                           ("down", "stop sites (all when none are named)"),
                           ("restart", "stop then start sites")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("sites", nargs="*", metavar="site")
    sub.add_parser("ls", help="list registered sites and whether they run")
    p = sub.add_parser("logs", help="follow a site's logs (its ./dev logs)")
    p.add_argument("site")
    p = sub.add_parser("open", help="open a site in the browser")
    p.add_argument("site")
    p = sub.add_parser("add", help="register a project")
    p.add_argument("name")
    p.add_argument("dir")
    p.add_argument("--host", help="hostname (default <name>.localhost)")
    p.add_argument("--up", help="start command; {port} {host} {url} {name} are filled in "
                   "(default './dev up -p {port}')")
    p.add_argument("--down", help="stop command (default './dev down')")
    p = sub.add_parser("rm", help="unregister a project")
    p.add_argument("name")
    p = sub.add_parser("proxy", help="control the proxy")
    p.add_argument("action", choices=["up", "down", "reload", "status"])
    sub.add_parser("trust", help="export Caddy's local CA certificate")
    sub.add_parser("doctor", help="check docker, ports and the registry")
    return ap


COMMANDS = {"up": cmd_up, "down": cmd_down, "restart": cmd_restart, "ls": cmd_ls, "logs": cmd_logs,
            "open": cmd_open, "add": cmd_add, "rm": cmd_rm, "proxy": cmd_proxy, "trust": cmd_trust,
            "doctor": cmd_doctor}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](load_registry(), args)
    except LpcpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
