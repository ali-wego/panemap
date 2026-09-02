"""Independent confirmation of a pane -> session pairing.

On zellij the pairing is inferred from pane ordering, so it deserves a check
that does not depend on that ordering. Two signals in a pane's own rendered
screen provide one:

self-identification
    Claude prints its scratchpad and artefact paths, and those paths contain
    its session id. Finding an id on a pane's screen identifies that pane
    outright.

prompt echo
    Lines the person typed are still on screen behind the ``❯`` marker. If one
    of them appears in the transcript we believe the pane holds, the pairing is
    confirmed from the other direction.

A caveat that cost real debugging time: the branch shown in Claude's status
line is the branch as of the pane's last *redraw*, not the branch the
conversation was on. It is not evidence of anything and is deliberately unused.
"""

from __future__ import annotations

import os
import re
import tempfile
from typing import Dict, List, Optional

from . import core, mux

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PROMPT = re.compile(r"❯\s+(.{15,})")
#: Prompt lines shorter than this are too generic to prove anything.
MIN_PROMPT = 15


def capture(backend: mux.Backend, rows: List[core.Row], out_dir: Optional[str] = None):
    """Dump every pane's screen. Returns ``{pane key: file path}``."""
    target = out_dir or tempfile.mkdtemp(prefix="ccmux-verify-")
    os.makedirs(target, exist_ok=True)
    dumps: Dict[str, str] = {}
    for row in rows:
        if not row.pane.key:
            continue
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", row.pane.tab) or "pane"
        path = os.path.join(target, "%s.%s.txt" % (safe, row.pane.key))
        if backend.dump_pane(row.pane.key, path):
            dumps[row.pane.key] = path
    return target, dumps


def check(row: core.Row, dump_path: str) -> List[str]:
    """Corroborating facts found on one pane's screen."""
    try:
        with open(dump_path, errors="replace") as fh:
            screen = fh.read()
    except OSError:
        return []

    found: List[str] = []
    sid = (row.session_id or "").lower()
    ids = {match.group(0) for match in UUID.finditer(screen.lower())}
    if sid and sid in ids:
        found.append("the pane prints this session id in its own paths")
    elif ids and sid:
        # Other ids on screen are artefact/message ids, not necessarily a
        # contradiction, so this is reported as unproven rather than as a clash.
        found.append("session id not visible on screen (nothing to confirm from)")

    if row.transcript:
        for match in PROMPT.finditer(screen):
            prompt = match.group(1).strip()
            if len(prompt) < MIN_PROMPT:
                continue
            if _in_transcript(row.transcript.path, prompt[:60]):
                found.append("a prompt on screen appears in this transcript")
                break
    return found


def _in_transcript(path: str, needle: str) -> bool:
    """Substring search over a transcript, streamed so size does not matter."""
    if not needle:
        return False
    probe = needle.encode("utf-8", "replace")
    # JSON escaping means the raw bytes may differ; compare against the escaped
    # form too, which is what a plain quote or backslash would become.
    escaped = (
        needle.replace("\\", "\\\\").replace('"', '\\"').encode("utf-8", "replace")
    )
    try:
        with open(path, "rb") as fh:
            tail = b""
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    return False
                window = tail + chunk
                if probe in window or escaped in window:
                    return True
                tail = window[-len(probe) - 8 :]
    except OSError:
        return False


def run(backend: mux.Backend, rows: List[core.Row], out_dir: Optional[str] = None):
    """Capture and check every pane, upgrading confidence where confirmed."""
    target, dumps = capture(backend, rows, out_dir)
    for row in rows:
        path = dumps.get(row.pane.key)
        if not path:
            row.issues.append("could not capture this pane's screen")
            continue
        facts = check(row, path)
        confirmed = [f for f in facts if "not visible" not in f]
        row.evidence.extend(facts)
        if confirmed and row.confidence == core.LIKELY:
            row.confidence = core.EXACT
    return target, dumps
