"""Record and exchange the structured occurrences used by the harness tracker."""

import re
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS ghidorah_items (
  fingerprint TEXT PRIMARY KEY,
  item_id INTEGER UNIQUE NOT NULL REFERENCES items(id),
  title TEXT NOT NULL,
  link TEXT,
  seed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ghidorah_hits (
  uid TEXT PRIMARY KEY,
  fingerprint TEXT NOT NULL REFERENCES ghidorah_items(fingerprint),
  session TEXT NOT NULL,
  machine TEXT NOT NULL,
  account TEXT NOT NULL,
  engine TEXT NOT NULL,
  correction INTEGER NOT NULL,
  silent INTEGER NOT NULL,
  ts REAL NOT NULL,
  local INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ghidorah_hits_item ON ghidorah_hits(fingerprint, ts);
CREATE INDEX IF NOT EXISTS ghidorah_hits_session ON ghidorah_hits(session, ts);
CREATE TABLE IF NOT EXISTS ghidorah_prompts (
  uid TEXT PRIMARY KEY,
  session TEXT NOT NULL,
  ts REAL NOT NULL,
  fingerprint TEXT,
  reminded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ghidorah_searches (
  session TEXT NOT NULL,
  digest TEXT NOT NULL,
  call TEXT NOT NULL,
  ts REAL NOT NULL,
  PRIMARY KEY(session, digest)
);
"""
FINGERPRINT = re.compile(
    r'(?:[a-z0-9][a-z0-9_-]{0,23}:){1}[a-z0-9][a-z0-9_-]{0,59}:[a-z0-9][a-z0-9_-]{0,39}'
)
ISSUE = re.compile(r'(?:HXR|MAI)-[1-9]\d{0,7}')
ENGINES = {'claude', 'codex'}
ACCOUNTS = {'default', 'perso', 'mainder', 'codex', 'unknown'}
MACHINE = re.compile(r'[a-z][a-z0-9_-]{0,23}')
FIELDS = ('uid', 'fingerprint', 'session', 'machine', 'account', 'engine', 'correction', 'silent', 'ts')
NAMESPACE = uuid.UUID('84169825-af19-495b-8e62-76d9ab77a785')


def occurrence_id(*parts):
    digest = uuid.uuid5(NAMESPACE, '\x1f'.join(parts))
    return str(uuid.UUID(bytes=digest.bytes, version=4))


def is_uuid(value):
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def valid_hit(hit):
    return (isinstance(hit, dict) and set(hit) == set(FIELDS)
            and all(isinstance(hit[key], str) for key in FIELDS[:6])
            and is_uuid(hit['uid']) and is_uuid(hit['session'])
            and FINGERPRINT.fullmatch(hit['fingerprint']) is not None
            and MACHINE.fullmatch(hit['machine']) is not None
            and hit['account'] in ACCOUNTS and hit['engine'] in ENGINES
            and type(hit['correction']) in (int, bool) and hit['correction'] in (0, 1)
            and type(hit['silent']) in (int, bool) and hit['silent'] in (0, 1)
            and type(hit['ts']) in (int, float) and 0 < hit['ts'] <= 4_102_444_800)


def ensure_schema(fr):
    fr.db.executescript(SCHEMA)


def ensure_item(fr, fingerprint, title=None, link=None, seed=False):
    """Inside a write transaction, get or create a Friction item from vetted metadata."""
    if not FINGERPRINT.fullmatch(fingerprint) or (link and not ISSUE.fullmatch(link)):
        raise ValueError('invalid Ghidorah fingerprint or Linear issue')
    now = time.time()
    db = fr.db
    display = title or ('Ghidorah: ' + fingerprint.replace(':', ' / '))
    row = db.execute('SELECT item_id FROM ghidorah_items WHERE fingerprint=?', (fingerprint,)).fetchone()
    if row is not None:
        if seed:
            db.execute('UPDATE ghidorah_items SET title=?, link=?, seed=1 WHERE fingerprint=?',
                       (display, link, fingerprint))
            changed = db.execute('UPDATE items SET title=? WHERE id=? AND remote_key IS NULL AND title!=?',
                                 (display, row['item_id'], display))
            if changed.rowcount:
                fr._index(row['item_id'])
        return row['item_id']
    # The caller supplies a tracked catalog title or the three mechanical codes, never raw tool output.
    item_id = db.execute('INSERT INTO items(title, created, updated) VALUES(?,?,?)',
                         (display, now, now)).lastrowid
    db.execute('INSERT INTO ghidorah_items(fingerprint,item_id,title,link,seed) VALUES(?,?,?,?,?)',
               (fingerprint, item_id, display, link, seed))
    fr._index(item_id)
    return item_id


def record(fr, *, fingerprint, uid, session, machine, account, engine, correction=False,
           silent=False, ts=None, local=True, title=None):
    hit = dict(uid=uid, fingerprint=fingerprint, session=session, machine=machine, account=account,
               engine=engine, correction=correction, silent=silent, ts=time.time() if ts is None else ts)
    if not valid_hit(hit):
        raise ValueError('invalid Ghidorah occurrence')
    with fr.db as db:
        db.execute('BEGIN IMMEDIATE')
        return insert_hit(fr, hit, local=local, title=title)


def insert_hit(fr, hit, *, local, title=None):
    """Insert a validated hit together with the associated Friction occurrence."""
    db = fr.db
    existing = db.execute('SELECT fingerprint FROM ghidorah_hits WHERE uid=?', (hit['uid'],)).fetchone()
    if existing:
        return ensure_item(fr, existing['fingerprint'])
    item_id = ensure_item(fr, hit['fingerprint'], title=title)
    db.execute('INSERT INTO ghidorah_hits(uid,fingerprint,session,machine,account,engine,correction,silent,ts,local)'
               ' VALUES(?,?,?,?,?,?,?,?,?,?)', tuple(hit[key] for key in FIELDS) + (int(local),))
    note = f"{hit['machine']} / {hit['account']} / {hit['engine']}"
    if hit['correction']:
        note += ' · corrección de Emiliano'
    if hit['silent']:
        note += ' · daño silencioso'
    db.execute("INSERT INTO occurrences(item_id,ts,session_id,agent,timing,note,source,uid)"
               " VALUES(?,?,?,?,'none',?,'live',?)",
               (item_id, hit['ts'], hit['session'], hit['engine'], note, hit['uid']))
    db.execute('UPDATE items SET updated=MAX(updated,?) WHERE id=?', (hit['ts'], item_id))
    if local:
        reopened = db.execute("UPDATE items SET status='open', resolved_at=NULL, regressions=regressions+1 "
                              "WHERE id=? AND remote_key IS NULL AND status='resolved' AND resolved_at<? "
                              "AND NOT EXISTS (SELECT 1 FROM ghidorah_items WHERE item_id=? AND link IS NOT NULL)",
                              (item_id, hit['ts'], item_id))
        if reopened.rowcount:
            db.execute('UPDATE occurrences SET regression=1 WHERE uid=?', (hit['uid'],))
    if local:
        db.execute("INSERT INTO events(ts,session_id,agent,kind,tool,tool_use_id,ok,outcome)"
                   " VALUES(?,?,?,'tool',?,?,0,?)",
                   (hit['ts'], hit['session'], hit['engine'], hit['fingerprint'].split(':')[1],
                    hit['uid'], hit['fingerprint'].rsplit(':', 1)[-1]))
    return item_id
