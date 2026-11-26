"""Sync friction items with Jira Cloud or Ghidorah items with Linear.

The local database stays the working copy: hooks, timing, search and the
heatmap never wait on the network. `friction sync` then makes the tracker the
shared home for the items themselves:

  pull   issues changed in the tracker update local items (a teammate's agent
         logged something, a human edited a workaround or closed the issue)
  push   new items become issues, each occurrence becomes a comment, and
         edits, hit totals and status changes are written back

Anything not pushed yet stays flagged locally, so being offline only delays
the sync. Raw tool-call events never leave the machine.

Configuration is an INI file, ~/.config/friction/config.ini (FRICTION_CONFIG
overrides the path):

  [tracker]
  enabled = true
  type = jira
  url = https://yoursite.atlassian.net
  email = you@example.com
  project = FRIC
  token_file = ~/.config/friction/jira-token     ; or token_env / token_cmd
  ; optional: issue_type = Task, label = friction, cloud_id = <id> (scoped tokens)

Sync is off unless `enabled = true`. `friction sync --enable` writes a template
the first time, and afterwards switches it on once the tracker answers;
`friction sync --disable` switches it off again.
"""
import atexit
import base64
import collections
import configparser
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from friction import DIRTY_CONTENT, DIRTY_STATS, DIRTY_STATUS, fmt_duration, redact

TIMEOUT = 20
# One line, and the last one in the comment: the note above it may quote a token.
HIT_TOKEN = re.compile(r'\[friction:hit ([^\]\n]+)\]')
WONTFIX_LABEL = 'friction-wontfix'


class TrackerError(Exception):
    pass


# ---- configuration -------------------------------------------------------

def config_path():
    explicit = os.environ.get('FRICTION_CONFIG')
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get('XDG_CONFIG_HOME') or '~/.config'
    return Path(base).expanduser() / 'friction' / 'config.ini'


TEMPLATE = """\
# friction tracker sync. Fill in the values, then run: friction sync --enable
[tracker]
enabled = false
type = jira
url = https://yoursite.atlassian.net
email = you@example.com
project = FRIC
token_file = ~/.config/friction/jira-token
# Workarounds written or edited in the tracker are shown to agents only after
# `friction verify <id>`, unless you trust everyone who can edit these issues:
trust_workarounds = false
"""


def read_config():
    """The [tracker] section as a dict (empty when there is none), whether or not it is enabled."""
    path = config_path()
    if not path.exists():
        return {}
    parser = configparser.ConfigParser(inline_comment_prefixes=(';',))
    try:
        parser.read(path)
    except configparser.Error as exc:
        raise TrackerError(f'{path}: {exc}') from None
    return dict(parser['tracker']) if parser.has_section('tracker') else {}


def is_enabled(cfg):
    return cfg.get('enabled', '').strip().lower() in ('1', 'true', 'yes', 'on')


def load_config():
    """The tracker settings, or None when sync is off. Sync is off unless `enabled = true`."""
    try:
        cfg = read_config()
    except TrackerError:
        return None
    return cfg if is_enabled(cfg) else None


def set_enabled(on):
    """Flip `enabled` in the config file, leaving every other line as the user wrote it."""
    path = config_path()
    lines = path.read_text().splitlines()
    value = f"enabled = {'true' if on else 'false'}"
    section, header, done = None, None, False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('['):
            section = stripped.strip('[]').strip().lower()
            if section == 'tracker':
                header = i
        elif section == 'tracker' and re.match(r'enabled\s*[=:]', stripped, re.I):
            lines[i], done = value, True
    if not done:
        lines.insert(header + 1, value)
    path.write_text('\n'.join(lines) + '\n')


def read_token(cfg):
    default_env = 'FRICTION_LINEAR_TOKEN' if cfg.get('type', '').lower() == 'linear' else 'FRICTION_JIRA_TOKEN'
    token = os.environ.get(cfg.get('token_env') or default_env)
    if not token and cfg.get('token_cmd'):
        try:
            proc = subprocess.run(cfg['token_cmd'], shell=True, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise TrackerError('could not run tracker token command') from None
        if proc.returncode == 0:
            token = proc.stdout.strip()
    if not token and cfg.get('token_file'):
        path = Path(cfg['token_file']).expanduser()
        if path.exists():
            token = path.read_text().strip()
    if not token:
        raise TrackerError('no API token: set token_file, token_cmd or token_env in ' + str(config_path()))
    return token


def make_tracker(cfg):
    kind = cfg.get('type', '').lower()
    if kind == 'linear':
        from linear_tracker import Linear
        return Linear(cfg)
    if kind != 'jira':
        raise TrackerError(f'unknown tracker type {kind!r} (supported: jira, linear)')
    return Jira(cfg)


# ---- item <-> tracker text -----------------------------------------------

def footer_text(fr, item):
    st = fr.stats(item['id'])
    last = time.strftime('%Y-%m-%d', time.gmtime(st['last_ts'])) if st['last_ts'] else '-'
    return (f"friction: {st['n']} hits · {fmt_duration(st['wasted'])} wasted"
            f" · {item['regressions']} regressions · last hit {last}")


def hit_text(occ):
    """A comment for one occurrence: a line for people, then a token for other machines."""
    how = {'hook': 'measured', 'transcript': 'measured from transcript',
           'estimate': 'estimated by the agent'}.get(occ['timing'])
    one_line = lambda s: ' '.join((s or '').split())     # the head must stay the first line
    head = f"Hit by {one_line(occ['agent']) or 'an agent'} in {one_line(occ['project']) or 'an unknown project'}"
    head += f": {fmt_duration(occ['wasted_s'])} wasted ({how})" if how else ': not timed'
    fields = {'uid': occ['uid'], 'ts': int(occ['ts']), 'timing': occ['timing'],
              'wasted': '' if occ['wasted_s'] is None else int(occ['wasted_s']),
              'agent': occ['agent'] or '', 'project': occ['project'] or ''}
    token = ' '.join(f'{k}={urllib.parse.quote(str(v), safe="")}' for k, v in fields.items())
    return '\n'.join(x for x in (head, occ['note'], f'[friction:hit {token}]') if x)


def parse_hit(text):
    m = None
    for m in HIT_TOKEN.finditer(text or ''):
        pass
    if not m:
        return None
    hit = {k: urllib.parse.unquote(v) for k, _, v in (p.partition('=') for p in m.group(1).split())}
    if not hit.get('uid') or not hit.get('ts', '').isdigit():
        return None
    hit['note'] = '\n'.join(text[:m.start()].strip().split('\n')[1:]).strip()
    return hit


# ---- Jira Cloud ----------------------------------------------------------

def adf_block(text):
    """Literal text as one ADF paragraph (no markup is interpreted)."""
    content = []
    for i, line in enumerate(text.split('\n')):
        if i:
            content.append({'type': 'hardBreak'})
        if line:
            content.append({'type': 'text', 'text': line})
    return {'type': 'paragraph', 'content': content}


def adf_heading(text):
    return {'type': 'heading', 'attrs': {'level': 3}, 'content': [{'type': 'text', 'text': text}]}


def adf_text(node):
    """Plain text of an ADF node."""
    if not isinstance(node, dict):
        return ''
    if node.get('type') == 'text':
        text = node.get('text')
        return text if isinstance(text, str) else ''
    if node.get('type') == 'hardBreak':
        return '\n'
    content = node.get('content')
    parts = [adf_text(child) for child in (content if isinstance(content, list) else [])]
    return ('\n' if node.get('type') in ('doc', 'bulletList', 'orderedList') else '').join(parts)


def adf_description(detail, workaround, fix, footer):
    blocks = [adf_block(detail)] if detail else []
    if workaround:
        blocks += [adf_heading('Workaround'), adf_block(workaround)]
    if fix:
        blocks += [adf_heading('Fix'), adf_block(fix)]
    blocks += [{'type': 'rule'}, adf_block(footer)]
    return {'type': 'doc', 'version': 1, 'content': blocks}


def parse_description(doc):
    """-> {'detail', 'workaround', 'fix', 'footer'} from a description we (or a person) wrote."""
    parts = {'detail': [], 'workaround': [], 'fix': [], 'footer': []}
    section = 'detail'
    content = doc.get('content') if isinstance(doc, dict) else None
    for node in content if isinstance(content, list) else []:
        if not isinstance(node, dict):
            continue
        kind = node.get('type')
        if kind == 'heading':
            name = adf_text(node).strip().lower()
            if name in ('workaround', 'fix'):
                section = name
                continue
        if kind == 'rule':
            section = 'footer'
            continue
        parts[section].append(adf_text(node))
    return {k: '\n'.join(v).strip() for k, v in parts.items()}


class Jira:
    name = 'Jira'

    def __init__(self, cfg):
        for key in ('url', 'email', 'project'):
            if not cfg.get(key):
                raise TrackerError(f'tracker config is missing `{key}` in {config_path()}')
        self.site = cfg['url'].rstrip('/')
        self.project = cfg['project']
        self.issue_type = cfg.get('issue_type') or 'Task'
        self.label = cfg.get('label') or 'friction'
        # Workarounds edited in the tracker reach every agent's SessionStart context, so they
        # are unverified (not shown) unless the tracker's editors are trusted.
        self.trust_workarounds = is_enabled({'enabled': cfg.get('trust_workarounds', '')})
        # Scoped API tokens only work through the api.atlassian.com gateway.
        self.base = (f"https://api.atlassian.com/ex/jira/{cfg['cloud_id']}" if cfg.get('cloud_id')
                     else self.site) + '/rest/api/3'
        auth = f"{cfg['email']}:{read_token(cfg)}".encode()
        self.headers = {'Authorization': 'Basic ' + base64.b64encode(auth).decode(),
                        'Accept': 'application/json', 'Content-Type': 'application/json'}

    def call(self, method, path, body=None, query=None):
        url = self.base + path + ('?' + urllib.parse.urlencode(query) if query else '')
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', 'replace')[:300]
            exc.close()
            raise TrackerError(f'Jira {method} {path}: HTTP {exc.code} {detail}') from None
        except (urllib.error.URLError, OSError) as exc:
            raise TrackerError(f'Jira unreachable: {getattr(exc, "reason", exc)}') from None
        return json.loads(raw) if raw.strip() else None

    def url_of(self, key):
        return f'{self.site}/browse/{key}'

    def check(self):
        me = self.call('GET', '/myself')
        project = self.call('GET', f'/project/{self.project}')
        return f"{me.get('displayName') or me.get('emailAddress')} on {self.site}, project {project['key']} ({project['name']})"

    def _remote(self, issue):
        f = issue['fields']
        out = parse_description(f.get('description'))
        out.update(key=issue['key'], url=self.url_of(issue['key']), title=f.get('summary') or '',
                   done=f['status']['statusCategory']['key'] == 'done',
                   wontfix=WONTFIX_LABEL in (f.get('labels') or []))
        return out

    def changed(self, minutes=None):
        """Friction issues updated in the last `minutes` (all of them when None)."""
        jql = f'project = "{self.project}" AND labels = "{self.label}"'
        if minutes is not None:
            jql += f' AND updated >= -{int(minutes)}m'
        token = None
        while True:
            body = {'jql': jql + ' ORDER BY updated ASC', 'maxResults': 100,
                    'fields': ['summary', 'description', 'status', 'labels']}
            if token:
                body['nextPageToken'] = token
            page = self.call('POST', '/search/jql', body)
            for issue in page.get('issues') or []:
                yield self._remote(issue)
            token = page.get('nextPageToken')
            if not token:
                return

    def get(self, key):
        return self._remote(self.call('GET', f'/issue/{key}',
                                      query={'fields': 'summary,description,status,labels'}))

    def create(self, title, description):
        made = self.call('POST', '/issue', {'fields': {
            'project': {'key': self.project}, 'issuetype': {'name': self.issue_type},
            'summary': title, 'labels': [self.label], 'description': adf_description(**description)}})
        return made['key'], self.url_of(made['key'])

    def update(self, key, title, description, wontfix):
        op = 'add' if wontfix else 'remove'
        self.call('PUT', f'/issue/{key}', {
            'fields': {'summary': title, 'description': adf_description(**description)},
            'update': {'labels': [{op: WONTFIX_LABEL}]}})

    def set_done(self, key, done):
        """Move the issue to a done (or back to a not-done) status, if it is not already there."""
        options = self.call('GET', f'/issue/{key}/transitions')['transitions']
        for tr in options:
            if (tr['to']['statusCategory']['key'] == 'done') == done:
                self.call('POST', f'/issue/{key}/transitions', {'transition': {'id': tr['id']}})
                return True
        return False

    def comment(self, key, text):
        self.call('POST', f'/issue/{key}/comment',
                  {'body': {'type': 'doc', 'version': 1, 'content': [adf_block(text)]}})

    def comments(self, key):
        start = 0
        while True:
            page = self.call('GET', f'/issue/{key}/comment', query={'startAt': start, 'maxResults': 100})
            batch = page.get('comments') or []
            for c in batch:
                yield adf_text(c.get('body'))
            start += len(batch)
            if not batch or start >= page.get('total', 0):
                return


# ---- sync ----------------------------------------------------------------

def description_of(fr, item):
    return {'detail': item['detail'], 'workaround': item['workaround'],
            'fix': item['fix_note'], 'footer': footer_text(fr, item)}


def apply_remote(fr, tr, remote, counts):
    """Bring one tracker issue into the local database."""
    db = fr.db
    item = db.execute('SELECT * FROM items WHERE remote_key=?', (remote['key'],)).fetchone()
    now = time.time()
    if item is None:
        item_id = fr.create(remote['title'], remote['detail'], remote['workaround'],
                            verified=getattr(tr, 'trust_workarounds', False) or not remote['workaround'])
        db.execute('UPDATE items SET remote_key=?, remote_url=?, fix_note=? WHERE id=?',
                   (remote['key'], remote['url'], redact(remote['fix'], 2000) or '', item_id))
        item = db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
        counts['items pulled'] += 1
    elif item['status'] == 'merged':
        return
    item_id = item['id']
    if not item['dirty'] & DIRTY_CONTENT:
        fresh = (redact(remote['title'], 200), redact(remote['detail'], 2000) or '',
                 redact(remote['workaround'], 2000) or '', redact(remote['fix'], 2000) or '')
        if fresh != (item['title'], item['detail'], item['workaround'], item['fix_note']) and fresh[0]:
            db.execute('UPDATE items SET title=?, detail=?, workaround=?, fix_note=? WHERE id=?',
                       fresh + (item_id,))
            if fresh[2] != item['workaround'] and fresh[2] and not getattr(tr, 'trust_workarounds', False):
                db.execute('UPDATE items SET verified=0 WHERE id=?', (item_id,))
            fr._index(item_id)
            counts['items updated from tracker'] += 1
    if not item['dirty'] & DIRTY_STATUS:
        closed = item['status'] in ('resolved', 'wontfix')
        if remote['done'] and not closed:
            db.execute('UPDATE items SET status=?, resolved_at=? WHERE id=?',
                       ('wontfix' if remote['wontfix'] else 'resolved', now, item_id))
            counts['items closed in tracker'] += 1
        elif not remote['done'] and closed:
            db.execute("UPDATE items SET status='open', resolved_at=NULL WHERE id=?", (item_id,))
            counts['items reopened in tracker'] += 1
    for text in tr.comments(remote['key']):
        hit = parse_hit(text)
        if not hit or db.execute('SELECT 1 FROM occurrences WHERE uid=?', (hit['uid'],)).fetchone():
            continue
        timing = hit.get('timing') if hit.get('timing') in ('hook', 'transcript', 'estimate') else 'none'
        wasted = float(hit['wasted']) if hit.get('wasted', '').isdigit() else None
        db.execute(
            'INSERT INTO occurrences(item_id, ts, agent, project, wasted_s, timing, note, source, uid, pushed)'
            " VALUES(?,?,?,?,?,?,?,'remote',?,1)",
            (item_id, float(hit['ts']), hit.get('agent') or None, hit.get('project') or None,
             wasted, timing if wasted is not None else 'none', redact(hit['note'], 1000) or '', hit['uid']))
        fr.mark_dirty(item_id, DIRTY_STATS)
        counts['hits pulled'] += 1
    if remote['footer'] and remote['footer'] == footer_text(fr, fr.item(item_id, follow=False)):
        db.execute('UPDATE items SET dirty = dirty & ? WHERE id=? AND dirty = ?',
                   (~DIRTY_STATS, item_id, DIRTY_STATS))      # totals already match
    db.commit()


def push_item(fr, tr, item, counts):
    """Write one local item, and its unpushed occurrences, to the tracker."""
    db = fr.db
    key, dirty = item['remote_key'], item['dirty']
    if item['status'] == 'merged':
        if key and dirty & DIRTY_STATUS:
            target = fr.item(item['id'])
            tr.comment(key, f"Merged into {target['remote_key'] or 'another item'} by friction.")
            tr.set_done(key, True)
        db.execute('UPDATE items SET dirty=0 WHERE id=?', (item['id'],))
        db.commit()
        return
    if not key:
        key, url = tr.create(item['title'], description_of(fr, item))
        db.execute('UPDATE items SET remote_key=?, remote_url=? WHERE id=?', (key, url, item['id']))
        db.commit()
        counts['items created'] += 1
        created = True                             # the new issue already carries current totals
        dirty = DIRTY_STATUS if item['status'] != 'open' else 0
        if item['status'] == 'wontfix':
            dirty |= DIRTY_CONTENT                 # carries the wontfix label
    else:
        created = False
    unpushed = db.execute("SELECT * FROM occurrences WHERE item_id=? AND pushed=0 AND source!='remote'"
                          ' ORDER BY ts', (item['id'],)).fetchall()
    for occ in unpushed:
        if not occ['uid']:                         # recorded before sync existed
            db.execute('UPDATE occurrences SET uid=? WHERE id=?', (uuid.uuid4().hex[:16], occ['id']))
            occ = db.execute('SELECT * FROM occurrences WHERE id=?', (occ['id'],)).fetchone()
        tr.comment(key, hit_text(occ))
        db.execute('UPDATE occurrences SET pushed=1 WHERE id=?', (occ['id'],))
        db.commit()
        counts['hits pushed'] += 1
        if not created:
            dirty |= DIRTY_STATS
    if dirty & (DIRTY_STATS | DIRTY_CONTENT):
        tr.update(key, item['title'], description_of(fr, item), item['status'] == 'wontfix')
        counts['items updated'] += 1
    if dirty & DIRTY_STATUS:
        want_done = item['status'] in ('resolved', 'wontfix')
        if tr.get(key)['done'] != want_done:
            if tr.set_done(key, want_done):
                counts['items closed' if want_done else 'items reopened'] += 1
            else:
                raise TrackerError(f'{key}: no workflow transition to a '
                                   f"{'done' if want_done else 'not-done'} status")
    # Only clear the flags we just wrote: the item may have changed while we were on the network.
    db.execute('UPDATE items SET dirty = dirty & ? WHERE id=?', (~item['dirty'], item['id']))
    db.commit()


def pending(fr, cfg=None):
    if cfg and cfg.get('type', '').lower() == 'linear':
        from linear_tracker import pending as linear_pending
        return linear_pending(fr)
    db = fr.db
    one = lambda sql: db.execute(sql).fetchone()[0]     # noqa: E731
    return {
        'items to create': one("SELECT COUNT(*) FROM items WHERE remote_key IS NULL AND status!='merged'"),
        'items to update': one('SELECT COUNT(*) FROM items WHERE remote_key IS NOT NULL AND dirty!=0'),
        'hits to push': one("SELECT COUNT(*) FROM occurrences o JOIN items i ON i.id=o.item_id"
                            " WHERE o.pushed=0 AND o.source!='remote' AND i.status!='merged'"),
    }


def sync(fr, tr):
    """One full pull then push. -> (counts, errors)"""
    if tr.name == 'Linear':
        return tr.sync(fr)
    counts, errors = collections.Counter(), []
    started = time.time()
    last = fr.meta('last_pull')
    minutes = None if last is None else int((started - float(last)) / 60) + 10
    for remote in tr.changed(minutes):
        apply_remote(fr, tr, remote, counts)
    fr.set_meta('last_pull', started)
    rows = fr.db.execute("SELECT * FROM items WHERE (remote_key IS NULL AND status!='merged') OR dirty!=0"
                         " OR id IN (SELECT item_id FROM occurrences WHERE pushed=0 AND source!='remote')"
                         ' ORDER BY id').fetchall()
    for item in rows:
        try:
            push_item(fr, tr, item, counts)
        except TrackerError as exc:
            errors.append(f"#{item['id']}: {exc}")
            if 'unreachable' in str(exc):
                break
    return counts, errors


def locked(fr):
    """Hold an exclusive lock for the life of the process. -> False if another sync is running."""
    try:
        import fcntl
    except ImportError:
        return True
    handle = open(fr.root / 'sync.lock', 'w')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    atexit.register(handle.close)       # keep it open, and so locked, until the process ends
    return True


def enable(fr):
    """Turn sync on, but only once the settings are complete and the tracker answers."""
    path = config_path()
    cfg = read_config()
    if not cfg:
        if path.exists():
            raise TrackerError(f'{path} has no [tracker] section')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TEMPLATE)
        path.chmod(0o600)
        print(f'Wrote a template to {path}.\nFill in your site, email and project, put an API token'
              ' in the token file, then run `friction sync --enable` again.')
        return 1
    if 'yoursite.atlassian.net' in cfg.get('url', ''):
        raise TrackerError(f'{path} still has the template values; fill them in first')
    who = make_tracker(cfg).check()            # raises if the settings or the token are wrong
    set_enabled(True)
    todo = pending(fr, cfg)
    print(f'Tracker sync is ON. Connected: {who}')
    print('The next `friction sync` will push: ' + ', '.join(f'{n} {k}' for k, n in todo.items()))
    return 0


def disable():
    if not read_config():
        print('Tracker sync is already off (nothing configured).')
        return 0
    set_enabled(False)
    print('Tracker sync is OFF. Items stay local; anything already in the tracker is left there.\n'
          'Changes made while it is off are sent if you enable it again.')
    return 0


def run(fr, args):
    say = (lambda *a: None) if args.quiet else print
    try:
        if args.enable:
            return enable(fr)
        if args.disable:
            return disable()
        raw = read_config()
    except TrackerError as exc:
        print(f'friction sync: {exc}', file=sys.stderr)
        return 1
    cfg = raw if (is_enabled(raw) or (args.check and raw)) else None
    if cfg is None:
        say('Tracker sync is off: items stay in the local database. Turn it on with'
            ' `friction sync --enable`.')
        return 0
    todo = pending(fr, cfg)
    if args.dry_run:
        print('Would push: ' + ', '.join(f'{n} {k}' for k, n in todo.items()))
        return 0
    try:
        tr = make_tracker(cfg)
        if args.check:
            print(f"Tracker sync is {'ON' if is_enabled(cfg) else 'OFF (enable with `friction sync --enable`)'}.")
            print('Connected: ' + tr.check())
            print('Pending: ' + ', '.join(f'{n} {k}' for k, n in todo.items()))
            if fr.meta('sync_error'):
                print('Last sync error: ' + fr.meta('sync_error'))
            return 0
        if not locked(fr):
            say('Another sync is running.')
            return 0
        counts, errors = sync(fr, tr)
    except TrackerError as exc:
        fr.set_meta('sync_error', f'{time.strftime("%Y-%m-%d %H:%M")} {exc}')
        print(f'friction sync: {exc}', file=sys.stderr)
        return 1
    fr.set_meta('sync_error', errors[0] if errors else None)
    fr.set_meta('last_sync', time.time())
    say(f'Synced with {tr.name}: ' + (', '.join(f'{n} {k}' for k, n in counts.items()) or 'nothing to do'))
    for err in errors:
        print(f'friction sync: {err}', file=sys.stderr)
    return 1 if errors else 0
