"""Process inspection, portable across macOS and Linux.

Two things are needed that ``os`` will not give us: the argv of every process,
and the environment of a process we did not start. The environment is where a
multiplexer stamps its pane identity, so on the terminals that provide no
pane->pid API it is the only way to tell which pane a process is sitting in.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional

#: Matches the interactive TUI only: bare ``claude``, or a resume/continue form
#: with or without an explicit session argument. Deliberately excludes the
#: daemon, ``bg-pty-host``, ``bg-spare`` and one-shot ``-p`` invocations, none
#: of which own a pane. The session argument is not assumed to be a UUID -- it
#: is whatever the user typed, and rejecting non-UUIDs here would silently drop
#: the pane from the inventory.
INTERACTIVE_CLAUDE = re.compile(
    r"^(?:\S*/)?claude(?:\s+(?:--resume|-r|--continue|-c)(?:\s+[^\s-]\S*)?)?\s*$"
)


@dataclass
class Process:
    pid: int
    ppid: int
    args: str


def list_processes() -> List[Process]:
    """Every process visible to this user."""
    out = _run(["ps", "-eo", "pid=,ppid=,args="])
    processes = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            processes.append(Process(int(parts[0]), int(parts[1]), parts[2].strip()))
        except ValueError:
            continue
    return processes


def claude_processes(processes: Optional[List[Process]] = None) -> List[Process]:
    """Interactive Claude Code TUIs, newest PID last."""
    procs = processes if processes is not None else list_processes()
    return sorted(
        (p for p in procs if INTERACTIVE_CLAUDE.match(p.args)), key=lambda p: p.pid
    )


def environ(pid: int) -> Dict[str, str]:
    """The environment of a running process. Empty dict if unreadable.

    Linux exposes this as a NUL-separated blob in procfs. macOS has no procfs,
    so ``ps eww`` is the only route, and it separates entries with spaces --
    meaning a value containing a space is indistinguishable from two entries.
    Only ``KEY=VALUE`` shaped tokens are kept, which is enough for pane ids.
    """
    procfs = "/proc/%d/environ" % pid
    if os.path.exists(procfs):
        try:
            with open(procfs, "rb") as fh:
                raw = fh.read()
        except OSError:
            return {}
        return _pairs(raw.decode("utf-8", "replace").split("\0"))

    out = _run(["ps", "eww", "-p", str(pid)])
    lines = out.splitlines()
    if len(lines) < 2:
        return {}
    return _pairs(" ".join(lines[1:]).split(" "))


def _pairs(tokens) -> Dict[str, str]:
    env = {}
    for token in tokens:
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        # argv precedes the environment in `ps eww` output; a real variable
        # name never contains a slash or a space.
        if key and "/" not in key and " " not in key and not key.startswith("-"):
            env[key] = value
    return env


def ancestry(pid: int, processes: List[Process]) -> List[int]:
    """PIDs from ``pid`` up to init, so a pane's shell can be found above it."""
    parents = {p.pid: p.ppid for p in processes}
    chain, seen = [pid], {pid}
    while True:
        parent = parents.get(chain[-1])
        if not parent or parent in seen or parent <= 1:
            return chain
        chain.append(parent)
        seen.add(parent)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _run(cmd: List[str]) -> str:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout
