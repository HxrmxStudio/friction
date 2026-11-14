#!/usr/bin/env python3
"""friction: a small shared log of what slows agents down.

Agents record friction items (a bad command, a missing credential, an auth
timeout), upvote ones another agent already found, and attach workarounds.
Hooks (friction_hook.py) record every tool call with real timestamps, so the
time an occurrence wasted is measured rather than guessed where possible.

Storage is one SQLite file (WAL), stdlib only. See README.md.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DIR = '~/.local/share/friction'
SUMMARY_MAX = 500
EVENT_RETENTION_DAYS = 30
STREAK_GAP_S = 600          # failures further apart than this are separate streaks
IDLE_GAP_S = 300            # a longer pause between calls is idle time, not wasted time
SELF_LOOKUP_S = 120         # how far back to look for our own in-flight Bash call

# How an occurrence's wasted time was obtained.
TIMING_HOOK = 'hook'              # measured from hook-recorded tool events
TIMING_TRANSCRIPT = 'transcript'  # measured from transcript timestamps (backfill)
TIMING_ESTIMATE = 'estimate'      # the agent's own guess: the fallback
TIMING_NONE = 'none'              # nothing to go on

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,              -- when the call started (epoch seconds)
  session_id TEXT NOT NULL,
  agent TEXT,
  project TEXT,
  kind TEXT NOT NULL,            -- tool | prompt
  tool TEXT,
  tool_use_id TEXT,
  ok INTEGER,                    -- NULL while in flight, 1 success, 0 failure
  duration_ms INTEGER,
  summary TEXT,
  error TEXT,
  source TEXT NOT NULL DEFAULT 'hook'   -- hook | transcript
);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, id);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_tuid ON events(tool_use_id);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  agent TEXT,
  project TEXT,
  cwd TEXT,
  transcript TEXT,
  started REAL,
  last_seen REAL,
  mark_event INTEGER NOT NULL DEFAULT 0,   -- failures at or before this event are already logged
  nudge_event INTEGER NOT NULL DEFAULT 0   -- failures at or before this event were nudged about
);

CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  workaround TEXT NOT NULL DEFAULT '',
  aliases TEXT NOT NULL DEFAULT '',       -- titles of items merged into this one
  status TEXT NOT NULL DEFAULT 'open',    -- open | resolved | wontfix | merged
  fix_note TEXT NOT NULL DEFAULT '',
  regressions INTEGER NOT NULL DEFAULT 0,
  merged_into INTEGER,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  resolved_at REAL
);

CREATE TABLE IF NOT EXISTS occurrences (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id),
  ts REAL NOT NULL,
  session_id TEXT,
  agent TEXT,
  project TEXT,
  wasted_s REAL,
  timing TEXT NOT NULL,          -- hook | transcript | estimate | none
  from_event INTEGER,
  to_event INTEGER,
  tool_calls INTEGER,
  failed_calls INTEGER,
  note TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'live',   -- live | backfill
  regression INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS occ_item ON occurrences(item_id);
CREATE INDEX IF NOT EXISTS occ_ts ON occurrences(ts);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  title, detail, workaround, aliases, tokenize='porter unicode61');

CREATE TABLE IF NOT EXISTS backfill_sessions (
  path TEXT PRIMARY KEY,
  session_id TEXT,
  mtime REAL,
  processed_at REAL,
  failures INTEGER,
  filed INTEGER,
  status TEXT
);
"""

SECRET_PATTERNS = [
    (re.compile(r'\b(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}'
                r'|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})'), '[REDACTED]'),
    (re.compile(r'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'), '[REDACTED_JWT]'),
    (re.compile(r'(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}'), r'\1 [REDACTED]'),
    (re.compile(r'(?i)([A-Za-z0-9_-]*(?:token|secret|passwd|password|api[_-]?key|access[_-]?key|authorization)'
                r'[A-Za-z0-9_-]*)(["\']?\s*[:=]\s*["\']?)([^\s"\'&]{6,})'), r'\1\2[REDACTED]'),
    (re.compile(r'(://[^/\s:@]+):[^@\s/]+@'), r'\1:[REDACTED]@'),
]

STOPWORDS = set('a an and are as at be but by for from has have i in is it its not of on or so that the '
                'this to was when with while after before then than into no'.split())


def redact(text, limit=SUMMARY_MAX):
    """Scrub obvious secrets, collapse whitespace runs, truncate."""
    if text is None:
        return None
    if not isinstance(text, str):
        try:
            text = json.dumps(text, default=str)
        except Exception:
            text = str(text)
    text = text[:limit * 8]
    for pat, rep in SECRET_PATTERNS:
        text = pat.sub(rep, text)
    text = re.sub(r'[ \t]+', ' ', text).strip()
    if len(text) > limit:
        text = text[:limit - 1] + '…'
    return text


def project_of(cwd):
    """A short project label for a working directory."""
    if not cwd:
        return None
    p = Path(cwd)
    home = Path.home()
    try:
        rel = p.relative_to(home)
    except ValueError:
        return p.name or str(p)
    parts = rel.parts
    if not parts:
        return '~'
    if parts[0] == 'code' and len(parts) > 1:
        return parts[1]
    return parts[0]


def parse_duration(text):
    """'90', '90s', '5m', '1h30m', '2m10s' -> seconds."""
    text = str(text).strip().lower()
    if re.fullmatch(r'\d+(\.\d+)?', text):
        return float(text)
    total, pos = 0.0, 0
    for m in re.finditer(r'(\d+(?:\.\d+)?)\s*(d|h|m|s)', text):
        if m.start() != pos and text[pos:m.start()].strip():
            raise ValueError(f'bad duration: {text!r}')
        total += float(m.group(1)) * {'d': 86400, 'h': 3600, 'm': 60, 's': 1}[m.group(2)]
        pos = m.end()
    if pos == 0 or text[pos:].strip():
        raise ValueError(f'bad duration: {text!r}')
    return total


def fmt_duration(seconds):
    if seconds is None:
        return '-'
    seconds = int(round(seconds))
    if seconds < 60:
        return f'{seconds}s'
    if seconds < 3600:
        return f'{seconds // 60}m{seconds % 60:02d}s'
    return f'{seconds // 3600}h{(seconds % 3600) // 60:02d}m'


def fmt_ts(ts, fmt='%Y-%m-%d %H:%M'):
    return time.strftime(fmt, time.localtime(ts))


class Friction:
    def __init__(self, root=None):
        root = root or os.environ.get('FRICTION_DIR') or DEFAULT_DIR
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'friction.db', timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.executescript(SCHEMA)

    # ---- sessions and events (written by hooks and backfill) -------------

    def touch_session(self, session_id, agent=None, cwd=None, transcript=None, ts=None):
        ts = ts or time.time()
        self.db.execute(
            'INSERT INTO sessions(session_id, agent, project, cwd, transcript, started, last_seen)'
            ' VALUES(?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET'
            ' agent=COALESCE(excluded.agent, agent), cwd=COALESCE(excluded.cwd, cwd),'
            ' project=COALESCE(excluded.project, project),'
            ' transcript=COALESCE(excluded.transcript, transcript),'
            ' started=MIN(started, excluded.started), last_seen=MAX(last_seen, excluded.last_seen)',
            (session_id, agent, project_of(cwd), cwd, transcript, ts, ts))

    def session(self, session_id):
        return self.db.execute('SELECT * FROM sessions WHERE session_id=?', (session_id,)).fetchone()

    def add_event(self, session_id, kind, tool=None, tool_use_id=None, ok=None, duration_ms=None,
                  summary=None, error=None, ts=None, agent=None, project=None, source='hook'):
        cur = self.db.execute(
            'INSERT INTO events(ts, session_id, agent, project, kind, tool, tool_use_id, ok,'
            ' duration_ms, summary, error, source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (ts or time.time(), session_id, agent, project, kind, tool, tool_use_id, ok,
             duration_ms, redact(summary), redact(error), source))
        return cur.lastrowid

    def finish_event(self, session_id, tool, tool_use_id, ok, error=None, duration_ms=None, now=None):
        """Close the in-flight event for a tool call. Returns its id, or None if there is none."""
        now = now or time.time()
        row = None
        if tool_use_id:
            row = self.db.execute(
                'SELECT id, ts FROM events WHERE session_id=? AND tool_use_id=? AND ok IS NULL'
                ' ORDER BY id DESC LIMIT 1', (session_id, tool_use_id)).fetchone()
        if row is None:
            row = self.db.execute(
                'SELECT id, ts FROM events WHERE session_id=? AND tool=? AND ok IS NULL'
                ' ORDER BY id DESC LIMIT 1', (session_id, tool)).fetchone()
        if row is None:
            return None
        if duration_ms is None:
            duration_ms = max(0, int((now - row['ts']) * 1000))
        self.db.execute('UPDATE events SET ok=?, error=?, duration_ms=? WHERE id=?',
                        (1 if ok else 0, redact(error), duration_ms, row['id']))
        return row['id']

    def has_events(self, session_id):
        return self.db.execute('SELECT 1 FROM events WHERE session_id=? LIMIT 1',
                               (session_id,)).fetchone() is not None

    def timeline(self, session_id, limit=30):
        rows = self.db.execute(
            'SELECT * FROM events WHERE session_id=? ORDER BY id DESC LIMIT ?',
            (session_id, limit)).fetchall()
        return rows[::-1]

    def unlogged_failures(self, session_id):
        """Failed tool calls in this session not yet covered by a logged occurrence."""
        sess = self.session(session_id)
        mark = sess['mark_event'] if sess else 0
        return self.db.execute(
            "SELECT * FROM events WHERE session_id=? AND id>? AND kind='tool' AND ok=0 ORDER BY id",
            (session_id, mark)).fetchall()

    def unnudged_failures(self, session_id):
        """Unlogged failures the Stop hook has not yet asked the agent about."""
        sess = self.session(session_id)
        seen = sess['nudge_event'] if sess else 0
        return [ev for ev in self.unlogged_failures(session_id) if ev['id'] > seen]

    def set_nudged(self, session_id):
        self.db.execute('UPDATE sessions SET nudge_event=(SELECT COALESCE(MAX(id), 0) FROM events'
                        ' WHERE session_id=?) WHERE session_id=?', (session_id, session_id))

    def set_mark(self, session_id, event_id=None):
        if event_id is None:
            row = self.db.execute('SELECT MAX(id) m FROM events WHERE session_id=?',
                                  (session_id,)).fetchone()
            event_id = row['m'] or 0
        self.db.execute('UPDATE sessions SET mark_event=MAX(mark_event, ?) WHERE session_id=?',
                        (event_id, session_id))

    def guess_session(self, env=os.environ, cwd=None, now=None):
        """Which session is calling the CLI: env first, else our own in-flight Bash call."""
        for key in ('FRICTION_SESSION', 'CLAUDE_CODE_SESSION_ID', 'CODEX_SESSION_ID',
                    'CODEX_THREAD_ID'):
            if env.get(key):
                return env[key]
        now = now or time.time()
        row = self.db.execute(
            "SELECT session_id FROM events WHERE kind='tool' AND ok IS NULL AND ts>?"
            " AND summary LIKE '%friction%' ORDER BY id DESC LIMIT 1",
            (now - SELF_LOOKUP_S,)).fetchone()
        return row['session_id'] if row else None

    def prune_events(self, days=EVENT_RETENTION_DAYS, now=None):
        cutoff = (now or time.time()) - days * 86400
        cur = self.db.execute('DELETE FROM events WHERE ts<?', (cutoff,))
        self.db.execute('DELETE FROM sessions WHERE last_seen<?', (cutoff,))
        self.db.commit()
        return cur.rowcount

    # ---- measuring the wasted window -------------------------------------

    def window(self, session_id, from_event=None, to_event=None, now=None):
        """Measure a friction window from recorded events.

        from_event None means the start of the current failure streak: the
        unlogged failures closest to now, joined while they are less than
        STREAK_GAP_S apart. Returns None when there is nothing to measure from.
        """
        now = now or time.time()
        if from_event is None:
            fails = self.unlogged_failures(session_id)
            if not fails:
                return None
            start = fails[-1]
            for ev in reversed(fails[:-1]):
                if start['ts'] - ev['ts'] > STREAK_GAP_S:
                    break
                start = ev
        else:
            start = self.db.execute('SELECT * FROM events WHERE id=? AND session_id=?',
                                    (from_event, session_id)).fetchone()
            if start is None:
                raise ValueError(f'event {from_event} is not in session {session_id}')
        end_ts, end_id = now, None
        if to_event is not None:
            end = self.db.execute('SELECT * FROM events WHERE id=? AND session_id=?',
                                  (to_event, session_id)).fetchone()
            if end is None:
                raise ValueError(f'event {to_event} is not in session {session_id}')
            end_ts, end_id = end['ts'] + (end['duration_ms'] or 0) / 1000, end['id']
        hi = end_id if end_id is not None else 1 << 62
        # Our own `friction ...` call is still in flight (ok IS NULL): not counted.
        counts = self.db.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(ok=0), 0) f, MAX(id) hi, MIN(source) src FROM events"
            " WHERE session_id=? AND kind='tool' AND id>=? AND id<=? AND ok IS NOT NULL",
            (session_id, start['id'], hi)).fetchone()
        return {
            'wasted_s': self.active_seconds(session_id, start['id'], hi, end_ts),
            'timing': TIMING_TRANSCRIPT if counts['src'] == 'transcript' else TIMING_HOOK,
            'from_event': start['id'],
            'to_event': end_id if end_id is not None else counts['hi'],
            'tool_calls': counts['n'],
            'failed_calls': counts['f'],
            'end_ts': end_ts,
        }

    def active_seconds(self, session_id, first_id, last_id, end_ts):
        """Time from event first_id to end_ts, not counting idle gaps.

        A gap between calls longer than IDLE_GAP_S beyond the call's own
        duration is the session sitting idle (user away, waiting for a prompt),
        not time the friction cost; only the call's duration is counted for it.
        """
        rows = self.db.execute(
            "SELECT ts, duration_ms FROM events WHERE session_id=? AND kind='tool' AND id>=? AND id<=?"
            ' ORDER BY ts', (session_id, first_id, last_id)).fetchall()
        total = 0.0
        for i, ev in enumerate(rows):
            nxt = rows[i + 1]['ts'] if i + 1 < len(rows) else end_ts
            ran = (ev['duration_ms'] or 0) / 1000
            gap = max(0.0, nxt - ev['ts'])
            total += gap if gap <= ran + IDLE_GAP_S else ran
        return total

    # ---- items -----------------------------------------------------------

    def _index(self, item_id):
        row = self.db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
        self.db.execute('DELETE FROM items_fts WHERE rowid=?', (item_id,))
        if row and row['status'] != 'merged':
            self.db.execute(
                'INSERT INTO items_fts(rowid, title, detail, workaround, aliases) VALUES(?,?,?,?,?)',
                (item_id, row['title'], row['detail'], row['workaround'], row['aliases']))

    def item(self, item_id, follow=True):
        row = self.db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
        seen = set()
        while follow and row is not None and row['merged_into'] and row['id'] not in seen:
            seen.add(row['id'])
            row = self.db.execute('SELECT * FROM items WHERE id=?', (row['merged_into'],)).fetchone()
        if row is None:
            raise KeyError(f'no friction item #{item_id}')
        return row

    def search(self, text, limit=5):
        """Existing items that look like this text, best first."""
        words = [w for w in re.findall(r'[A-Za-z0-9_]+', text.lower())
                 if w not in STOPWORDS and len(w) > 1]
        if not words:
            return []
        query = ' OR '.join(f'"{w}"' for w in dict.fromkeys(words))
        try:
            rows = self.db.execute(
                'SELECT i.*, bm25(items_fts, 8.0, 2.0, 1.0, 6.0) AS rank FROM items_fts'
                ' JOIN items i ON i.id = items_fts.rowid WHERE items_fts MATCH ?'
                ' ORDER BY rank LIMIT ?', (query, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
        return rows

    def stats(self, item_id):
        return self.db.execute(
            'SELECT COUNT(*) n, COALESCE(SUM(wasted_s), 0) wasted, AVG(wasted_s) avg_wasted,'
            " COALESCE(SUM(timing='estimate'), 0) estimated, MAX(ts) last_ts,"
            ' COUNT(DISTINCT session_id) sessions FROM occurrences WHERE item_id=?',
            (item_id,)).fetchone()

    def create(self, title, detail='', workaround='', now=None):
        now = now or time.time()
        cur = self.db.execute(
            'INSERT INTO items(title, detail, workaround, created, updated) VALUES(?,?,?,?,?)',
            (redact(title, 200), redact(detail, 2000) or '', redact(workaround, 2000) or '', now, now))
        self._index(cur.lastrowid)
        return cur.lastrowid

    def occur(self, item_id, session_id=None, agent=None, project=None, note='', from_event=None,
              to_event=None, estimate_s=None, source='live', now=None):
        """Record one occurrence of an item (the upvote). Reopens a resolved item as a regression."""
        now = now or time.time()
        item = self.item(item_id)
        item_id = item['id']
        sess = self.session(session_id) if session_id else None
        if sess:
            agent = agent or sess['agent']
            project = project or sess['project']
        win = None
        if session_id and self.has_events(session_id):
            win = self.window(session_id, from_event, to_event, now)
        if win:
            wasted, timing, ts = win['wasted_s'], win['timing'], win['end_ts']
        elif estimate_s is not None:
            wasted, timing, ts = estimate_s, TIMING_ESTIMATE, now
        else:
            wasted, timing, ts = None, TIMING_NONE, now
        # A historical (backfilled) hit from before the fix is not a regression.
        regression = 1 if item['status'] == 'resolved' and ts >= (item['resolved_at'] or 0) else 0
        cur = self.db.execute(
            'INSERT INTO occurrences(item_id, ts, session_id, agent, project, wasted_s, timing,'
            ' from_event, to_event, tool_calls, failed_calls, note, source, regression)'
            ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (item_id, ts, session_id, agent, project, wasted, timing,
             win and win['from_event'], win and win['to_event'], win and win['tool_calls'],
             win and win['failed_calls'], redact(note, 1000) or '', source, regression))
        if regression:
            self.db.execute("UPDATE items SET status='open', regressions=regressions+1,"
                            ' resolved_at=NULL, updated=? WHERE id=?', (now, item_id))
        else:
            self.db.execute('UPDATE items SET updated=? WHERE id=?', (now, item_id))
        if session_id and source == 'live':
            self.set_mark(session_id)
        self.db.commit()
        return {'occurrence': cur.lastrowid, 'item': item_id, 'wasted_s': wasted, 'timing': timing,
                'regression': bool(regression), 'tool_calls': win and win['tool_calls'],
                'failed_calls': win and win['failed_calls']}

    def set_workaround(self, item_id, text, append=False, now=None):
        item = self.item(item_id)
        text = redact(text, 2000)
        if append and item['workaround']:
            text = item['workaround'] + '\n' + text
        self.db.execute('UPDATE items SET workaround=?, updated=? WHERE id=?',
                        (text, now or time.time(), item['id']))
        self._index(item['id'])
        self.db.commit()
        return item['id']

    def set_status(self, item_id, status, note='', now=None):
        now = now or time.time()
        item = self.item(item_id)
        if status == 'open':
            self.db.execute("UPDATE items SET status='open', resolved_at=NULL, updated=? WHERE id=?",
                            (now, item['id']))
        else:
            self.db.execute('UPDATE items SET status=?, fix_note=?, resolved_at=?, updated=? WHERE id=?',
                            (status, redact(note, 2000) or item['fix_note'], now, now, item['id']))
        self.db.commit()
        return item['id']

    def merge(self, src_id, dst_id, now=None):
        """Fold item src into dst: occurrences move, src's title stays searchable as an alias."""
        now = now or time.time()
        src, dst = self.item(src_id), self.item(dst_id)
        if src['id'] == dst['id']:
            raise ValueError('cannot merge an item into itself')
        aliases = '\n'.join(x for x in (dst['aliases'], src['title'], src['aliases']) if x)
        detail = dst['detail'] or src['detail']
        workaround = dst['workaround']
        if src['workaround'] and src['workaround'] not in workaround:
            workaround = (workaround + '\n' + src['workaround']).strip()
        status = 'open' if 'open' in (src['status'], dst['status']) else dst['status']
        self.db.execute('UPDATE occurrences SET item_id=? WHERE item_id=?', (dst['id'], src['id']))
        self.db.execute('UPDATE items SET merged_into=? WHERE merged_into=?', (dst['id'], src['id']))
        self.db.execute(
            'UPDATE items SET aliases=?, detail=?, workaround=?, status=?, regressions=regressions+?,'
            ' created=MIN(created, ?), updated=? WHERE id=?',
            (aliases, detail, workaround, status, src['regressions'], src['created'], now, dst['id']))
        self.db.execute("UPDATE items SET status='merged', merged_into=?, updated=? WHERE id=?",
                        (dst['id'], now, src['id']))
        self._index(src['id'])
        self._index(dst['id'])
        self.db.commit()
        return dst['id']

    # ---- reporting -------------------------------------------------------

    def hot(self, days=14, limit=5, now=None):
        """Open items hit recently, costliest first."""
        since = (now or time.time()) - days * 86400
        return self.db.execute(
            'SELECT i.*, COUNT(o.id) n, COALESCE(SUM(o.wasted_s), 0) wasted FROM items i'
            " JOIN occurrences o ON o.item_id=i.id WHERE i.status='open' AND o.ts>=?"
            ' GROUP BY i.id ORDER BY n * 60 + wasted DESC, MAX(o.ts) DESC LIMIT ?',
            (since, limit)).fetchall()

    def report(self, days=7, project=None, agent=None, now=None):
        now = now or time.time()
        since = now - days * 86400
        where, args = ['o.ts>=?'], [since]
        if project:
            where.append('o.project=?')
            args.append(project)
        if agent:
            where.append('o.agent=?')
            args.append(agent)
        cond = ' AND '.join(where)
        rows = self.db.execute(
            'SELECT i.id, i.title, i.status, i.workaround, i.fix_note, i.regressions,'
            ' COUNT(o.id) n, COUNT(DISTINCT o.session_id) sessions,'
            ' COALESCE(SUM(o.wasted_s), 0) wasted, AVG(o.wasted_s) avg_wasted,'
            " COALESCE(SUM(o.timing='estimate'), 0) estimated,"
            " COALESCE(SUM(CASE WHEN o.timing='estimate' THEN o.wasted_s END), 0) wasted_estimated,"
            " COALESCE(SUM(o.timing='none'), 0) untimed,"
            ' COALESCE(SUM(o.regression), 0) regressed, MAX(o.ts) last_ts,'
            " COALESCE(SUM(o.source='backfill'), 0) backfilled"
            f' FROM occurrences o JOIN items i ON i.id=o.item_id WHERE {cond}'
            ' GROUP BY i.id ORDER BY wasted DESC, n DESC', args).fetchall()
        items = [dict(r) for r in rows]
        daily = self.db.execute(
            "SELECT o.item_id, date(o.ts, 'unixepoch', 'localtime') day, COUNT(*) n,"
            f' COALESCE(SUM(o.wasted_s), 0) wasted FROM occurrences o WHERE {cond}'
            ' GROUP BY o.item_id, day', args).fetchall()
        day_list = [time.strftime('%Y-%m-%d', time.localtime(now - d * 86400))
                    for d in range(days - 1, -1, -1)]
        by_item = {}
        for r in daily:
            by_item.setdefault(r['item_id'], {})[r['day']] = {'n': r['n'], 'wasted': r['wasted']}
        for it in items:
            it['days'] = by_item.get(it['id'], {})
        facets = {}
        for col in ('project', 'agent'):
            facets[col] = [dict(r) for r in self.db.execute(
                f'SELECT o.{col} name, COUNT(*) n, COALESCE(SUM(o.wasted_s), 0) wasted'
                f' FROM occurrences o WHERE {cond} GROUP BY o.{col} ORDER BY wasted DESC', args)]
        total = {
            'occurrences': sum(i['n'] for i in items),
            'items': len(items),
            'wasted_s': sum(i['wasted'] for i in items),
            'wasted_estimated_s': sum(i['wasted_estimated'] for i in items),
            'estimated': sum(i['estimated'] for i in items),
            'untimed': sum(i['untimed'] for i in items),
            'regressed': sum(i['regressed'] for i in items),
        }
        return {'days': days, 'since': since, 'now': now, 'day_list': day_list, 'total': total,
                'items': items, 'projects': facets['project'], 'agents': facets['agent']}

    def occurrences(self, item_id, limit=50):
        return self.db.execute('SELECT * FROM occurrences WHERE item_id=? ORDER BY ts DESC LIMIT ?',
                               (item_id, limit)).fetchall()

    def evidence(self, occ):
        """The recorded tool events inside an occurrence's window (gone once pruned)."""
        if not occ['from_event'] or not occ['session_id']:
            return []
        return self.db.execute(
            'SELECT * FROM events WHERE session_id=? AND id>=? AND id<=? ORDER BY id LIMIT 200',
            (occ['session_id'], occ['from_event'], occ['to_event'] or occ['from_event'])).fetchall()


# ---- CLI -----------------------------------------------------------------

def timing_label(timing, wasted):
    if timing == TIMING_ESTIMATE:
        return f'~{fmt_duration(wasted)} (ESTIMATED by agent, not measured)'
    if timing == TIMING_NONE:
        return 'not timed'
    return f'{fmt_duration(wasted)} (measured from {timing} events)'


def item_line(fr, row):
    st = fr.stats(row['id'])
    bits = [f"#{row['id']}", f"[{row['status']}]", row['title'],
            f"- {st['n']} hits, {fmt_duration(st['wasted'])} wasted"]
    if st['estimated']:
        bits.append(f"({st['estimated']} est.)")
    return ' '.join(bits)


def event_line(ev):
    state = {None: '…', 1: 'ok', 0: 'FAIL'}[ev['ok']]
    dur = f"{ev['duration_ms'] / 1000:.1f}s" if ev['duration_ms'] is not None else '  -  '
    line = f"{ev['id']:>7} {fmt_ts(ev['ts'], '%H:%M:%S')} {state:<4} {dur:>7} {ev['tool'] or ev['kind']}"
    if ev['summary']:
        line += ': ' + ev['summary'].replace('\n', ' ')[:110]
    if ev['ok'] == 0 and ev['error']:
        line += '\n' + ' ' * 30 + '-> ' + ev['error'].replace('\n', ' ')[:140]
    return line


def _occur_args(p):
    p.add_argument('--note', default='', help='what happened this time')
    p.add_argument('--from', dest='from_event', type=int, metavar='EVENT',
                   help='event id where the friction began (see `friction timeline`);'
                        ' default: start of the current failure streak')
    p.add_argument('--wasted', metavar='DUR',
                   help='your estimate (90s, 5m): used only when no recorded events can time it')
    p.add_argument('--session', help='session id (default: detected)')


def _occur(fr, args, item_id):
    session = args.session or fr.guess_session()
    estimate = parse_duration(args.wasted) if args.wasted else None
    known = bool(session and fr.session(session))
    ai = os.environ.get('AI_AGENT', '').lower()
    agent = None if known else next((a for a in ('claude', 'codex') if a in ai), None)
    res = fr.occur(item_id, session_id=session, note=args.note, from_event=args.from_event,
                   estimate_s=estimate, agent=agent,
                   project=None if known else project_of(os.getcwd()))
    item = fr.item(res['item'])
    print(f"Logged occurrence on #{item['id']} \"{item['title']}\": "
          f"{timing_label(res['timing'], res['wasted_s'])}"
          + (f", {res['tool_calls']} tool calls ({res['failed_calls']} failed)"
             if res['tool_calls'] is not None else ''))
    if res['regression']:
        print('REGRESSION: this item was resolved; it is open again.')
        if item['fix_note']:
            print(f"  previous fix: {item['fix_note']}")
    if res['timing'] == TIMING_NONE:
        print('No recorded events or estimate: pass --wasted <dur> next time so it can be costed.')
    if item['workaround']:
        print(f"Known workaround: {item['workaround']}")
    else:
        print(f"No workaround recorded. If you found one: friction workaround {item['id']} \"...\"")
    return 0


def cmd_log(fr, args):
    if not args.new:
        matches = fr.search(args.title + ' ' + (args.detail or ''))
        if matches:
            print('Similar friction items already exist:')
            for row in matches:
                print('  ' + item_line(fr, row))
                if row['workaround']:
                    print('      workaround: ' + row['workaround'].replace('\n', ' ')[:200])
            print('\nNothing was logged. If one is the same underlying problem, upvote it:\n'
                  '  friction bump <id> [--note "..."]\n'
                  'If yours is genuinely different, re-run the same command with --new.')
            return 3
    item_id = fr.create(args.title, args.detail or '', args.workaround or '')
    print(f'Created friction item #{item_id}.')
    return _occur(fr, args, item_id)


def cmd_bump(fr, args):
    return _occur(fr, args, args.id)


def cmd_search(fr, args):
    rows = fr.search(' '.join(args.text), limit=args.limit)
    if not rows:
        print('No matching friction items.')
        return 0
    for row in rows:
        print(item_line(fr, row))
        if row['workaround']:
            print('    workaround: ' + row['workaround'].replace('\n', ' ')[:200])
    return 0


def cmd_show(fr, args):
    item = fr.item(args.id)
    st = fr.stats(item['id'])
    print(f"#{item['id']} [{item['status']}] {item['title']}")
    if item['detail']:
        print(f"  detail:     {item['detail']}")
    if item['aliases']:
        print('  also known: ' + item['aliases'].replace('\n', ' | '))
    print(f"  workaround: {item['workaround'] or '-'}")
    if item['fix_note']:
        print(f"  fix note:   {item['fix_note']}")
    print(f"  hits: {st['n']} in {st['sessions']} sessions, wasted {fmt_duration(st['wasted'])}"
          f" (avg {fmt_duration(st['avg_wasted'])}), {st['estimated']} estimated,"
          f" regressions {item['regressions']}")
    print(f"  first seen {fmt_ts(item['created'])}, last hit "
          f"{fmt_ts(st['last_ts']) if st['last_ts'] else '-'}")
    for occ in fr.occurrences(item['id'], args.limit):
        flag = {'estimate': ' ~EST', 'none': ' untimed'}.get(occ['timing'], '')
        print(f"  - {fmt_ts(occ['ts'])} {occ['agent'] or '?'}/{occ['project'] or '?'}"
              f" {fmt_duration(occ['wasted_s'])}{flag}"
              f"{' REGRESSION' if occ['regression'] else ''}"
              f"{' [backfill]' if occ['source'] == 'backfill' else ''}"
              f"{' — ' + occ['note'] if occ['note'] else ''}")
        if args.events:
            for ev in fr.evidence(occ):
                print('      ' + event_line(ev).replace('\n', '\n      '))
    return 0


def cmd_list(fr, args):
    cond = "status != 'merged'" if args.all else "status = 'open'"
    rows = fr.db.execute(f'SELECT * FROM items WHERE {cond} ORDER BY updated DESC LIMIT ?',
                         (args.limit,)).fetchall()
    for row in rows:
        print(item_line(fr, row))
    if not rows:
        print('No friction items.')
    return 0


def cmd_workaround(fr, args):
    item_id = fr.set_workaround(args.id, args.text, append=args.append)
    print(f'Workaround saved on #{item_id}.')
    return 0


def cmd_resolve(fr, args):
    item_id = fr.set_status(args.id, 'resolved', args.fix or '')
    print(f'#{item_id} resolved. It reopens as a regression if anyone hits it again.')
    return 0


def cmd_wontfix(fr, args):
    print(f"#{fr.set_status(args.id, 'wontfix', args.reason or '')} marked wontfix.")
    return 0


def cmd_reopen(fr, args):
    print(f"#{fr.set_status(args.id, 'open')} reopened.")
    return 0


def cmd_merge(fr, args):
    dst = fr.merge(args.src, args.dst)
    print(f'Merged #{args.src} into #{dst}.')
    return 0


def cmd_timeline(fr, args):
    session = args.session or fr.guess_session()
    if not session:
        print('No session detected. Pass --session <id>.')
        return 1
    rows = fr.timeline(session, args.limit)
    if not rows:
        print(f'No recorded events for session {session} (hooks not installed for this agent?).')
        return 0
    print(f'session {session}: id, start, state, duration, call')
    for ev in rows:
        print(event_line(ev))
    return 0


def cmd_hot(fr, args):
    rows = fr.hot(days=args.days, limit=args.limit)
    if not rows:
        print('No open friction items hit recently.')
        return 0
    for row in rows:
        print(f"#{row['id']} {row['title']} — {row['n']} hits, {fmt_duration(row['wasted'])} wasted")
        if row['workaround']:
            print('    workaround: ' + row['workaround'].replace('\n', ' ')[:240])
    return 0


def cmd_report(fr, args):
    rep = fr.report(days=args.days, project=args.project, agent=args.agent)
    if args.json:
        print(json.dumps(rep, indent=2))
        return 0
    tot = rep['total']
    print(f"Friction, last {rep['days']} days: {tot['occurrences']} occurrences of {tot['items']} items,"
          f" {fmt_duration(tot['wasted_s'])} wasted")
    if tot['estimated'] or tot['untimed']:
        print(f"  ~ {tot['estimated']} occurrences ({fmt_duration(tot['wasted_estimated_s'])}) use the"
              f" agent's ESTIMATE instead of measured time; {tot['untimed']} have no timing at all")
    if not rep['items']:
        return 0
    scale = ' ░▒▓█'
    peak = max((d['n'] for it in rep['items'] for d in it['days'].values()), default=1)
    print(f"\n  {'id':>4} {'hits':>4} {'wasted':>8} {'avg':>7}  {'heat (' + rep['day_list'][0][5:] + ' → ' + rep['day_list'][-1][5:] + ')':<{max(len(rep['day_list']), 22)}}  item")
    for it in rep['items'][:args.limit]:
        heat = ''.join(
            scale[min(4, 1 + int(3 * it['days'][d]['n'] / peak))] if d in it['days'] else '·'
            for d in rep['day_list'])
        flags = ''
        if it['estimated']:
            flags += f" ~{it['estimated']}est"
        if it['regressed']:
            flags += ' REGRESSED'
        if it['status'] != 'open':
            flags += f" [{it['status']}]"
        print(f"  {it['id']:>4} {it['n']:>4} {fmt_duration(it['wasted']):>8}"
              f" {fmt_duration(it['avg_wasted']):>7}  {heat:<{max(len(rep['day_list']), 22)}}  {it['title'][:70]}{flags}")
    for label, key in (('by project', 'projects'), ('by agent', 'agents')):
        parts = [f"{r['name'] or '?'} {r['n']}× {fmt_duration(r['wasted'])}" for r in rep[key][:6]]
        print(f"\n  {label}: " + ', '.join(parts))
    return 0


def cmd_prune(fr, args):
    print(f'Pruned {fr.prune_events(args.days)} events older than {args.days} days.')
    return 0


def cmd_backfill(fr, args):
    import backfill
    return backfill.run(fr, args)


def cmd_web(fr, args):
    import web
    print(f'friction web view: http://{args.host}:{args.port}  (ctrl-c to stop)')
    try:
        web.serve(args.host, args.port)
    except KeyboardInterrupt:
        pass
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog='friction', description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('log', help='record a friction item (searches for an existing one first)')
    p.add_argument('title', help='short name of the underlying problem, not of this instance')
    p.add_argument('--detail', help='what goes wrong and when')
    p.add_argument('--workaround', help='what got you past it, if anything')
    p.add_argument('--new', action='store_true', help='create even though similar items exist')
    _occur_args(p)
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser('bump', help='upvote an existing item: you hit it too')
    p.add_argument('id', type=int)
    _occur_args(p)
    p.set_defaults(fn=cmd_bump)

    p = sub.add_parser('search', help='find existing items')
    p.add_argument('text', nargs='+')
    p.add_argument('--limit', type=int, default=8)
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser('show', help='one item with its occurrences')
    p.add_argument('id', type=int)
    p.add_argument('--limit', type=int, default=20)
    p.add_argument('--events', action='store_true', help='include recorded tool events')
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser('list', help='open items, most recently touched first')
    p.add_argument('--all', action='store_true', help='include resolved and wontfix')
    p.add_argument('--limit', type=int, default=50)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser('workaround', help='record how to get past an item')
    p.add_argument('id', type=int)
    p.add_argument('text')
    p.add_argument('--append', action='store_true')
    p.set_defaults(fn=cmd_workaround)

    p = sub.add_parser('resolve', help='the underlying cause is fixed')
    p.add_argument('id', type=int)
    p.add_argument('--fix', help='what fixed it')
    p.set_defaults(fn=cmd_resolve)

    p = sub.add_parser('wontfix', help='accepted, stop showing it as hot')
    p.add_argument('id', type=int)
    p.add_argument('--reason')
    p.set_defaults(fn=cmd_wontfix)

    p = sub.add_parser('reopen')
    p.add_argument('id', type=int)
    p.set_defaults(fn=cmd_reopen)

    p = sub.add_parser('merge', help='fold duplicate SRC into DST')
    p.add_argument('src', type=int)
    p.add_argument('dst', type=int)
    p.set_defaults(fn=cmd_merge)

    p = sub.add_parser('timeline', help="this session's recent tool calls with event ids")
    p.add_argument('--session')
    p.add_argument('--limit', type=int, default=30)
    p.set_defaults(fn=cmd_timeline)

    p = sub.add_parser('hot', help='open items hit recently, with workarounds')
    p.add_argument('--days', type=int, default=14)
    p.add_argument('--limit', type=int, default=5)
    p.set_defaults(fn=cmd_hot)

    p = sub.add_parser('report', help='heatmap of friction over time')
    p.add_argument('--days', type=int, default=7)
    p.add_argument('--week', dest='days', action='store_const', const=7)
    p.add_argument('--month', dest='days', action='store_const', const=30)
    p.add_argument('--project')
    p.add_argument('--agent')
    p.add_argument('--limit', type=int, default=25)
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser('prune', help='delete raw tool events past retention (items are kept)')
    p.add_argument('--days', type=int, default=EVENT_RETENTION_DAYS)
    p.set_defaults(fn=cmd_prune)

    p = sub.add_parser('backfill', help='mine past transcripts for friction nobody logged')
    p.add_argument('--since', default='1d', help='how far back to look (2d, 30d, 12h); default 1d')
    p.add_argument('--dry-run', action='store_true', help='pre-pass only: list suspicious sessions')
    p.add_argument('--limit', type=int, default=40, help='most sessions to send to the analyser')
    p.add_argument('--min-failures', type=int, default=2)
    p.add_argument('--model', default='haiku')
    p.add_argument('--redo', action='store_true', help='re-analyse sessions already processed')
    p.set_defaults(fn=cmd_backfill)

    p = sub.add_parser('web', help='serve the read-only web heatmap')
    p.add_argument('--port', type=int, default=8798)
    p.add_argument('--host', default='127.0.0.1')
    p.set_defaults(fn=cmd_web)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        fr = Friction()
        return args.fn(fr, args)
    except (KeyError, ValueError) as exc:
        print(f'friction: {exc.args[0] if exc.args else exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
