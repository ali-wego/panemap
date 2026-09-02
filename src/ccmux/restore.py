"""Generating the artefact that rebuilds the panes after a reboot.

The point of the artefact is that every pane comes back with its
``claude --resume <id>`` already typed but *not* run. Nothing is launched
without a keypress, so restoring a dozen panes does not start a dozen
conversations at once, and a pane whose transcript is gone comes back as a
plain shell rather than a misleading new session.
"""

from __future__ import annotations

import os
import shlex
from typing import List, Optional, Tuple

from . import core, mux

ZELLIJ_HEADER = """\
// Rebuilt by ccmux -- restores each pane's Claude session.
//
//   zellij -n {path}
//
// Use -n (--new-session-with-layout): plain `--layout` ADDS these tabs to a
// zellij session that is already running instead of starting a fresh one.
// Panes are start_suspended -- press ENTER in a pane to launch it.
"""


def write(
    snapshot: mux.Snapshot,
    rows: List[core.Row],
    out_dir: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Write the restore artefact. Returns ``(path, how-to-run)``."""
    os.makedirs(out_dir, exist_ok=True)
    if snapshot.backend == "zellij":
        return _zellij(snapshot, rows, out_dir)
    if snapshot.backend == "tmux":
        return _tmux(snapshot, rows, out_dir)
    return None, None


def _zellij(snapshot, rows, out_dir):
    if not snapshot.raw_layout.strip():
        return None, None
    path = os.path.join(out_dir, "restore.kdl")
    body = mux.rewrite_zellij_layout(
        snapshot.raw_layout, core.restore_session_ids(rows)
    )
    with open(path, "w") as fh:
        fh.write(ZELLIJ_HEADER.format(path=_tilde(path)))
        fh.write(body)
    return path, "zellij -n %s" % _tilde(path)


def _tmux(snapshot, rows, out_dir):
    """A shell script, because tmux has no layout-file equivalent.

    Only the windows that hold a Claude pane are rebuilt; splits and unrelated
    windows are not reproduced.
    """
    path = os.path.join(out_dir, "restore.sh")
    name = "ccmux"
    lines = [
        "#!/bin/sh",
        "# Rebuilt by ccmux -- restores each pane's Claude session.",
        "#   sh %s   then:  tmux attach -t %s" % (_tilde(path), name),
        "# Each pane has its resume command typed but NOT run; press Enter.",
        "set -e",
        'session=%s' % shlex.quote(name),
        'if tmux has-session -t "$session" 2>/dev/null; then',
        '  echo "tmux session $session already exists" >&2; exit 1',
        "fi",
        "",
    ]
    first = True
    for row in sorted(rows, key=lambda r: r.pane.order):
        window = row.pane.window or row.pane.tab
        cwd = row.cwd or os.path.expanduser("~")
        # Ask tmux for the index it actually assigned rather than assuming a
        # 0-based count, which `base-index` may well have shifted.
        create = "new-session -d -s \"$session\"" if first else 'new-window -t "$session"'
        lines.append(
            "index=$(tmux %s -n %s -c %s -P -F '#{window_index}')"
            % (create, shlex.quote(window), shlex.quote(cwd))
        )
        first = False
        command = row.resume_command_only()
        if command:
            # send-keys without a trailing Enter leaves the command typed at the
            # prompt, matching zellij's start_suspended.
            lines.append(
                'tmux send-keys -t "$session:$index" %s' % shlex.quote(command)
            )
        elif row.resumable:
            lines.append(
                "# %s: same session as another pane; resumed there instead" % window
            )
        else:
            lines.append("# %s: no transcript on disk, left as a plain shell" % window)
        lines.append("")
    lines.append('echo "attach with: tmux attach -t $session"')
    with open(path, "w") as fh:
        fh.write("\n".join(lines))
    os.chmod(path, 0o755)
    return path, "sh %s" % _tilde(path)


def _tilde(path: str) -> str:
    home = os.path.expanduser("~")
    return path.replace(home, "~", 1) if path.startswith(home) else path
