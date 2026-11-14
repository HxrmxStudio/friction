#!/usr/bin/env python3
"""Mine finished agent transcripts for friction nobody logged.

Stage 1 (free, pure Python): read Claude Code and Codex transcripts, turn tool
calls into timed events, and keep only sessions with unlogged failures.

Stage 2 (one small headless `claude -p` call per suspicious session, no tools):
show the analyser an excerpt around the failures plus the existing item list;
it answers with JSON saying which windows were real friction and whether each
matches an existing item. Python then files them through the normal code path.

Timings come from recorded events, so occurrences are marked `hook` when the
live hooks saw the session and `transcript` when rebuilt from the transcript.
"""
import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from friction import fmt_duration, fmt_ts, parse_duration
from friction_hook import failed, summarize, text_of

IDLE_S = 900            # leave sessions touched more recently than this alone
EXCERPT_MAX = 160
ANALYSER_TIMEOUT = 180

SYSTEM = """You review tool-call logs from AI coding agent sessions and identify FRICTION: time the \
agent lost to its environment, tooling or instructions rather than to the task itself. Examples: \
expired or missing credentials, commands invoked with invalid arguments, a tool that is not \
installed, permission denials, flaky services, wrong assumptions about paths or APIs that cost \
several retries, repeating the same failing call.

NOT friction: ordinary iteration on the task (tests failing while code is being written, a build \
error from the agent's own fresh edit that it fixes next call), a search with no matches, a single \
harmless non-zero exit, deliberately probing whether something exists.

You output ONLY a JSON array, no prose, no code fences. Each element:
{"item_id": <id of an existing item that is the same underlying problem, or null>,
 "title": "<short generic name of the underlying problem, e.g. 'gh auth token expired'>",
 "detail": "<what goes wrong and when, one or two sentences>",
 "workaround": "<what got the agent past it, or empty string>",
 "from_event": <event id where the friction began>,
 "to_event": <event id of the last call spent on it>,
 "note": "<what happened in this session, one sentence>"}
Prefer an existing item_id over a new title whenever the root cause is the same. Titles describe \
the problem class, not this instance. Never include secrets. If nothing is real friction, output []."""


def iso_ts(text):
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()
    except (AttributeError, ValueError):
        return None


def parse_claude(path):
    """-> (session_id, cwd, [event dicts]) from a Claude Code transcript."""
    session, cwd, calls, order = None, None, {}, []
    with open(path, errors='replace') as fh:
        for line in fh:
            if '"tool_use"' not in line and '"tool_result"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            content = (d.get('message') or {}).get('content')
            if not isinstance(content, list):
                continue
            ts = iso_ts(d.get('timestamp'))
            session = session or d.get('sessionId') or d.get('session_id')
            cwd = cwd or d.get('cwd')
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'tool_use' and ts:
                    calls[block.get('id')] = {
                        'ts': ts, 'tool': block.get('name'), 'tool_use_id': block.get('id'),
                        'summary': summarize(block.get('name'), block.get('input')),
                        'ok': None, 'error': None, 'duration_ms': None}
                    order.append(block.get('id'))
                elif block.get('type') == 'tool_result':
                    call = calls.get(block.get('tool_use_id'))
                    if not call:
                        continue
                    bad = bool(block.get('is_error'))
                    call['ok'] = 0 if bad else 1
                    call['error'] = text_of(block.get('content')) if bad else None
                    if ts:
                        call['duration_ms'] = max(0, int((ts - call['ts']) * 1000))
    return session, cwd, [calls[i] for i in order if calls[i]['ok'] is not None]


def parse_codex(path):
    """-> (session_id, cwd, [event dicts]) from a Codex rollout file."""
    session, cwd, calls, order = None, None, {}, []
    with open(path, errors='replace') as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            p = d.get('payload')
            if not isinstance(p, dict):
                continue
            if d.get('type') == 'session_meta':
                session, cwd = p.get('session_id') or p.get('id'), p.get('cwd')
                continue
            kind, ts = p.get('type'), iso_ts(d.get('timestamp'))
            if kind in ('function_call', 'custom_tool_call', 'local_shell_call') and ts:
                raw = p.get('arguments') if 'arguments' in p else p.get('input', p.get('action'))
                try:
                    parsed = json.loads(raw) if isinstance(raw, str) else raw
                except ValueError:
                    parsed = raw
                calls[p.get('call_id')] = {
                    'ts': ts, 'tool': p.get('name') or kind, 'tool_use_id': p.get('call_id'),
                    'summary': summarize(p.get('name'), parsed), 'ok': None, 'error': None,
                    'duration_ms': None}
                order.append(p.get('call_id'))
            elif kind in ('function_call_output', 'custom_tool_call_output', 'local_shell_call_output'):
                call = calls.get(p.get('call_id'))
                if not call:
                    continue
                out = p.get('output')
                if isinstance(out, str):
                    try:
                        parsed = json.loads(out)
                        out = parsed if isinstance(parsed, dict) else out
                    except ValueError:
                        pass
                bad, error = failed({'tool_response': out})
                call['ok'] = 0 if bad else 1
                call['error'] = error
                if ts:
                    call['duration_ms'] = max(0, int((ts - call['ts']) * 1000))
    return session, cwd, [calls[i] for i in order if calls[i]['ok'] is not None]


def transcripts(since_ts, now):
    """(path, agent, mtime) for transcripts touched in the window and idle since."""
    home = Path.home()
    skip = '-local-share-friction'      # the analyser's own working directory
    found = []
    for agent, pattern in (('claude', '.claude/projects/*/*.jsonl'),
                           ('codex', '.codex/sessions/*/*/*/*.jsonl')):
        for path in home.glob(pattern):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if since_ts <= mtime <= now - IDLE_S and skip not in str(path):
                found.append((path, agent, mtime))
    return sorted(found, key=lambda x: x[2])


def import_events(fr, session, agent, cwd, path, events):
    """Add transcript events newer than anything recorded, unless hooks covered the session."""
    if fr.db.execute("SELECT 1 FROM events WHERE session_id=? AND source='hook' LIMIT 1",
                     (session,)).fetchone():
        return 0
    last = fr.db.execute('SELECT MAX(ts) m FROM events WHERE session_id=?', (session,)).fetchone()['m']
    fresh = [ev for ev in events if last is None or ev['ts'] > last]
    if events:
        fr.touch_session(session, agent, cwd, str(path), ts=events[0]['ts'])
        fr.touch_session(session, agent, cwd, str(path), ts=events[-1]['ts'])
    project = fr.session(session)['project'] if events else None
    for ev in fresh:
        fr.add_event(session, 'tool', tool=ev['tool'], tool_use_id=ev['tool_use_id'], ok=ev['ok'],
                     duration_ms=ev['duration_ms'], summary=ev['summary'], error=ev['error'],
                     ts=ev['ts'], agent=agent, project=project, source='transcript')
    fr.db.commit()
    return len(fresh)


def excerpt(fr, session):
    """Unlogged failures with a little context either side, as text. -> (text, valid ids)"""
    sess = fr.session(session)
    rows = fr.db.execute(
        "SELECT * FROM events WHERE session_id=? AND id>? AND kind='tool' AND ok IS NOT NULL"
        ' ORDER BY id', (session, sess['mark_event'])).fetchall()
    keep = set()
    for i, ev in enumerate(rows):
        if ev['ok'] == 0:
            keep.update(range(max(0, i - 2), min(len(rows), i + 4)))
    lines, prev = [], None
    for i in sorted(keep)[:EXCERPT_MAX]:
        ev = rows[i]
        if prev is not None and i - prev > 1:
            lines.append(f'      … {i - prev - 1} successful calls omitted …')
        dur = f"{ev['duration_ms'] / 1000:.1f}s" if ev['duration_ms'] is not None else '?'
        line = (f"[{ev['id']}] {fmt_ts(ev['ts'], '%H:%M:%S')} {'ok' if ev['ok'] else 'FAIL'} {dur}"
                f" {ev['tool']}: {(ev['summary'] or '').replace(chr(10), ' ')[:220]}")
        if ev['ok'] == 0 and ev['error']:
            line += '\n      -> ' + ev['error'].replace('\n', ' ')[:320]
        lines.append(line)
        prev = i
    return '\n'.join(lines), {rows[i]['id'] for i in keep}


def build_prompt(fr, session, text):
    sess = fr.session(session)
    items = fr.db.execute("SELECT id, status, title, aliases FROM items WHERE status!='merged'"
                          ' ORDER BY updated DESC LIMIT 150').fetchall()
    parts = ['EXISTING FRICTION ITEMS:']
    parts += [f"  #{r['id']} [{r['status']}] {r['title']}"
              + (f" (aka {r['aliases'].replace(chr(10), '; ')})" if r['aliases'] else '')
              for r in items] or ['  (none yet)']
    logged = fr.db.execute(
        'SELECT o.item_id, i.title, o.from_event, o.to_event FROM occurrences o'
        ' JOIN items i ON i.id=o.item_id WHERE o.session_id=?', (session,)).fetchall()
    if logged:
        parts.append('\nALREADY LOGGED FOR THIS SESSION (do not report again):')
        parts += [f"  #{r['item_id']} {r['title']} (events {r['from_event']}–{r['to_event']})"
                  for r in logged]
    parts.append(f"\nSESSION: agent={sess['agent']} project={sess['project']} cwd={sess['cwd']}")
    parts.append('TOOL CALLS ([event id] time result duration tool: input -> error):')
    parts.append(text)
    return '\n'.join(parts)


def analyse(prompt, model):
    """Run the headless analyser. -> list of findings (raises on failure)."""
    exe = shutil.which('claude') or str(Path.home() / '.local/bin/claude')
    workdir = Path.home() / '.local/share/friction/analyser'
    workdir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, FRICTION_DISABLE='1')
    for key in ('CLAUDE_CODE_SESSION_ID', 'CLAUDECODE', 'AGENT_MAILBOX_ID', 'HC_TERMINAL_ID'):
        env.pop(key, None)
    proc = subprocess.run(
        [exe, '-p', '--model', model, '--output-format', 'json', '--no-session-persistence',
         '--tools', '', '--strict-mcp-config', '--setting-sources', '', '--system-prompt', SYSTEM],
        input=prompt, capture_output=True, text=True, timeout=ANALYSER_TIMEOUT, cwd=workdir, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f'analyser exit {proc.returncode}: {(proc.stderr or proc.stdout)[:300]}')
    reply = json.loads(proc.stdout)
    if reply.get('is_error'):
        raise RuntimeError(f"analyser error: {str(reply.get('result'))[:300]}")
    text = (reply.get('result') or '').strip()
    start, end = text.find('['), text.rfind(']')
    if start < 0 or end < start:
        raise RuntimeError(f'analyser returned no JSON array: {text[:200]}')
    found = json.loads(text[start:end + 1])
    return [f for f in found if isinstance(f, dict)]


def file_findings(fr, session, findings, valid_ids):
    """Record analyser findings as occurrences. -> number filed"""
    filed = 0
    for f in findings:
        start, end = f.get('from_event'), f.get('to_event')
        if not isinstance(start, int) or start not in valid_ids:
            continue
        if not isinstance(end, int) or end not in valid_ids or end < start:
            end = start
        item_id = f.get('item_id')
        if isinstance(item_id, int):
            try:
                item_id = fr.item(item_id)['id']
            except KeyError:
                item_id = None
        else:
            item_id = None
        title = str(f.get('title') or '').strip()
        if item_id is None:
            if not title:
                continue
            item_id = fr.create(title, str(f.get('detail') or ''), str(f.get('workaround') or ''))
        elif f.get('workaround') and not fr.item(item_id)['workaround']:
            fr.set_workaround(item_id, str(f['workaround']))
        fr.occur(item_id, session_id=session, note=str(f.get('note') or ''), from_event=start,
                 to_event=end, source='backfill')
        filed += 1
    return filed


def run(fr, args):
    now = time.time()
    since_ts = now - parse_duration(args.since)
    current = os.environ.get('CLAUDE_CODE_SESSION_ID')
    candidates = []
    for path, agent, mtime in transcripts(since_ts, now):
        done = fr.db.execute('SELECT mtime FROM backfill_sessions WHERE path=?',
                             (str(path),)).fetchone()
        if done and done['mtime'] == mtime and not args.redo:
            continue
        try:
            session, cwd, events = (parse_claude if agent == 'claude' else parse_codex)(path)
        except OSError:
            continue
        if not session or session == current:
            continue
        if events:
            import_events(fr, session, agent, cwd, path, events)
        fails = fr.unlogged_failures(session) if fr.session(session) else []
        if len(fails) < args.min_failures:
            fr.db.execute('INSERT OR REPLACE INTO backfill_sessions VALUES(?,?,?,?,?,?,?)',
                          (str(path), session, mtime, now, len(fails), 0, 'clean'))
            fr.db.commit()
            continue
        candidates.append((path, agent, mtime, session, fails))

    print(f'{len(candidates)} sessions with unlogged failures since {fmt_ts(since_ts)}.')
    if args.dry_run:
        for path, agent, mtime, session, fails in candidates:
            sess = fr.session(session)
            span = fails[-1]['ts'] - fails[0]['ts']
            print(f"  {fmt_ts(mtime)} {agent:<6} {sess['project'] or '?':<24} {len(fails):>3} failures"
                  f" over {fmt_duration(span)}  {session}")
        return 0

    total = 0
    for path, agent, mtime, session, fails in candidates[:args.limit]:
        text, valid = excerpt(fr, session)
        status, filed = 'analysed', 0
        try:
            findings = analyse(build_prompt(fr, session, text), args.model)
            filed = file_findings(fr, session, findings, valid)
        except Exception as exc:     # leave unprocessed so the next run retries
            print(f'  {session}: analyser failed: {exc}')
            continue
        fr.set_mark(session)
        fr.db.execute('INSERT OR REPLACE INTO backfill_sessions VALUES(?,?,?,?,?,?,?)',
                      (str(path), session, mtime, time.time(), len(fails), filed, status))
        fr.db.commit()
        total += filed
        sess = fr.session(session)
        print(f"  {fmt_ts(mtime)} {agent:<6} {sess['project'] or '?':<24} {len(fails):>3} failures"
              f" -> {filed} friction occurrences")
    if len(candidates) > args.limit:
        print(f'{len(candidates) - args.limit} more sessions left for the next run (--limit).')
    print(f'Filed {total} occurrences. Pruned {fr.prune_events()} old events.')
    return 0
