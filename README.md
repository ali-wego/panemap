# panemap

Answers one question: **which Claude Code conversation is running in which terminal pane?**

If you keep a dozen multiplexer tabs open with a long-running Claude Code
session in each, that mapping lives only in your head. Reboot, and you are left
guessing which of forty transcript ids belonged to the tab called `Tab #12`.
`panemap` reconstructs the mapping, describes each conversation in a line, and
writes a file that rebuilds every pane with its `claude --resume <id>` already
in place.

```
$ panemap list
TAB               SESSION                                 AGE  WHAT IT IS
----------------  ------------------------------------  -----  ----------------------------------------
checkout-rewrite  6f1a20c4-8d3e-4b17-9a55-0c2d81e4f7ab     2h  Rewriting the checkout form; PR open
scratch           6f1a20c4-8d3e-4b17-9a55-0c2d81e4f7ab     2h  Rewriting the checkout form; PR open
Tab #4            b0c93d51-27af-4e68-8f10-6a4b5c9d2e33   GONE  Search indexing spike
perf-profiling    14e7a9b8-5c02-4d3a-b8e6-9f7c1a2b3d40    46d  Profiling the slow dashboard query
flaky-tests       9a3f8e21-6b74-4c05-a1d9-3e5f70b8c612    20d  Chasing the intermittent CI failure
deploy-notes      2d5c7b90-4e18-42fa-9c63-8b1a0f6d5e77    22h  Working out the staging deploy steps

  GONE = transcript deleted; cannot be resumed
```

The first two rows are the same conversation open in two panes — `panemap` spots
that and restores it in one of them.

Zero dependencies, standard library only.

## Install

```sh
pipx install panemap         # or: uv tool install panemap / pip install --user panemap
```

Or just copy the package and run `python3 -m panemap`.

## Commands

| Command | What it does |
|---|---|
| `panemap list` | The table above. `--json` for scripts, `--md` for a shareable sheet. |
| `panemap save` | Writes the recovery sheet, `sessions.json`, and a restore file. `--rescue` also captures every pane's screen. |
| `panemap doctor` | Reports what could cost you a conversation. Exits non-zero if anything is at risk. |
| `panemap verify` | Confirms each pane→session pairing from the pane's own screen contents. |
| `panemap rescue` | Captures every Claude pane's screen — the only backup for a session whose transcript is already deleted. |
| `panemap sessions` | Browses recent transcripts with a description each, whether or not a pane holds them. A readable `claude --resume` picker. |

Run `panemap save` before you reboot, then afterwards:

```sh
zellij -n ~/.panemap/restore.kdl      # zellij
sh ~/.panemap/restore.sh              # tmux
```

Every pane comes back with its resume command **typed but not run**, so
restoring twelve panes does not start twelve conversations at once. Press Enter
in the ones you want.

### Naming your sessions

Without a name, the description falls back to the conversation's opening
message — often not what the conversation became. The best fix is Claude Code's
own naming, because it pays off in more than one place:

```sh
claude -n "checkout rewrite"      # also shown in /resume and the terminal title
```

`panemap` reads that name and prefers it over the opening message. It comes from
the `custom-title` record in the transcript, so it survives the process exiting
— a session dormant for weeks still knows what it was called. Claude Code also
derives a throwaway name for every session (`myproject-0a`); those are ignored,
since they say less than the opening message does.

For sessions you would rather not rename, `~/.panemap/notes.json` overrides
anything else, keyed by session id:

```json
{
  "6f1a20c4-8d3e-4b17-9a55-0c2d81e4f7ab": "Checkout rewrite; PR open, merging to staging"
}
```

So the order is: your note, then the session's name, then its opening message.
`panemap list --json` will hand you the ids.

## How the mapping is worked out

No multiplexer records "this pane runs that Claude session", so `panemap`
triangulates.

1. **The session registry.** Each running Claude Code process writes
   `~/.claude/sessions/<pid>.json`, containing its `sessionId`. This is the only
   authoritative PID→session link on the system.
2. **Pane ownership.** Under tmux, `list-panes` reports `pane_pid` and the
   Claude process is found by walking up its parent chain — exact. Under
   zellij, which exposes no pane→pid mapping at all, every process inherits
   `ZELLIJ_PANE_ID` in its environment, and pane ids ascend with creation, so
   sorting processes by pane id lines them up with `dump-layout` order.
3. **Corroboration.** Because step 2 is an assumption on zellij, each row is
   graded and the reasons are printed by `panemap verify`:
   - a pane launched as `claude --resume <id>` must agree with the registry —
     if it does not, the row is marked `conflict` rather than quietly reported;
   - a registry file's mtime should match its transcript's last entry, since
     the same process writes both;
   - Claude prints its scratchpad and artefact paths on screen, and those paths
     contain its session id, so a pane can identify itself outright;
   - a prompt still visible on screen should be findable in the transcript.

Anything not confirmed is marked `~` in the table rather than presented as fact.

## Three things that will bite you

**Transcripts are deleted after 30 days.** `cleanupPeriodDays` defaults to 30,
cleanup runs at startup and goes by file mtime, so a session you have not
touched in a month becomes unresumable *while its process is still running* —
the conversation then exists only in memory. `panemap doctor` flags this before it
happens. Raise the limit in `~/.claude/settings.json`:

```json
{ "cleanupPeriodDays": 3650 }
```

If a transcript is already gone, `/export` in that pane is the only way to save
the history; `panemap rescue` at least keeps the visible screen.

**`zellij --layout` is not `zellij -n`.** Run inside an existing session,
`--layout` *adds* the tabs to the session you are already in. `-n`
(`--new-session-with-layout`) always starts a fresh one. The generated file says
so at the top.

**The branch in Claude's status line means nothing.** It shows the branch as of
the pane's last redraw, not the branch the conversation was on. Comparing it
against a transcript produces convincing false mismatches. `panemap` ignores it.

## Support

| Multiplexer | Pane→process | Status |
|---|---|---|
| zellij ≥ 0.44 | inferred from pane id ordering, corroborated | tested against a live 12-tab session |
| tmux ≥ 3.0 | exact, via `pane_pid` | logic covered by tests against stubbed `tmux` output; **not yet exercised against a real tmux server** — reports welcome |

Requires Python 3.9+, macOS or Linux. Reading another process's environment uses
`/proc` where available and `ps eww` otherwise, so it only sees your own
processes.

Not supported: screen, WezTerm and Kitty (no equivalent pane/session
introspection wired up yet), and Claude Code builds old enough not to write
`~/.claude/sessions/`.

## Limitations

- The tmux restore script rebuilds one window per Claude pane; splits and
  unrelated windows are not reproduced. The zellij restore file edits your real
  `dump-layout`, so it keeps splits, floating panes and plugins intact.
- A session open in two panes is restored in one — the pane that drove it most
  recently. Resuming one transcript twice interleaves both conversations into
  the same file.
- `panemap` never writes to your transcripts or sends input to a pane. `rescue`
  and `verify` only read pane screens.

## Development

```sh
python3 -m unittest discover -s tests -v
```

No dependencies to install. MIT licensed.
