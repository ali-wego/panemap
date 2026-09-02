"""Terminal-multiplexer backends.

Each backend answers two questions: *what panes exist* and *which process sits
in each one*. How well it can answer the second differs, and that difference is
the main source of uncertainty in this tool:

tmux
    ``list-panes`` reports ``pane_pid`` directly, so a Claude process is matched
    to its pane by walking up its parent chain. Exact.

zellij
    Exposes no pane->pid mapping at all. Every process it starts inherits
    ``ZELLIJ_PANE_ID`` in its environment, and pane ids are allocated in
    ascending creation order, so sorting live processes by pane id lines them up
    with the panes in ``dump-layout`` document order. Correct in ordinary use,
    but an assumption -- see ``core.assign`` for how it is checked.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import proc


@dataclass
class Pane:
    """A pane that is running Claude Code."""

    #: Backend-native identifier, used for pane-targeted commands.
    key: str
    #: The tab (zellij) or window (tmux) name the user sees.
    tab: str
    #: Position in document order, used for the zellij ordering match.
    order: int
    cwd: Optional[str] = None
    #: Bare window/tab name without any session:index prefix, for rebuilding.
    window: Optional[str] = None
    #: Session id the pane's command line pins, when it has one. An anchor:
    #: it must agree with whatever process we match to this pane.
    pinned_session: Optional[str] = None
    #: Owning process, when the backend can say so authoritatively.
    pid: Optional[int] = None


@dataclass
class Snapshot:
    backend: str
    session: Optional[str]
    panes: List[Pane]
    #: Backend-native layout text, kept verbatim so restore can edit rather
    #: than regenerate it.
    raw_layout: str = ""
    notes: List[str] = field(default_factory=list)


class Backend:
    name = "?"
    #: True when the backend knows pane->pid without guessing.
    exact = False

    def available(self) -> bool:
        raise NotImplementedError

    def snapshot(self, processes=None, env_reader=None) -> Snapshot:
        """Panes plus their owning processes.

        ``processes`` and ``env_reader`` exist so the correlation logic can be
        driven with fixtures instead of a live multiplexer.
        """
        raise NotImplementedError

    def dump_pane(self, key: str, path: str) -> bool:
        """Write a pane's current screen to ``path``. False if unsupported."""
        return False


# --------------------------------------------------------------------------- #
# zellij
# --------------------------------------------------------------------------- #

_TAB = re.compile(r'^tab name="((?:[^"\\]|\\.)*)"')
_PANE_CMD = re.compile(r'^pane command="((?:[^"\\]|\\.)*)"(?:\s+cwd="((?:[^"\\]|\\.)*)")?')
_ARGS = re.compile(r"^args\s+(.*)$")
_LAYOUT_CWD = re.compile(r'^cwd\s+"((?:[^"\\]|\\.)*)"')
_UUID = re.compile(r"^[0-9a-fA-F-]{36}$")
#: Sections of dump-layout output that describe templates, not live tabs.
_TEMPLATE_KEYS = ("new_tab_template", "swap_tiled_layout", "swap_floating_layout")


class Zellij(Backend):
    name = "zellij"
    exact = False

    def available(self) -> bool:
        return shutil.which("zellij") is not None

    def inside(self) -> bool:
        return bool(os.environ.get("ZELLIJ_SESSION_NAME"))

    def snapshot(self, processes=None, env_reader=None) -> Snapshot:
        raw = self._run(["zellij", "action", "dump-layout"])
        panes = parse_zellij_layout(raw)
        session = os.environ.get("ZELLIJ_SESSION_NAME")
        snap = Snapshot(self.name, session, panes, raw_layout=raw)

        # ZELLIJ_PANE_ID is the only pane identity a process carries, and pane
        # ids ascend with creation, so ascending id == document order.
        read_env = env_reader or proc.environ
        procs = proc.claude_processes(processes)
        with_pane = []
        for process in procs:
            env = read_env(process.pid)
            pane_id = env.get("ZELLIJ_PANE_ID")
            if pane_id is None:
                continue  # not started by this zellij (or env unreadable)
            if session and env.get("ZELLIJ_SESSION_NAME") not in (None, session):
                continue  # belongs to a different zellij session
            try:
                with_pane.append((int(pane_id), process))
            except ValueError:
                continue
        with_pane.sort(key=lambda item: item[0])

        for pane, (pane_id, process) in zip(panes, with_pane):
            pane.pid = process.pid
            pane.key = str(pane_id)
        if len(panes) != len(with_pane):
            snap.notes.append(
                "%d Claude pane(s) in the layout but %d live Claude process(es) "
                "carrying a zellij pane id; the pairing may be offset."
                % (len(panes), len(with_pane))
            )
        return snap

    def dump_pane(self, key: str, path: str) -> bool:
        """Dump a pane by id, which needs no focus change."""
        done = subprocess.run(
            ["zellij", "action", "dump-screen", "-p", key, "--full", "--path", path],
            capture_output=True,
            text=True,
        )
        return done.returncode == 0 and os.path.exists(path)

    def _run(self, cmd: List[str]) -> str:
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return ""
        return done.stdout if done.returncode == 0 else ""


def _unquote(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")


def _join_cwd(base: str, cwd: Optional[str]) -> Optional[str]:
    """dump-layout emits a layout-level cwd and pane cwds relative to it."""
    if cwd is None:
        return base or None
    if cwd.startswith("/"):
        return cwd
    if base:
        return os.path.normpath(os.path.join(base, cwd))
    return cwd


def parse_zellij_layout(text: str) -> List[Pane]:
    """Claude panes from ``zellij action dump-layout``, in document order.

    Tracks brace depth so that template and swap-layout sections -- which
    describe hypothetical tabs, not real ones -- are skipped.
    """
    panes: List[Pane] = []
    depth = 0
    base_cwd = ""
    tab = ""
    tab_index = 0
    skip_until: Optional[int] = None
    current: Optional[Pane] = None

    for line in text.splitlines():
        stripped = line.strip()
        opens = stripped.count("{") - stripped.count("}")

        if skip_until is not None:
            depth += opens
            if depth <= skip_until:
                skip_until = None
            continue

        if depth == 1 and any(stripped.startswith(key) for key in _TEMPLATE_KEYS):
            skip_until = depth
            depth += opens
            continue

        if depth <= 1:
            match = _LAYOUT_CWD.match(stripped)
            if match:
                base_cwd = _unquote(match.group(1))
                if not base_cwd.startswith("/"):
                    base_cwd = "/" + base_cwd

        match = _TAB.match(stripped)
        if match:
            tab = _unquote(match.group(1))
            tab_index += 1

        match = _PANE_CMD.match(stripped)
        if match and os.path.basename(_unquote(match.group(1))) == "claude":
            label = tab or "tab %d" % tab_index
            current = Pane(
                key="",
                tab=label,
                order=len(panes),
                cwd=_join_cwd(base_cwd, match.group(2) and _unquote(match.group(2))),
                window=label,
            )
            panes.append(current)
            if not stripped.endswith("{"):
                current = None  # property-less pane, no args block follows
        elif current is not None:
            match = _ARGS.match(stripped)
            if match:
                values = re.findall(r'"((?:[^"\\]|\\.)*)"', match.group(1))
                for value in values:
                    if _UUID.match(value):
                        current.pinned_session = value.lower()
                current = None
            elif stripped.startswith("}"):
                current = None

        depth += opens

    return panes


def rewrite_zellij_layout(text: str, session_ids: List[Optional[str]]) -> str:
    """Return ``dump-layout`` text with each Claude pane pinned to a session.

    Editing the live layout rather than regenerating one keeps splits, floating
    panes, plugins and templates exactly as they are. ``session_ids`` is
    positional: one entry per Claude pane in document order, ``None`` to leave a
    pane unpinned (it will open a plain shell instead).
    """
    out: List[str] = []
    depth = 0
    #: Inside a template section: copy verbatim, process nothing.
    keep_until: Optional[int] = None
    #: Inside the property block of a pane we have already replaced: discard.
    drop_until: Optional[int] = None
    #: A pinned pane whose block is open and still needs its args line.
    pending: Optional[Tuple[int, str, str]] = None  # depth, session id, indent
    seen = 0

    for line in text.splitlines():
        stripped = line.strip()
        opens = stripped.count("{") - stripped.count("}")
        indent = line[: len(line) - len(line.lstrip())]

        if drop_until is not None:
            depth += opens
            if depth <= drop_until:
                drop_until = None
            continue

        if keep_until is not None:
            out.append(line)
            depth += opens
            if depth <= keep_until:
                keep_until = None
            continue

        if depth == 1 and any(stripped.startswith(key) for key in _TEMPLATE_KEYS):
            keep_until = depth
            out.append(line)
            depth += opens
            continue

        match = _PANE_CMD.match(stripped)
        if match and os.path.basename(_unquote(match.group(1))) == "claude":
            sid = session_ids[seen] if seen < len(session_ids) else None
            seen += 1
            if sid is None:
                # Strip the command so the pane opens a plain shell in the same
                # directory, and discard the old properties along with it.
                cwd = match.group(2)
                out.append(indent + "pane" + (' cwd="%s"' % cwd if cwd else ""))
                if stripped.endswith("{"):
                    drop_until = depth
                    depth += opens
                continue
            if stripped.endswith("{"):
                out.append(line)
                depth += opens
                pending = (depth, sid, indent + "    ")
            else:
                # A property-less pane needs a block built around it.
                out.append(line + " {")
                out.append(indent + '    args "--resume" "%s"' % sid)
                out.append(indent + "    start_suspended true")
                out.append(indent + "}")
            continue

        if pending is not None:
            pending_depth, sid, pending_indent = pending
            if depth == pending_depth and _ARGS.match(stripped):
                out.append(pending_indent + 'args "--resume" "%s"' % sid)
                pending = None
                depth += opens
                continue
            if depth == pending_depth and stripped.startswith("}"):
                # Block is closing and it had no args of its own.
                out.append(pending_indent + 'args "--resume" "%s"' % sid)
                pending = None

        out.append(line)
        depth += opens

    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# tmux
# --------------------------------------------------------------------------- #

#: Tab-separated so that paths and window names containing spaces survive.
TMUX_FORMAT = "\t".join(
    [
        "#{session_name}",
        "#{window_index}",
        "#{window_name}",
        "#{pane_index}",
        "#{pane_id}",
        "#{pane_pid}",
        "#{pane_current_path}",
    ]
)


class Tmux(Backend):
    name = "tmux"
    exact = True

    def available(self) -> bool:
        return shutil.which("tmux") is not None

    def inside(self) -> bool:
        return bool(os.environ.get("TMUX"))

    def snapshot(self, processes=None, env_reader=None) -> Snapshot:
        raw = self._run(["tmux", "list-panes", "-a", "-F", TMUX_FORMAT])
        session = os.environ.get("TMUX_PANE") and self._run(
            ["tmux", "display-message", "-p", "#{session_name}"]
        ).strip()

        processes = processes if processes is not None else proc.list_processes()
        claude = {p.pid: p for p in proc.claude_processes(processes)}

        panes: List[Pane] = []
        for order, line in enumerate(raw.splitlines()):
            fields = line.split("\t")
            if len(fields) < 7:
                continue
            sess, win_idx, win_name, pane_idx, pane_id, pane_pid, path = fields[:7]
            try:
                pane_pid_int = int(pane_pid)
            except ValueError:
                continue
            # A pane reports its shell's pid; claude runs as a descendant.
            owner = None
            for pid in list(claude):
                if pane_pid_int in proc.ancestry(pid, processes):
                    owner = pid
                    break
            if owner is None:
                continue
            pinned = None
            match = re.search(r"--resume\s+([0-9a-fA-F-]{36})", claude[owner].args)
            if match:
                pinned = match.group(1).lower()
            panes.append(
                Pane(
                    key=pane_id,
                    tab="%s:%s.%s %s" % (sess, win_idx, pane_idx, win_name)
                    if win_name
                    else "%s:%s.%s" % (sess, win_idx, pane_idx),
                    order=order,
                    cwd=path,
                    window=win_name or None,
                    pinned_session=pinned,
                    pid=owner,
                )
            )
            claude.pop(owner, None)

        return Snapshot(self.name, session or None, panes, raw_layout=raw)

    def dump_pane(self, key: str, path: str) -> bool:
        out = self._run(["tmux", "capture-pane", "-p", "-J", "-t", key])
        if not out:
            return False
        with open(path, "w") as fh:
            fh.write(out)
        return True

    def _run(self, cmd: List[str]) -> str:
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return ""
        return done.stdout if done.returncode == 0 else ""


BACKENDS: Dict[str, Backend] = {}


def _register() -> None:
    for backend in (Zellij(), Tmux()):
        BACKENDS[backend.name] = backend


_register()


def detect(preferred: Optional[str] = None) -> Backend:
    """Pick a backend: an explicit choice, else whichever session we are in."""
    if preferred:
        backend = BACKENDS.get(preferred)
        if backend is None:
            raise SystemExit(
                "unknown multiplexer %r (known: %s)"
                % (preferred, ", ".join(sorted(BACKENDS)))
            )
        if not backend.available():
            # Otherwise every command reports "no panes found", which reads as
            # an empty session rather than a missing program.
            raise SystemExit("%s was requested but is not on PATH" % preferred)
        return backend
    for backend in BACKENDS.values():
        if backend.available() and getattr(backend, "inside", lambda: False)():
            return backend
    for backend in BACKENDS.values():
        if backend.available():
            return backend
    raise SystemExit(
        "no supported multiplexer found on PATH (looked for: %s)"
        % ", ".join(sorted(BACKENDS))
    )
