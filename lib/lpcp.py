# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 AntzCode
"""LanProjectsControlPanel: run several docker projects side by side.

Each registered project gets its own address and is started with its own `./dev up --bind=<address>`,
so every project keeps its usual ports (web, database, ...) without colliding with the others: a port
belongs to an address, not to the whole machine. Projects on loopback addresses (127.0.0.2, 127.0.0.3, ...)
are served at <name>.localhost through one Caddy proxy on 127.0.0.1:80/443 (`*.localhost` always resolves to
127.0.0.1, so there's no DNS or hosts file to edit). A project with real hostnames and its own TLS can be
bound to the LAN address instead and left out of the proxy. Standard library only.
"""
from __future__ import annotations

import argparse
import ipaddress
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
    "domain": "localhost",      # proxied sites are <name>.<domain>
    "https": True,              # serve https (Caddy's local CA); false = plain http
    "http_port": 80,
    "https_port": 443,
    "bind": "127.0.0.1",        # the proxy's own address
    "admin": "localhost:2019",  # Caddy's admin endpoint (used for reloads)
    "address_range": ["127.0.0.2", "127.0.0.99"],  # where project addresses are assigned from
    "wait": 60,                 # seconds to wait for a started project to answer
    "caddy_image": "caddy:2.11.9",
    "container": "lpcp-caddy",
}
REMOVED_SETTINGS = {
    "port_range": "projects now get an address instead of a port; use address_range (see projects.example.toml)",
}
PROJECT_KEYS = {"dir", "host", "bind", "port", "proxy", "url", "up", "down", "logs"}


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
    bind: str = "auto"          # "auto" (an address from address_range), "lan", or an IPv4 address
    port: int = 80              # its web port on that address: what the proxy forwards to and `up` waits for
    proxy: bool = True          # serve `host` through the proxy
    url: str | None = None      # shown by ls/open instead of the derived URL
    up: str = "./dev up --bind={bind}"
    down: str = "./dev down"
    logs: str = "./dev logs"


@dataclass
class Registry:
    settings: dict = field(default_factory=lambda: dict(DEFAULTS))
    projects: dict[str, Project] = field(default_factory=dict)


def check_bind(value: str, where: str) -> str:
    if value in ("auto", "lan"):
        return value
    try:
        ip = ipaddress.IPv4Address(value)
    except ValueError:
        raise LpcpError(f"{where}: bind must be auto, lan or an IPv4 address, not {value!r}") from None
    if ip.is_unspecified:
        raise LpcpError(f"{where}: 0.0.0.0 takes every address and would block the other projects")
    return value


def address_range(settings: dict) -> tuple[int, int]:
    try:
        lo, hi = (ipaddress.IPv4Address(a) for a in settings["address_range"])
    except (ValueError, TypeError):
        raise LpcpError("address_range must be two IPv4 addresses, e.g. [\"127.0.0.2\", \"127.0.0.99\"]") from None
    if not (lo.is_loopback and hi.is_loopback and lo <= hi):
        raise LpcpError("address_range must be two addresses in 127.0.0.0/8, lowest first")
    return int(lo), int(hi)


def parse_registry(text: str) -> Registry:
    data = tomllib.loads(text)
    raw = data.get("settings", {})
    for key, why in REMOVED_SETTINGS.items():
        if key in raw:
            raise LpcpError(f"setting '{key}' was removed: {why}")
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise LpcpError(f"unknown setting(s) in projects.toml: {', '.join(sorted(unknown))}")
    settings = {**DEFAULTS, **raw}
    address_range(settings)
    reg = Registry(settings=settings)
    for name, p in data.get("projects", {}).items():
        where = f"project '{name}'"
        if not NAME_RE.match(name):
            raise LpcpError(f"project name '{name}' must be lowercase letters, digits and dashes")
        if "dir" not in p:
            raise LpcpError(f"{where} has no dir")
        unknown = set(p) - PROJECT_KEYS
        if unknown:
            raise LpcpError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
        for key in ("up", "down", "logs"):
            if "{port}" in p.get(key, ""):
                raise LpcpError(f"{where}: the {{port}} token was removed; projects get an address now, so start "
                                f"them with --bind={{bind}} (see projects.example.toml)")
        port = p.get("port", 80)
        if not isinstance(port, int) or not 0 < port < 65536:
            raise LpcpError(f"{where}: port must be a number from 1 to 65535")
        if not isinstance(p.get("proxy", True), bool):
            raise LpcpError(f"{where}: proxy must be true or false")
        reg.projects[name] = Project(
            name=name,
            dir=Path(p["dir"]).expanduser(),
            host=p.get("host", f"{name}.{settings['domain']}"),
            bind=check_bind(p.get("bind", "auto"), where),
            port=port,
            proxy=p.get("proxy", True),
            url=p.get("url"),
            **{k: p[k] for k in ("up", "down", "logs") if k in p},
        )
    return reg


def registry_path() -> Path:
    return home() / "projects.toml"


def load_registry() -> Registry:
    path = registry_path()
    return parse_registry(path.read_text(encoding="utf-8")) if path.exists() else Registry()


def toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value)


def append_project(text: str, name: str, directory: str, options: dict | None = None) -> str:
    """Add a [projects.<name>] block to the registry text. `options` holds the optional keys to write."""
    if not NAME_RE.match(name):
        raise LpcpError(f"project name '{name}' must be lowercase letters, digits and dashes")
    if f"[projects.{name}]" in text:
        raise LpcpError(f"'{name}' is already registered")
    block = [f"[projects.{name}]", f"dir = {toml_value(directory)}"]
    for key, value in (options or {}).items():
        if value is not None:
            block.append(f"{key} = {toml_value(value)}")
    if text and not text.endswith("\n"):
        text += "\n"
    return text + ("\n" if text.strip() else "") + "\n".join(block) + "\n"


def remove_project(text: str, name: str) -> str:
    """Drop the [projects.<name>] block (up to the next table header) from the registry text."""
    out, skipping, found = [], False, False
    for line in text.splitlines(keepends=True):
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


# --------------------------------------------------------------------------- state and addresses

def state_dir() -> Path:
    d = home() / ".state"
    d.mkdir(exist_ok=True)
    return d


def load_addresses() -> dict[str, str]:
    f = state_dir() / "addresses.json"
    return json.loads(f.read_text()) if f.exists() else {}


def save_addresses(addresses: dict[str, str]) -> None:
    (state_dir() / "addresses.json").write_text(json.dumps(addresses, indent=2, sort_keys=True) + "\n")


def listening(port: int, host: str = "127.0.0.1") -> bool:
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def address_in_use(port: int, ip: str) -> bool:
    """Whether a new project should skip this address: something answers on its web port, or anything at all
    listens on the address (a project started by hand, for example), since a project owns its whole address."""
    if listening(port, ip):
        return True
    try:
        out = subprocess.run(["ss", "-Hltn"], text=True, capture_output=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(len(cols) > 3 and cols[3].rsplit(":", 1)[0] == ip for cols in map(str.split, out.splitlines()))


def lan_address() -> str:
    """This machine's LAN address: the source address of its default route (a UDP connect sends nothing)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))
            ip = s.getsockname()[0]
        except OSError:
            ip = ""
    if not ip or ip.startswith("127."):
        raise LpcpError("no LAN address found (is there a default route?); set the project's bind to an IP")
    return ip


def project_address(project: Project, reg: Registry, addresses: dict[str, str], assign: bool = True,
                    in_use=address_in_use, lan=lan_address) -> str | None:
    """The address a project runs on: fixed, the LAN address, remembered from before, or the first free one."""
    if project.bind == "lan":
        return lan()
    if project.bind != "auto":
        return project.bind
    if project.name in addresses:
        return addresses[project.name]
    if not assign:
        return None
    taken = set(addresses.values()) | {reg.settings["bind"]}
    taken |= {p.bind for p in reg.projects.values() if p.bind not in ("auto", "lan")}
    lo, hi = address_range(reg.settings)
    for n in range(lo, hi + 1):
        ip = str(ipaddress.IPv4Address(n))
        if ip not in taken and not in_use(project.port, ip):
            addresses[project.name] = ip
            return ip
    raise LpcpError(f"no free address left in {reg.settings['address_range'][0]}-{reg.settings['address_range'][1]}")


def site_url(project: Project, settings: dict, ip: str | None = None) -> str:
    if project.url:
        return project.url
    if project.proxy:
        https = settings["https"]
        port = settings["https_port"] if https else settings["http_port"]
        default = 443 if https else 80
        return f"{'https' if https else 'http'}://{project.host}" + ("" if port == default else f":{port}")
    return f"http://{ip or '?'}" + ("" if project.port == 80 else f":{project.port}")


def project_env(project: Project, ip: str, settings: dict) -> dict[str, str]:
    return {"LPCP_PROJECT": project.name, "LPCP_BIND": ip, "LPCP_HOST": project.host,
            "LPCP_URL": site_url(project, settings, ip)}


def fill(template: str, project: Project, ip: str, settings: dict) -> str:
    """Substitute {bind} {host} {url} {name}. Plain replacement, so other braces pass through."""
    for token, value in (("{bind}", ip), ("{host}", project.host),
                         ("{url}", site_url(project, settings, ip)), ("{name}", project.name)):
        template = template.replace(token, value)
    return template


# --------------------------------------------------------------------------- who holds a port

def sh(cmd: list[str], check=True, quiet=True, **kw) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(cmd, text=True, capture_output=quiet, **kw)
    except FileNotFoundError:
        raise LpcpError(f"'{cmd[0]}' not found") from None
    if check and r.returncode != 0:
        raise LpcpError(f"{' '.join(cmd[:3])} failed: {(r.stderr or r.stdout or '').strip()}")
    return r


WILDCARDS = ("0.0.0.0", "*", "[::]", "::")


def port_holder(port: int, ip: str = "0.0.0.0") -> str:
    """Best-effort description of what listens on ip:port (or on every address, which covers it)."""
    published = [f"{w}:{port}->" for w in ("0.0.0.0", "[::]", "::")]
    if ip not in WILDCARDS:
        published.insert(0, f"{ip}:{port}->")
    r = sh(["docker", "ps", "--format", "{{.Names}}|{{.Ports}}"], check=False)
    for line in r.stdout.splitlines():
        name, _, ports = line.partition("|")
        if any(p in ports for p in published):
            return f"container {name}"
    r = sh(["ss", "-Hltnp", f"sport = :{port}"], check=False)
    for line in r.stdout.splitlines():
        m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
        if m:
            return f"process {m.group(1)} (pid {m.group(2)})"
    return "another process"


def wildcard_holder(port: int) -> str | None:
    """What holds `port` on every address (0.0.0.0 / [::]), which blocks every project's address, or None."""
    r = sh(["ss", "-Hltn", f"sport = :{port}"], check=False)
    for line in r.stdout.splitlines():
        cols = line.split()
        if len(cols) > 3 and cols[3].rsplit(":", 1)[0] in WILDCARDS:
            return port_holder(port)
    return None


# --------------------------------------------------------------------------- proxy

def render_caddyfile(settings: dict, sites: list[tuple[str, str]]) -> str:
    """Caddyfile for the given (host, "ip:port") sites. Caddy runs with host networking."""
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
    for host, upstream in sorted(sites):
        lines += ["", f"{host if https else 'http://' + host} {{"]
        if https:
            lines.append("\ttls internal")
        lines += [f"\treverse_proxy {upstream}", "}"]
    return "\n".join(lines) + "\n"


def container_state(name: str) -> str | None:
    r = sh(["docker", "inspect", "-f", "{{.State.Status}}", name], check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def write_caddyfile(settings: dict, sites: list[tuple[str, str]]) -> Path:
    d = state_dir() / "caddy"          # a directory is mounted, so rewriting the file is always seen
    d.mkdir(exist_ok=True)
    f = d / "Caddyfile"
    f.write_text(render_caddyfile(settings, sites), encoding="utf-8")
    return f


def proxy_start(settings: dict) -> None:
    c, ip = settings["container"], settings["bind"]
    for key in ("http_port", "https_port"):
        if listening(settings[key], ip):
            raise LpcpError(
                f"cannot start the proxy: {ip}:{settings[key]} is held by {port_holder(settings[key], ip)}. "
                "Stop it, or change http_port/https_port/bind in projects.toml.")
    sh(["docker", "rm", "-f", c], check=False)
    sh(["docker", "run", "-d", "--name", c, "--restart", "unless-stopped", "--network", "host",
        "-v", f"{state_dir() / 'caddy'}:/etc/caddy:ro", "-v", "lpcp-caddy-data:/data",
        "-v", "lpcp-caddy-config:/config", settings["caddy_image"]])
    for _ in range(50):
        if listening(settings["https_port" if settings["https"] else "http_port"], ip):
            return
        time.sleep(0.2)
    logs = sh(["docker", "logs", "--tail", "15", c], check=False)
    raise LpcpError("the proxy did not start listening:\n" + (logs.stdout + logs.stderr).strip())


def proxy_stop(settings: dict) -> None:
    sh(["docker", "rm", "-f", settings["container"]], check=False)


def proxy_reload(settings: dict) -> None:
    sh(["docker", "exec", settings["container"], "caddy", "reload", "--config",
        "/etc/caddy/Caddyfile", "--adapter", "caddyfile"])


def active_sites(reg: Registry, addresses: dict[str, str], include: set[str] = frozenset()):
    """Sites to route: proxied projects that answer on their address, plus those just started."""
    sites = []
    for p in reg.projects.values():
        if not p.proxy:
            continue
        try:
            ip = project_address(p, reg, addresses, assign=False)
        except LpcpError:
            continue
        if ip and (p.name in include or listening(p.port, ip)):
            sites.append((p.host, f"{ip}:{p.port}"))
    return sites


def sync_proxy(reg: Registry, addresses: dict[str, str], include: set[str] = frozenset()) -> str:
    """Make the proxy match what is running. It lives only while a site needs it (it holds 80/443)."""
    s = reg.settings
    sites = active_sites(reg, addresses, include)
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


def run_project(project: Project, template: str, ip: str, reg: Registry) -> int:
    if not project.dir.is_dir():
        raise LpcpError(f"{project.name}: {project.dir} does not exist")
    env = {**os.environ, **project_env(project, ip, reg.settings)}
    cmd = fill(template, project, ip, reg.settings)
    return subprocess.run(["bash", "-c", cmd], cwd=project.dir, env=env).returncode


def cmd_up(reg: Registry, args) -> int:
    projects, addresses = select(reg, args.sites), load_addresses()
    started, failed = {}, []
    for p in projects:
        # First: a 0.0.0.0 listener answers on every address, so every address would look taken below.
        holder = wildcard_holder(p.port)
        if holder:
            print(f"{p.name}: port {p.port} is held on every address (0.0.0.0) by {holder}, which blocks it. "
                  "Stop that (a project started with --lan, for example), then run this again.", file=sys.stderr)
            failed.append(p.name)
            continue
        try:
            ip = project_address(p, reg, addresses)
        except LpcpError as e:
            print(f"{p.name}: {e}", file=sys.stderr)
            failed.append(p.name)
            continue
        save_addresses(addresses)
        if listening(p.port, ip):
            print(f"{p.name}: already running on {ip}")
            started[p.name] = ip
            continue
        print(f"{p.name}: starting on {ip} ...")
        if run_project(p, p.up, ip, reg) != 0:
            failed.append(p.name)
            print(f"{p.name}: start command failed", file=sys.stderr)
            continue
        deadline = time.time() + reg.settings["wait"]
        while not listening(p.port, ip) and time.time() < deadline:
            time.sleep(0.5)
        if not listening(p.port, ip):
            print(f"{p.name}: nothing answers on {ip}:{p.port} (does its ./dev support --bind, and is "
                  f"`port` its web port?)", file=sys.stderr)
        started[p.name] = ip
    status = None
    if any(reg.projects[n].proxy for n in started):
        try:
            status = sync_proxy(reg, addresses, set(started))
        except LpcpError as e:
            print(f"proxy: {e}", file=sys.stderr)
            failed.append("proxy")
    for name, ip in started.items():
        p = reg.projects[name]
        if p.proxy and status not in ("started", "reloaded"):
            url = site_url(Project(**{**p.__dict__, "proxy": False, "url": None}), reg.settings, ip)
        else:
            url = site_url(p, reg.settings, ip)
        print(f"{name}: {url}")
    return 1 if failed else 0


def cmd_down(reg: Registry, args) -> int:
    projects, addresses = select(reg, args.sites), load_addresses()
    failed = []
    for p in projects:
        try:
            ip = project_address(p, reg, addresses, assign=False)
        except LpcpError as e:
            print(f"{p.name}: {e}", file=sys.stderr)
            failed.append(p.name)
            continue
        # A named site is always stopped: half-started (nothing answers) still needs stopping.
        if not args.sites and (not ip or not listening(p.port, ip)):
            print(f"{p.name}: not running")
            continue
        print(f"{p.name}: stopping ...")
        if run_project(p, p.down, ip or "", reg) != 0:      # no address yet: started by hand, standalone
            failed.append(p.name)
    try:
        sync_proxy(reg, addresses)
    except LpcpError as e:
        print(f"proxy: {e}", file=sys.stderr)
        failed.append("proxy")
    return 1 if failed else 0


def cmd_restart(reg: Registry, args) -> int:
    rc = cmd_down(reg, args)
    return cmd_up(reg, args) or rc


def cmd_ls(reg: Registry, args) -> int:
    addresses = load_addresses()
    rows = [("NAME", "ADDRESS", "URL", "STATE")]
    for p in reg.projects.values():
        try:
            ip = project_address(p, reg, addresses, assign=False)
        except LpcpError:
            ip = None
        state = "running" if ip and listening(p.port, ip) else "stopped"
        rows.append((p.name, ip or ("lan?" if p.bind == "lan" else "-"), site_url(p, reg.settings, ip), state))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    if not reg.projects:
        print("(no projects registered: ./dev add <name> <dir>)")
    return 0


def cmd_logs(reg: Registry, args) -> int:
    (p,) = select(reg, [args.site])
    return run_project(p, p.logs, project_address(p, reg, load_addresses(), assign=False) or "", reg)


def cmd_open(reg: Registry, args) -> int:
    (p,) = select(reg, [args.site])
    sh(["xdg-open", site_url(p, reg.settings, project_address(p, reg, load_addresses(), assign=False))])
    return 0


def cmd_add(reg: Registry, args) -> int:
    directory = Path(args.dir).expanduser().resolve()
    if not directory.is_dir():
        raise LpcpError(f"{directory} is not a directory")
    path = registry_path()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    options = {"host": args.host, "bind": args.bind, "port": args.port,
               "proxy": False if args.no_proxy else None, "url": args.url, "up": args.up, "down": args.down}
    new = append_project(text, args.name, str(directory), options)
    parse_registry(new)                 # refuse to write anything invalid
    path.write_text(new, encoding="utf-8")
    print(f"added {args.name} -> {directory}")
    if not (directory / "dev").exists() and not args.up:
        print(f"note: {directory} has no ./dev; give it one that accepts --bind=IP, or re-add with --up '...'",
              file=sys.stderr)
    return 0


def cmd_rm(reg: Registry, args) -> int:
    path = registry_path()
    path.write_text(remove_project(path.read_text(encoding="utf-8"), args.name), encoding="utf-8")
    addresses = load_addresses()
    if addresses.pop(args.name, None) is not None:
        save_addresses(addresses)
    print(f"removed {args.name} (its containers are untouched; `./dev down {args.name}` first to stop them)")
    return 0


def cmd_proxy(reg: Registry, args) -> int:
    s, addresses = reg.settings, load_addresses()
    if args.action == "status":
        print(f"proxy container {s['container']}: {container_state(s['container']) or 'not created'}")
        for host, upstream in active_sites(reg, addresses):
            print(f"  {host} -> {upstream}")
        return 0
    if args.action == "down":
        proxy_stop(s)
        print("proxy stopped")
        return 0
    if args.action == "up":
        write_caddyfile(s, active_sites(reg, addresses))
        if container_state(s["container"]) == "running":
            proxy_reload(s)
        else:
            proxy_start(s)
        print("proxy running")
        return 0
    print(f"proxy {sync_proxy(reg, addresses)}")        # reload
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
    s, addresses, problems = reg.settings, load_addresses(), 0

    def report(ok: bool, text: str) -> None:
        nonlocal problems
        problems += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {text}")

    report(shutil.which("docker") is not None and sh(["docker", "info"], check=False).returncode == 0,
           "docker is installed and reachable")
    report(registry_path().exists(), f"registry {registry_path()} exists")
    ours = container_state(s["container"]) == "running"
    ports = {s["http_port"], s["https_port"]} | {p.port for p in reg.projects.values()}
    for port in sorted(ports):
        holder = wildcard_holder(port)
        if holder:
            report(False, f"port {port} is held on every address (0.0.0.0) by {holder}: it blocks every project")
    for key in ("http_port", "https_port"):
        port = s[key]
        if not listening(port, s["bind"]):
            report(True, f"proxy address {s['bind']}:{port} is free")
        elif ours:
            report(True, f"proxy address {s['bind']}:{port} is held by our proxy")
        else:
            report(False, f"proxy address {s['bind']}:{port} is held by {port_holder(port, s['bind'])}")
    if any(p.bind == "lan" for p in reg.projects.values()):
        try:
            report(True, f"LAN address is {lan_address()}")
        except LpcpError as e:
            report(False, str(e))
    for p in reg.projects.values():
        label = f"project {p.name}"
        report(p.dir.is_dir(), f"{label}: {p.dir} exists")
        if p.dir.is_dir() and p.up.startswith("./dev"):
            report(os.access(p.dir / "dev", os.X_OK), f"{label}: ./dev is executable")
        try:
            ip = project_address(p, reg, addresses, assign=False)
        except LpcpError:
            ip = None
        if ip and listening(p.port, ip):
            report(True, f"{label}: answering on {ip}:{p.port}")
    return 1 if problems else 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="./dev", description="Run several docker projects side by side, each on "
                                 "its own address, with <name>.localhost served by one proxy.")
    sub = ap.add_subparsers(dest="command", required=True)
    for name, helptext in (("up", "start sites (all registered when none are named)"),
                           ("down", "stop sites (all when none are named)"),
                           ("restart", "stop then start sites")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("sites", nargs="*", metavar="site")
    sub.add_parser("ls", help="list registered sites, their addresses and whether they run")
    p = sub.add_parser("logs", help="follow a site's logs (its ./dev logs)")
    p.add_argument("site")
    p = sub.add_parser("open", help="open a site in the browser")
    p.add_argument("site")
    p = sub.add_parser("add", help="register a project")
    p.add_argument("name")
    p.add_argument("dir")
    p.add_argument("--bind", help="auto (default: an address from address_range), lan, or an IPv4 address")
    p.add_argument("--port", type=int, help="its web port on that address (default 80)")
    p.add_argument("--host", help="hostname served by the proxy (default <name>.localhost)")
    p.add_argument("--no-proxy", action="store_true", help="don't serve it through the proxy (real hostnames, "
                   "its own TLS)")
    p.add_argument("--url", help="URL shown by ls and open")
    p.add_argument("--up", help="start command; {bind} {host} {url} {name} are filled in "
                   "(default './dev up --bind={bind}')")
    p.add_argument("--down", help="stop command (default './dev down')")
    p = sub.add_parser("rm", help="unregister a project")
    p.add_argument("name")
    p = sub.add_parser("proxy", help="control the proxy")
    p.add_argument("action", choices=["up", "down", "reload", "status"])
    sub.add_parser("trust", help="export Caddy's local CA certificate")
    sub.add_parser("doctor", help="check docker, addresses, ports and the registry")
    p = sub.add_parser("ui", help="open the control panel window (needs GTK 3 for Python)")
    p.add_argument("--install", action="store_true", help="add a launcher to the desktop's application menu")
    return ap


COMMANDS = {"up": cmd_up, "down": cmd_down, "restart": cmd_restart, "ls": cmd_ls, "logs": cmd_logs,
            "open": cmd_open, "add": cmd_add, "rm": cmd_rm, "proxy": cmd_proxy, "trust": cmd_trust,
            "doctor": cmd_doctor}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "ui":
            import lpcp_ui  # GTK is only needed for the panel, which reads the registry itself
            return lpcp_ui.main(args)
        return COMMANDS[args.command](load_registry(), args)
    except LpcpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
