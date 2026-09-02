"""Correlating panes, Claude's session registry and transcripts on disk."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from . import claude, mux, proc

#: Seconds by which a registry file's mtime may differ from its transcript's
#: last entry and still be considered corroborating. They are written by the
#: same process at nearly the same moment.
CORROBORATION_WINDOW = 15 * 60

EXACT = "exact"
LIKELY = "likely"
CONFLICT = "conflict"
UNKNOWN = "unknown"


@dataclass
class Row:
    pane: mux.Pane
    registry: Optional[claude.Registry] = None
    transcript: Optional[claude.Transcript] = None
    note: Optional[str] = None
    confidence: str = UNKNOWN
    evidence: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    #: False when another pane holds the same session and drove it more
    #: recently; that pane is the one that should resume it.
    owner: bool = True

    @property
    def session_id(self) -> Optional[str]:
        return self.registry.session_id if self.registry else None

    @property
    def cwd(self) -> Optional[str]:
        if self.registry and self.registry.cwd:
            return self.registry.cwd
        return self.pane.cwd

    @property
    def resumable(self) -> bool:
        return bool(self.transcript and self.session_id)

    @property
    def title(self) -> Optional[str]:
        """A name someone deliberately gave this session, if any."""
        if self.transcript and self.transcript.title:
            return self.transcript.title
        return self.registry.chosen_name if self.registry else None

    @property
    def description(self) -> str:
        """Best available label, most deliberate first.

        A local note wins because it is written for this listing. Then the
        session's own name, which the user set with ``claude -n`` and which
        Claude Code shows in its own picker too. The opening message is the
        fallback, and it is often not what the conversation became.
        """
        if self.note:
            return self.note
        if self.title:
            return self.title
        if self.transcript and self.transcript.opened_with:
            return self.transcript.opened_with
        return "(no transcript on disk)"

    @property
    def last_active(self) -> Optional[datetime]:
        if self.transcript and self.transcript.last_activity:
            return self.transcript.last_activity
        if self.registry:
            return datetime.fromtimestamp(self.registry.file_mtime, timezone.utc)
        return None

    def resume_command(self) -> Optional[str]:
        """Full command, including the cd - transcripts are keyed by project."""
        if not self.resumable:
            return None
        return "cd %s && claude --resume %s" % (self.cwd, self.session_id)

    def resume_command_only(self) -> Optional[str]:
        """Just the resume, for callers that set the working directory."""
        if not (self.resumable and self.owner):
            return None
        return "claude --resume %s" % self.session_id


def load_notes(path: str) -> Dict[str, str]:
    """Curated per-session descriptions, keyed by session id."""
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def build(
    backend: Optional[mux.Backend] = None,
    root: Optional[str] = None,
    notes_path: Optional[str] = None,
    scan: bool = True,
    processes=None,
    env_reader=None,
):
    """Snapshot the multiplexer and resolve every Claude pane to a session."""
    backend = backend or mux.detect()
    snapshot = backend.snapshot(processes=processes, env_reader=env_reader)
    registry = claude.read_registry(root)
    notes = load_notes(notes_path) if notes_path else {}

    rows: List[Row] = []
    for pane in snapshot.panes:
        row = Row(pane=pane)
        entry = registry.get(pane.pid) if pane.pid else None
        if entry is None and pane.pid:
            row.issues.append(
                "no %s/sessions/%d.json - this Claude build may be too old to "
                "record its session id" % (claude.config_dir(), pane.pid)
            )
        row.registry = entry
        sid = entry.session_id if entry else None

        if sid and scan:
            path = claude.find_transcript(sid, root)
            if path:
                try:
                    row.transcript = claude.scan_transcript(path)
                except OSError as exc:
                    row.issues.append("transcript unreadable: %s" % exc)
            else:
                row.issues.append(
                    "transcript is gone from disk - this session cannot be "
                    "resumed once the process exits"
                )

        _grade(row, backend, sid)
        row.note = notes.get(sid or "")
        rows.append(row)

    _resolve_duplicates(rows)
    return snapshot, rows


def _grade(row: Row, backend: mux.Backend, sid: Optional[str]) -> None:
    """Assign a confidence to the pane -> session pairing, and say why."""
    pinned = row.pane.pinned_session
    if pinned and sid:
        if pinned == sid.lower():
            row.confidence = EXACT
            row.evidence.append("the pane's own command line pins this session id")
        else:
            row.confidence = CONFLICT
            row.issues.append(
                "pane runs `--resume %s` but the registry reports %s; the "
                "pane-to-process pairing is wrong" % (pinned, sid)
            )
            return
    elif backend.exact:
        row.confidence = EXACT
        row.evidence.append("%s reports the pane's owning process" % backend.name)
    elif sid:
        row.confidence = LIKELY
        row.evidence.append(
            "matched by ascending %s pane id against layout order" % backend.name
        )
    else:
        row.confidence = UNKNOWN

    # The registry file and the transcript are written by the same process at
    # essentially the same time, so agreement is independent corroboration.
    if row.registry and row.transcript and row.transcript.last_activity:
        delta = abs(
            row.registry.file_mtime - row.transcript.last_activity.timestamp()
        )
        if delta <= CORROBORATION_WINDOW:
            row.evidence.append(
                "registry mtime agrees with the transcript's last entry"
            )
            if row.confidence == LIKELY:
                row.confidence = EXACT


def _resolve_duplicates(rows: List[Row]) -> None:
    """One session, several panes: the last one to drive it owns the resume.

    Resuming the same transcript in two panes interleaves both conversations
    into one file, so exactly one pane should reopen it.
    """
    by_session: Dict[str, List[Row]] = {}
    for row in rows:
        if row.session_id:
            by_session.setdefault(row.session_id, []).append(row)
    for sid, group in by_session.items():
        if len(group) < 2:
            continue
        best = max(group, key=lambda r: r.registry.updated_at if r.registry else 0)
        for row in group:
            row.owner = row is best
            others = [r.pane.tab for r in group if r is not row]
            if row.owner:
                row.issues.append(
                    "this session is also open in %s; resume it here only"
                    % ", ".join(repr(t) for t in others)
                )
            else:
                row.issues.append(
                    "same session as %r, which drove it more recently - resume "
                    "it there, not here" % best.pane.tab
                )


def restore_session_ids(rows: List[Row]) -> List[Optional[str]]:
    """Session id per Claude pane in document order, for layout rewriting."""
    return [
        row.session_id if (row.resumable and row.owner) else None
        for row in sorted(rows, key=lambda r: r.pane.order)
    ]


# --------------------------------------------------------------------------- #
# health checks
# --------------------------------------------------------------------------- #

@dataclass
class Finding:
    level: str  # "risk" | "warn" | "info"
    message: str
    fix: Optional[str] = None


def diagnose(rows: List[Row], root: Optional[str] = None) -> List[Finding]:
    """Everything that could cost the user a conversation on next reboot."""
    root = root or claude.config_dir()
    findings: List[Finding] = []
    days = claude.cleanup_period_days(root)

    gone = [r for r in rows if r.session_id and not r.transcript]
    for row in gone:
        findings.append(
            Finding(
                "risk",
                "%r (%s) has no transcript on disk; only the running process "
                "still holds this conversation."
                % (row.pane.tab, row.session_id),
                "Run /export in that pane before shutting down, and keep a "
                "screen capture with `panemap rescue`.",
            )
        )

    # Cleanup deletes by file mtime, so an idle session's transcript ages out
    # even though the process is still alive.
    horizon = days - 7
    now = time.time()
    for row in rows:
        if not row.transcript:
            continue
        age = (now - os.path.getmtime(row.transcript.path)) / 86400
        if age >= horizon > 0:
            findings.append(
                Finding(
                    "risk",
                    "%r transcript is %d days old and cleanupPeriodDays is %d; "
                    "Claude Code deletes it on a startup after %d days."
                    % (row.pane.tab, age, days, days),
                    'Raise it: set "cleanupPeriodDays" in %s/settings.json.'
                    % root,
                )
            )

    if days == claude.DEFAULT_CLEANUP_DAYS:
        findings.append(
            Finding(
                "info",
                "cleanupPeriodDays is unset, so transcripts are deleted after "
                "%d days and long-idle sessions become unresumable."
                % claude.DEFAULT_CLEANUP_DAYS,
                'Set "cleanupPeriodDays": 3650 in %s/settings.json.' % root,
            )
        )

    for row in rows:
        if row.confidence == CONFLICT:
            findings.append(
                Finding("warn", "%r: %s" % (row.pane.tab, row.issues[-1]))
            )
        if not row.owner:
            findings.append(Finding("warn", "%r: %s" % (row.pane.tab, row.issues[-1])))

    stale = [
        pid
        for pid in claude.read_registry(root)
        if not proc.alive(pid)
    ]
    if len(stale) > 20:
        findings.append(
            Finding(
                "info",
                "%d registry entries in %s/sessions/ belong to processes that "
                "have exited." % (len(stale), root),
            )
        )
    return findings
