"""Tests for ccmux. Standard library only: `python3 -m unittest discover tests`."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ccmux import claude, core, mux, proc, render, restore, verify  # noqa: E402

# A trimmed but structurally faithful `zellij action dump-layout`: a
# layout-level cwd making pane cwds relative, a non-Claude command pane, a
# floating plugin pane, a Claude pane with pinned args, one without, and the
# template sections that must not be mistaken for real tabs.
DUMP = """\
layout {
    cwd "/"
    tab name="server" hide_floating_panes=true {
        pane size=1 borderless=true {
            plugin location="zellij:tab-bar"
        }
        pane command="npm" cwd="srv/app" {
            args "run" "dev"
            start_suspended true
        }
        floating_panes {
            pane name="About Zellij" {
                plugin location="zellij:about"
            }
        }
    }
    tab name="pinned" {
        pane command="claude" cwd="work/repo" {
            args "--resume" "11111111-1111-1111-1111-111111111111"
            start_suspended true
        }
    }
    tab name="bare" {
        pane command="claude" cwd="work/repo" {
            start_suspended true
        }
    }
    new_tab_template {
        pane size=1 borderless=true {
            plugin location="zellij:tab-bar"
        }
        pane
    }
    swap_tiled_layout name="vertical" {
        tab max_panes=5 {
            pane command="claude" {
                args "--resume" "99999999-9999-9999-9999-999999999999"
            }
        }
    }
}
"""


def transcript_lines(session_id, branch, first, last, ts="2026-01-02T03:04:05.000Z"):
    return [
        {"type": "user", "isMeta": True, "sessionId": session_id,
         "message": {"content": "<meta>"}, "gitBranch": branch},
        {"type": "user", "sessionId": session_id, "gitBranch": branch,
         "timestamp": "2026-01-01T00:00:00.000Z",
         "message": {"content": [{"type": "text", "text": first}]}},
        {"type": "assistant", "sessionId": session_id,
         "message": {"content": [{"type": "text", "text": "working on it"}]}},
        {"type": "user", "sessionId": session_id, "gitBranch": branch,
         "timestamp": ts, "message": {"content": last}},
    ]


class FakeClaudeHome:
    """A throwaway ~/.claude containing registry entries and transcripts."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="ccmux-home-")
        os.makedirs(os.path.join(self.root, "sessions"))
        os.makedirs(os.path.join(self.root, "projects"))

    def registry(self, pid, session_id, cwd, updated_at=1000, **extra):
        payload = {"pid": pid, "sessionId": session_id, "cwd": cwd,
                   "status": "idle", "kind": "interactive",
                   "updatedAt": updated_at}
        payload.update(extra)
        path = os.path.join(self.root, "sessions", "%d.json" % pid)
        with open(path, "w") as fh:
            json.dump(payload, fh)
        return path

    def transcript(self, session_id, cwd, branch="main", first="hello there friend",
                   last="and then this", ts="2026-01-02T03:04:05.000Z"):
        directory = os.path.join(self.root, "projects", claude.project_slug(cwd))
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, session_id + ".jsonl")
        with open(path, "w") as fh:
            for entry in transcript_lines(session_id, branch, first, last, ts):
                fh.write(json.dumps(entry) + "\n")
        return path

    def settings(self, **values):
        with open(os.path.join(self.root, "settings.json"), "w") as fh:
            json.dump(values, fh)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.home = FakeClaudeHome()
        self.addCleanup(self.home.close)

    def test_reads_first_and_last_human_turn(self):
        path = self.home.transcript(
            "aaaa", "/work/repo", first="explain the bundle flow",
            last="ship it please",
        )
        scanned = claude.scan_transcript(path)
        self.assertEqual(scanned.opened_with, "explain the bundle flow")
        self.assertEqual(scanned.last_message, "ship it please")
        self.assertEqual(scanned.branch, "main")
        self.assertEqual(scanned.last_activity.isoformat(), "2026-01-02T03:04:05+00:00")

    def test_ignores_meta_reminders_and_tool_noise(self):
        path = os.path.join(self.home.root, "noise.jsonl")
        with open(path, "w") as fh:
            for entry in [
                {"type": "user", "isMeta": True,
                 "message": {"content": "meta only"}},
                {"type": "user",
                 "message": {"content": "<system-reminder>hidden</system-reminder>"}},
                {"type": "user", "isVisibleInTranscript": False,
                 "message": {"content": "invisible"}},
                {"type": "user",
                 "message": {"content": "[Request interrupted by user]"}},
                {"type": "user", "message": {"content": "the real question"}},
            ]:
                fh.write(json.dumps(entry) + "\n")
        self.assertEqual(claude.scan_transcript(path).opened_with, "the real question")

    def test_survives_truncated_json_lines(self):
        path = os.path.join(self.home.root, "broken.jsonl")
        with open(path, "w") as fh:
            fh.write('{"type": "user", "message": {"content": "good line"}}\n')
            fh.write('{"type": "user", "message": {"content": "trunca\n')
            fh.write("not json at all\n")
        self.assertEqual(claude.scan_transcript(path).opened_with, "good line")

    def test_registry_skips_key_files_and_bad_json(self):
        self.home.registry(4242, "aaaa", "/work/repo")
        open(os.path.join(self.home.root, "sessions", "4242.deadbeef.key"), "w").close()
        with open(os.path.join(self.home.root, "sessions", "77.json"), "w") as fh:
            fh.write("{not json")
        entries = claude.read_registry(self.home.root)
        self.assertEqual(list(entries), [4242])
        self.assertEqual(entries[4242].session_id, "aaaa")

    def test_missing_kind_counts_as_interactive(self):
        self.home.registry(11, "aaaa", "/work/repo", kind=None)
        self.assertTrue(claude.read_registry(self.home.root)[11].interactive)

    def test_transcript_lookup_is_by_glob_not_slug_guess(self):
        path = self.home.transcript("bbbb", "/some/../weird/./path")
        self.assertEqual(claude.find_transcript("bbbb", self.home.root), path)
        self.assertIsNone(claude.find_transcript("nope", self.home.root))

    def test_cleanup_period_defaults_and_overrides(self):
        self.assertEqual(claude.cleanup_period_days(self.home.root), 30)
        self.home.settings(cleanupPeriodDays=3650)
        self.assertEqual(claude.cleanup_period_days(self.home.root), 3650)
        self.home.settings(cleanupPeriodDays="lots")
        self.assertEqual(claude.cleanup_period_days(self.home.root), 30)


class ZellijLayoutTests(unittest.TestCase):
    def test_finds_claude_panes_in_document_order(self):
        panes = mux.parse_zellij_layout(DUMP)
        self.assertEqual([p.tab for p in panes], ["pinned", "bare"])

    def test_resolves_pane_cwd_against_layout_cwd(self):
        panes = mux.parse_zellij_layout(DUMP)
        self.assertEqual(panes[0].cwd, "/work/repo")

    def test_captures_pinned_session_as_anchor(self):
        panes = mux.parse_zellij_layout(DUMP)
        self.assertEqual(
            panes[0].pinned_session, "11111111-1111-1111-1111-111111111111"
        )
        self.assertIsNone(panes[1].pinned_session)

    def test_ignores_template_and_swap_layout_sections(self):
        # The swap layout also contains a claude pane; counting it would both
        # invent a pane and shift every later pairing.
        self.assertEqual(len(mux.parse_zellij_layout(DUMP)), 2)

    def test_ignores_non_claude_command_panes(self):
        self.assertNotIn("server", [p.tab for p in mux.parse_zellij_layout(DUMP)])

    def test_matches_absolute_paths_to_the_claude_binary(self):
        layout = 'layout {\n    tab name="t" {\n        pane command="/usr/local/bin/claude" {\n            args "--resume" "22222222-2222-2222-2222-222222222222"\n        }\n    }\n}\n'
        panes = mux.parse_zellij_layout(layout)
        self.assertEqual(len(panes), 1)
        self.assertEqual(
            panes[0].pinned_session, "22222222-2222-2222-2222-222222222222"
        )


class ZellijRewriteTests(unittest.TestCase):
    def test_replaces_existing_args_and_adds_missing_ones(self):
        out = mux.rewrite_zellij_layout(DUMP, ["cafe0000", "beef0000"])
        self.assertIn('args "--resume" "cafe0000"', out)
        self.assertIn('args "--resume" "beef0000"', out)
        self.assertNotIn("11111111-1111-1111-1111-111111111111", out)

    def test_unpinned_pane_becomes_a_plain_shell_in_the_same_cwd(self):
        out = mux.rewrite_zellij_layout(DUMP, ["cafe0000", None])
        self.assertIn('pane cwd="work/repo"', out)
        # exactly one live claude pane survives (the swap-layout one is not a
        # real pane and is counted by neither the parser nor the rewriter)
        self.assertEqual(len(mux.parse_zellij_layout(out)), 1)

    def test_leaves_unrelated_panes_and_templates_untouched(self):
        out = mux.rewrite_zellij_layout(DUMP, ["cafe0000", "beef0000"])
        self.assertIn('pane command="npm" cwd="srv/app"', out)
        self.assertIn('plugin location="zellij:about"', out)
        self.assertIn("new_tab_template", out)
        # the swap layout's claude pane must not have been rewritten
        self.assertIn("99999999-9999-9999-9999-999999999999", out)

    def test_output_still_parses_to_the_same_panes(self):
        out = mux.rewrite_zellij_layout(DUMP, ["cafe0000", "beef0000"])
        panes = mux.parse_zellij_layout(out)
        self.assertEqual([p.tab for p in panes], ["pinned", "bare"])
        self.assertEqual(panes[0].cwd, "/work/repo")

    def test_fewer_ids_than_panes_leaves_the_remainder_unpinned(self):
        out = mux.rewrite_zellij_layout(DUMP, ["cafe0000"])
        self.assertEqual(len(mux.parse_zellij_layout(out)), 1)


class StubZellij(mux.Zellij):
    """Zellij backend with dump-layout stubbed, so no zellij is needed."""

    def __init__(self, dump):
        self.dump = dump

    def available(self):
        return True

    def _run(self, cmd):
        return self.dump


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.home = FakeClaudeHome()
        self.addCleanup(self.home.close)
        self.env = {}

    def reader(self, pid):
        return self.env.get(pid, {})

    def test_pane_order_follows_ascending_pane_id_not_pid(self):
        # Pane ids ascend with creation; PIDs need not. The higher pane id
        # belongs to the later tab even though its PID is lower.
        self.home.registry(500, "sess-bare", "/work/repo")
        self.home.registry(900, "sess-pinned", "/work/repo")
        self.home.transcript("sess-bare", "/work/repo")
        self.home.transcript("sess-pinned", "/work/repo")
        self.env = {900: {"ZELLIJ_PANE_ID": "2"}, 500: {"ZELLIJ_PANE_ID": "7"}}
        procs = [proc.Process(900, 1, "claude"), proc.Process(500, 1, "claude")]

        _, rows = core.build(
            backend=StubZellij(DUMP), root=self.home.root,
            processes=procs, env_reader=self.reader,
        )
        self.assertEqual([r.pane.tab for r in rows], ["pinned", "bare"])
        self.assertEqual([r.pane.pid for r in rows], [900, 500])

    def test_pinned_arg_agreement_is_reported_as_exact(self):
        sid = "11111111-1111-1111-1111-111111111111"
        self.home.registry(900, sid, "/work/repo")
        self.home.transcript(sid, "/work/repo")
        self.env = {900: {"ZELLIJ_PANE_ID": "2"}}
        _, rows = core.build(
            backend=StubZellij(DUMP), root=self.home.root,
            processes=[proc.Process(900, 1, "claude --resume " + sid)],
            env_reader=self.reader,
        )
        self.assertEqual(rows[0].confidence, core.EXACT)
        self.assertTrue(any("pins this session" in e for e in rows[0].evidence))

    def test_pinned_arg_disagreement_is_a_conflict(self):
        self.home.registry(900, "some-other-session", "/work/repo")
        self.env = {900: {"ZELLIJ_PANE_ID": "2"}}
        _, rows = core.build(
            backend=StubZellij(DUMP), root=self.home.root,
            processes=[proc.Process(900, 1, "claude --resume "
                                   "11111111-1111-1111-1111-111111111111")],
            env_reader=self.reader,
        )
        self.assertEqual(rows[0].confidence, core.CONFLICT)
        self.assertTrue(rows[0].issues)

    def test_registry_mtime_agreement_upgrades_an_inferred_pairing(self):
        # A bare pane can only be matched by ordering, but if the registry file
        # and the transcript were written at the same moment that corroborates.
        path = self.home.registry(500, "sess-bare", "/work/repo")
        written = self.home.transcript(
            "sess-bare", "/work/repo", ts="2026-01-02T03:04:05.000Z"
        )
        stamp = claude.scan_transcript(written).last_activity.timestamp()
        os.utime(path, (stamp, stamp))
        self.env = {500: {"ZELLIJ_PANE_ID": "7"}}
        layout = DUMP.replace('tab name="pinned"', 'tab name="gone"').replace(
            '            args "--resume" "11111111-1111-1111-1111-111111111111"\n', ""
        )
        _, rows = core.build(
            backend=StubZellij(layout), root=self.home.root,
            processes=[proc.Process(500, 1, "claude")], env_reader=self.reader,
        )
        row = [r for r in rows if r.pane.pid == 500][0]
        self.assertEqual(row.confidence, core.EXACT)
        self.assertTrue(any("registry mtime" in e for e in row.evidence))

    def test_missing_transcript_marks_the_row_unresumable(self):
        self.home.registry(900, "vanished", "/work/repo")
        self.env = {900: {"ZELLIJ_PANE_ID": "2"}}
        _, rows = core.build(
            backend=StubZellij(DUMP), root=self.home.root,
            processes=[proc.Process(900, 1, "claude")], env_reader=self.reader,
        )
        self.assertFalse(rows[0].resumable)
        self.assertIsNone(rows[0].resume_command())
        self.assertTrue(any("gone from disk" in i for i in rows[0].issues))

    def test_offset_between_panes_and_processes_is_reported(self):
        self.home.registry(900, "only-one", "/work/repo")
        self.env = {900: {"ZELLIJ_PANE_ID": "2"}}
        snapshot, _ = core.build(
            backend=StubZellij(DUMP), root=self.home.root,
            processes=[proc.Process(900, 1, "claude")], env_reader=self.reader,
        )
        self.assertTrue(any("may be offset" in n for n in snapshot.notes))

    def test_process_without_pane_id_is_ignored(self):
        # A Claude running outside the multiplexer must not consume a pane slot.
        self.home.registry(900, "inside", "/work/repo")
        self.home.registry(901, "outside", "/work/repo")
        self.env = {900: {"ZELLIJ_PANE_ID": "2"}, 901: {}}
        _, rows = core.build(
            backend=StubZellij(DUMP), root=self.home.root,
            processes=[proc.Process(900, 1, "claude"), proc.Process(901, 1, "claude")],
            env_reader=self.reader,
        )
        self.assertEqual([r.pane.pid for r in rows if r.pane.pid], [900])


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        self.home = FakeClaudeHome()
        self.addCleanup(self.home.close)

    def test_most_recent_driver_owns_the_shared_session(self):
        sid = "shared-session"
        self.home.registry(900, sid, "/work/repo", updated_at=100)
        self.home.registry(500, sid, "/work/repo", updated_at=999)
        self.home.transcript(sid, "/work/repo")
        env = {900: {"ZELLIJ_PANE_ID": "2"}, 500: {"ZELLIJ_PANE_ID": "7"}}
        layout = DUMP.replace(
            '            args "--resume" "11111111-1111-1111-1111-111111111111"\n', ""
        )
        _, rows = core.build(
            backend=StubZellij(layout), root=self.home.root,
            processes=[proc.Process(900, 1, "claude"), proc.Process(500, 1, "claude")],
            env_reader=lambda pid: env.get(pid, {}),
        )
        owners = {r.pane.tab: r.owner for r in rows}
        self.assertEqual(owners, {"pinned": False, "bare": True})
        # only the owner is restored, so the transcript is not opened twice
        self.assertEqual(core.restore_session_ids(rows), [None, sid])
        self.assertIsNone(rows[0].resume_command_only())


class DiagnoseTests(unittest.TestCase):
    def setUp(self):
        self.home = FakeClaudeHome()
        self.addCleanup(self.home.close)
        self.env = {900: {"ZELLIJ_PANE_ID": "2"}, 500: {"ZELLIJ_PANE_ID": "7"}}

    def build(self):
        return core.build(
            backend=StubZellij(DUMP), root=self.home.root,
            processes=[proc.Process(900, 1, "claude"), proc.Process(500, 1, "claude")],
            env_reader=lambda pid: self.env.get(pid, {}),
        )

    def test_deleted_transcript_is_a_risk(self):
        self.home.settings(cleanupPeriodDays=3650)
        self.home.registry(900, "vanished", "/work/repo")
        self.home.registry(500, "kept", "/work/repo")
        self.home.transcript("kept", "/work/repo")
        _, rows = self.build()
        findings = core.diagnose(rows, self.home.root)
        risks = [f for f in findings if f.level == "risk"]
        self.assertEqual(len(risks), 1)
        self.assertIn("no transcript on disk", risks[0].message)

    def test_transcript_near_the_cleanup_horizon_is_a_risk(self):
        self.home.settings(cleanupPeriodDays=30)
        self.home.registry(900, "aging", "/work/repo")
        self.home.registry(500, "fresh", "/work/repo")
        old = self.home.transcript("aging", "/work/repo")
        self.home.transcript("fresh", "/work/repo")
        stale = time.time() - 26 * 86400
        os.utime(old, (stale, stale))
        _, rows = self.build()
        messages = [f.message for f in core.diagnose(rows, self.home.root)]
        self.assertTrue(any("deletes it on a startup" in m for m in messages))

    def test_default_cleanup_period_is_flagged_as_info(self):
        self.home.registry(900, "a", "/work/repo")
        self.home.registry(500, "b", "/work/repo")
        self.home.transcript("a", "/work/repo")
        self.home.transcript("b", "/work/repo")
        _, rows = self.build()
        findings = core.diagnose(rows, self.home.root)
        self.assertTrue(any("cleanupPeriodDays is unset" in f.message for f in findings))

    def test_clean_setup_reports_nothing(self):
        # pid 900 sits in the "pinned" tab, so its registry must agree with the
        # session that tab's command line pins or that is itself a finding.
        pinned = "11111111-1111-1111-1111-111111111111"
        self.home.settings(cleanupPeriodDays=3650)
        self.home.registry(900, pinned, "/work/repo")
        self.home.registry(500, "b", "/work/repo")
        self.home.transcript(pinned, "/work/repo")
        self.home.transcript("b", "/work/repo")
        _, rows = self.build()
        self.assertEqual(core.diagnose(rows, self.home.root), [])


class TmuxTests(unittest.TestCase):
    """Exercises the tmux backend against a stub `tmux` on PATH."""

    def setUp(self):
        self.home = FakeClaudeHome()
        self.addCleanup(self.home.close)
        self.bin = tempfile.mkdtemp(prefix="ccmux-bin-")
        self.addCleanup(shutil.rmtree, self.bin, True)

    def install_stub(self, payload):
        path = os.path.join(self.bin, "tmux")
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\ncat <<'EOF'\n%s\nEOF\n" % payload)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        original = os.environ.get("PATH", "")
        os.environ["PATH"] = self.bin + os.pathsep + original
        self.addCleanup(os.environ.__setitem__, "PATH", original)

    def test_pane_pid_is_matched_through_the_process_ancestry(self):
        # tmux reports the pane's *shell* pid; claude runs beneath it.
        self.install_stub(
            "work\t0\teditor\t0\t%0\t400\t/work/repo\n"
            "work\t1\tagent\t0\t%1\t500\t/work/other"
        )
        self.home.registry(600, "sess-a", "/work/repo")
        self.home.registry(700, "sess-b", "/work/other")
        self.home.transcript("sess-a", "/work/repo")
        self.home.transcript("sess-b", "/work/other")
        procs = [
            proc.Process(400, 1, "-zsh"),
            proc.Process(600, 400, "claude"),
            proc.Process(500, 1, "-zsh"),
            proc.Process(700, 500, "claude --resume sess-b"),
        ]
        snapshot, rows = core.build(
            backend=mux.Tmux(), root=self.home.root, processes=procs
        )
        self.assertEqual([r.pane.pid for r in rows], [600, 700])
        self.assertEqual([r.pane.key for r in rows], ["%0", "%1"])
        self.assertEqual(rows[0].pane.window, "editor")
        # tmux knows the owner outright, so no ordering guess is involved
        self.assertTrue(all(r.confidence == core.EXACT for r in rows))
        self.assertTrue(
            any("reports the pane's owning process" in e for e in rows[0].evidence)
        )

    def test_panes_without_claude_are_skipped(self):
        self.install_stub("work\t0\tshell\t0\t%0\t400\t/work/repo")
        snapshot, rows = core.build(
            backend=mux.Tmux(), root=self.home.root,
            processes=[proc.Process(400, 1, "-zsh")],
        )
        self.assertEqual(rows, [])

    def test_paths_containing_spaces_survive_the_format(self):
        self.install_stub("work\t0\tmy window\t0\t%0\t400\t/work/my repo")
        self.home.registry(600, "sess-a", "/work/my repo")
        self.home.transcript("sess-a", "/work/my repo")
        _, rows = core.build(
            backend=mux.Tmux(), root=self.home.root,
            processes=[proc.Process(400, 1, "-zsh"), proc.Process(600, 400, "claude")],
        )
        self.assertEqual(rows[0].cwd, "/work/my repo")
        self.assertEqual(rows[0].pane.window, "my window")


class ProcessTests(unittest.TestCase):
    def test_only_interactive_claude_matches(self):
        for args in [
            "claude",
            "claude --resume",
            "claude --resume 6f1a20c4-8d3e-4b17-9a55-0c2d81e4f7ab",
            "/usr/local/bin/claude",
        ]:
            self.assertTrue(proc.INTERACTIVE_CLAUDE.match(args), args)
        for args in [
            "claude daemon run --json-path /x/daemon.json",
            "claude bg-pty-host --bg-pty-host /tmp/x.sock 200 50",
            "claude bg-spare --bg-spare /tmp/x.sock",
            "claude -p 'answer this'",
            "claudette",
            "grep claude",
        ]:
            self.assertIsNone(proc.INTERACTIVE_CLAUDE.match(args), args)

    def test_environ_pairs_ignore_argv_and_paths(self):
        pairs = proc._pairs(
            ["/usr/bin/claude", "--resume", "ZELLIJ_PANE_ID=11", "TERM=xterm",
             "-x=1", "novalue"]
        )
        self.assertEqual(pairs, {"ZELLIJ_PANE_ID": "11", "TERM": "xterm"})

    def test_ancestry_walks_up_and_stops_on_cycles(self):
        procs = [proc.Process(3, 2, "c"), proc.Process(2, 1, "b"), proc.Process(1, 1, "a")]
        self.assertEqual(proc.ancestry(3, procs), [3, 2])
        looped = [proc.Process(5, 6, "x"), proc.Process(6, 5, "y")]
        self.assertEqual(sorted(proc.ancestry(5, looped)), [5, 6])


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.home = FakeClaudeHome()
        self.addCleanup(self.home.close)
        self.home.registry(900, "11111111-1111-1111-1111-111111111111", "/work/repo")
        self.home.transcript(
            "11111111-1111-1111-1111-111111111111", "/work/repo",
            first="explain the bundle flow", last="ship it",
        )
        self.home.registry(500, "vanished", "/work/repo")
        env = {900: {"ZELLIJ_PANE_ID": "2"}, 500: {"ZELLIJ_PANE_ID": "7"}}
        self.snapshot, self.rows = core.build(
            backend=StubZellij(DUMP), root=self.home.root,
            processes=[proc.Process(900, 1, "claude"), proc.Process(500, 1, "claude")],
            env_reader=lambda pid: env.get(pid, {}),
        )

    def test_table_marks_a_deleted_transcript(self):
        text = render.table(self.rows, self.snapshot, width=140)
        self.assertIn("GONE", text)
        self.assertIn("cannot be resumed", text)

    def test_table_uses_the_transcript_opening_message_as_description(self):
        self.assertIn("explain the bundle flow", render.table(self.rows, self.snapshot, 140))

    def test_json_is_parseable_and_carries_evidence(self):
        payload = json.loads(render.as_json(self.rows, self.snapshot))
        self.assertEqual(payload["backend"], "zellij")
        self.assertEqual(len(payload["panes"]), 2)
        self.assertTrue(payload["panes"][0]["resumable"])
        self.assertFalse(payload["panes"][1]["resumable"])
        self.assertIn("confidence", payload["panes"][0])

    def test_markdown_escapes_pipes_in_descriptions(self):
        self.rows[0].note = "uses a | pipe"
        body = render.markdown(self.rows, self.snapshot)
        self.assertIn("uses a \\| pipe", body)

    def test_notes_file_overrides_the_description(self):
        notes = os.path.join(self.home.root, "notes.json")
        with open(notes, "w") as fh:
            json.dump({"11111111-1111-1111-1111-111111111111": "curated label"}, fh)
        env = {900: {"ZELLIJ_PANE_ID": "2"}, 500: {"ZELLIJ_PANE_ID": "7"}}
        _, rows = core.build(
            backend=StubZellij(DUMP), root=self.home.root, notes_path=notes,
            processes=[proc.Process(900, 1, "claude"), proc.Process(500, 1, "claude")],
            env_reader=lambda pid: env.get(pid, {}),
        )
        self.assertEqual(rows[0].description, "curated label")


class RestoreArtifactTests(unittest.TestCase):
    def setUp(self):
        self.home = FakeClaudeHome()
        self.addCleanup(self.home.close)
        self.out = tempfile.mkdtemp(prefix="ccmux-out-")
        self.addCleanup(shutil.rmtree, self.out, True)

    def rows_for(self, backend, procs):
        return core.build(backend=backend, root=self.home.root, processes=procs)

    def test_zellij_artifact_pins_sessions_and_documents_the_n_flag(self):
        sid = "11111111-1111-1111-1111-111111111111"
        self.home.registry(900, sid, "/work/repo")
        self.home.registry(500, "second", "/work/repo")
        self.home.transcript(sid, "/work/repo")
        self.home.transcript("second", "/work/repo")
        env = {900: {"ZELLIJ_PANE_ID": "2"}, 500: {"ZELLIJ_PANE_ID": "7"}}
        snapshot, rows = core.build(
            backend=StubZellij(DUMP), root=self.home.root,
            processes=[proc.Process(900, 1, "claude"), proc.Process(500, 1, "claude")],
            env_reader=lambda pid: env.get(pid, {}),
        )
        path, hint = restore.write(snapshot, rows, self.out)
        body = open(path).read()
        self.assertIn('args "--resume" "%s"' % sid, body)
        self.assertIn('args "--resume" "second"', body)
        self.assertTrue(hint.startswith("zellij -n "))
        # the -n vs --layout distinction is the difference between a new
        # session and tabs bolted onto the running one
        self.assertIn("--new-session-with-layout", body)
        self.assertEqual(len(mux.parse_zellij_layout(body)), 2)

    def test_tmux_artifact_pretypes_without_running(self):
        stub = os.path.join(self.out, "tmux")
        with open(stub, "w") as fh:
            fh.write("#!/bin/sh\ncat <<'EOF'\nwork\t0\tagent\t0\t%0\t400\t/work/repo\nEOF\n")
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
        original = os.environ.get("PATH", "")
        os.environ["PATH"] = self.out + os.pathsep + original
        self.addCleanup(os.environ.__setitem__, "PATH", original)

        self.home.registry(600, "sess-a", "/work/repo")
        self.home.transcript("sess-a", "/work/repo")
        snapshot, rows = self.rows_for(
            mux.Tmux(), [proc.Process(400, 1, "-zsh"), proc.Process(600, 400, "claude")]
        )
        path, hint = restore.write(snapshot, rows, self.out)
        body = open(path).read()
        self.assertIn("new-session -d", body)
        self.assertIn("send-keys", body)
        self.assertIn("claude --resume sess-a", body)
        # send-keys without Enter leaves it typed, matching zellij's suspension
        self.assertNotIn("send-keys -t \"$session:0\" 'claude --resume sess-a' Enter", body)
        self.assertTrue(hint.startswith("sh "))
        self.assertTrue(os.access(path, os.X_OK))

    def test_tmux_artifact_is_valid_shell(self):
        # tmux itself cannot be exercised here, so the generated script is at
        # least checked by the shell's own parser.
        stub = os.path.join(self.out, "tmux")
        with open(stub, "w") as fh:
            fh.write(
                "#!/bin/sh\ncat <<'EOF'\n"
                "work\t0\tit's here\t0\t%0\t400\t/work/my repo\n"
                "work\t1\tplain\t0\t%1\t500\t/work/other\n"
                "EOF\n"
            )
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
        original = os.environ.get("PATH", "")
        os.environ["PATH"] = self.out + os.pathsep + original
        self.addCleanup(os.environ.__setitem__, "PATH", original)

        self.home.registry(600, "sess-a", "/work/my repo")
        self.home.registry(700, "sess-b", "/work/other")
        self.home.transcript("sess-a", "/work/my repo")
        # sess-b has no transcript, so its pane must degrade to a comment
        snapshot, rows = self.rows_for(
            mux.Tmux(),
            [
                proc.Process(400, 1, "-zsh"),
                proc.Process(600, 400, "claude"),
                proc.Process(500, 1, "-zsh"),
                proc.Process(700, 500, "claude"),
            ],
        )
        path, _ = restore.write(snapshot, rows, self.out)
        check = subprocess.run(["sh", "-n", path], capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn("no transcript on disk", open(path).read())

    def test_tmux_artifact_quotes_awkward_names(self):
        stub = os.path.join(self.out, "tmux")
        with open(stub, "w") as fh:
            fh.write(
                "#!/bin/sh\ncat <<'EOF'\nwork\t0\tit's here\t0\t%0\t400\t/work/my repo\nEOF\n"
            )
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
        original = os.environ.get("PATH", "")
        os.environ["PATH"] = self.out + os.pathsep + original
        self.addCleanup(os.environ.__setitem__, "PATH", original)

        self.home.registry(600, "sess-a", "/work/my repo")
        self.home.transcript("sess-a", "/work/my repo")
        snapshot, rows = self.rows_for(
            mux.Tmux(), [proc.Process(400, 1, "-zsh"), proc.Process(600, 400, "claude")]
        )
        path, _ = restore.write(snapshot, rows, self.out)
        body = open(path).read()
        self.assertIn("'it'\"'\"'s here'", body)
        self.assertIn("'/work/my repo'", body)


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.home = FakeClaudeHome()
        self.addCleanup(self.home.close)
        self.dir = tempfile.mkdtemp(prefix="ccmux-verify-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def make_row(self, sid="11111111-1111-1111-1111-111111111111", **kwargs):
        path = self.home.transcript(sid, "/work/repo", **kwargs)
        row = core.Row(pane=mux.Pane(key="2", tab="t", order=0))
        row.registry = claude.Registry(
            pid=1, session_id=sid, cwd="/work/repo", status="idle",
            kind="interactive", version="x", updated_at=1, file_mtime=0.0,
        )
        row.transcript = claude.scan_transcript(path)
        return row

    def screen(self, text):
        path = os.path.join(self.dir, "screen.txt")
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def test_session_id_printed_on_screen_confirms_the_pairing(self):
        row = self.make_row()
        path = self.screen(
            "Artifact(/tmp/claude-501/proj/11111111-1111-1111-1111-111111111111"
            "/scratchpad/deck.html)"
        )
        self.assertIn(
            "prints this session id", " ".join(verify.check(row, path))
        )

    def test_visible_prompt_found_in_transcript_confirms_the_pairing(self):
        row = self.make_row(first="explain the bundle flow in detail")
        path = self.screen("⏺ sure\n\n❯ explain the bundle flow in detail\n")
        self.assertIn("appears in this transcript", " ".join(verify.check(row, path)))

    def test_unrelated_prompt_does_not_confirm(self):
        row = self.make_row(first="explain the bundle flow in detail")
        path = self.screen("❯ something entirely different was typed here\n")
        self.assertNotIn("appears in this transcript", " ".join(verify.check(row, path)))

    def test_short_prompts_are_not_treated_as_proof(self):
        row = self.make_row(first="ok")
        path = self.screen("❯ ok\n")
        self.assertNotIn("appears in this transcript", " ".join(verify.check(row, path)))

    def test_quotes_in_a_prompt_still_match_the_escaped_transcript(self):
        row = self.make_row(first='rename it to "the big one" please')
        path = self.screen('❯ rename it to "the big one" please\n')
        self.assertIn("appears in this transcript", " ".join(verify.check(row, path)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
