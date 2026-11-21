---
name: friction
description: Log, upvote and look up friction that wastes agent time on this machine (failed or invalid commands, missing credentials, auth timeouts, tool errors, detours) with the local `friction` CLI. Use when something costs you turns, when a hook asks whether recent failures were friction, when the user asks what slows agents down, or to check for a known workaround before retrying a failing tool.
---

# friction

A shared local log of what slows agents down. Every agent on this machine writes to the same
SQLite database, so one agent's discovery (and workaround) reaches the next. Hooks record tool
calls with real timestamps; you supply the judgement about what was actually friction.

What counts is your call. Rule of thumb: time lost to the environment, tooling or instructions
rather than to the task itself. Ordinary iteration (a test failing while you write the code) is not.

## Log it

```
friction log "gh auth token expired" --detail "gh calls fail mid-session until re-auth" --workaround "gh auth refresh"
```

- Title = the underlying problem, generic enough that another agent would recognise it. Not this instance.
- `log` searches first. If it prints similar items and exits 3, **nothing was logged**: either
  `friction bump <id> --note "what happened this time"` (same root cause: upvote it) or re-run
  the same command with `--new` (genuinely different).
- Never put secrets in titles, details or notes.

## Timing

Wasted time is measured from hook-recorded tool events: from the first bad call of your latest
cluster (a failure, or a call marked `SOFT` because it didn't do what you expected) to the end of
the call that finally worked. If the friction began earlier or ended elsewhere, run
`friction timeline` and pass `--from <event id>` / `--to <event id>`. Only when no events exist for your session is `--wasted 5m` (your
estimate) used; those occurrences are flagged as estimated in every report, so always pass it in
that case rather than leaving the occurrence untimed.

## Other commands

| Command | Use |
|---|---|
| `friction search <words>` | is this known? is there a workaround? |
| `friction hot` | open items hit recently, with workarounds |
| `friction workaround <id> "..."` | record how to get past an item (`--append` to add) |
| `friction resolve <id> --fix "..."` | you fixed the underlying cause; reopens as a regression if hit again |
| `friction merge <src> <dst>` | fold a duplicate into the canonical item |
| `friction show <id> [--events]` | occurrences and the recorded tool calls |
| `friction report [--week\|--month] [--project P] [--agent A] [--json]` | heatmap; web view at http://127.0.0.1:8798 |

Log as soon as you are past the problem, while the window is fresh; then carry on with the task.
