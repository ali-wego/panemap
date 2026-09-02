"""Reading Claude Code's own on-disk state: the session registry and transcripts.

Two things live under the Claude config dir (``~/.claude`` unless
``CLAUDE_CONFIG_DIR`` says otherwise):

``sessions/<pid>.json``
    Written by each running Claude Code process. Holds the authoritative
    ``sessionId`` for that PID, plus cwd/status/updatedAt. This is the only
    reliable PID -> session-id link; nothing else on the system records it.

``projects/<slug>/<session-id>.jsonl``
    The conversation transcript. ``<slug>`` is the session's cwd with ``/`` and
    ``.`` replaced by ``-``, but we always locate transcripts by glob rather
    than by recomputing the slug, so the encoding can change without breaking.

A session with no transcript file cannot be resumed: the file *is* the
conversation. Claude Code deletes transcripts older than ``cleanupPeriodDays``
(default 30) on startup, so idle sessions quietly become unrecoverable.
"""

from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

DEFAULT_CLEANUP_DAYS = 30
#: How much of a transcript's tail to read when looking for the last message.
TAIL_BYTES = 4_000_000
#: How many leading lines to scan for the opening message.
HEAD_LINES = 4000
#: Leading lines always scanned for a startup ``customTitle``, even after the
#: opening message has been found.
TITLE_WINDOW = 60


def config_dir() -> str:
    """Claude Code's config directory, honouring CLAUDE_CONFIG_DIR."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return os.path.expanduser(env)
    return os.path.join(os.path.expanduser("~"), ".claude")


def cleanup_period_days(root: Optional[str] = None) -> int:
    """Days a transcript survives before Claude Code deletes it."""
    root = root or config_dir()
    try:
        with open(os.path.join(root, "settings.json")) as fh:
            value = json.load(fh).get("cleanupPeriodDays")
    except (OSError, ValueError):
        return DEFAULT_CLEANUP_DAYS
    return value if isinstance(value, int) and value > 0 else DEFAULT_CLEANUP_DAYS


@dataclass
class Registry:
    """One ``sessions/<pid>.json`` entry."""

    pid: int
    session_id: Optional[str]
    cwd: Optional[str]
    status: Optional[str]
    kind: Optional[str]
    version: Optional[str]
    updated_at: int
    #: mtime of the registry file itself - tracks the session's last activity
    #: closely enough to cross-check the transcript, and needs no parsing.
    file_mtime: float
    #: Display name, and where it came from. Claude Code derives a throwaway
    #: name (``myproject-0a``) for every session, so only a name whose source
    #: is *not* "derived" was actually chosen by the user and worth showing.
    name: Optional[str] = None
    name_source: Optional[str] = None

    @property
    def chosen_name(self) -> Optional[str]:
        """The name only if a person set it, never Claude's derived one."""
        if self.name and self.name_source and self.name_source != "derived":
            return self.name
        return None

    @property
    def interactive(self) -> bool:
        """True for the TUI sessions that occupy a pane.

        Older Claude Code builds omit ``kind`` entirely; treat those as
        interactive rather than dropping them.
        """
        return self.kind in (None, "interactive")


def read_registry(root: Optional[str] = None) -> Dict[int, Registry]:
    """All registry entries, keyed by PID. Unparseable files are skipped."""
    root = root or config_dir()
    out: Dict[int, Registry] = {}
    for path in glob.glob(os.path.join(root, "sessions", "*.json")):
        stem = os.path.basename(path)[:-5]
        if not stem.isdigit():
            continue  # e.g. the sibling "<pid>.<hash>.key" files
        try:
            with open(path) as fh:
                data = json.load(fh)
            mtime = os.path.getmtime(path)
        except (OSError, ValueError):
            continue
        pid = data.get("pid")
        if not isinstance(pid, int):
            pid = int(stem)
        out[pid] = Registry(
            pid=pid,
            session_id=data.get("sessionId"),
            cwd=data.get("cwd"),
            status=data.get("status"),
            kind=data.get("kind"),
            version=data.get("version"),
            updated_at=data.get("updatedAt") or 0,
            file_mtime=mtime,
            name=data.get("name"),
            name_source=data.get("nameSource"),
        )
    return out


def find_transcript(session_id: str, root: Optional[str] = None) -> Optional[str]:
    """Path to a session's transcript, or None if it is not on disk."""
    if not session_id:
        return None
    root = root or config_dir()
    hits = glob.glob(os.path.join(root, "projects", "*", session_id + ".jsonl"))
    return hits[0] if hits else None


def project_slug(path: str) -> str:
    """Claude Code's directory name for a project path."""
    return re.sub(r"[/.]", "-", path)


# --------------------------------------------------------------------------- #
# transcript parsing
# --------------------------------------------------------------------------- #

def _entry_text(entry: dict) -> str:
    message = entry.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _is_human_turn(entry: dict) -> bool:
    """True for something the person actually typed.

    Transcripts also carry tool results, injected reminders, command output and
    meta entries as ``type: "user"``; none of those describe the conversation.
    """
    if entry.get("type") != "user" or entry.get("isMeta"):
        return False
    if entry.get("isVisibleInTranscript") is False:
        return False
    text = _entry_text(entry).strip()
    if not text or text.startswith("<") or text.startswith("[Request interrupted"):
        return False
    head = text[:200]
    return "system-reminder" not in head and "local-command" not in head


def squeeze(text: str, limit: int = 150) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return (text[: limit - 1] + "…") if len(text) > limit else text


@dataclass
class Transcript:
    path: str
    size: int
    #: Name given with ``claude -n <name>`` (or a later rename). Recorded as a
    #: ``custom-title`` entry and absent unless someone actually set one, which
    #: makes it a trustworthy label -- and unlike the registry it survives the
    #: process exiting, so a dormant session still knows what it was called.
    title: Optional[str] = None
    opened_with: Optional[str] = None
    last_message: Optional[str] = None
    branch: Optional[str] = None
    last_activity: Optional[datetime] = None
    #: Every session id seen in the file. A resumed conversation can carry more
    #: than one, which is how a chain of resumes is spotted.
    session_ids: List[str] = field(default_factory=list)

    @property
    def megabytes(self) -> float:
        return round(self.size / 1e6, 1)


def _parse_ts(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def scan_transcript(path: str, limit: int = 150) -> Transcript:
    """Read a transcript's head and tail without loading the whole file.

    Transcripts reach hundreds of megabytes, so the opening message comes from
    the first lines and everything else from the final few MB.
    """
    size = os.path.getsize(path)
    result = Transcript(path=path, size=size)
    seen_ids: List[str] = []

    with open(path, "rb") as fh:
        for index in range(HEAD_LINES):
            line = fh.readline()
            if not line:
                break
            entry = _load(line)
            if entry is None:
                continue
            result.branch = result.branch or entry.get("gitBranch")
            result.title = entry.get("customTitle") or result.title
            sid = entry.get("sessionId")
            if sid and sid not in seen_ids:
                seen_ids.append(sid)
            if result.opened_with is None and _is_human_turn(entry):
                result.opened_with = squeeze(_entry_text(entry), limit)
            # A name given at startup is recorded near the top, so stop once
            # the opening message is in hand and that window has passed. A name
            # set later in the conversation is picked up by the tail scan
            # instead of by reading the whole file.
            if result.opened_with and index >= TITLE_WINDOW:
                break

        fh.seek(max(0, size - TAIL_BYTES))
        if size > TAIL_BYTES:
            fh.readline()  # discard the partial line we landed in
        for line in fh:
            entry = _load(line)
            if entry is None:
                continue
            result.branch = entry.get("gitBranch") or result.branch
            result.title = entry.get("customTitle") or result.title
            result.last_activity = _parse_ts(entry.get("timestamp")) or result.last_activity
            sid = entry.get("sessionId")
            if sid and sid not in seen_ids:
                seen_ids.append(sid)
            if _is_human_turn(entry):
                result.last_message = squeeze(_entry_text(entry), limit)

    result.session_ids = seen_ids
    return result


def _load(line: bytes) -> Optional[dict]:
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def iter_transcripts(
    root: Optional[str] = None, project: Optional[str] = None
) -> Iterable[str]:
    """Transcript paths, optionally restricted to one project directory."""
    root = root or config_dir()
    slug = project_slug(project) if project else "*"
    return glob.glob(os.path.join(root, "projects", slug, "*.jsonl"))
