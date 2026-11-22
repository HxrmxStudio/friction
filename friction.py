#!/usr/bin/env python3
"""friction: a small shared log of what slows agents down.

Agents record friction items (a bad command, a missing credential, an auth
timeout), upvote ones another agent already found, and attach workarounds.
Hooks (friction_hook.py) record every tool call with real timestamps, so the
time an occurrence wasted is measured rather than guessed where possible.

Storage is one SQLite file (WAL), stdlib only. See README.md.
"""
import argparse
import difflib
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path

DEFAULT_DIR = '~/.local/share/friction'
SUMMARY_MAX = 500
EVENT_RETENTION_DAYS = 30
STREAK_GAP_S = 600          # failures further apart than this are separate streaks
IDLE_GAP_S = 300            # a longer pause between calls is idle time, not wasted time
SELF_LOOKUP_S = 120         # how far back to look for our own in-flight Bash call
CLUSTER_BREAK_OK = 3        # more clean calls than this between two bad calls split them into clusters
RETRY_WINDOW_S = 120        # a near-identical re-run within this long counts as a retry
RETRY_LOOKAHEAD = 3         # how many later calls are checked for a retry of a call
RETRY_SIMILARITY = 0.8
# Tools whose whole run is the agent waiting on the user, not working.
USER_INPUT_TOOLS = frozenset({'AskUserQuestion', 'ExitPlanMode', 'request_user_input'})

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
  kind TEXT NOT NULL,            -- tool | wait (pending user input: tool = prompt | permission | <tool>)
  tool TEXT,
  tool_use_id TEXT,
  ok INTEGER,                    -- NULL while in flight, 1 success, 0 failure
  duration_ms INTEGER,
  summary TEXT,
  error TEXT,                    -- failure text; for soft calls (ok=1 with an outcome) an output snippet
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

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

# Columns added after the first release, for syncing items to an external tracker (tracker.py).
MIGRATIONS = [
    'ALTER TABLE items ADD COLUMN remote_key TEXT',          # e.g. the Jira issue key
    'ALTER TABLE items ADD COLUMN remote_url TEXT',
    'ALTER TABLE items ADD COLUMN dirty INTEGER NOT NULL DEFAULT 0',   # DIRTY_* bits not yet pushed
    'ALTER TABLE occurrences ADD COLUMN uid TEXT',           # identity shared across machines
    'ALTER TABLE occurrences ADD COLUMN pushed INTEGER NOT NULL DEFAULT 0',
    'CREATE INDEX IF NOT EXISTS occ_uid ON occurrences(uid)',
    'CREATE INDEX IF NOT EXISTS items_remote ON items(remote_key)',
    # NULL = the call did what was expected; else why not (empty, not_found, auth, timeout, denied, retried)
    'ALTER TABLE events ADD COLUMN outcome TEXT',
    # 0 = generated by the backfill analyser: its workaround is not shown to agents until verified
    'ALTER TABLE items ADD COLUMN verified INTEGER NOT NULL DEFAULT 1',
    # Items the analyser created before `verified` existed: only backfill hits, no live agent
    # has ever hit (and so seen) them.
    "UPDATE items SET verified=0 WHERE workaround != '' AND EXISTS (SELECT 1 FROM occurrences o"
    " WHERE o.item_id = items.id) AND NOT EXISTS (SELECT 1 FROM occurrences o"
    " WHERE o.item_id = items.id AND o.source = 'live')",
    # 1 = the window end was given (--to, or a backfill finding's to_event), so `retime` keeps it
    # instead of re-deriving the resolution call.
    'ALTER TABLE occurrences ADD COLUMN to_explicit INTEGER NOT NULL DEFAULT 0',
    "UPDATE occurrences SET to_explicit=1 WHERE source='backfill' AND to_event IS NOT NULL",
    # Workarounds written before verification existed (by agents, the analyser, or teammates via
    # the tracker) are all untrusted: hide every one from SessionStart until someone reviews it.
    "UPDATE items SET verified=0 WHERE workaround != ''",
    # Computed from the raw command at PreToolUse, because the stored summary is scrubbed later
    # (and NULL until then): 2 = `friction log|bump|timeline`, 1 = any other friction subcommand.
    'ALTER TABLE events ADD COLUMN self_call INTEGER NOT NULL DEFAULT 0',
    # One row per cluster the Jev classifier judged, kept so min_confidence can be tuned
    # (`friction backfill --jev-stats`). outcome: uncertain | skipped | matched | new | unfiled.
    # summary is the first bad call's stored (already scrubbed) summary and error, since events
    # get pruned.
    'CREATE TABLE IF NOT EXISTS jev_decisions (id INTEGER PRIMARY KEY, ts REAL NOT NULL,'
    ' session_id TEXT, from_event INTEGER, to_event INTEGER, summary TEXT,'
    ' kind TEXT, kind_conf REAL, kind_probs TEXT, match TEXT, match_conf REAL,'
    ' min_confidence REAL, model TEXT, outcome TEXT NOT NULL, item_id INTEGER)',
]
DIRTY_STATS = 1      # hit count or time wasted changed
DIRTY_CONTENT = 2    # title, detail, workaround or fix note changed
DIRTY_STATUS = 4     # opened, resolved, wontfix or merged

STOPWORDS = set('a an and are as at be but by for from has have i in is it its not of on or so that the '
                'this to was when with while after before then than into no'.split())


# ---- secret scrubbing: gitleaks does it, friction has no scrubber of its own ----------------
#
# Text only reaches the database through redact()/redact_many(), which pipe it through
# `gitleaks stdin`. If gitleaks is missing, slow or broken, they return None: no text is
# stored rather than unscanned text.

GITLEAKS_BIN = Path('~/.local/share/friction/bin/gitleaks')
GITLEAKS_ARGS = ('stdin', '--no-banner', '--no-color', '--log-level', 'error', '--exit-code', '0',
                 '--ignore-gitleaks-allow', '--report-format', 'json', '--report-path', '-')
SCAN_TIMEOUT_S = 3
SCAN_CHUNK_CHARS = 200_000     # one gitleaks run per this much text (backfill batches big lists)
MIN_SECRET_LEN = 3             # shorter "secrets" are rule noise: replacing them would mangle text
_SCAN_CACHE = {}               # (scanner, text) -> redacted text or None; this process only


def gitleaks_path():
    """FRICTION_GITLEAKS, then the installer's copy, then PATH. None if there is no usable binary."""
    import shutil
    env = os.environ.get('FRICTION_GITLEAKS')
    for cand in ([env] if env else [GITLEAKS_BIN.expanduser(), shutil.which('gitleaks')]):
        if cand and os.access(cand, os.X_OK) and os.path.isfile(cand):
            return str(cand)
    return None


def scanner_status():
    """(ok, message). Cheap: does not run the scanner."""
    path = gitleaks_path()
    if path:
        return True, f'gitleaks at {path}'
    return False, ('gitleaks is not installed, so friction stores no tool-call text (summaries, errors)'
                   ' or notes. Run friction\'s installer (python3 installer.py) to install it.')


def _run_gitleaks(path, text, timeout):
    """Findings [(secret, rule)] for text, or None if the scan did not complete."""
    import subprocess
    env = {k: v for k, v in os.environ.items() if not k.startswith('GITLEAKS_')}
    try:
        proc = subprocess.run((path,) + GITLEAKS_ARGS, input=text.encode('utf-8', 'replace'),
                              capture_output=True, timeout=timeout, env=env,
                              cwd=os.path.dirname(os.path.realpath(path)) or '/')
        if proc.returncode != 0:
            return None
        report = json.loads(proc.stdout.decode('utf-8', 'replace') or 'null') or []
        found = []
        for f in report:
            secret = f.get('Secret') or f.get('Match') or ''
            if len(secret) >= MIN_SECRET_LEN:
                found.append((secret, f.get('RuleID') or 'secret'))
        return found
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        return None


def _scan_chunk(path, texts):
    """Scrub a list of non-empty strings with one gitleaks run. Returns a list, or None on failure."""
    sep = f'<<friction-sep-{uuid.uuid4().hex}>>'
    joined = f'\n{sep}\n'.join(texts)
    found = _run_gitleaks(path, joined, SCAN_TIMEOUT_S + len(joined) / 100_000)
    if found is None:
        return None
    for secret, rule in sorted(set(found), key=lambda f: -len(f[0])):
        joined = joined.replace(secret, f'[REDACTED:{rule}]')
    parts = joined.split(f'\n{sep}\n')
    return parts if len(parts) == len(texts) else None


def redact_many(texts, limit=SUMMARY_MAX):
    """redact() for several texts with as few gitleaks runs as possible (`limit` may be a list)."""
    limits = list(limit) if isinstance(limit, (list, tuple)) else [limit] * len(texts)
    out, todo = [None] * len(texts), []
    for i, text in enumerate(texts):
        if text is None:
            continue
        if not isinstance(text, str):
            try:
                text = json.dumps(text, default=str)
            except Exception:
                text = str(text)
        text = text[:limits[i] * 8]
        if text.strip():
            todo.append((i, text))
        else:
            out[i] = ''
    path = gitleaks_path() if todo else None
    pending = []
    for i, text in todo:
        key = (path, limits[i], text)
        if path and key in _SCAN_CACHE:
            out[i] = _SCAN_CACHE[key]
        elif path:
            pending.append((i, text, key))
    while pending:
        chunk, size = [], 0
        while pending and (not chunk or size + len(pending[0][1]) <= SCAN_CHUNK_CHARS):
            chunk.append(pending.pop(0))
            size += len(chunk[-1][1])
        scrubbed = _scan_chunk(path, [t for _, t, _ in chunk])
        for n, (i, _, key) in enumerate(chunk):
            if scrubbed is not None:
                s = re.sub(r'[ \t]+', ' ', scrubbed[n]).strip()
                out[i] = s if len(s) <= limits[i] else s[:limits[i] - 1] + '…'
            if len(_SCAN_CACHE) > 50_000:
                _SCAN_CACHE.clear()
            _SCAN_CACHE[key] = out[i]
    return out


def redact(text, limit=SUMMARY_MAX):
    """Scrub secrets with gitleaks, collapse whitespace runs, truncate.

    None in, None out. None also when the scan could not run: nothing unscanned is returned."""
    return redact_many([text], [limit])[0]


def redact_required(text, limit=SUMMARY_MAX):
    """redact() for text someone chose to write: refuse, with a reason, if it cannot be scanned."""
    out = redact(text, limit)
    if out is None and text is not None:
        raise ValueError(scanner_status()[1] if not scanner_status()[0]
                         else 'gitleaks failed or timed out, so the text was not stored')
    return out


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


# ---- calls that succeeded but did not do what the agent wanted -------------

SHELL_TOOLS = frozenset({'bash', 'shell', 'exec', 'exec_command', 'local_shell'})
SEARCH_TOOLS = frozenset({'grep', 'glob'})
EDIT_TOOLS = frozenset({'edit', 'write', 'multiedit', 'apply_patch', 'notebookedit'})
# Re-reading a file or asking the user again is not a retry of something that failed.
NO_RETRY_TOOLS = frozenset({'read', 'todowrite', 'agent', 'task', 'askuserquestion', 'exitplanmode',
                            'request_user_input'}) | EDIT_TOOLS
# Outcomes too weak to be friction alone: they only count once a retry follows (-> 'retried').
WEAK_OUTCOMES = frozenset({'empty'})

# Commands that print file or search content: words like "unauthorized" in it mean nothing.
CONTENT_CMDS = frozenset({'cat', 'grep', 'egrep', 'fgrep', 'rg', 'ag', 'head', 'tail', 'sed', 'awk', 'less',
                          'more', 'echo', 'printf', 'jq', 'wc', 'diff', 'sort', 'uniq', 'cut', 'tr', 'bat',
                          'nl', 'tac', 'xxd', 'od', 'strings', 'column', 'yq'})
# Commands whose output is a network or service answer: auth and timeout messages are real there.
NET_CMDS = frozenset({'curl', 'wget', 'http', 'https', 'xh', 'gh', 'glab', 'ssh', 'scp', 'sftp', 'rsync',
                      'aws', 'gcloud', 'az', 'kubectl', 'docker', 'podman', 'terraform', 'ping', 'nc',
                      'ncat', 'psql', 'mysql', 'redis-cli', 'jira', 'helm', 'flyctl', 'vercel', 'heroku',
                      'claude', 'codex', 'op', 'bw'})
EMPTY_CMDS = frozenset({'ls', 'find', 'fd', 'fdfind', 'grep', 'egrep', 'fgrep', 'rg', 'ag', 'which',
                        'locate', 'cat'})

_NO_RESULTS = re.compile(r'(?i)^\(?(no (matches|files|results|such)\b[^\n]{0,40}|found 0 \w+)\)?\.?$')
_TOOL_ERR = re.compile(r'(?im)^(?:[\w.+/-]+: (?:\d+: )?(?:line \d+: )?(?:.+: )?'
                       r'(?:command not found|no such file or directory|not found)'
                       r'|[\w.+/-]+: command not found: \S+|fish: unknown command.*)\s*$')
_DENIED = re.compile(r'(?im)^(?:[\w.+/-]+: (?:.+: )?(?:permission denied|operation not permitted)'
                     r'|permission denied.*|.*permission denied \((?:publickey|password)[^)]*\).*)\s*$')
_AUTH = re.compile(r'(?i)\bunauthori[sz]ed\b|\bforbidden\b|bad credentials|authentication (?:failed|required)'
                   r'|not logged in|requires authentication|login required|invalid (?:api[ _-]?key|token|'
                   r'credentials)|token (?:has )?(?:expired|is invalid)|\bHTTP/[\d.]+\s+40[13]\b'
                   r'|["\']?(?:status|code|http_code)["\']?\s*[:=]\s*40[13]\b|^\s*40[13]\s*$', re.M)
_TIMEOUT = re.compile(r'(?i)\btimed out\b|\bETIMEDOUT\b|\bESOCKETTIMEDOUT\b|deadline exceeded'
                      r'|timeout (?:was )?(?:reached|exceeded|expired)|gateway time-?out|\bHTTP/[\d.]+\s+504\b')
_HTTP_404 = re.compile(r'(?i)\bHTTP/[\d.]+\s+404\b|["\']?(?:status|code|http_code)["\']?\s*[:=]\s*404\b'
                       r'|\b404 not found\b|^\s*404\s*$', re.M)


# Codex wraps shell/exec output in a header: `Chunk ID: x\nWall time: 0.1 seconds\nProcess exited
# with code 1\nOriginal token count: 9\nOutput:\n<body>` (exec_command, write_stdin), the older
# `Exit code: 1\nWall time: 0.1 seconds\nOutput:\n<body>`, and `Script completed|failed\nWall time
# 0.1 seconds\nOutput:\n<body>` for its exec/wait tools (a list of input_text items, joined).
CODEX_HEADER = re.compile(
    r'\A\s*(?:Chunk ID:[^\n]*\n)?'
    r'(?:(?P<script>Script (?:completed|failed|running)[^\n]*)\n|(?:Exit code:? *(?P<code0>-?\d+)\n))?'
    r'Wall time:?[^\n]*\n'
    r'(?:Process (?:exited with code (?P<code>-?\d+)|running[^\n]*)\n)?'
    r'(?:Original token count:[^\n]*\n)?Output:[ \t]*(?:\n|\Z)')


def split_codex_output(text):
    """(failed?, body) for Codex-wrapped shell output. failed is None when `text` has no such
    header (then body is `text` itself): True for a non-zero exit code or `Script failed`."""
    m = CODEX_HEADER.match(text or '')
    if not m:
        return None, text
    code = m.group('code') if m.group('code') is not None else m.group('code0')
    bad = (code is not None and int(code) != 0) or bool(m.group('script') and
                                                       m.group('script').startswith('Script failed'))
    return bad, text[m.end():]


def _command_names(cmd):
    """First word of each shell segment of `cmd`, minus cd/env/sudo prefixes."""
    names = []
    for seg in re.split(r'&&|\|\||[;|\n]', cmd or ''):
        words = seg.split()
        while words and (re.match(r'^[A-Za-z_]\w*=', words[0]) or words[0] in ('sudo', 'time', 'env', 'nice')):
            words.pop(0)
        if words and words[0] != 'cd':
            names.append(words[0].rsplit('/', 1)[-1])
    return names


def _shell_outcome(cmd, text):
    names = _command_names(cmd)
    first = names[0] if names else ''
    lines = text.splitlines()
    head = '\n'.join(lines[:5])[:800]
    if not text:
        simple = not re.search(r'&&|\|\||[;|]|<<| -q\b|--quiet|-delete|-exec|(?<![0-9&])>', re.sub(
            r'\d?>\s*/dev/null|\d>&\d', '', re.sub(r'^\s*cd\s+\S+\s*&&\s*', '', cmd or '')))
        return 'empty' if first in EMPTY_CMDS and simple else None
    if first in CONTENT_CMDS:
        # Content printers: only the tool's own error line (`cat: x: No such file or directory`).
        for m in re.finditer(r'(?im)^' + re.escape(first) + r': .*$', head):
            if re.search(r'(?i)no such file or directory\s*$', m.group(0)):
                return 'not_found'
            if re.search(r'(?i)(permission denied|operation not permitted)\s*$', m.group(0)):
                return 'denied'
        return None
    if _DENIED.search(head):
        return 'denied'
    if _TOOL_ERR.search(head):
        return 'not_found'
    if any(n in NET_CMDS for n in names) or (first == 'git' and re.search(
            r'\bgit\s+(?:-\S+\s+)*(push|pull|fetch|clone|ls-remote)\b', cmd)):
        scope = text if len(text) <= 800 else head
        if _AUTH.search(scope):
            return 'auth'
        if _TIMEOUT.search(scope):
            return 'timeout'
        if _HTTP_404.search(scope):
            return 'not_found'
    return None


def unexpected_outcome(tool, summary, output_text):
    """Why a call that *succeeded* (no error, exit 0) still did not do what the agent wanted.

    Returns None (expected), or a short reason: 'not_found', 'denied', 'auth',
    'timeout' or the weak 'empty'. Pure and conservative; the rules:

    * Read/Edit/Write/WebFetch/Agent and every other tool: None. Their output is
      arbitrary content ("not found" inside a file is fine). Exception: MCP tools
      (`mcp__*`) whose whole output is 300 characters or less are checked for
      auth/timeout/denied wording.
    * Grep/Glob tools: no output, or "No matches/files found" -> 'empty'.
    * Shell tools (Bash, shell, exec_command...):
      - empty output from a single plain ls/find/fd/grep/rg/which/cat command (no
        pipe, `&&`, `;`, redirect, -q, -exec, -delete) -> 'empty'.
      - content printers (cat, grep, rg, head, tail, sed, echo, jq...): only that
        tool's own error line (`cat: x: No such file or directory`) counts
        ('not_found' / 'denied'); their content is never inspected otherwise.
      - any other command, first 5 lines: a line shaped `prog: ...: command not
        found | No such file or directory | not found` (bash/zsh/sh/fish) ->
        'not_found'; `prog: ...: Permission denied | Operation not permitted`,
        `Permission denied (publickey...)` -> 'denied'.
      - network/service commands (curl, wget, gh, ssh, aws, kubectl, docker, git
        push/pull/fetch/clone ...), output <= 800 chars (else its first 5 lines):
        401/403/Unauthorized/Forbidden/Bad credentials/not logged in -> 'auth';
        "timed out"/ETIMEDOUT/deadline exceeded/504 -> 'timeout'; HTTP 404 ->
        'not_found'.
    * 'empty' is weak: the hook does not store it as an outcome. It only turns
      into friction when the agent then re-runs a near-identical call
      (`retry_flags` -> 'retried').
    """
    tool_l = (tool or '').lower()
    text = (output_text or '').strip()
    if tool_l in SEARCH_TOOLS:
        return 'empty' if not text or _NO_RESULTS.match(text) else None
    if tool_l in SHELL_TOOLS:
        text = split_codex_output(text)[1].strip()
        if text in ('(no output)', '(Bash completed with no output)'):
            text = ''
        return _shell_outcome(summary or '', text)
    if tool_l.startswith('mcp__') and text and len(text) <= 300:
        if _DENIED.search(text):
            return 'denied'
        if _AUTH.search(text):
            return 'auth'
        if _TIMEOUT.search(text):
            return 'timeout'
    return None


def _norm_call(summary):
    return re.sub(r'\d+', '0', re.sub(r'\s+', ' ', (summary or '').lower())).strip()[:500]


def _similar(a, b):
    a, b = _norm_call(a), _norm_call(b)
    if not a or not b:
        return False
    if a == b:
        return True
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sm.real_quick_ratio() >= RETRY_SIMILARITY and sm.quick_ratio() >= RETRY_SIMILARITY \
        and sm.ratio() >= RETRY_SIMILARITY


def retry_flags(events):
    """Indexes of calls followed by a near-identical retry. `events`: time-ordered dicts with
    tool, summary, ok, outcome (and optionally ts, epoch seconds).

    Call i is flagged when one of the next RETRY_LOOKAHEAD calls has the same tool, a
    summary at least RETRY_SIMILARITY similar after normalising (case, whitespace, digits;
    difflib ratio), and starts within RETRY_WINDOW_S of call i, with no
    Edit/Write/MultiEdit/apply_patch/NotebookEdit call in between (so re-running tests after
    an edit is not a retry). Read, TodoWrite and agent/question tools never count, and calls
    still in flight (ok is None) are ignored.
    """
    flagged = set()
    for i, a in enumerate(events):
        tool = (a['tool'] or '').lower()
        if a['ok'] is None or tool in NO_RETRY_TOOLS or a.get('self_call') or is_self_call(a['summary']):
            continue
        for j in range(i + 1, min(len(events), i + 1 + RETRY_LOOKAHEAD)):
            b = events[j]
            btool = (b['tool'] or '').lower()
            if btool in EDIT_TOOLS:
                break
            try:
                late = b['ts'] - a['ts'] > RETRY_WINDOW_S
            except (KeyError, TypeError, IndexError):
                late = False
            if late:
                break
            if btool == tool and _similar(a['summary'], b['summary']):
                flagged.add(i)
                break
    return flagged


def _bad(ev):
    """A bad call: it failed, or succeeded without the expected outcome."""
    return ev['ok'] == 0 or (ev['ok'] is not None and ev['outcome'] is not None)


# A shell call that *invokes* friction (not one that merely mentions it, e.g. `cd ~/code/friction`).
SELF_CALL = re.compile(r'(?:^|[;&|]\s*)(?:python3?\s+)?(?:\S*/)?friction(?:\.py)?\s+(?:log|bump|timeline)\b',
                       re.M)
BAD_SQL ="kind='tool' AND (ok=0 OR outcome IS NOT NULL)"
# An event inside the window of an occurrence already logged for its session is logged too,
# whatever the session's mark says (older events imported after hooks were installed).
NOT_IN_WINDOW_SQL = ('NOT EXISTS (SELECT 1 FROM occurrences o WHERE o.session_id = events.session_id'
                     ' AND o.from_event IS NOT NULL AND o.to_event IS NOT NULL'
                     ' AND events.id BETWEEN o.from_event AND o.to_event)')
# Any friction subcommand. Its exit status is a message to the agent (`log` exits 3 when a
# similar item exists), not friction, so such calls are never bad calls or retries.
ANY_SELF_CALL = re.compile(r'(?:^|[;&|]\s*)(?:python3?\s+)?(?:\S*/)?friction(?:\.py)?\s+[a-z]', re.M)


def is_self_call(summary):
    """True for a shell call whose command runs the friction CLI itself."""
    return bool(summary) and bool(ANY_SELF_CALL.search(summary))


def self_call_kind(summary):
    """The events.self_call value for a raw command: 2 log/bump/timeline, 1 other subcommand, 0."""
    if not summary:
        return 0
    if SELF_CALL.search(summary):
        return 2
    return 1 if ANY_SELF_CALL.search(summary) else 0


class Friction:
    def __init__(self, root=None):
        root = root or os.environ.get('FRICTION_DIR') or DEFAULT_DIR
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.last_guess_ambiguous = False
        self.db = sqlite3.connect(self.root / 'friction.db', timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.executescript(SCHEMA)
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version < len(MIGRATIONS):
            for sql in MIGRATIONS[version:]:
                try:
                    self.db.execute(sql)
                except sqlite3.OperationalError:      # another process got there first
                    pass
            self.db.execute(f'PRAGMA user_version={len(MIGRATIONS)}')
            self.db.commit()

    def meta(self, key, default=None):
        row = self.db.execute('SELECT v FROM meta WHERE k=?', (key,)).fetchone()
        return row['v'] if row else default

    def set_meta(self, key, value):
        self.db.execute('INSERT INTO meta(k, v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',
                        (key, None if value is None else str(value)))
        self.db.commit()

    def mark_dirty(self, item_id, bits):
        """Flag an item as changed since it was last pushed to the tracker."""
        self.db.execute('UPDATE items SET dirty = dirty | ? WHERE id=?', (bits, item_id))

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
                  summary=None, error=None, ts=None, agent=None, project=None, source='hook',
                  outcome=None, defer_scan=False):
        """Insert an event. defer_scan=True (the hook): store NO text now (error becomes the bare
        outcome, if any) and let the caller hand the raw text to a scrubber, so no unscanned text
        is ever written. self_call is always computed from the raw summary."""
        self_call = self_call_kind(summary)
        if defer_scan:
            summary, error = None, outcome
        else:
            summary, error = redact_many([summary, error])
        cur = self.db.execute(
            'INSERT INTO events(ts, session_id, agent, project, kind, tool, tool_use_id, ok,'
            ' duration_ms, summary, error, source, outcome, self_call) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (ts or time.time(), session_id, agent, project, kind, tool, tool_use_id, ok,
             duration_ms, summary, error, source, outcome, self_call))
        return cur.lastrowid

    def flag_retry(self, session_id, event_id):
        """Mark a finished call that did not already count as bad as 'retried': the agent re-ran it."""
        cur = self.db.execute(
            "UPDATE events SET outcome='retried', error=COALESCE(error, 're-run with a near-identical call')"
            ' WHERE id=? AND session_id=? AND ok=1 AND outcome IS NULL', (event_id, session_id))
        return cur.rowcount > 0

    def finish_event(self, session_id, tool, tool_use_id, ok, error=None, duration_ms=None, now=None,
                     outcome=None, defer_scan=False):
        """Close the in-flight event for a tool call. Returns its id, or None if there is none.

        Only ok, duration, outcome and error change. defer_scan=True stores the bare outcome (or
        NULL) as the error; the caller hands the raw text to the scrubber (see scrub_events)."""
        now = now or time.time()
        row = None
        if tool_use_id:
            row = self.db.execute(
                "SELECT id, ts FROM events WHERE session_id=? AND kind='tool' AND tool_use_id=?"
                ' AND ok IS NULL ORDER BY id DESC LIMIT 1', (session_id, tool_use_id)).fetchone()
        if row is None:
            row = self.db.execute(
                "SELECT id, ts FROM events WHERE session_id=? AND kind='tool' AND tool=? AND ok IS NULL"
                ' ORDER BY id DESC LIMIT 1', (session_id, tool)).fetchone()
        if row is None:
            return None
        if duration_ms is None:
            duration_ms = max(0, int((now - row['ts']) * 1000))
        self.db.execute('UPDATE events SET ok=?, error=?, duration_ms=?, outcome=? WHERE id=?',
                        (1 if ok else 0, outcome if defer_scan else redact(error), duration_ms,
                         outcome, row['id']))
        return row['id']

    def scrub_events(self, items):
        """Fill in the text of hook events once gitleaks has scanned it (one run for the batch).

        items: dicts with id and any of summary, error (raw text), error_limit, provisional (what
        the error column holds until then: the bare outcome or NULL). Only text columns are
        touched, and only while still unscrubbed, so a scrub that lands after PostToolUse (or a
        retry flag) never overwrites ok, outcome or duration. Returns how many columns were set."""
        texts, limits = [], []
        for it in items:
            texts += [it.get('summary'), it.get('error')]
            limits += [SUMMARY_MAX, it.get('error_limit', SUMMARY_MAX)]
        out = redact_many(texts, limits)
        done = 0
        for n, it in enumerate(items):
            summary, error = out[2 * n], out[2 * n + 1]
            if summary:
                done += self.db.execute('UPDATE events SET summary=? WHERE id=? AND summary IS NULL',
                                        (summary, it['id'])).rowcount
            if error:
                done += self.db.execute('UPDATE events SET error=? WHERE id=? AND error IS ?',
                                        (error, it['id'], it.get('provisional'))).rowcount
        self.db.commit()
        return done

    # ---- time spent waiting on the user ----------------------------------

    def open_wait(self, session_id, reason, ts=None):
        """Start a wait on the user (a turn ended, a permission prompt is up). Idempotent."""
        if self.db.execute("SELECT 1 FROM events WHERE session_id=? AND kind='wait' AND tool=?"
                           ' AND ok IS NULL LIMIT 1', (session_id, reason)).fetchone():
            return None
        return self.add_event(session_id, 'wait', tool=reason, ts=ts)

    def close_waits(self, session_id, reasons=None, now=None):
        """End open waits (all, or those for `reasons`) at `now`."""
        now = now or time.time()
        rows = self.db.execute("SELECT id, ts, tool FROM events WHERE session_id=? AND kind='wait'"
                               ' AND ok IS NULL', (session_id,)).fetchall()
        for row in rows:
            if reasons is None or row['tool'] in reasons:
                self.db.execute('UPDATE events SET ok=1, duration_ms=? WHERE id=?',
                                (max(0, int((now - row['ts']) * 1000)), row['id']))

    def add_wait(self, session_id, reason, start, end, source='hook'):
        """Record a finished wait, e.g. the run of an AskUserQuestion call."""
        if end > start:
            self.add_event(session_id, 'wait', tool=reason, ok=1, ts=start, source=source,
                           duration_ms=int((end - start) * 1000))

    def waits(self, session_id, lo, hi):
        """Merged (start, end) intervals the session spent waiting on the user within [lo, hi].

        A wait still open (its hook never closed it) runs until the next tool call."""
        rows = self.db.execute(
            "SELECT ts, duration_ms, ok FROM events WHERE session_id=? AND kind='wait' AND ts<?"
            ' ORDER BY ts', (session_id, hi)).fetchall()
        spans = []
        for w in rows:
            if w['ok'] is None:
                nxt = self.db.execute("SELECT MIN(ts) m FROM events WHERE session_id=? AND"
                                      " kind='tool' AND ts>?", (session_id, w['ts'])).fetchone()['m']
                end = nxt if nxt is not None else hi
            else:
                end = w['ts'] + (w['duration_ms'] or 0) / 1000
            start, end = max(w['ts'], lo), min(end, hi)
            if end <= start:
                continue
            if spans and start <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], end)
            else:
                spans.append([start, end])
        return spans

    def has_events(self, session_id):
        return self.db.execute('SELECT 1 FROM events WHERE session_id=? LIMIT 1',
                               (session_id,)).fetchone() is not None

    def timeline(self, session_id, limit=30):
        rows = self.db.execute(
            'SELECT * FROM events WHERE session_id=? ORDER BY id DESC LIMIT ?',
            (session_id, limit)).fetchall()
        return rows[::-1]

    def unlogged_failures(self, session_id):
        """Bad calls (failed, or succeeded without the expected outcome) not yet logged."""
        sess = self.session(session_id)
        mark = sess['mark_event'] if sess else 0
        return self.db.execute(
            f'SELECT * FROM events WHERE session_id=? AND id>? AND {BAD_SQL} AND {NOT_IN_WINDOW_SQL}'
            ' ORDER BY id',
            (session_id, mark)).fetchall()

    def clusters(self, session_id, events=None):
        """Unlogged bad calls grouped into clusters, oldest first.

        `events`: finished tool-call rows in id order (default: this session's since the log
        mark). A cluster is a run of bad calls less than STREAK_GAP_S apart with at most
        CLUSTER_BREAK_OK clean calls between neighbours. Each is
        {from_event, last_bad, to_event, bad_ids}; to_event is the resolution call (the
        first clean call after last_bad, before any later bad call) or last_bad if none yet.
        """
        if events is None:
            sess = self.session(session_id)
            events = self.db.execute(
                "SELECT * FROM events WHERE session_id=? AND id>? AND kind='tool' AND ok IS NOT NULL"
                f' AND {NOT_IN_WINDOW_SQL} ORDER BY id', (session_id, sess['mark_event'] if sess else 0)).fetchall()
        # Questions to the user are neither friction nor a resolution: the agent was just waiting.
        events = [ev for ev in events if ev['tool'] not in USER_INPUT_TOOLS]
        out, cur, clean = [], None, 0
        for idx, ev in enumerate(events):
            if _bad(ev):
                if cur is not None and (ev['ts'] - cur['ts'] > STREAK_GAP_S or clean > CLUSTER_BREAK_OK):
                    out.append(cur)
                    cur = None
                if cur is None:
                    cur = {'from_event': ev['id'], 'bad_ids': []}
                cur.update(last_bad=ev['id'], idx=idx, ts=ev['ts'])
                cur['bad_ids'].append(ev['id'])
                clean = 0
            else:
                clean += 1
        if cur is not None:
            out.append(cur)
        for c in out:
            c['to_event'] = c['last_bad']
            nxt = events[c.pop('idx') + 1:][:1]       # the call right after the last bad one
            if nxt and not _bad(nxt[0]):
                c['to_event'] = nxt[0]['id']
            c.pop('ts')
        return out

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
        self.last_guess_ambiguous = False
        rows = self.db.execute(
            "SELECT session_id FROM events WHERE kind='tool' AND ok IS NULL AND ts>?"
            ' AND self_call=2 ORDER BY id DESC', (now - SELF_LOOKUP_S,)).fetchall()
        found = list(dict.fromkeys(r['session_id'] for r in rows))
        if len(found) > 1 and cwd:
            same = [s for s in found if (self.session(s) or {'cwd': None})['cwd'] == cwd]
            if len(same) == 1:
                found = same
        if len(found) > 1:
            self.last_guess_ambiguous = True      # do not guess: another agent's call could be ours
            return None
        return found[0] if found else None

    def prune_events(self, days=EVENT_RETENTION_DAYS, now=None):
        cutoff = (now or time.time()) - days * 86400
        cur = self.db.execute('DELETE FROM events WHERE ts<?', (cutoff,))
        self.db.execute('DELETE FROM sessions WHERE last_seen<?', (cutoff,))
        self.db.commit()
        return cur.rowcount

    # ---- measuring the wasted window -------------------------------------

    def window(self, session_id, from_event=None, to_event=None, now=None):
        """Measure a friction window from recorded events.

        from_event None means the start of the latest cluster of unlogged bad calls
        (see `clusters`). Returns None when there is nothing to measure from.

        to_event None means the end of the resolution call: the first clean call after
        the cluster's last bad call, or the end of that last bad call if none yet.
        (Only with no bad call at all from from_event on does the window run to `now`.)
        """
        now = now or time.time()
        default_to = None
        to_event_given = to_event is not None
        if from_event is None:
            found = self.clusters(session_id)
            if not found:
                return None
            start = self.db.execute('SELECT * FROM events WHERE id=?', (found[-1]['from_event'],)).fetchone()
            default_to = found[-1]['to_event']
        else:
            start = self.db.execute('SELECT * FROM events WHERE id=? AND session_id=?',
                                    (from_event, session_id)).fetchone()
            if start is None:
                raise ValueError(f'event {from_event} is not in session {session_id}')
            if to_event is None:
                default_to = self._resolution_end(session_id, from_event)
        if to_event is None:
            to_event = default_to
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
            "SELECT COUNT(*) n, COALESCE(SUM(ok=0 OR outcome IS NOT NULL), 0) f, MAX(id) hi,"
            " MIN(source) src FROM events"
            " WHERE session_id=? AND kind='tool' AND id>=? AND id<=? AND ok IS NOT NULL",
            (session_id, start['id'], hi)).fetchone()
        return {
            'wasted_s': self.active_seconds(session_id, start['id'], hi, end_ts),
            'timing': TIMING_TRANSCRIPT if counts['src'] == 'transcript' else TIMING_HOOK,
            'from_event': start['id'],
            'to_event': end_id if end_id is not None else counts['hi'],
            'to_explicit': 1 if to_event_given else 0,
            'tool_calls': counts['n'],
            'failed_calls': counts['f'],
            'end_ts': end_ts,
        }

    def _resolution_end(self, session_id, from_event, until_ts=None):
        """Id of the resolution call for the cluster of bad calls that starts at/after from_event,
        or None when there are no bad calls from there on."""
        rows = self.db.execute(
            "SELECT * FROM events WHERE session_id=? AND id>=? AND kind='tool' AND ok IS NOT NULL"
            ' AND ts<=? ORDER BY id', (session_id, from_event, 1e18 if until_ts is None else until_ts)).fetchall()
        found = self.clusters(session_id, rows)
        return found[0]['to_event'] if found else None

    def active_seconds(self, session_id, first_id, last_id, end_ts):
        """Time from event first_id to end_ts, not counting idle gaps.

        A gap between calls longer than IDLE_GAP_S beyond the call's own
        duration is the session sitting idle (user away), not time the friction
        cost; only the call's duration is counted for it. Time spent waiting on
        the user (recorded `wait` events: between a turn ending and the next
        prompt, permission prompts, AskUserQuestion) is never counted.
        """
        rows = self.db.execute(
            "SELECT ts, duration_ms FROM events WHERE session_id=? AND kind='tool' AND id>=? AND id<=?"
            ' AND ts<=? ORDER BY ts', (session_id, first_id, last_id, end_ts)).fetchall()
        if not rows:
            return 0.0
        spans = []
        for i, ev in enumerate(rows):
            nxt = rows[i + 1]['ts'] if i + 1 < len(rows) else end_ts
            ran = (ev['duration_ms'] or 0) / 1000
            gap = max(0.0, nxt - ev['ts'])
            spans.append((ev['ts'], ev['ts'] + (gap if gap <= ran + IDLE_GAP_S else ran)))
        waits = self.waits(session_id, rows[0]['ts'], max(end_ts, spans[-1][1]))
        total = 0.0
        for start, end in spans:
            total += end - start
            for ws, we in waits:
                total -= max(0.0, min(end, we) - max(start, ws))
        return max(0.0, total)

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

    def create(self, title, detail='', workaround='', now=None, verified=True):
        """New item. verified=False (analyser-generated) keeps its workaround out of agent context."""
        now = now or time.time()
        cur = self.db.execute(
            'INSERT INTO items(title, detail, workaround, created, updated, verified) VALUES(?,?,?,?,?,?)',
            (redact_required(title, 200), redact_required(detail, 2000) or '',
             redact_required(workaround, 2000) or '', now, now,
             1 if verified else 0))
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
            ' from_event, to_event, tool_calls, failed_calls, note, source, regression, uid,'
            ' to_explicit) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (item_id, ts, session_id, agent, project, wasted, timing,
             win and win['from_event'], win and win['to_event'], win and win['tool_calls'],
             win and win['failed_calls'], redact_required(note, 1000) or '', source, regression,
             uuid.uuid4().hex[:16],
             1 if win and (win['to_explicit'] or source == 'backfill') else 0))
        if regression:
            self.db.execute("UPDATE items SET status='open', regressions=regressions+1,"
                            ' resolved_at=NULL, updated=? WHERE id=?', (now, item_id))
            self.mark_dirty(item_id, DIRTY_STATS | DIRTY_STATUS)
        else:
            self.db.execute('UPDATE items SET updated=? WHERE id=?', (now, item_id))
            self.mark_dirty(item_id, DIRTY_STATS)
        if session_id and source == 'live':
            if win is None:
                self.set_mark(session_id)
            elif win['to_event']:       # only up to the window's end: later bad calls stay loggable
                self.set_mark(session_id, win['to_event'])
        self.db.commit()
        return {'occurrence': cur.lastrowid, 'item': item_id, 'wasted_s': wasted, 'timing': timing,
                'regression': bool(regression), 'tool_calls': win and win['tool_calls'],
                'failed_calls': win and win['failed_calls']}

    def set_workaround(self, item_id, text, append=False, now=None):
        item = self.item(item_id)
        text = redact_required(text, 2000)
        if append and item['workaround']:
            text = item['workaround'] + '\n' + text
        # Set by a human or an agent on purpose, so it is trusted (appending to an unverified one
        # would launder the analyser's text, so that stays unverified).
        verified = 0 if append and item['workaround'] and not item['verified'] else 1
        self.db.execute('UPDATE items SET workaround=?, updated=?, verified=? WHERE id=?',
                        (text, now or time.time(), verified, item['id']))
        self.mark_dirty(item['id'], DIRTY_CONTENT)
        self._index(item['id'])
        self.db.commit()
        return item['id']

    def verify(self, item_id, now=None):
        """A human checked this item's analyser-written text: its workaround may reach agents."""
        item = self.item(item_id)
        self.db.execute('UPDATE items SET verified=1, updated=? WHERE id=?',
                        (now or time.time(), item['id']))
        self.mark_dirty(item['id'], DIRTY_CONTENT)
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
                            (status, redact_required(note, 2000) or item['fix_note'], now, now, item['id']))
        self.mark_dirty(item['id'], DIRTY_STATUS | DIRTY_CONTENT)
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
        verified = 1 if dst['verified'] and (src['verified'] or not src['workaround']) else 0
        self.db.execute(
            'UPDATE items SET aliases=?, detail=?, workaround=?, status=?, regressions=regressions+?,'
            ' created=MIN(created, ?), updated=?, verified=? WHERE id=?',
            (aliases, detail, workaround, status, src['regressions'], src['created'], now, verified,
             dst['id']))
        self.db.execute("UPDATE items SET status='merged', merged_into=?, updated=? WHERE id=?",
                        (dst['id'], now, src['id']))
        self.mark_dirty(src['id'], DIRTY_STATUS)
        self.mark_dirty(dst['id'], DIRTY_STATS | DIRTY_CONTENT | DIRTY_STATUS)
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
            ' i.remote_key, i.remote_url,'
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

    def remeasure(self, occ):
        """Measure an event-timed occurrence again over its original window.

        -> {wasted_s, to_event, tool_calls, failed_calls} or None (not event-timed, or the raw
        events are past retention). An explicit end (--to, or a backfill finding's to_event) is
        kept; otherwise the end is the resolution call, by the same rule as `window`."""
        if occ['timing'] not in (TIMING_HOOK, TIMING_TRANSCRIPT) or not occ['from_event']:
            return None
        if not self.db.execute('SELECT 1 FROM events WHERE id=? AND session_id=?',
                               (occ['from_event'], occ['session_id'])).fetchone():
            return None       # the raw events are past retention
        explicit = (occ['to_explicit'] or occ['source'] == 'backfill') and occ['to_event']
        if explicit:
            end_id, end_ts = occ['to_event'], occ['ts']
            hi = end_id
        else:
            end_id = self._resolution_end(occ['session_id'], occ['from_event'], occ['ts'])
            if end_id is None:
                hi, end_ts = 1 << 62, occ['ts']
            else:
                end = self.db.execute('SELECT ts, duration_ms FROM events WHERE id=?', (end_id,)).fetchone()
                end_ts = end['ts'] + (end['duration_ms'] or 0) / 1000
                if occ['ts']:
                    end_ts = min(occ['ts'], end_ts)
                hi = end_id
        counts = self.db.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(ok=0 OR outcome IS NOT NULL), 0) f, MAX(id) hi FROM events"
            " WHERE session_id=? AND kind='tool' AND id>=? AND id<=? AND ok IS NOT NULL",
            (occ['session_id'], occ['from_event'], hi)).fetchone()
        return {'wasted_s': self.active_seconds(occ['session_id'], occ['from_event'], hi, end_ts),
                'to_event': end_id if end_id is not None else counts['hi'],
                'tool_calls': counts['n'], 'failed_calls': counts['f']}

    def retime(self, occ):
        """Seconds from `remeasure`, or None."""
        res = self.remeasure(occ)
        return res and res['wasted_s']

    def set_wasted(self, occ_id, wasted_s, to_event=None, tool_calls=None, failed_calls=None):
        """Store a re-measured wasted time, with the window's end and counts when given."""
        row = self.db.execute('SELECT item_id FROM occurrences WHERE id=?', (occ_id,)).fetchone()
        self.db.execute('UPDATE occurrences SET wasted_s=?, to_event=COALESCE(?, to_event),'
                        ' tool_calls=COALESCE(?, tool_calls), failed_calls=COALESCE(?, failed_calls)'
                        ' WHERE id=?', (wasted_s, to_event, tool_calls, failed_calls, occ_id))
        self.mark_dirty(row['item_id'], DIRTY_STATS)

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


def unverified_note(row):
    return f"(unverified workaround: run `friction show {row['id']}`)"


def workaround_text(row, limit=240):
    """A workaround as shown to agents: unverified (analyser-written) ones are withheld."""
    if not row['workaround']:
        return ''
    if not row['verified']:
        return unverified_note(row)
    return row['workaround'].replace('\n', ' ')[:limit]


def event_line(ev):
    state = {None: '…', 1: 'ok', 0: 'FAIL'}[ev['ok']]
    soft = ev['ok'] == 1 and ev['outcome'] is not None
    if soft:
        state = 'SOFT'
    dur = f"{ev['duration_ms'] / 1000:.1f}s" if ev['duration_ms'] is not None else '  -  '
    line = f"{ev['id']:>7} {fmt_ts(ev['ts'], '%H:%M:%S')} {state:<4} {dur:>7} {ev['tool'] or ev['kind']}"
    if ev['summary']:
        line += ': ' + ev['summary'].replace('\n', ' ')[:110]
    if soft:
        line += f" [{ev['outcome']}]"
    if (ev['ok'] == 0 or soft) and ev['error']:
        line += '\n' + ' ' * 30 + '-> ' + ev['error'].replace('\n', ' ')[:140]
    return line


def _occur_args(p):
    p.add_argument('--note', default='', help='what happened this time')
    p.add_argument('--from', dest='from_event', type=int, metavar='EVENT',
                   help='event id where the friction began (see `friction timeline`);'
                        ' default: start of the latest cluster of bad calls')
    p.add_argument('--to', dest='to_event', type=int, metavar='EVENT',
                   help='event id where the friction ended; default: the call that resolved it'
                        ' (first clean call after the last bad one)')
    p.add_argument('--wasted', metavar='DUR',
                   help='your estimate (90s, 5m): used only when no recorded events can time it')
    p.add_argument('--session', help='session id (default: detected)')


def _occur(fr, args, item_id):
    session = args.session or fr.guess_session(cwd=os.getcwd())
    if not session and not args.session and fr.last_guess_ambiguous:
        print('friction: several agents are running friction commands right now, so the session cannot'
              ' be told apart; re-run with --session <id> to get this timed from recorded events.',
              file=sys.stderr)
    estimate = parse_duration(args.wasted) if args.wasted else None
    known = bool(session and fr.session(session))
    ai = os.environ.get('AI_AGENT', '').lower()
    agent = None if known else next((a for a in ('claude', 'codex') if a in ai), None)
    res = fr.occur(item_id, session_id=session, note=args.note, from_event=args.from_event,
                   to_event=args.to_event, estimate_s=estimate, agent=agent,
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
        print(f"Known workaround: {item['workaround'] if item['verified'] else unverified_note(item)}")
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
                    print('      workaround: ' + workaround_text(row, 200))
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
            print('    workaround: ' + workaround_text(row, 200))
    return 0


def cmd_show(fr, args):
    item = fr.item(args.id)
    st = fr.stats(item['id'])
    print(f"#{item['id']} [{item['status']}] {item['title']}")
    if item['remote_key']:
        print(f"  tracker:    {item['remote_key']}  {item['remote_url'] or ''}")
    if item['detail']:
        print(f"  detail:     {item['detail']}")
    if item['aliases']:
        print('  also known: ' + item['aliases'].replace('\n', ' | '))
    label = '' if item['verified'] or not item['workaround'] else \
        f" [UNVERIFIED: written by the backfill analyser; `friction verify {item['id']}` once checked]"
    print(f"  workaround: {item['workaround'] or '-'}{label}")
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


def cmd_verify(fr, args):
    item_id = fr.verify(args.id)
    print(f'#{item_id} verified: its workaround is now shown to agents.')
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
    session = args.session or fr.guess_session(cwd=os.getcwd())
    if not session:
        print('No session detected (or several agents are running friction commands at once).'
              ' Pass --session <id>.')
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
            print('    workaround: ' + workaround_text(row, 240))
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


def cmd_scrub(fr, args):
    """Internal: the hook pipes {"items": [...]} (raw text) here; gitleaks scans it and the
    scrubbed text is written to the events. Raw text exists only in this pipe and process."""
    try:
        items = json.load(sys.stdin)['items']
    except (ValueError, KeyError, TypeError):
        return 1
    fr.scrub_events(items)
    return 0


def cmd_doctor(fr, args):
    """Is the secret scanner working? Scans a canary token end to end."""
    import subprocess
    ok, msg = scanner_status()
    print(msg)
    if not ok:
        return 1
    path = gitleaks_path()
    try:
        ver = subprocess.run([path, 'version'], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        ver = '?'
    canary = 'ghp_' + 'aB3dE5gH7jK9mN1pQ3sT5vX7zA9cE1gH3jK5'
    out = redact(f'token {canary}')
    print(f'version {ver}')
    if out is None or canary in out:
        print(f'scan test FAILED (got {out!r}): tool text is not being stored')
        return 1
    print('scan test ok')
    return 0


def cmd_backfill(fr, args):
    import backfill
    return backfill.run(fr, args)


def cmd_retime(fr, args):
    import backfill
    return backfill.retime(fr, args)


def cmd_sync(fr, args):
    import tracker
    return tracker.run(fr, args)


def kick_sync():
    """Start a detached `friction sync` if a tracker is configured. Never blocks or raises."""
    try:
        import subprocess
        import tracker
        if not tracker.config_path().exists() or tracker.load_config() is None:
            return                                  # sync is off unless explicitly enabled
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'sync', '--quiet'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


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

    p = sub.add_parser('verify', help='mark an analyser-written item as checked: its workaround'
                       ' then reaches agents')
    p.add_argument('id', type=int)
    p.set_defaults(fn=cmd_verify)

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

    p = sub.add_parser('scrub', help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_scrub)

    p = sub.add_parser('doctor', help='check that the gitleaks secret scanner works')
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser('backfill', help='mine past transcripts for friction nobody logged')
    p.add_argument('--since', default=None, help='how far back to look (2d, 30d, 12h); default 1d')
    p.add_argument('--dry-run', action='store_true', help='pre-pass only: list suspicious sessions')
    p.add_argument('--limit', type=int, default=40, help='most sessions to send to the analyser')
    p.add_argument('--min-failures', type=int, default=2)
    p.add_argument('--model', default='haiku')
    p.add_argument('--redo', action='store_true', help='re-analyse sessions already processed')
    p.add_argument('--classifier', choices=['claude', 'jev'], default=None,
                   help='override classifier.json (default: claude; jev is opt-in)')
    p.add_argument('--jev-check', action='store_true',
                   help='send a synthetic request to test the Jev key/config (no session data)')
    p.add_argument('--jev-stats', action='store_true',
                   help="report Jev's recorded confidence per cluster, to tune min_confidence"
                   ' (all history unless --since is given)')
    p.set_defaults(fn=cmd_backfill)

    p = sub.add_parser('retime', help='re-measure logged occurrences, taking out time spent'
                       ' waiting on the user (read from the session transcripts)')
    p.add_argument('--dry-run', action='store_true', help='show the changes, write nothing')
    p.set_defaults(fn=cmd_retime)

    p = sub.add_parser('sync', help='sync items with an issue tracker such as Jira (off by default)')
    p.add_argument('--enable', action='store_true',
                   help='turn tracker sync on (writes a config template the first time)')
    p.add_argument('--disable', action='store_true', help='turn tracker sync off; items stay local')
    p.add_argument('--check', action='store_true', help='test the connection and show what is pending')
    p.add_argument('--dry-run', action='store_true', help='show what would be pushed, change nothing')
    p.add_argument('--quiet', action='store_true', help='no output unless something fails')
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser('web', help='serve the read-only web heatmap')
    p.add_argument('--port', type=int, default=8798)
    p.add_argument('--host', default='127.0.0.1')
    p.set_defaults(fn=cmd_web)
    return ap


SYNC_AFTER = {'log', 'bump', 'workaround', 'verify','resolve', 'wontfix', 'reopen', 'merge', 'backfill',
              'retime'}


def main(argv=None):
    args = build_parser().parse_args(argv)
    fr = None
    try:
        fr = Friction()
        code = args.fn(fr, args)
        if args.cmd in SYNC_AFTER:
            kick_sync()
        return code
    except (KeyError, ValueError, OverflowError) as exc:    # OverflowError: an id past SQLite's range
        print(f'friction: {exc.args[0] if exc.args else exc}', file=sys.stderr)
        return 1
    finally:
        if fr is not None:
            fr.db.close()       # rolls back a half-done write instead of holding the lock


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
