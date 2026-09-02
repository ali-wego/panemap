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

## Surviving a reboot

### Before

Run this from inside a multiplexer pane — from a plain terminal there are no
panes to find:

```sh
panemap save --rescue
panemap doctor
```

`save` writes everything to `~/.panemap/`, in your home directory so a reboot
cannot clear it. `doctor` tells you if anything will not survive; act on it now,
because after the reboot it is too late. In particular, a session whose
transcript has already been deleted exists only inside its running process —
`/export` in that pane is the only way to keep the history.

### After

```sh
zellij -n ~/.panemap/restore.kdl -s work   # zellij  (-s names the session)
sh ~/.panemap/restore.sh                   # tmux    (then: tmux attach -t panemap)
```

Your tabs come back with **nothing running**. Under zellij each Claude pane shows
its command waiting:

```
Waiting to run: claude --resume 6f1a20c4-8d3e-4b17-9a55-0c2d81e4f7ab
```

Under tmux the same command is typed at the prompt, unsent.

Either way, press **Enter** in the tabs you want. That is the point: restoring
twelve panes does not start twelve conversations, and a very large transcript
only costs you time if you ask for it.

Some panes come back as an ordinary shell instead, on purpose — either the
session's transcript is gone, or another pane holds the same session and drove
it more recently. `panemap list` says which.

Then confirm and re-snapshot:

```sh
panemap list           # every pane should show the session id it had before
panemap save --rescue  # the old file describes the old session; refresh it
```

### Do not use your multiplexer's own session restore

Both zellij and tmux resurrection tools offer to bring the old session back, and
it looks like the obvious move. It is the one thing that will quietly lose work.
They restore the command each pane was *launched* with — and a pane launched as a
bare `claude` comes back as a bare `claude`, which starts a **new, empty
conversation** with no error to tell you it happened. Panes that were launched
with an explicit `--resume` do come back correctly, so you get a confusing mix.

`panemap`'s restore file carries the session id for every pane, which is the
whole reason it exists. Keep the old session around as a fallback until you are
satisfied, then discard it.

## Naming your sessions

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
