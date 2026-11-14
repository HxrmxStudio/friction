#!/usr/bin/env python3
"""Claude Code / Codex hook for agent-friction.

Registered for SessionStart, PreToolUse, PostToolUse, PostToolUseFailure and
Stop (Claude: ~/.claude/settings.json, Codex: ~/.codex/hooks.json; pass
`--agent codex` there).

  PreToolUse          opens a timed event for the tool call
  PostToolUse[Failure] closes it with success/failure, duration and error text
  SessionStart        tells the agent the tool exists and lists hot items
  Stop                once per failure streak, asks the agent whether the
                      failures it just had are worth logging

Tool input and errors are truncated and scrubbed of obvious secrets before
they are stored. Any error exits 0 silently: this must never break an agent.
Set AGENT_FRICTION_DISABLE=1 to switch it off for a process tree.
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from friction import Friction, fmt_duration  # noqa: E402

NUDGE_MIN_FAILURES = 2
INPUT_KEYS = ('command', 'file_path', 'path', 'pattern', 'url', 'query', 'skill', 'description',
              'prompt', 'subagent_type', 'cmd')
FAIL_TEXT = re.compile(r'(?i)^\s*(error\b|exit code [1-9]|command failed|permission denied'
                       r'|process exited with code [1-9])')
EXIT_CODE = re.compile(r'(?i)\b(?:exit(?:ed)?(?: with)?(?: code)?|exit_code"?)\s*[:=]?\s*(\d+)')


def summarize(tool, tool_input):
    if isinstance(tool_input, str):
        return tool_input
    if not isinstance(tool_input, dict):
        return None
    if tool in ('Bash', 'shell', 'exec', 'exec_command', 'local_shell'):
        cmd = tool_input.get('command') or tool_input.get('cmd')
        if isinstance(cmd, list):
            cmd = ' '.join(str(c) for c in cmd)
        if cmd:
            return cmd
    parts = [f'{k}={tool_input[k]}' for k in INPUT_KEYS if tool_input.get(k) not in (None, '')]
    return ' '.join(str(p) for p in parts) if parts else json.dumps(tool_input, default=str)


def text_of(resp):
    if resp is None:
        return ''
    if isinstance(resp, str):
        return resp
    if isinstance(resp, list):
        return '\n'.join(text_of(x) for x in resp)
    if isinstance(resp, dict):
        for key in ('error', 'stderr', 'text', 'output', 'content', 'stdout', 'message'):
            if resp.get(key):
                return text_of(resp[key])
        return json.dumps(resp, default=str)
    return str(resp)


def failed(event):
    """(failed?, error text) for a PostToolUse / PostToolUseFailure payload."""
    if event.get('hook_event_name') == 'PostToolUseFailure':
        return True, text_of(event.get('error') or event.get('tool_response'))
    resp = event.get('tool_response')
    if isinstance(resp, dict):
        code = resp.get('exit_code', resp.get('exitCode'))
        if resp.get('is_error') or resp.get('isError') or resp.get('success') is False \
                or (isinstance(code, int) and code != 0):
            return True, text_of(resp)
        meta = resp.get('metadata')
        if isinstance(meta, dict) and isinstance(meta.get('exit_code'), int) and meta['exit_code'] != 0:
            return True, text_of(resp)
        return False, None
    text = text_of(resp)
    head = text[:300]
    if FAIL_TEXT.search(head):
        return True, text
    m = EXIT_CODE.search(head)
    if m and m.group(1) != '0':
        return True, text
    return False, None


def start_context(fr):
    lines = [
        'agent-friction is installed: a shared log of things that waste agent time (bad commands,'
        ' missing credentials, auth timeouts, tool errors, detours). When something costs you turns,'
        ' run `friction log "<short name of the underlying problem>" --detail "..."`; it lists'
        ' similar existing items so you can `friction bump <id>` instead. Add `--workaround "..."`'
        ' if you got past it. What counts as friction is your call. `friction --help` for more.'
    ]
    hot = fr.hot()
    if hot:
        lines.append('Hot friction right now (avoid these; bump the id if you hit one anyway):')
        for row in hot:
            line = f"  #{row['id']} {row['title']} ({row['n']}x, {fmt_duration(row['wasted'])} wasted)"
            if row['workaround']:
                line += ' — workaround: ' + row['workaround'].replace('\n', ' ')[:200]
            lines.append(line)
    return '\n'.join(lines)


def nudge_text(fails):
    tools = ', '.join(dict.fromkeys(ev['tool'] or '?' for ev in fails))
    return (f'agent-friction: {len(fails)} tool calls failed this session ({tools}) and none were'
            ' logged. If any of that was real friction (wasted turns, a missing credential, a bad'
            ' command, a detour), record it now: `friction log "<problem>" --detail "..."`, or'
            ' `friction bump <id>` for a known one; `friction timeline` shows the calls. If it was'
            ' just normal work, ignore this and finish.')


def handle(event, fr, agent):
    name = event.get('hook_event_name')
    session = event.get('session_id') or event.get('thread_id')
    if not session:
        return None
    if event.get('agent_id') and name in ('SessionStart', 'Stop'):   # subagent: record calls only
        return None
    cwd = event.get('cwd')
    fr.touch_session(session, agent, cwd, event.get('transcript_path'))
    out = None
    if name == 'SessionStart':
        if event.get('source') in (None, 'startup', 'clear', 'resume', 'compact'):
            out = {'hookSpecificOutput': {'hookEventName': 'SessionStart',
                                          'additionalContext': start_context(fr)}}
    elif name == 'PreToolUse':
        sess = fr.session(session)
        fr.add_event(session, 'tool', tool=event.get('tool_name'),
                     tool_use_id=event.get('tool_use_id'),
                     summary=summarize(event.get('tool_name'), event.get('tool_input')),
                     agent=agent, project=sess['project'] if sess else None)
    elif name in ('PostToolUse', 'PostToolUseFailure'):
        bad, error = failed(event)
        done = fr.finish_event(session, event.get('tool_name'), event.get('tool_use_id'),
                               ok=not bad, error=error, duration_ms=event.get('duration_ms'))
        if done is None:      # no PreToolUse seen (hook added mid-session): record it untimed
            sess = fr.session(session)
            fr.add_event(session, 'tool', tool=event.get('tool_name'),
                         tool_use_id=event.get('tool_use_id'), ok=0 if bad else 1,
                         duration_ms=event.get('duration_ms'), error=error,
                         summary=summarize(event.get('tool_name'), event.get('tool_input')),
                         agent=agent, project=sess['project'] if sess else None)
    elif name == 'Stop' and not event.get('stop_hook_active'):
        fails = fr.unnudged_failures(session)
        if len(fails) >= NUDGE_MIN_FAILURES:
            fr.set_nudged(session)
            out = {'decision': 'block', 'reason': nudge_text(fails)}
    fr.db.commit()
    return out


def main():
    try:
        if os.environ.get('AGENT_FRICTION_DISABLE'):
            return 0
        agent = 'claude'
        if '--agent' in sys.argv:
            agent = sys.argv[sys.argv.index('--agent') + 1]
        event = json.load(sys.stdin)
        out = handle(event, Friction(), agent)
        if out:
            print(json.dumps(out))
    except Exception:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
