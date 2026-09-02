"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone
from typing import List, Optional

from . import claude, core, mux, render, restore, verify

HOME_DIR = os.path.join(os.path.expanduser("~"), ".panemap")
NOTES_FILE = os.path.join(HOME_DIR, "notes.json")

DESCRIPTION = """\
Map terminal-multiplexer panes to the Claude Code sessions running in them,
and rebuild them after a reboot.
"""


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="panemap", description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mux", choices=sorted(mux.BACKENDS), help="force a multiplexer backend"
    )
    parser.add_argument(
        "--claude-dir", help="Claude config dir (default: $CLAUDE_CONFIG_DIR or ~/.claude)"
    )
    parser.add_argument(
        "--notes", default=NOTES_FILE,
        help="JSON file of {session id: description} (default: %s)" % NOTES_FILE,
    )
    sub = parser.add_subparsers(dest="command")

    listing = sub.add_parser("list", help="show each pane and its session (default)")
    listing.add_argument("--json", action="store_true", help="machine-readable output")
    listing.add_argument("--md", action="store_true", help="Markdown recovery sheet")

    saver = sub.add_parser("save", help="write the recovery sheet and restore artefact")
    saver.add_argument("--out", default=HOME_DIR, help="output directory")
    saver.add_argument(
        "--rescue", action="store_true",
        help="also capture every pane's screen (the only backup for a session "
             "whose transcript is already deleted)",
    )

    sub.add_parser("doctor", help="report what could cost you a conversation")
    sub.add_parser(
        "verify", help="confirm each pairing from the pane's own screen contents"
    )

    rescue = sub.add_parser("rescue", help="capture every Claude pane's screen")
    rescue.add_argument("--out", default=os.path.join(HOME_DIR, "rescue"))

    sessions = sub.add_parser(
        "sessions", help="browse recent transcripts, whether or not a pane holds them"
    )
    sessions.add_argument("-n", "--limit", type=int, default=20)
    sessions.add_argument(
        "--project", help="restrict to one project path (default: current directory)"
    )
    sessions.add_argument("--all", action="store_true", help="every project")

    args = parser.parse_args(argv)
    command = args.command or "list"

    if command == "sessions":
        return _sessions(args)

    try:
        backend = mux.detect(args.mux)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2

    snapshot, rows = core.build(
        backend=backend, root=args.claude_dir, notes_path=args.notes
    )
    for note in snapshot.notes:
        print("warning: %s" % note, file=sys.stderr)

    if command == "list":
        if args.json:
            print(render.as_json(rows, snapshot))
        elif args.md:
            print(render.markdown(rows, snapshot))
        else:
            print(render.table(rows, snapshot))
        return 0

    if command == "doctor":
        return _doctor(rows, args.claude_dir)

    if command == "verify":
        return _verify(backend, snapshot, rows)

    if command == "rescue":
        target, dumps = verify.capture(backend, rows, args.out)
        print("captured %d of %d pane(s) to %s" % (len(dumps), len(rows), target))
        return 0 if dumps else 1

    if command == "save":
        return _save(backend, snapshot, rows, args)

    parser.print_help()
    return 1


def _save(backend, snapshot, rows, args) -> int:
    os.makedirs(args.out, exist_ok=True)
    rescue_dir = None
    if args.rescue:
        rescue_dir, dumps = verify.capture(
            backend, rows, os.path.join(args.out, "rescue")
        )
        print("captured %d pane screen(s) to %s" % (len(dumps), rescue_dir))

    path, hint = restore.write(snapshot, rows, args.out)
    findings = core.diagnose(rows, args.claude_dir)
    sheet = os.path.join(args.out, "claude-sessions.md")
    with open(sheet, "w") as fh:
        fh.write(
            render.markdown(rows, snapshot, hint, rescue_dir, findings)
        )
    data = os.path.join(args.out, "sessions.json")
    with open(data, "w") as fh:
        fh.write(render.as_json(rows, snapshot))

    print("recovery sheet: %s" % sheet)
    print("session data:   %s" % data)
    if path:
        print("restore file:   %s" % path)
        print("\nafter a reboot, run:\n  %s" % hint)
    else:
        print(
            "no restore artefact for backend %r; the sheet lists a resume "
            "command per pane" % snapshot.backend
        )
    risks = [f for f in findings if f.level == "risk"]
    if risks:
        print("\n%d risk(s) found - run `panemap doctor`" % len(risks))
    return 0


def _doctor(rows, root) -> int:
    findings = core.diagnose(rows, root)
    if not findings:
        print("Nothing to flag: every pane's session is on disk and resumable.")
        return 0
    order = {"risk": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: order.get(f.level, 3))
    for finding in findings:
        print("[%s] %s" % (finding.level.upper(), finding.message))
        if finding.fix:
            print("       fix: %s" % finding.fix)
    return 1 if any(f.level == "risk" for f in findings) else 0


def _verify(backend, snapshot, rows) -> int:
    target, dumps = verify.run(backend, rows)
    print("pane screens captured to %s\n" % target)
    width = max([len(r.pane.tab) for r in rows] + [3])
    unproven = 0
    for row in rows:
        confirmed = [e for e in row.evidence if "not visible" not in e]
        print("%-*s  %-9s  %s" % (width, row.pane.tab, row.confidence, row.session_id))
        for item in confirmed:
            print("%s  - %s" % (" " * width, item))
        if row.confidence != core.EXACT:
            unproven += 1
            print("%s  - not independently confirmed" % (" " * width))
    print(
        "\n%d of %d pairing(s) confirmed."
        % (len(rows) - unproven, len(rows))
    )
    return 0


def _sessions(args) -> int:
    root = args.claude_dir or claude.config_dir()
    project = None if args.all else (args.project or os.getcwd())
    paths = list(claude.iter_transcripts(root, project))
    if not paths and project:
        print(
            "No transcripts for %s.\nTry --all, or --project <path>." % project,
            file=sys.stderr,
        )
        return 1

    scanned = []
    for path in paths:
        try:
            scanned.append(claude.scan_transcript(path))
        except OSError:
            continue
    scanned.sort(
        key=lambda t: t.last_activity or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    notes = core.load_notes(args.notes)
    width = shutil.get_terminal_size((120, 24)).columns
    print("%d transcript(s)%s\n" % (len(scanned), "" if args.all else " in " + project))
    for transcript in scanned[: args.limit]:
        sid = os.path.basename(transcript.path)[:-6]
        desc = (
            notes.get(sid)
            or transcript.title
            or transcript.opened_with
            or "(no readable message)"
        )
        room = max(20, width - 66)
        if len(desc) > room:
            desc = desc[: room - 1] + "…"
        print(
            "%s  %6s MB  %-36s  %s"
            % (render.when(transcript.last_activity), transcript.megabytes, sid, desc)
        )
        if transcript.branch:
            print("%s  %s" % (" " * 16, transcript.branch))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
