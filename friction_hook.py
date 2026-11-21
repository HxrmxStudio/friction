#!/usr/bin/env python3
"""Claude Code / Codex hook for friction.

Registered for SessionStart, UserPromptSubmit, PreToolUse, PermissionRequest,
PostToolUse, PostToolUseFailure and Stop (Claude: ~/.claude/settings.json,
Codex: ~/.codex/hooks.json minus PostToolUseFailure; pass `--agent codex` there).

  PreToolUse          opens a timed event for the tool call
  PostToolUse[Failure] closes it with success/failure, duration and error text
  SessionStart        tells the agent the tool exists and lists hot items
  Stop                once per failure streak, asks the agent whether the
                      failures it just had are worth logging; otherwise the
                      turn is over and a wait on the user starts
  UserPromptSubmit    ends that wait
  PermissionRequest   starts a wait on a permission prompt, ended by the
                      call finishing (or the next prompt or call)

Waits are subtracted from measured wasted time: pending user input is not
friction. So is the whole run of a question tool such as AskUserQuestion.

Tool input and errors are truncated and scrubbed of obvious secrets before
they are stored. Any error exits 0 silently: this must never break an agent.
Set FRICTION_DISABLE=1 to switch it off for a process tree.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from friction import (SUMMARY_MAX, USER_INPUT_TOOLS, WEAK_OUTCOMES, RETRY_LOOKAHEAD, Friction, fmt_duration,  # noqa: E402
                      is_self_call, kick_sync, redact, retry_flags, scanner_status, split_codex_output,
                      unexpected_outcome)

NUDGE_MIN_FAILURES = 2
SYNC_EVERY_S = 600
INPUT_KEYS = ('command', 'file_path', 'path', 'pattern', 'url', 'query', 'skill', 'description',
              'prompt', 'subagent_type', 'cmd')
# Plain-text fallback, only for the shapes the Claude/Codex tool wrappers print: a first line that
# is `Error: ...` or `Exit code N`, or a last line `Process exited with code N`.
FAIL_FIRST_LINE = re.compile(r'^\s*(Error:|Exit code:?\s*[1-9]\d*\b)')
FAIL_LAST_LINE = re.compile(r'(?i)^\s*process exited with code [1-9]\d*\s*$')
UNVERIFIED_TITLE_MAX = 80


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
        text = text_of(resp)
        return (True, text) if split_codex_output(text)[0] else (False, None)
    # Plain text: no structured status, so only trust the wrapper's own shapes. Words like
    # "error" or "exit code 1" elsewhere are the *content* (test logs, grep hits), not a failure.
    text = text_of(resp)
    wrapped, _ = split_codex_output(text)       # Codex's own header carries the exit code
    if wrapped is not None:
        return (True, text) if wrapped else (False, None)
    lines = text.strip().splitlines()
    if lines and (FAIL_FIRST_LINE.match(lines[0]) or FAIL_LAST_LINE.match(lines[-1])):
        return True, text
    return False, None


def output_of(resp):
    """All the output text of a tool response: stdout and stderr together."""
    if isinstance(resp, dict):
        parts = [text_of(resp[k]) for k in ('stdout', 'stderr') if resp.get(k)]
        if parts:
            return '\n'.join(parts)
    return text_of(resp)


def soft_outcome(event, summary):
    """(outcome, snippet) for a call that succeeded; outcome None when it did what was expected.

    Weak outcomes (an empty search) are not stored: they only count once a retry follows."""
    out = output_of(event.get('tool_response'))
    outcome = unexpected_outcome(event.get('tool_name'), summary, out)
    if outcome is None or outcome in WEAK_OUTCOMES:
        return None, None
    return outcome, ' '.join(out.split())[:300] or outcome       # raw: scrubbed later, see flush_scrub


def safe_title(title):
    """An unverified (analyser-written) title as shown to agents: short, no markup, one line."""
    title = re.sub(r'[`\r\n\t]+', ' ', title or '')
    return re.sub(r' {2,}', ' ', title).strip()[:UNVERIFIED_TITLE_MAX]


def start_context(fr):
    lines = [
        'friction is installed: a shared log of things that waste agent time (bad commands,'
        ' missing credentials, auth timeouts, tool errors, detours). When something costs you turns,'
        ' run `friction log "<short name of the underlying problem>" --detail "..."`; it lists'
        ' similar existing items so you can `friction bump <id>` instead. Add `--workaround "..."`'
        ' if you got past it. What counts as friction is your call. `friction --help` for more.'
    ]
    ok, why = scanner_status()
    if not ok:
        lines.append('friction warning: ' + why)
    hot = fr.hot()
    if hot:
        lines.append('Hot friction right now (avoid these; bump the id if you hit one anyway):')
        for row in hot:
            title = row['title'] if row['verified'] else safe_title(row['title'])
            line = f"  #{row['id']} {title} ({row['n']}x, {fmt_duration(row['wasted'])} wasted)"
            if row['workaround'] and row['verified']:
                line += ' — workaround: ' + row['workaround'].replace('\n', ' ')[:200]
            elif row['workaround']:
                line += f" — (unverified workaround: run `friction show {row['id']}`)"
            lines.append(line)
    return '\n'.join(lines)


def nudge_text(fails):
    tools = ', '.join(dict.fromkeys(ev['tool'] or '?' for ev in fails))
    return (f'friction: {len(fails)} tool calls failed or didn\'t do what you expected this session'
            f' ({tools}) and none were logged. If any of that was real friction (wasted turns, a missing credential, a bad'
            ' command, a detour), record it now: `friction log "<problem>" --detail "..."`, or'
            ' `friction bump <id>` for a known one; `friction timeline` shows the calls. If it was'
            ' just normal work, ignore this and finish.')


def queue_scrub(pending, event_id, summary=None, error=None, provisional=None, error_limit=SUMMARY_MAX):
    """Remember raw text to scan for an event (cut like redact() cuts it: limit * 8 characters)."""
    item = {'id': event_id, 'provisional': provisional, 'error_limit': error_limit}
    if summary:
        item['summary'] = summary[:SUMMARY_MAX * 8]
    if error:
        item['error'] = error[:error_limit * 8] if isinstance(error, str) else str(error)[:error_limit * 8]
    pending.append(item)


def flush_scrub(fr, items):
    """Hand raw text to a detached `friction.py scrub` over a pipe; it writes the scanned text to
    the events a moment later. Raw text lives only in the pipe and that process: never in the DB,
    a file, argv or the environment. With FRICTION_SCRUB_SYNC=1 (tests) it is scrubbed in-process.
    If gitleaks is missing or the scrubber cannot start, the rows simply keep NULL text."""
    items = [it for it in items if it.get('summary') or it.get('error')]
    if not items or not scanner_status()[0]:
        return
    if os.environ.get('FRICTION_SCRUB_SYNC'):
        fr.scrub_events(items)
        return
    import subprocess
    try:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / 'friction.py'),
                                 'scrub'], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        proc.stdin.write(json.dumps({'items': items}).encode('utf-8', 'replace'))
        proc.stdin.close()
    except (OSError, ValueError):
        pass


STR_FIELDS = ('hook_event_name', 'session_id', 'thread_id', 'tool_name', 'tool_use_id', 'cwd',
              'transcript_path', 'agent_id', 'source')
DURATION_MAX_MS = 7 * 86400 * 1000


def clean_event(event):
    """The event with identity fields that are not strings dropped and a sane duration_ms.

    Payloads come from other programs: a list where a session id belongs, or a duration of
    1e308, must not reach SQLite or the string code below (it would lose the event)."""
    if not isinstance(event, dict):
        return {}
    event = {k: v for k, v in event.items() if k not in STR_FIELDS or isinstance(v, str)}
    dur = event.get('duration_ms')
    if dur is not None:
        ok = isinstance(dur, (int, float)) and not isinstance(dur, bool) and 0 <= dur <= DURATION_MAX_MS
        event['duration_ms'] = int(dur) if ok else None
    return event


def handle(event, fr, agent):
    pending = []
    out = handle_event(clean_event(event), fr, agent, pending)
    flush_scrub(fr, pending)
    return out


def handle_event(event, fr, agent, pending):
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
            # Pick up what teammates logged in the tracker, at most every few minutes, in the
            # background: this session starts with the cached list, the next one has the news.
            if time.time() - float(fr.meta('sync_kick') or 0) > SYNC_EVERY_S:
                fr.set_meta('sync_kick', time.time())
                kick_sync()
    elif name == 'UserPromptSubmit':
        fr.close_waits(session)
    elif name == 'PermissionRequest':
        fr.open_wait(session, 'permission')
    elif name == 'PreToolUse':
        fr.close_waits(session)       # a denied permission prompt never sees a PostToolUse
        sess = fr.session(session)
        summary = summarize(event.get('tool_name'), event.get('tool_input'))
        # Is this call a re-run of one of the last few? Then those did not do what was wanted.
        recent = fr.db.execute(
            "SELECT * FROM events WHERE session_id=? AND kind='tool' ORDER BY id DESC LIMIT ?",
            (session, RETRY_LOOKAHEAD)).fetchall()[::-1]
        # Stored summaries are scrubbed later, so a previous call whose scrub is still pending has
        # a NULL summary and cannot match: that retry flag is missed (rare: calls are seconds apart).
        seq = [dict(r) for r in recent] + [dict(tool=event.get('tool_name'), summary=summary,
                                                ok=None, outcome=None, ts=time.time())]
        for idx in retry_flags(seq):
            if recent[idx]['ok'] == 1:        # hard failures already count; in-flight ones are ignored
                fr.flag_retry(session, recent[idx]['id'])
        eid = fr.add_event(session, 'tool', tool=event.get('tool_name'),
                           tool_use_id=event.get('tool_use_id'), summary=summary,
                           agent=agent, project=sess['project'] if sess else None, defer_scan=True)
        queue_scrub(pending, eid, summary=summary)
    elif name in ('PostToolUse', 'PostToolUseFailure'):
        bad, error = failed(event)
        outcome = None
        summary = summarize(event.get('tool_name'), event.get('tool_input'))
        self_call = is_self_call(summary)
        if summary is None and event.get('tool_use_id'):      # payload without tool_input
            row = fr.db.execute('SELECT self_call FROM events WHERE session_id=? AND tool_use_id=?'
                                ' ORDER BY id DESC LIMIT 1', (session, event['tool_use_id'])).fetchone()
            self_call = bool(row and row['self_call'])        # the stored summary may not be scrubbed yet
        if self_call:                     # e.g. `friction log` exiting 3 to say "bump instead"
            bad, error = False, None
        elif not bad:
            outcome, error = soft_outcome(event, summary)
        fr.close_waits(session, ('permission',))
        done = fr.finish_event(session, event.get('tool_name'), event.get('tool_use_id'),
                               ok=not bad, error=error, duration_ms=event.get('duration_ms'),
                               outcome=outcome, defer_scan=True)
        if done is not None:
            queue_scrub(pending, done, error=error, provisional=outcome,
                        error_limit=200 if outcome else SUMMARY_MAX)
        if done is not None and event.get('tool_name') in USER_INPUT_TOOLS:
            start = fr.db.execute('SELECT ts FROM events WHERE id=?', (done,)).fetchone()['ts']
            fr.add_wait(session, event.get('tool_name'), start, time.time())
        if done is None:      # no PreToolUse seen (hook added mid-session): record it untimed
            sess = fr.session(session)
            raw = summarize(event.get('tool_name'), event.get('tool_input'))
            eid = fr.add_event(session, 'tool', tool=event.get('tool_name'),
                               tool_use_id=event.get('tool_use_id'), ok=0 if bad else 1,
                               duration_ms=event.get('duration_ms'), error=error, outcome=outcome,
                               summary=raw, agent=agent, project=sess['project'] if sess else None,
                               defer_scan=True)
            queue_scrub(pending, eid, summary=raw, error=error, provisional=outcome,
                        error_limit=200 if outcome else SUMMARY_MAX)
    elif name == 'Stop':
        fails = [] if event.get('stop_hook_active') else fr.unnudged_failures(session)
        if len(fails) >= NUDGE_MIN_FAILURES:
            fr.set_nudged(session)
            out = {'decision': 'block', 'reason': nudge_text(fails)}
        else:                         # the turn is over: from here on it is the user's time
            fr.close_waits(session)
            fr.open_wait(session, 'prompt')
    fr.db.commit()
    return out


def main():
    try:
        if os.environ.get('FRICTION_DISABLE'):
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
