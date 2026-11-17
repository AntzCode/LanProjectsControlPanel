# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 AntzCode
"""What the control panel shows and does, with no GUI code (so it can be tested without a display).

Monitor.snapshot() reads the registry, `docker ps` and `docker stats`, and works out each project's state:
  green  one of its containers publishes its web port, and something answers there
  red    its last start from the panel failed, or its web port is published but nothing answers
  off    none of its containers publishes its web port
A container belongs to a project when compose started it from the project's folder (or a folder inside it).
Actions runs `./dev up <name>` / `./dev down <name>` one at a time in a background thread, logging to
.state/logs/<name>.log, so the panel and the command line share one code path.
"""
from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import lpcp

REPO = Path(__file__).resolve().parent.parent
WILDCARDS = {"0.0.0.0", "::", "[::]", "*", ""}
UNITS = {"B": 1, "kB": 1e3, "KB": 1e3, "KiB": 1024, "MB": 1e6, "MiB": 1024 ** 2, "GB": 1e9,
         "GiB": 1024 ** 3, "TB": 1e12, "TiB": 1024 ** 4}
LABEL = "com.docker.compose.project.working_dir"


# --------------------------------------------------------------------------- parsing docker output

def parse_bytes(text: str) -> float:
    """'20.71MiB' -> bytes. Unknown or empty ('--' while a container starts) -> 0."""
    m = re.match(r"\s*([\d.]+)\s*([A-Za-z]+)", text or "")
    return float(m.group(1)) * UNITS.get(m.group(2), 0) if m else 0.0


def parse_cpu(text: str) -> float:
    m = re.match(r"\s*([\d.]+)\s*%", text or "")
    return float(m.group(1)) if m else 0.0


def parse_published(ports: str) -> set[tuple[str, int]]:
    """'0.0.0.0:443->443/tcp, [::]:443->443/tcp, 127.0.0.2:3000-3001->3000-3001/tcp' -> {(address, host port)}.
    docker collapses adjacent published ports into a range."""
    out = set()
    for m in re.finditer(r"(\S*?):(\d+)(?:-(\d+))?->\d+(?:-\d+)?/(?:tcp|udp)", ports or ""):
        first = int(m.group(2))
        out.update((m.group(1), port) for port in range(first, int(m.group(3) or first) + 1))
    return out


@dataclass
class Container:
    id: str
    workdir: str
    published: set[tuple[str, int]]


def parse_ps(output: str) -> list[Container]:
    """Lines of `docker ps --format '{{.ID}}\\t{{.Label "...working_dir"}}\\t{{.Ports}}'`."""
    out = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0]:
            out.append(Container(parts[0][:12], parts[1], parse_published(parts[2] if len(parts) > 2 else "")))
    return out


def parse_stats(output: str) -> dict[str, tuple[float, float]]:
    """Lines of `docker stats --format '{{.ID}}\\t{{.CPUPerc}}\\t{{.MemUsage}}'` -> {id: (cpu %, bytes)}."""
    out = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            out[parts[0][:12]] = (parse_cpu(parts[1]), parse_bytes(parts[2].split("/")[0]))
    return out


def belongs(workdir: str, project_dir: Path) -> bool:
    """compose's working_dir is the folder of its first compose file: the project's folder or one inside it."""
    if not workdir:
        return False
    try:
        return Path(workdir).resolve().is_relative_to(project_dir)
    except OSError:
        return False


def web_addresses(containers: list[Container], port: int, ip: str | None) -> list[str]:
    """Where the web port is published, as addresses to probe (0.0.0.0 is probed on the project's address)."""
    out = []
    for c in containers:
        for host, p in sorted(c.published):
            if p == port:
                address = (ip or "127.0.0.1") if host in WILDCARDS else host
                if address not in out:
                    out.append(address)
    return out


def derive_state(published: bool, answering: bool, failure: str | None) -> tuple[str, str]:
    """-> (green|red|off, why). Running wins over an older failure."""
    if published and answering:
        return "green", "running"
    if failure:
        return "red", failure
    if published:
        return "red", "its web port is published but nothing answers"
    return "off", "stopped"


# --------------------------------------------------------------------------- snapshot

@dataclass
class Row:
    name: str
    state: str                  # green | red | off
    why: str
    action: str                 # what a click does: "up" or "down"
    address: str | None
    url: str
    cpu: float | None = None    # percent of one CPU, summed over the project's containers; None when none run
    mem_gb: float | None = None  # GB (10^9 bytes), summed
    containers: int = 0
    ids: list[str] = field(default_factory=list)    # its running containers


@dataclass
class Snapshot:
    rows: list[Row] = field(default_factory=list)
    error: str | None = None    # registry or docker problem, shown in the window
    stats: bool = False         # False: cpu/mem were not read this time (keep showing the previous values)


def docker(args: list[str], timeout: float = 15) -> str:
    r = subprocess.run(["docker", *args], text=True, capture_output=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        lines = (r.stderr or r.stdout).strip().splitlines()
        raise lpcp.LpcpError(lines[-1] if lines else f"docker {args[0]} failed")
    return r.stdout


class Monitor:
    def __init__(self, failures: dict[str, str] | None = None):
        self.failures = failures if failures is not None else {}

    def snapshot(self, stats: bool = True) -> Snapshot:
        try:
            reg = lpcp.load_registry()
        except (lpcp.LpcpError, OSError, ValueError) as e:
            return Snapshot(error=f"projects.toml: {e}")
        if not reg.projects:
            return Snapshot(error="No projects registered. Add one with: ./dev add <name> <dir>")
        try:
            containers = parse_ps(docker(["ps", "--format",
                                          f'{{{{.ID}}}}\t{{{{.Label "{LABEL}"}}}}\t{{{{.Ports}}}}']))
        except (lpcp.LpcpError, OSError, subprocess.TimeoutExpired) as e:
            rows = [Row(p.name, "off", "docker is not reachable", "up", None, lpcp.site_url(p, reg.settings))
                    for p in reg.projects.values()]
            return Snapshot(rows=rows, error=f"Docker is not reachable: {e}")
        groups = {}
        for name, p in reg.projects.items():
            try:
                project_dir = p.dir.resolve()
            except OSError:
                project_dir = p.dir
            groups[name] = [c for c in containers if belongs(c.workdir, project_dir)]
        usage: dict[str, tuple[float, float]] = {}
        ids = sorted({c.id for group in groups.values() for c in group})
        if stats and ids:
            try:
                usage = parse_stats(docker(["stats", "--no-stream", "--format",
                                            "{{.ID}}\t{{.CPUPerc}}\t{{.MemUsage}}", *ids], timeout=20))
            except (lpcp.LpcpError, OSError, subprocess.TimeoutExpired):
                usage = {}
        addresses = lpcp.load_addresses()
        rows = []
        for name, p in reg.projects.items():
            try:
                ip = lpcp.project_address(p, reg, addresses, assign=False)
            except lpcp.LpcpError:
                ip = None
            group = groups[name]
            web = web_addresses(group, p.port, ip)
            answering = any(lpcp.listening(p.port, a) for a in web)
            state, why = derive_state(bool(web), answering, self.failures.get(name))
            if state == "green":
                self.failures.pop(name, None)
            if state == "off" and group:
                why = f"stopped ({len(group)} other container{'s' if len(group) != 1 else ''} of it running)"
            row = Row(name, state, why, "down" if web else "up", ip, lpcp.site_url(p, reg.settings, ip),
                      containers=len(group), ids=[c.id for c in group])
            if group:
                row.cpu = sum(usage.get(c.id, (0.0, 0.0))[0] for c in group)
                row.mem_gb = sum(usage.get(c.id, (0.0, 0.0))[1] for c in group) / 1e9
            rows.append(row)
        return Snapshot(rows=rows, stats=stats)


# --------------------------------------------------------------------------- panic

PANIC_MIN_CPU = 5.0     # percent: below this nothing is busy enough to blame, so nothing is killed


def panic_target(rows: list[Row]) -> Row | None:
    """The running project using the most CPU, if it uses at least PANIC_MIN_CPU."""
    running = [r for r in rows if r.ids and r.cpu is not None]
    top = max(running, key=lambda r: r.cpu, default=None)
    return top if top is not None and top.cpu >= PANIC_MIN_CPU else None


def log_event(names: list[str], message: str) -> None:
    """Append a timestamped line to .state/logs/<name>.log for each name (best effort)."""
    try:
        logs = lpcp.state_dir() / "logs"
        logs.mkdir(exist_ok=True)
        for name in names:
            with (logs / f"{name}.log").open("a", encoding="utf-8") as f:
                f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")
    except OSError:
        pass


def panic(monitor: Monitor) -> str:
    """Kill (SIGKILL, no graceful stop: the machine may be struggling) every container of the busiest registered
    project. The containers are stopped, not removed: `./dev up <name>` starts it again. -> what happened, which
    is also logged to .state/logs/panic.log (and the killed project's own log)."""
    message, killed = _panic(monitor)
    log_event(["panic"] + ([killed] if killed else []), message)
    return message


def _panic(monitor: Monitor) -> tuple[str, str | None]:
    snapshot = monitor.snapshot(stats=True)
    if snapshot.error and not snapshot.rows:
        return f"Panic: nothing killed ({snapshot.error})", None
    usage = ", ".join(f"{r.name} {r.cpu:.1f}%" for r in snapshot.rows if r.cpu is not None) or "none running"
    top = panic_target(snapshot.rows)
    if top is None:
        return f"Panic: nothing killed, no project uses {PANIC_MIN_CPU:.0f}% CPU or more ({usage})", None
    try:
        docker(["kill", *top.ids], timeout=30)
    except (lpcp.LpcpError, OSError, subprocess.TimeoutExpired) as e:
        return f"Panic: killing {top.name} failed: {e}", top.name
    return (f"Panic: killed {top.name} ({top.cpu:.0f}% CPU, {len(top.ids)} container"
            f"{'s' if len(top.ids) != 1 else ''}; all: {usage}). Start it again with ./dev up {top.name}"), top.name


# --------------------------------------------------------------------------- start/stop

def failure_line(name: str, lines: list[str]) -> str:
    """The most useful line of a failed `./dev up|down <name>`: lpcp's own message, else the last line."""
    for line in reversed(lines):
        if line.startswith(f"{name}: ") and not line.startswith(f"{name}: http"):
            return line.removeprefix(f"{name}: ")
        if line.startswith(("error: ", "proxy: ")):
            return line
    return lines[-1] if lines else "failed"


class Actions:
    """Runs `./dev up|down <name>` one at a time; calls on_change() (from the worker thread) when one ends."""

    def __init__(self, on_change=lambda: None):
        self.on_change = on_change
        self.busy: dict[str, str] = {}          # name -> "up" | "down"
        self.failures: dict[str, str] = {}      # name -> why its last start/stop failed
        self.messages: list[str] = []           # newest last
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        threading.Thread(target=self._work, daemon=True).start()

    def request(self, name: str, verb: str) -> bool:
        with self._lock:
            if name in self.busy:
                return False
            self.busy[name] = verb
        self._queue.put((name, verb))
        return True

    def _work(self) -> None:
        while True:
            name, verb = self._queue.get()
            try:
                ok, message = self.run(name, verb)
            except Exception as e:  # keep the worker alive: a launcher has nowhere to show a traceback
                ok, message = False, f"{'start' if verb == 'up' else 'stop'} failed: {e}"
            with self._lock:
                self.busy.pop(name, None)
                if ok:
                    self.failures.pop(name, None)
                else:
                    self.failures[name] = message
                self.messages.append(f"{name}: {message}")
                del self.messages[:-20]
            self.on_change()

    def run(self, name: str, verb: str) -> tuple[bool, str]:
        logs = lpcp.state_dir() / "logs"
        logs.mkdir(exist_ok=True)
        log = logs / f"{name}.log"
        with log.open("a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')}  ./dev {verb} {name}\n")
            f.flush()
            start = f.tell()
            try:
                rc = subprocess.run([sys.executable, str(REPO / "dev"), verb, name], cwd=REPO,
                                    stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT,
                                    env={**os.environ, "PYTHONUNBUFFERED": "1"}).returncode
            except OSError as e:
                f.write(f"{e}\n")
                rc = 1
        if rc == 0:
            return True, "started" if verb == "up" else "stopped"
        with log.open(encoding="utf-8", errors="replace") as f:
            f.seek(start)
            lines = [line.strip() for line in f.read().splitlines() if line.strip()]
        return False, f"{'start' if verb == 'up' else 'stop'} failed: {failure_line(name, lines)} (log: {log})"
