"""Output formats: a terminal table, JSON, and the Markdown recovery sheet."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from typing import List, Optional

from . import core, mux

MARK = {core.EXACT: " ", core.LIKELY: "~", core.CONFLICT: "!", core.UNKNOWN: "?"}


def when(moment: Optional[datetime]) -> str:
    if moment is None:
        return "unknown"
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def ago(moment: Optional[datetime]) -> str:
    if moment is None:
        return "?"
    seconds = (datetime.now(timezone.utc) - moment).total_seconds()
    if seconds < 3600:
        return "%dm" % max(1, seconds // 60)
    if seconds < 86400:
        return "%dh" % (seconds // 3600)
    return "%dd" % (seconds // 86400)


def _widths(rows: List[core.Row], budget: int):
    tab = max([len(r.pane.tab) for r in rows] + [3])
    tab = min(tab, 28)
    # session id (36) + age (5) + marker (1) + separators
    fixed = tab + 36 + 5 + 1 + 8
    return tab, max(24, budget - fixed)


def table(rows: List[core.Row], snapshot: mux.Snapshot, width: Optional[int] = None) -> str:
    if not rows:
        return "No Claude Code panes found in this %s session." % snapshot.backend
    budget = width or shutil.get_terminal_size((120, 24)).columns
    tab_w, desc_w = _widths(rows, budget)

    lines = [
        "%-*s  %-36s  %5s  %s"
        % (tab_w, "TAB", "SESSION", "AGE", "WHAT IT IS"),
        "%-*s  %-36s  %5s  %s"
        % (tab_w, "-" * tab_w, "-" * 36, "-" * 5, "-" * min(desc_w, 40)),
    ]
    for row in rows:
        desc = row.description
        if len(desc) > desc_w:
            desc = desc[: desc_w - 1] + "…"
        lines.append(
            "%-*s%s %-36s  %5s  %s"
            % (
                tab_w,
                _clip(row.pane.tab, tab_w),
                MARK.get(row.confidence, "?"),
                row.session_id or "-",
                # A session with no transcript has no meaningful age: what
                # matters is that there is nothing left to resume.
                "GONE" if not row.resumable else ago(row.last_active),
                desc,
            )
        )

    footnotes = []
    if any(r.confidence == core.LIKELY for r in rows):
        footnotes.append("~ pairing inferred from pane order, not confirmed")
    if any(r.confidence == core.CONFLICT for r in rows):
        footnotes.append("! pane and registry disagree - see `ccmux doctor`")
    if any(not r.resumable for r in rows):
        footnotes.append("GONE = transcript deleted; cannot be resumed")
    if footnotes:
        lines += [""] + ["  " + note for note in footnotes]
    return "\n".join(lines)


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def as_json(rows: List[core.Row], snapshot: mux.Snapshot) -> str:
    payload = {
        "backend": snapshot.backend,
        "session": snapshot.session,
        "generated": datetime.now(timezone.utc).isoformat(),
        "notes": snapshot.notes,
        "panes": [
            {
                "tab": row.pane.tab,
                "pane": row.pane.key,
                "pid": row.pane.pid,
                "session_id": row.session_id,
                "cwd": row.cwd,
                "branch": row.transcript.branch if row.transcript else None,
                "transcript": row.transcript.path if row.transcript else None,
                "transcript_mb": row.transcript.megabytes if row.transcript else None,
                "last_active": row.last_active.isoformat() if row.last_active else None,
                "description": row.description,
                "opened_with": row.transcript.opened_with if row.transcript else None,
                "last_message": row.transcript.last_message if row.transcript else None,
                "resumable": row.resumable,
                "resume": row.resume_command(),
                "confidence": row.confidence,
                "evidence": row.evidence,
                "issues": row.issues,
                "primary": row.owner,
            }
            for row in rows
        ],
    }
    return json.dumps(payload, indent=2)


def markdown(
    rows: List[core.Row],
    snapshot: mux.Snapshot,
    restore_hint: Optional[str] = None,
    rescue_dir: Optional[str] = None,
    findings: Optional[List[core.Finding]] = None,
) -> str:
    out = [
        "# Claude sessions by %s tab" % snapshot.backend,
        "",
        "Snapshot: %s  " % datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        "%s session: `%s`" % (snapshot.backend, snapshot.session or "?"),
        "",
    ]
    if restore_hint:
        out += [
            "Rebuild every pane after a reboot:",
            "",
            "```",
            restore_hint,
            "```",
            "",
        ]
    if rescue_dir:
        out += [
            "Last-known screen of each pane: `%s` - the only copy of anything "
            "whose transcript is already gone." % rescue_dir,
            "",
        ]

    out += [
        "| # | Tab | Session id | Description | Last active | Resume |",
        "|---|-----|------------|-------------|-------------|--------|",
    ]
    for index, row in enumerate(rows, 1):
        command = row.resume_command()
        cell = "`%s`" % command if command else "**unrecoverable**"
        if command and not row.owner:
            cell = "(resume in another tab)"
        out.append(
            "| %d | `%s` | `%s` | %s | %s | %s |"
            % (
                index,
                row.pane.tab,
                row.session_id or "?",
                row.description.replace("|", "\\|"),
                when(row.last_active),
                cell,
            )
        )

    out += ["", "## Detail", ""]
    for index, row in enumerate(rows, 1):
        transcript = row.transcript
        out += [
            "### %d. `%s`" % (index, row.pane.tab),
            "",
            "- session: `%s` (pid %s, %s)"
            % (row.session_id, row.pane.pid, row.registry.status if row.registry else "?"),
            "- cwd: `%s`" % row.cwd,
            "- branch: `%s` | transcript: %s | last active: %s"
            % (
                transcript.branch if transcript else "?",
                ("%s MB" % transcript.megabytes) if transcript else "none",
                when(row.last_active),
            ),
            "- pairing: %s (%s)"
            % (row.confidence, "; ".join(row.evidence) or "no corroboration"),
        ]
        if row.note:
            out.append("- what it is: %s" % row.note)
        if transcript:
            out += [
                "- opened with: %s" % (transcript.opened_with or "n/a"),
                "- last message: %s" % (transcript.last_message or "n/a"),
            ]
        for issue in row.issues:
            out.append("- **note:** %s" % issue)
        out.append("")

    if findings:
        out += ["## Health", ""]
        for finding in findings:
            out.append("- **%s** %s" % (finding.level, finding.message))
            if finding.fix:
                out.append("  - fix: %s" % finding.fix)
        out.append("")
    return "\n".join(out)
