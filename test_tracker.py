"""Tracker sync tests against an in-process fake of the Jira Cloud REST API."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import fake_gitleaks
import tracker
from friction import Friction


def setUpModule():
    global _cleanup
    _cleanup = fake_gitleaks.install()


def tearDownModule():
    _cleanup()


class FakeJira:
    """Just enough of /rest/api/3 for tracker.Jira: issues, comments, two statuses."""

    def __init__(self):
        self.issues = {}
        self.writes = 0
        self.down = False

    def handle(self, method, path, body):
        if self.down:
            return 503, {'error': 'down'}
        parts = path.split('?')[0].split('/')[4:]          # after /rest/api/3
        if parts == ['myself']:
            return 200, {'displayName': 'Test User'}
        if parts[:1] == ['project']:
            return 200, {'key': parts[1], 'name': 'Friction'}
        if parts == ['search', 'jql']:
            label = body['jql'].split('labels = "')[1].split('"')[0]
            hits = [self.view(k) for k, i in self.issues.items() if label in i['labels']]
            return 200, {'issues': hits}
        if parts == ['issue'] and method == 'POST':
            key = f"FRIC-{len(self.issues) + 1}"
            f = body['fields']
            self.issues[key] = {'summary': f['summary'], 'description': f['description'],
                                'labels': list(f['labels']), 'done': False, 'comments': []}
            self.writes += 1
            return 201, {'key': key}
        issue = self.issues.get(parts[1]) if len(parts) > 1 else None
        if issue is None:
            return 404, {'error': 'no such issue'}
        rest = parts[2:]
        if not rest and method == 'GET':
            return 200, self.view(parts[1])
        if not rest and method == 'PUT':
            issue.update(summary=body['fields']['summary'], description=body['fields']['description'])
            for op in body.get('update', {}).get('labels', []):
                for verb, label in op.items():
                    if verb == 'add' and label not in issue['labels']:
                        issue['labels'].append(label)
                    if verb == 'remove' and label in issue['labels']:
                        issue['labels'].remove(label)
            self.writes += 1
            return 204, None
        if rest == ['transitions'] and method == 'GET':
            return 200, {'transitions': [
                {'id': '1', 'to': {'statusCategory': {'key': 'new'}}},
                {'id': '2', 'to': {'statusCategory': {'key': 'done'}}}]}
        if rest == ['transitions']:
            issue['done'] = body['transition']['id'] == '2'
            self.writes += 1
            return 204, None
        if rest == ['comment'] and method == 'GET':
            return 200, {'comments': [{'body': c} for c in issue['comments']],
                         'total': len(issue['comments'])}
        if rest == ['comment']:
            issue['comments'].append(body['body'])
            self.writes += 1
            return 201, {}
        return 404, {'error': 'unhandled ' + path}

    def view(self, key):
        i = self.issues[key]
        return {'key': key, 'fields': {
            'summary': i['summary'], 'description': i['description'], 'labels': i['labels'],
            'status': {'statusCategory': {'key': 'done' if i['done'] else 'new'}}}}


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.jira = jira = FakeJira()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def serve(self):
                size = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(size)) if size else None
                code, out = jira.handle(self.command, self.path, body)
                data = json.dumps(out).encode() if out is not None else b''
                self.send_response(code)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = serve

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        os.environ['FRICTION_JIRA_TOKEN'] = 'test-token'
        self.cfg = {'type': 'jira', 'url': f'http://127.0.0.1:{self.server.server_port}',
                    'email': 'me@example.com', 'project': 'FRIC'}
        self.a = Friction(Path(self.tmp.name) / 'a')
        self.b = Friction(Path(self.tmp.name) / 'b')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.a.db.close()
        self.b.db.close()
        self.tmp.cleanup()
        os.environ.pop('FRICTION_JIRA_TOKEN', None)

    def sync(self, fr):
        counts, errors = tracker.sync(fr, tracker.make_tracker(self.cfg))
        self.assertEqual(errors, [])
        return counts

    def test_push_creates_issue_with_hits_and_is_idempotent(self):
        item = self.a.create('gh auth token expired', 'fails mid-session', 'gh auth refresh')
        self.a.occur(item, agent='claude', project='web app', estimate_s=90, note='during release')
        counts = self.sync(self.a)
        self.assertEqual((counts['items created'], counts['hits pushed']), (1, 1))
        issue = self.jira.issues['FRIC-1']
        desc = tracker.parse_description(issue['description'])
        self.assertEqual(desc['detail'], 'fails mid-session')
        self.assertEqual(desc['workaround'], 'gh auth refresh')
        self.assertIn('1 hits', desc['footer'])
        self.assertEqual(self.a.item(item)['remote_key'], 'FRIC-1')
        writes = self.jira.writes
        self.assertEqual(sum(self.sync(self.a).values()), 0)
        self.assertEqual(self.jira.writes, writes)

    def test_two_machines_share_items_and_hits_then_settle(self):
        item = self.a.create('docker port already allocated', 'stale container', '')
        self.a.occur(item, agent='claude', project='api', estimate_s=120)
        self.sync(self.a)
        self.sync(self.b)
        theirs = self.b.search('docker port allocated')[0]
        self.assertEqual(theirs['remote_key'], 'FRIC-1')
        self.assertEqual(self.b.stats(theirs['id'])['n'], 1)
        self.b.occur(theirs['id'], agent='codex', project='infra', estimate_s=60, note='again')
        self.b.set_workaround(theirs['id'], 'docker compose down first')
        self.sync(self.b)
        self.sync(self.a)
        mine = self.a.item(item)
        self.assertEqual(self.a.stats(item)['n'], 2)
        self.assertEqual(self.a.stats(item)['wasted'], 180)
        self.assertEqual(mine['workaround'], 'docker compose down first')
        hit = self.a.db.execute("SELECT * FROM occurrences WHERE source='remote'").fetchone()
        self.assertEqual((hit['agent'], hit['project'], hit['note']), ('codex', 'infra', 'again'))
        self.sync(self.b)
        writes = self.jira.writes
        for fr in (self.a, self.b, self.a):
            self.sync(fr)
        self.assertEqual(self.jira.writes, writes, 'machines keep rewriting the issue')

    def test_status_flows_both_ways(self):
        item = self.a.create('npm install hangs', '', '')
        self.a.occur(item, estimate_s=30)
        self.sync(self.a)
        self.jira.issues['FRIC-1']['done'] = True                 # a person closes it in Jira
        self.sync(self.a)
        self.assertEqual(self.a.item(item)['status'], 'resolved')
        self.a.occur(item, estimate_s=30)                          # hit again: regression
        self.assertEqual(self.a.item(item)['status'], 'open')
        self.sync(self.a)
        self.assertFalse(self.jira.issues['FRIC-1']['done'])
        self.a.set_status(item, 'wontfix', 'not worth it')
        self.sync(self.a)
        issue = self.jira.issues['FRIC-1']
        self.assertTrue(issue['done'])
        self.assertIn(tracker.WONTFIX_LABEL, issue['labels'])
        self.assertEqual(tracker.parse_description(issue['description'])['fix'], 'not worth it')

    def test_edits_in_tracker_reach_local_items(self):
        item = self.a.create('pytest uses wrong venv', 'system python', '')
        self.a.occur(item, estimate_s=10)
        self.sync(self.a)
        issue = self.jira.issues['FRIC-1']
        issue['summary'] = 'pytest picks the system python'
        issue['description'] = tracker.adf_description('system python', 'use uv run pytest', '', 'x')
        self.sync(self.a)
        row = self.a.item(item)
        self.assertEqual((row['title'], row['workaround']),
                         ('pytest picks the system python', 'use uv run pytest'))
        self.assertEqual(self.a.search('uv run')[0]['id'], item)
        self.assertEqual(row['verified'], 0)        # edited in the tracker: not shown to agents
        self.a.db.execute('UPDATE items SET verified=1 WHERE id=?', (item,))
        self.a.db.commit()
        self.cfg['trust_workarounds'] = 'true'
        issue['description'] = tracker.adf_description('system python', 'uv run -m pytest', '', 'x')
        self.sync(self.a)
        self.assertEqual(self.a.item(item)['verified'], 1)

    def test_offline_changes_wait_and_push_later(self):
        item = self.a.create('aws sso expired', '', '')
        self.a.occur(item, estimate_s=45)
        self.jira.down = True
        with self.assertRaises(tracker.TrackerError):
            tracker.sync(self.a, tracker.make_tracker(self.cfg))
        self.assertIsNone(self.a.item(item)['remote_key'])
        self.jira.down = False
        self.assertEqual(self.sync(self.a)['items created'], 1)
        self.assertEqual(len(self.jira.issues['FRIC-1']['comments']), 1)

    def test_merge_closes_the_duplicate(self):
        a = self.a.create('gh token expired', '', '')
        b = self.a.create('github auth timeout', '', '')
        self.a.occur(a, estimate_s=10)
        self.a.occur(b, estimate_s=10)
        self.sync(self.a)
        self.a.merge(b, a)
        self.sync(self.a)
        self.assertTrue(self.jira.issues['FRIC-2']['done'])
        self.assertFalse(self.jira.issues['FRIC-1']['done'])
        self.assertEqual(len(self.jira.issues['FRIC-1']['comments']), 1)

    def test_sync_is_off_until_enabled(self):
        import argparse
        import contextlib
        import io
        path = Path(self.tmp.name) / 'config.ini'
        os.environ['FRICTION_CONFIG'] = str(path)
        self.addCleanup(os.environ.pop, 'FRICTION_CONFIG', None)

        def run(**flags):
            args = argparse.Namespace(enable=False, disable=False, check=False, dry_run=False, quiet=False)
            vars(args).update(flags)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = tracker.run(self.a, args)
            return code, out.getvalue()

        item = self.a.create('npm proxy timeout', '', '')
        self.a.occur(item, estimate_s=5)
        self.assertIsNone(tracker.load_config())                       # nothing configured: off
        self.assertIn('off', run()[1])
        self.assertEqual(run(enable=True)[0], 1)                       # first enable writes a template
        self.assertIn('enabled = false', path.read_text())
        self.assertEqual(run(enable=True)[0], 1)                       # template values: refused
        path.write_text('# my settings\n[tracker]\ntype = jira\n'
                        + ''.join(f'{k} = {v}\n' for k, v in self.cfg.items() if k != 'type'))
        self.assertIsNone(tracker.load_config())                       # complete but not enabled: off
        run()
        self.assertEqual(self.jira.writes, 0)
        self.jira.down = True
        self.assertEqual(run(enable=True)[0], 1)                       # unreachable: stays off
        self.assertIsNone(tracker.load_config())
        self.jira.down = False
        self.assertEqual(run(enable=True)[0], 0)
        self.assertIn('# my settings', path.read_text())               # the user's file is preserved
        self.assertEqual(run()[0], 0)
        self.assertEqual(len(self.jira.issues), 1)
        self.assertEqual(run(disable=True)[0], 0)
        self.assertIsNone(tracker.load_config())
        self.a.occur(item, estimate_s=5)
        writes = self.jira.writes
        run()
        self.assertEqual(self.jira.writes, writes)                     # off: nothing is sent

    def test_hit_comment_round_trips_awkward_text(self):
        occ = {'uid': 'abc123', 'ts': 1700000000.7, 'timing': 'hook', 'wasted_s': 61.9,
               'agent': 'claude', 'project': 'my app [v2] = x', 'note': 'line one\nline ] two'}
        hit = tracker.parse_hit(tracker.adf_text(tracker.adf_block(tracker.hit_text(occ))))
        self.assertEqual((hit['uid'], hit['ts'], hit['wasted'], hit['project'], hit['note']),
                         ('abc123', '1700000000', '61', 'my app [v2] = x', 'line one\nline ] two'))


class FuzzRegressions(unittest.TestCase):
    """Inputs the fuzz suite (fuzz/) found mis-parsed or crashing."""

    def occ(self, **kw):
        return {'uid': 'u1', 'ts': 1700000000, 'timing': 'hook', 'wasted_s': 5, 'agent': 'claude',
                'project': 'p', 'note': '', **kw}

    def test_note_quoting_a_hit_token(self):
        for note in ('[friction:hit ', 'see [friction:hit uid=zz ts=1] above'):
            hit = tracker.parse_hit(tracker.hit_text(self.occ(note=note)))
            self.assertEqual((hit['uid'], hit['note']), ('u1', note.strip()))

    def test_multiline_project_keeps_note_intact(self):
        hit = tracker.parse_hit(tracker.hit_text(self.occ(project='a\nb', note='n')))
        self.assertEqual(hit['note'], 'n')

    def test_malformed_remote_description(self):
        for doc in ([], 'x', {'content': '0'}, {'content': [None, 'x', 3]},
                    {'content': [{'type': 'paragraph', 'content': 5}, {'type': 'text', 'text': None}]}):
            self.assertEqual(tracker.parse_description(doc)['detail'], '')


if __name__ == '__main__':
    unittest.main()
