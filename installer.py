#!/usr/bin/env python3
"""Install, update or remove agent-friction for the current user.

  python3 installer.py                 install or update (safe to re-run)
  python3 installer.py --dry-run       show what would change
  python3 installer.py --uninstall     remove everything except the database

It links the `friction` CLI and the agent skill, registers the hook with
Claude Code and Codex where they are installed, and on systemd machines starts
the web view and the nightly backfill timer. Existing settings files are
merged, never replaced, and a `.bak` copy is written before the first change.
"""
import argparse
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent
HOME = Path.home()
HOOK_MARK = 'friction_hook.py'
CLAUDE_EVENTS = ('SessionStart', 'PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'Stop')
CODEX_EVENTS = ('SessionStart', 'PreToolUse', 'PostToolUse', 'Stop')
UNITS = ('agent-friction-web.service', 'agent-friction-backfill.service',
         'agent-friction-backfill.timer')
ENABLE = ('agent-friction-web.service', 'agent-friction-backfill.timer')

DRY = False


def say(msg):
    print(('[dry-run] ' if DRY else '') + msg)


def write(path, text):
    if not DRY:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def link(target, dest):
    """Point symlink `dest` at `target`. Never replaces a real file."""
    if dest.is_symlink():
        if dest.resolve() == target.resolve():
            return say(f'ok       {dest}')
        if not DRY:
            dest.unlink()
    elif dest.exists():
        return say(f'skipped  {dest} (exists and is not a symlink; remove it to link)')
    say(f'link     {dest} -> {target}')
    if not DRY:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.symlink_to(target)


def unlink(dest):
    if dest.is_symlink() and APP in dest.resolve().parents:
        say(f'remove   {dest}')
        if not DRY:
            dest.unlink()


# ---- hooks ---------------------------------------------------------------

def is_ours(group):
    return any(HOOK_MARK in str(h.get('command', '')) for h in group.get('hooks', []))


def merge_hooks(path, events, command, remove=False):
    """Add (or drop) our hook group for each event, leaving everything else alone."""
    try:
        config = json.loads(path.read_text()) if path.exists() else {}
    except ValueError as exc:
        return say(f'skipped  {path} (not valid JSON: {exc})')
    before = json.dumps(config, sort_keys=True)
    hooks = config.setdefault('hooks', {})
    for event in events:
        groups = [g for g in hooks.get(event, []) if not is_ours(g)]
        if not remove:
            groups.append({'hooks': [{'type': 'command', 'command': command, 'timeout': 5}]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not hooks:
        config.pop('hooks')
    if json.dumps(config, sort_keys=True) == before:
        return say(f'ok       {path}')
    say(f"{'unhook' if remove else 'hook':<8} {path} ({', '.join(events)})")
    if DRY:
        return
    backup = path.with_name(path.name + '.bak')
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    write(path, json.dumps(config, indent=2) + '\n')


def hooks(args, remove=False):
    base = f'{shlex.quote(sys.executable)} {shlex.quote(str(APP / HOOK_MARK))}'
    if not args.no_claude and (HOME / '.claude').is_dir():
        merge_hooks(HOME / '.claude/settings.json', CLAUDE_EVENTS, base, remove)
    if not args.no_codex and (HOME / '.codex').is_dir():
        merge_hooks(HOME / '.codex/hooks.json', CODEX_EVENTS, base + ' --agent codex', remove)


# ---- skill and CLI -------------------------------------------------------

def skill_dirs():
    dirs, seen = [], set()
    for root in ('.agents', '.claude', '.codex'):
        skills = HOME / root / 'skills'
        if root != '.agents' and not (HOME / root).is_dir():
            continue
        real = skills.resolve()
        if real not in seen:
            seen.add(real)
            dirs.append(skills / 'agent-friction')
    return dirs


# ---- services ------------------------------------------------------------

def unit_text(name, port):
    py, app = sys.executable, APP
    if name == 'agent-friction-web.service':
        return (f'[Unit]\nDescription=Agent friction web view (read-only, 127.0.0.1:{port})\n\n'
                f'[Service]\nExecStart="{py}" "{app}/web.py" --port {port}\nRestart=on-failure\n\n'
                '[Install]\nWantedBy=default.target\n')
    if name == 'agent-friction-backfill.service':
        claude = shutil.which('claude')
        path = os.pathsep.join(dict.fromkeys(
            ([str(Path(claude).parent)] if claude else [])
            + [str(HOME / '.local/bin'), '/usr/local/bin', '/usr/bin', '/bin']))
        return ("[Unit]\nDescription=Agent friction: mine the day's transcripts for unlogged friction\n\n"
                f'[Service]\nType=oneshot\nEnvironment=PATH={path}\n'
                f'ExecStart="{py}" "{app}/friction.py" backfill --since 2d\n')
    return ('[Unit]\nDescription=Nightly agent friction backfill\n\n'
            '[Timer]\nOnCalendar=*-*-* 03:30:00\nPersistent=true\n\n'
            '[Install]\nWantedBy=timers.target\n')


def systemctl(*cmd):
    if DRY:
        return True
    return subprocess.run(('systemctl', '--user') + cmd, capture_output=True).returncode == 0


def have_systemd():
    return bool(shutil.which('systemctl')) and \
        subprocess.run(('systemctl', '--user', 'show-environment'), capture_output=True).returncode == 0


def services(args):
    if not have_systemd():
        return say('services skipped (no systemd user session): run `friction web` for the web'
                   ' view and schedule `friction backfill` yourself if you want it')
    unit_dir = HOME / '.config/systemd/user'
    changed = False
    for name in UNITS:
        dest, text = unit_dir / name, unit_text(name, args.port)
        if dest.is_symlink() or not dest.exists() or dest.read_text() != text:
            say(f'unit     {dest}')
            changed = True
            if not DRY:
                if dest.is_symlink():
                    dest.unlink()
                write(dest, text)
        else:
            say(f'ok       {dest}')
    if changed:
        systemctl('daemon-reload')
    for name in ENABLE:
        systemctl('enable', '--now', name)
    if changed:
        systemctl('restart', 'agent-friction-web.service')
    say(f'web view http://127.0.0.1:{args.port}')


def remove_services():
    unit_dir = HOME / '.config/systemd/user'
    if have_systemd():
        for name in ENABLE:
            systemctl('disable', '--now', name)
    for name in UNITS:
        dest = unit_dir / name
        if dest.exists() or dest.is_symlink():
            say(f'remove   {dest}')
            if not DRY:
                dest.unlink()
    if have_systemd():
        systemctl('daemon-reload')


# ---- entry point ---------------------------------------------------------

def check():
    if sys.version_info < (3, 8):
        sys.exit(f'agent-friction needs Python 3.8+, this is {sys.version.split()[0]}')
    try:
        sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(x)')
    except sqlite3.OperationalError:
        sys.exit('agent-friction needs SQLite with FTS5, which this Python build lacks')


def main():
    global DRY
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--uninstall', action='store_true', help='remove links, hooks and services')
    ap.add_argument('--dry-run', action='store_true', help='print changes without making them')
    ap.add_argument('--no-hooks', action='store_true', help='do not touch agent hook settings')
    ap.add_argument('--no-claude', action='store_true', help='skip Claude Code')
    ap.add_argument('--no-codex', action='store_true', help='skip Codex')
    ap.add_argument('--no-services', action='store_true', help='skip the systemd user units')
    ap.add_argument('--port', type=int, default=8798, help='web view port (default 8798)')
    ap.add_argument('--bin-dir', default=str(HOME / '.local/bin'), help='where to link `friction`')
    args = ap.parse_args()
    DRY = args.dry_run
    cli = Path(args.bin_dir).expanduser() / 'friction'

    if args.uninstall:
        unlink(cli)
        for d in skill_dirs():
            unlink(d / 'SKILL.md')
            if d.is_dir() and not any(d.iterdir()) and not DRY:
                d.rmdir()
        if not args.no_hooks:
            hooks(args, remove=True)
        if not args.no_services:
            remove_services()
        say('Uninstalled. Your data is still in ~/.local/share/agent-friction (delete it to forget).')
        return 0

    check()
    if not DRY:
        for name in ('friction.py', HOOK_MARK, 'web.py'):
            (APP / name).chmod((APP / name).stat().st_mode | 0o111)
    link(APP / 'friction.py', cli)
    for d in skill_dirs():
        link(APP / 'SKILL.md', d / 'SKILL.md')
    if not args.no_hooks:
        hooks(args)
    if not args.no_services:
        services(args)
    print()
    if str(cli.parent) not in os.environ.get('PATH', '').split(os.pathsep):
        print(f'Note: {cli.parent} is not on your PATH; add it so agents can run `friction`.')
    if not shutil.which('claude'):
        print('Note: `friction backfill` needs the `claude` CLI; live logging works without it.')
    print('Done. Start a new agent session, then try: friction hot')
    return 0


if __name__ == '__main__':
    sys.exit(main())
