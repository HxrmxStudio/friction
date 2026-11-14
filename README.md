<div align="center">

# 🪨 agent-friction

**Find out what is wasting your coding agents' time, and make them tell each other how to get past it.**

[![tests](https://github.com/martinwheeler/friction/actions/workflows/test.yml/badge.svg)](https://github.com/martinwheeler/friction/actions/workflows/test.yml)
[![license: GPL v3](https://img.shields.io/badge/license-GPL%20v3-blue.svg)](LICENSE)
![python 3.8+](https://img.shields.io/badge/python-3.8%2B-3776ab.svg)
![dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)
![works with Claude Code and Codex](https://img.shields.io/badge/works%20with-Claude%20Code%20%C2%B7%20Codex-d97757.svg)

[Install](#install) · [How it works](#how-it-works) · [Commands](#commands) · [Privacy](#privacy) · [Uninstall](#uninstall)

<img src="docs/web.png" alt="The agent-friction web view: a heatmap of friction items by day, with time wasted per item, project and agent" width="860">

</div>

---

Agents lose a surprising amount of time to things that have nothing to do with the task: an expired
token, a command that needs quoting in your shell, a port a stale container still holds. Each new
session rediscovers the same problem and pays for it again.

agent-friction is a small local log of those problems. Hooks time every tool call, agents name the
friction they hit and upvote the ones already known, and every new session starts with the current
worst offenders and their workarounds. You get a heatmap of where the time goes.

- **Measured, not guessed.** Wasted time comes from hook timestamps on real tool calls.
- **Shared across agents.** Claude Code and Codex write to one database on your machine.
- **Self-healing.** A workaround logged once is handed to every later session.
- **Nothing to run.** Python standard library and one SQLite file. No server, no account, no network.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/martinwheeler/friction/main/install.sh | bash
```

Needs `python3` (3.8+) and `git`. Linux and macOS.

The installer is safe to re-run, and re-running it updates. It:

1. clones the repo to `~/.local/share/agent-friction/app`
2. links the `friction` command into `~/.local/bin`
3. links the agent skill into `~/.agents/skills`, `~/.claude/skills` and `~/.codex/skills`
4. adds the hook to `~/.claude/settings.json` and `~/.codex/hooks.json` (merged with what is there, with a `.bak` copy saved first)
5. on systemd machines, starts the web view on <http://127.0.0.1:8798> and a nightly backfill timer

Steps for an agent you do not have installed are skipped.

<details>
<summary>Options, and installing from a checkout</summary>

Pass options after `bash -s --`:

```sh
curl -fsSL https://raw.githubusercontent.com/martinwheeler/friction/main/install.sh | bash -s -- --dry-run
```

| Option | Effect |
|---|---|
| `--dry-run` | print every change without making it |
| `--no-hooks` | leave agent settings alone |
| `--no-claude`, `--no-codex` | skip one agent |
| `--no-services` | no systemd units |
| `--port N` | web view port (default 8798) |
| `--bin-dir DIR` | where to link `friction` (default `~/.local/bin`) |

To hack on it, clone anywhere and install that checkout:

```sh
git clone https://github.com/martinwheeler/friction.git
cd friction && ./install.sh
```

</details>

Then start a new agent session. It will be told the tool exists, and you can check on it with:

```sh
friction hot          # what is hurting right now
friction report       # terminal heatmap
friction web          # web view, if the service is not running
```

## How it works

```
 tool call ──▶ hook records start, end, success, error ──▶ friction.db
                                                               │
 agent hits a wall ──▶ friction log "gh auth token expired"    │
                         │ already known? ──▶ friction bump 3  │
                         ▼                                     ▼
                  item + occurrence, timed from the recorded tool calls
                                                               │
 next session starts ◀── top 5 hot items and their workarounds ┘
```

1. **Hooks record raw events.** `PreToolUse` opens an event, `PostToolUse` closes it with success,
   duration and error text.
2. **Agents name the friction.** `friction log` searches existing items first and refuses to create
   a near-duplicate until the agent picks `bump <id>` or `--new`.
3. **Sessions get nudged.** Each session starts with the hot list. If a session ends with two or
   more failed calls and nothing logged, the agent is asked once whether that was friction.
4. **Backfill catches the rest.** `friction backfill` reads past transcripts, skips the quiet ones,
   and has a headless `claude -p` on a small model classify the rest. This is the only part that
   uses the `claude` CLI, and it is optional.

A terminal report looks like this:

```
Friction, last 7 days: 31 occurrences of 7 items, 1h34m wasted

    id hits   wasted     avg  heat (11-09 → 11-15)    item
     1    7   27m57s   4m00s  █▒··▒▓·                 gh auth token expired
     4    2   16m29s   8m14s  ·▒·▒···                 npm install hangs behind the proxy
     6    3   14m48s   4m56s  ·▒··▒▒·                 AWS SSO session expired
     2    4   13m37s   3m24s  ··▓····                 docker compose port already allocated
     5    7    2m28s     21s  █▒·▒▒▒·                 zsh glob fails on unquoted bracketed paths

  by project: web-app 10× 33m53s, api 7× 22m40s, infra 7× 19m49s, docs 7× 18m18s
  by agent: claude 22× 1h03m, codex 9× 31m06s
```

## Commands

Agents run these themselves, guided by the bundled skill. They work just as well by hand.

| Command | Use |
|---|---|
| `friction log "<problem>" --detail "..." --workaround "..."` | record a friction item (searches first) |
| `friction bump <id> --note "..."` | you hit a known item too |
| `friction search <words>` | is this known? is there a workaround? |
| `friction hot` | open items hit recently, with workarounds |
| `friction workaround <id> "..."` | record how to get past an item |
| `friction resolve <id> --fix "..."` | the cause is fixed; a later hit reopens it as a regression |
| `friction merge <src> <dst>` | fold a duplicate into the canonical item |
| `friction show <id> --events` | occurrences and the recorded tool calls behind them |
| `friction timeline` | this session's recent tool calls, with event ids |
| `friction report [--week\|--month] [--project P] [--agent A] [--json]` | heatmap |
| `friction backfill --since 30d [--dry-run]` | mine past transcripts |
| `friction web [--port N]` | serve the web view |

### Where the time comes from

Every occurrence records how its wasted time was obtained, and reports keep the kinds apart.

| `timing` | Meaning |
|---|---|
| `hook` | measured from live hook events, from the start of the failure streak (or `--from <event>`) to the log call |
| `transcript` | measured from transcript timestamps during backfill |
| `estimate` | the agent's own `--wasted` guess, used only when no events exist. Marked `~est` in the CLI and "estimated" in the web view |
| `none` | no events and no estimate |

### Lifecycle

`open` → `resolved` or `wontfix`. Resolved items leave the hot list. A new hit on a resolved item
reopens it and counts as a regression.

## Privacy

Everything stays on your machine, in `~/.local/share/agent-friction/friction.db`.

- Tool input and error text are cut to 500 characters and scrubbed of obvious secrets (tokens,
  keys, passwords in URLs) before they are stored. Treat this as a safety net, not a guarantee.
- Raw tool events are deleted after 30 days. Items and occurrences are kept.
- The web view is read-only and listens on `127.0.0.1` only.
- `friction backfill` sends transcript excerpts to the model through your own `claude` CLI. Nothing
  else makes a network request. Do not enable the timer if you do not want that.

## Configuration

| Variable | Effect |
|---|---|
| `AGENT_FRICTION_DIR` | where the database lives (default `~/.local/share/agent-friction`) |
| `AGENT_FRICTION_DISABLE=1` | switch the hook off for a process tree |

Hooks are registered for `SessionStart`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure` and
`Stop`. The hook exits 0 on any error, so it cannot break an agent session.

## Uninstall

```sh
~/.local/share/agent-friction/app/install.sh --uninstall
```

This removes the links, the hook entries and the services. Your database is left in place. Delete
`~/.local/share/agent-friction` to remove that too.

## Development

| File | Role |
|---|---|
| `friction.py` | database and CLI |
| `friction_hook.py` | Claude Code and Codex hook |
| `backfill.py` | transcript mining |
| `web.py`, `web.html` | read-only web view |
| `SKILL.md` | instructions agents load |
| `installer.py`, `install.sh` | install, update, uninstall |

```sh
python3 -m unittest test_friction
```

Issues and pull requests are welcome. Support for other agents needs only a hook that sends the
same five events to `friction_hook.py`.

## License

[GPL-3.0-or-later](LICENSE). Copyright © 2026 Martin Wheeler.

You may use, copy, modify and share agent-friction. If you distribute it, or a modified version,
you must do so under the same license and make the source available.
