# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 AntzCode
"""One interface for a project's logs, whatever they are: a command that streams (`docker compose logs -f`) or
a file (`data/logs/error.log`). A project lists its sources with `./dev config.logs`, a JSON list of
`{"id": ..., "title": ..., "cmd": ...}` or `{"id": ..., "title": ..., "file": ...}`.

This module is the logic only (standard library, no GTK), so it is tested without a display and also drives the
command line (`./dev logs <site> --source ID --grep PATTERN`). The window is lpcp_logview.py.

- LogSource / parse_log_sources: what a project declares, validated.
- Stream: a running source. A command runs in the project's directory with the LPCP_* variables, a file is
  followed with `tail -F` (so a rotated or not yet created file works, and a big file is never read whole).
  Each runs in its own process group, which stop() ends, so no `docker compose logs` or `tail` is left behind.
- LineBuffer: the most recent lines, capped.
- LineFilter, find_matches: grep-style include / exclude patterns, and the positions of a search term.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
TAIL_LINES = 500        # lines of a file shown before following it
MAX_LINES = 20000       # lines a window keeps; older ones are dropped
MAX_MATCHES = 5000      # find stops here


class LogError(Exception):
    """A log source or pattern that can't be used; the message is for the user."""


@dataclass(frozen=True)
class LogSource:
    id: str
    title: str
    cmd: str | None = None      # a shell command that writes the log to stdout (and stderr) and keeps running
    file: str | None = None     # a file to follow: relative to the project's directory, or absolute, or ~/...

    @property
    def kind(self) -> str:
        return "cmd" if self.cmd is not None else "file"


def parse_log_sources(text: str) -> list[LogSource]:
    """The output of `./dev config.logs`: a non-empty JSON list; each entry has an id, a title and exactly one
    of cmd or file."""
    try:
        data = json.loads(text)
    except ValueError:
        raise LogError("config.logs did not print JSON") from None
    if not isinstance(data, list) or not data:
        raise LogError("config.logs must print a non-empty JSON list")
    out, seen = [], set()
    for n, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise LogError(f"config.logs entry {n} is not an object")
        sid, title, cmd, file = item.get("id"), item.get("title"), item.get("cmd"), item.get("file")
        if not (isinstance(sid, str) and NAME_RE.match(sid)):
            raise LogError(f"config.logs entry {n}: id must be lowercase letters, digits and dashes")
        if sid in seen:
            raise LogError(f"config.logs lists {sid!r} twice")
        seen.add(sid)
        if not isinstance(title, str) or not title.strip():
            raise LogError(f"config.logs entry {n} ({sid}) has no title")
        if (cmd is None) == (file is None):
            raise LogError(f"config.logs entry {n} ({sid}) needs exactly one of cmd or file")
        value = cmd if cmd is not None else file
        if not isinstance(value, str) or not value.strip():
            raise LogError(f"config.logs entry {n} ({sid}): cmd or file must be a non-empty string")
        out.append(LogSource(sid, title.strip(), cmd=cmd, file=file))
    return out


def source_argv(source: LogSource, project_dir: Path, tail: int = TAIL_LINES) -> tuple[list[str] | str, bool]:
    """-> (what to run, whether it is a shell command line)."""
    if source.cmd is not None:
        return source.cmd, True
    path = Path(source.file).expanduser()
    if not path.is_absolute():
        path = project_dir / path
    return ["tail", "-n", str(tail), "-F", "--", str(path)], False


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")


def clean(raw: bytes) -> str:
    """One line of output as text: invalid bytes replaced, colour codes and the line ending removed."""
    return ANSI_RE.sub("", raw.decode("utf-8", errors="replace").rstrip("\r\n")).replace("\r", "")


class Stream:
    """A running log source. A thread reads its lines; the consumer collects them with drain(). stop() ends the
    whole process group, and is safe to call again."""

    def __init__(self, source: LogSource, project_dir: Path, env: dict[str, str] | None = None,
                 tail: int = TAIL_LINES):
        self.source = source
        argv, shell = source_argv(source, project_dir, tail)
        self._lock = threading.Lock()
        self._lines: deque[str] = deque()
        self.ended = threading.Event()
        self.returncode: int | None = None
        self.error: str | None = None
        try:
            self._proc = subprocess.Popen(
                argv, shell=shell, cwd=project_dir, env={**os.environ, **(env or {})}, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            self._proc = None
            self.error = str(e)
            self.returncode = 127
            self.ended.set()
            return
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for raw in iter(self._proc.stdout.readline, b""):
            line = clean(raw)
            with self._lock:
                self._lines.append(line)
        self._proc.stdout.close()
        self.returncode = self._proc.wait()
        self.ended.set()

    def drain(self, limit: int | None = None) -> list[str]:
        """The lines read since the last call (at most `limit`, oldest first)."""
        with self._lock:
            n = len(self._lines) if limit is None else min(limit, len(self._lines))
            return [self._lines.popleft() for _ in range(n)]

    def pending(self) -> int:
        with self._lock:
            return len(self._lines)

    @property
    def running(self) -> bool:
        return not self.ended.is_set()

    def stop(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            deadline = time.monotonic() + 2.0
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            if proc.poll() is not None:
                break
        self.ended.wait(2.0)


class LineBuffer:
    """The most recent `maxlen` lines. `dropped` counts the older ones that were pushed out."""

    def __init__(self, maxlen: int = MAX_LINES):
        self.lines: deque[str] = deque(maxlen=maxlen)
        self.dropped = 0

    def extend(self, lines: list[str]) -> None:
        overflow = len(self.lines) + len(lines) - (self.lines.maxlen or 0)
        if self.lines.maxlen and overflow > 0:
            self.dropped += overflow
        self.lines.extend(lines)

    def clear(self) -> None:
        self.lines.clear()
        self.dropped = 0

    def __len__(self) -> int:
        return len(self.lines)


def compile_pattern(pattern: str, ignore_case: bool, what: str = "pattern") -> re.Pattern | None:
    if not pattern:
        return None
    try:
        return re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise LogError(f"{what}: {e}") from None


class LineFilter:
    """grep for a log: a line is shown when it matches `include` (when set) and not `exclude` (when set). Both
    are regular expressions, like `grep -E` and `grep -v`."""

    def __init__(self, include: str = "", exclude: str = "", ignore_case: bool = False):
        self.include_text, self.exclude_text, self.ignore_case = include, exclude, ignore_case
        self._include = compile_pattern(include, ignore_case, "include")
        self._exclude = compile_pattern(exclude, ignore_case, "exclude")

    def __call__(self, line: str) -> bool:
        if self._include is not None and not self._include.search(line):
            return False
        return not (self._exclude is not None and self._exclude.search(line))

    @property
    def active(self) -> bool:
        return self._include is not None or self._exclude is not None


def find_matches(lines: list[str], pattern: str, ignore_case: bool = True) -> list[tuple[int, int, int]]:
    """Where `pattern` occurs: (line index, start, end), in order, at most MAX_MATCHES. Empty matches are skipped."""
    rx = compile_pattern(pattern, ignore_case, "find")
    if rx is None:
        return []
    out = []
    for i, line in enumerate(lines):
        for m in rx.finditer(line):
            if m.end() > m.start():
                out.append((i, m.start(), m.end()))
                if len(out) >= MAX_MATCHES:
                    return out
    return out


LEVEL_ERROR = re.compile(r"\b(error|fatal|critical|emerg|alert|exception|traceback|panic)\b", re.IGNORECASE)
LEVEL_WARN = re.compile(r"\bwarn(ing)?\b", re.IGNORECASE)


def level(line: str) -> str | None:
    """'error', 'warn' or None: what a line looks like, for colouring."""
    if LEVEL_ERROR.search(line):
        return "error"
    if LEVEL_WARN.search(line):
        return "warn"
    return None


def follow(stream: Stream, flt: LineFilter, write, stop: threading.Event | None = None,
           poll: float = 0.05) -> int:
    """Send the stream's matching lines to `write` until it ends or `stop` is set. -> its exit code."""
    while True:
        batch = stream.drain()
        for line in batch:
            if flt(line):
                write(line)
        if stream.ended.is_set() and not stream.pending():
            return stream.returncode or 0
        if stop is not None and stop.is_set():
            return 0
        if not batch:
            time.sleep(poll)
