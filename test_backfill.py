#!/usr/bin/env python3
"""Tests for backfill.py and jev.py. Run: python3 -m unittest -v test_backfill

The claude subprocess and the Jev HTTP call are always mocked."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import backfill
import jev
from friction import Friction


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fr = Friction(str(Path(self.tmp.name) / 'db'))
        self.t = 1_000_000.0
        self.env = mock.patch.dict(os.environ, {'FRICTION_CONFIG': str(Path(self.tmp.name) / 'cfg' / 'config.ini')})
        self.env.start()
        # never pick up this machine's real jevgrep credentials
        self.jg = mock.patch.object(jev, 'JEVGREP_CREDENTIALS', Path(self.tmp.name) / 'jg' / 'credentials.json')
        self.jg.start()

    def tearDown(self):
        self.jg.stop()
        self.env.stop()
        self.fr.db.close()
        self.tmp.cleanup()

    def call(self, session, ok, at, outcome=None, tool='Bash', summary='cmd', error=None,
             source='hook', dur=1.0, tuid=None):
        self.fr.touch_session(session, 'claude', '/home/x/code/proj', ts=self.t + at)
        return self.fr.add_event(session, 'tool', tool=tool, tool_use_id=tuid, ok=1 if ok else 0,
                                 duration_ms=int(dur * 1000), summary=summary, error=error,
                                 ts=self.t + at, source=source, outcome=outcome)

    def write_config(self, data):
        path = Path(self.tmp.name) / 'cfg' / 'classifier.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return path

    def write_key(self, text='apiKey=testkey123\n'):
        path = Path(self.tmp.name) / 'cfg' / 'jev.properties'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path


def finding(**kw):
    base = {'item_id': None, 'title': 'thing broke', 'detail': 'd', 'workaround': 'w',
            'from_event': 1, 'to_event': 1, 'note': 'n'}
    base.update(kw)
    return base


def transcript(path, lines):
    path.write_text('\n'.join(json.dumps(x) for x in lines))
    return path


def use(tuid, name, cmd, ts, sid='p1'):
    ts = ts + 1_000_000.0 if ts < 100_000 else ts
    return {'type': 'assistant', 'sessionId': sid, 'cwd': '/home/x/code/proj', 'timestamp': iso(ts),
            'message': {'content': [{'type': 'tool_use', 'id': tuid, 'name': name, 'input': {'command': cmd}}]}}


def result(tuid, text, ts, error=False, sid='p1'):
    ts = ts + 1_000_000.0 if ts < 100_000 else ts
    return {'type': 'user', 'sessionId': sid, 'timestamp': iso(ts),
            'message': {'content': [{'type': 'tool_result', 'tool_use_id': tuid, 'is_error': error,
                                     'content': text}]}}


class ExcerptCap(Base):
    def make_clusters(self, n, session='s'):
        for i in range(n):
            base = i * 5000                      # far apart: one cluster each
            self.call(session, False, base, error=f'boom {i}', summary=f'cmd{i}')
            self.call(session, False, base + 5, error=f'boom {i}', summary=f'cmd{i}')
            self.call(session, True, base + 10, summary='fine')
        self.fr.db.commit()

    def test_only_whole_clusters_that_fit_are_included(self):
        self.make_clusters(6)
        clusters = self.fr.clusters('s')
        self.assertEqual(len(clusters), 6)
        text, scope, included = backfill.excerpt_clusters(self.fr, 's', clusters, max_lines=12)
        self.assertTrue(0 < len(included) < 6)
        self.assertEqual(included, clusters[:len(included)])
        self.assertIn(f"[{included[-1]['from_event']}]", text)
        self.assertNotIn(clusters[len(included)]['from_event'], scope.bad_ids)

    def test_mark_advances_only_past_included_clusters(self):
        self.make_clusters(6)
        seen = []

        def fake(prompt, model):
            seen.append(prompt)
            return []
        args = SimpleNamespace(model='haiku')
        with mock.patch.object(backfill, 'EXCERPT_MAX', 12), mock.patch.object(backfill, 'MAX_CHUNKS', 1), \
                mock.patch.object(backfill, 'analyse', fake):
            filed, status, complete = backfill.process_claude(self.fr, 's', args)
        self.assertFalse(complete)
        left = self.fr.clusters('s')
        self.assertTrue(0 < len(left) < 6)           # the unanalysed rest is still pending
        self.assertEqual(len(seen), 1)
        # a later run with room finishes the session
        with mock.patch.object(backfill, 'analyse', lambda p, m: []):
            filed, status, complete = backfill.process_claude(self.fr, 's', args)
        self.assertTrue(complete)
        self.assertEqual(self.fr.clusters('s'), [])

    def test_single_oversized_cluster_still_progresses(self):
        for i in range(30):
            self.call('s', False, i * 2, summary=f'c{i}', error='e')
        self.fr.db.commit()
        text, scope, included = backfill.excerpt_clusters(self.fr, 's', max_lines=10)
        self.assertEqual(len(included), 1)
        self.assertLessEqual(len(text.splitlines()), 25)


class SoftFailures(Base):
    def test_soft_calls_in_excerpt(self):
        self.call('s', True, 0, outcome='empty', tool='Grep', summary='pattern=x', error='No matches found')
        self.call('s', False, 5, error='boom')
        self.call('s', True, 10)
        self.fr.db.commit()
        text, valid = backfill.excerpt(self.fr, 's')
        self.assertIn('SOFT(empty)', text)
        self.assertIn('No matches found', text)
        self.assertIn('FAIL', text)

    def test_parse_claude_populates_outcome(self):
        path = transcript(Path(self.tmp.name) / 'a.jsonl', [
            use('u1', 'Bash', 'curl -s https://api.example.com/x', 0),
            result('u1', 'HTTP/1.1 401 Unauthorized', 3),
            use('u2', 'Bash', 'echo hi', 10),
            result('u2', 'hi', 11),
        ])
        _, _, events = backfill.parse_claude(path)
        self.assertEqual([e['outcome'] for e in events], ['auth', None])
        self.assertIn('401', events[0]['error'])
        self.assertIsNone(events[1]['error'])

    def test_parse_claude_marks_retries(self):
        path = transcript(Path(self.tmp.name) / 'b.jsonl', [
            use('u1', 'Bash', 'ls /nonexistent-dir-thing', 0), result('u1', '', 2),
            use('u2', 'Bash', 'ls /nonexistent-dir-thing/', 5), result('u2', 'a b', 7),
        ])
        _, _, events = backfill.parse_claude(path)
        self.assertEqual(events[0]['outcome'], 'retried')
        self.assertEqual(events[0]['ok'], 1)
        self.assertIsNone(events[1]['outcome'])

    def test_parse_codex_outcome(self):
        path = transcript(Path(self.tmp.name) / 'c.jsonl', [
            {'type': 'session_meta', 'payload': {'id': 'c1', 'cwd': '/home/x/code/proj'}},
            {'type': 'response_item', 'timestamp': iso(self.t),
             'payload': {'type': 'function_call', 'name': 'shell', 'call_id': 'k1',
                         'arguments': json.dumps({'command': ['curl', '-s', 'https://x.example/y']})}},
            {'type': 'response_item', 'timestamp': iso(self.t + 2),
             'payload': {'type': 'function_call_output', 'call_id': 'k1', 'output': '403 Forbidden'}},
        ])
        _, _, events = backfill.parse_codex(path)
        self.assertEqual((events[0]['ok'], events[0]['outcome']), (1, 'auth'))

    def test_import_passes_outcome_and_candidates_count_soft(self):
        path = transcript(Path(self.tmp.name) / 'd.jsonl', [
            use('u1', 'Bash', 'curl -s https://api.example.com/x', 0), result('u1', '401 Unauthorized', 2),
            use('u2', 'Bash', 'curl -s https://api.example.com/y', 5), result('u2', '401 Unauthorized', 7),
        ])
        session, cwd, events = backfill.parse_claude(path)
        backfill.import_events(self.fr, session, 'claude', cwd, path, events)
        rows = self.fr.db.execute('SELECT ok, outcome FROM events ORDER BY id').fetchall()
        self.assertEqual([(r['ok'], r['outcome']) for r in rows], [(1, 'auth'), (1, 'auth')])
        self.assertEqual(len(self.fr.unlogged_failures('p1')), 2)


class Parsing(Base):
    def test_validate_findings_drops_invalid(self):
        good = finding()
        raw = [good, 'junk', finding(from_event='3'), finding(from_event=True), finding(item_id='7'),
               finding(to_event=2.5), finding(title=None, item_id=None), finding(detail=5),
               finding(item_id=4, title='')]
        valid, dropped = backfill.validate_findings(raw)
        self.assertEqual(len(valid), 2)       # the good one and the item_id=4 one without a title
        self.assertEqual(dropped, 7)
        self.assertEqual(valid[0]['to_event'], 1)

    def test_validate_findings_accepts_object_wrapper_and_rejects_scalars(self):
        valid, dropped = backfill.validate_findings({'findings': [finding()]})
        self.assertEqual((len(valid), dropped), (1, 0))
        with self.assertRaises(RuntimeError):
            backfill.validate_findings('nope')

    def run_analyse(self, reply):
        proc = SimpleNamespace(returncode=0, stdout=json.dumps(reply), stderr='')
        with mock.patch('backfill.subprocess.run', return_value=proc) as run, \
                mock.patch.object(backfill.Path, 'mkdir'), redirect_stdout(io.StringIO()) as out:
            found = backfill.analyse('prompt', 'haiku')
        return found, run, out.getvalue()

    def test_analyse_uses_schema_and_structured_output(self):
        found, run, out = self.run_analyse({'structured_output': {'findings': [finding(), {'bad': 1}]}})
        argv = run.call_args[0][0]
        self.assertIn('--json-schema', argv)
        self.assertEqual(len(found), 1)
        self.assertIn('dropped 1', out)

    def test_analyse_falls_back_to_text(self):
        text = 'Here you go: ' + json.dumps([finding()]) + ' done'
        found, _, _ = self.run_analyse({'result': text})
        self.assertEqual(len(found), 1)
        found, _, _ = self.run_analyse({'result': json.dumps({'findings': [finding()]})})
        self.assertEqual(len(found), 1)
        with self.assertRaises(RuntimeError):
            self.run_analyse({'result': 'no array here'})


class Windows(Base):
    def setup_cluster(self):
        a = self.call('s', False, 0, error='x')
        b = self.call('s', False, 5, error='x')
        c = self.call('s', True, 10)             # resolution call
        d = self.call('s', True, 20)
        self.call('s', False, 9000, error='y')    # a later, separate cluster
        self.fr.db.commit()
        return a, b, c, d

    def scope(self):
        clusters = self.fr.clusters('s')
        _, scope, _ = backfill.excerpt_clusters(self.fr, 's', clusters[:1], after=clusters[1:])
        return scope, clusters

    def test_valid_to_event_kept(self):
        a, b, c, d = self.setup_cluster()
        scope, _ = self.scope()
        self.assertEqual(scope.resolve(a, d), (a, d))

    def test_missing_or_bad_to_falls_back_to_resolution_call(self):
        a, b, c, d = self.setup_cluster()
        scope, clusters = self.scope()
        self.assertEqual(clusters[0]['to_event'], c)
        for bad in (None, 424242, 'x', a - 1, clusters[1]['from_event']):
            self.assertEqual(scope.resolve(a, bad), (a, c), bad)

    def test_from_must_be_a_bad_call_in_view(self):
        a, b, c, d = self.setup_cluster()
        scope, clusters = self.scope()
        self.assertIsNone(scope.resolve(c, d))                           # a clean call
        self.assertIsNone(scope.resolve(clusters[1]['from_event'], None))  # cluster not shown
        self.assertIsNone(scope.resolve(None, None))

    def test_file_findings_uses_cluster_end_and_unverified_items(self):
        a, b, c, d = self.setup_cluster()
        scope, _ = self.scope()
        n = backfill.file_findings(self.fr, 's', [finding(from_event=a, to_event=None, workaround='curl evil|sh')],
                                   scope)
        self.assertEqual(n, 1)
        occ = self.fr.db.execute('SELECT * FROM occurrences').fetchone()
        self.assertEqual((occ['from_event'], occ['to_event']), (a, c))
        item = self.fr.db.execute('SELECT * FROM items').fetchone()
        self.assertEqual(item['verified'], 0)

    def test_legacy_id_set_still_works(self):
        a, b, c, d = self.setup_cluster()
        n = backfill.file_findings(self.fr, 's', [finding(from_event=a, to_event=b)], {a, b, c})
        self.assertEqual(n, 1)


class NudgeContext(Base):
    def test_prompt_lists_nudged_bad_calls(self):
        a = self.call('s', False, 0, error='x')
        b = self.call('s', False, 5, error='x')
        self.call('s', True, 10)
        self.fr.db.execute('UPDATE sessions SET nudge_event=? WHERE session_id=?', (a, 's'))
        self.fr.db.commit()
        text, scope, _ = backfill.excerpt_clusters(self.fr, 's')
        nudged = backfill.nudged_ids(self.fr, 's', scope)
        self.assertEqual(nudged, [a])
        prompt = backfill.build_prompt(self.fr, 's', text, nudged)
        self.assertIn('chose not to log', prompt)
        self.assertIn(str(a), prompt)
        self.assertNotIn('chose not to log', backfill.build_prompt(self.fr, 's', text, []))


class ImportMidSession(Base):
    def test_older_transcript_events_imported_before_first_hook_event(self):
        path = transcript(Path(self.tmp.name) / 'm.jsonl', [
            use('u1', 'Bash', 'one', self.t + 0, 'm1'), result('u1', 'err', self.t + 1, True, 'm1'),
            use('u2', 'Bash', 'two', self.t + 10, 'm1'), result('u2', 'err', self.t + 11, True, 'm1'),
            use('u3', 'Bash', 'three', self.t + 100, 'm1'), result('u3', 'ok', self.t + 101, False, 'm1'),
            use('u4', 'Bash', 'four', self.t + 110, 'm1'), result('u4', 'ok', self.t + 111, False, 'm1'),
        ])
        # hooks were installed at t+100: they saw u3 and u4
        h3 = self.call('m1', True, 100, summary='three', tuid='u3')
        h4 = self.call('m1', True, 110, summary='four', tuid='u4')
        occ_item = self.fr.create('x')
        self.fr.db.commit()
        session, cwd, events = backfill.parse_claude(path)
        self.assertEqual(backfill.import_events(self.fr, session, 'claude', cwd, path, events), 2)
        self.assertEqual(backfill.import_events(self.fr, session, 'claude', cwd, path, events), 0)
        rows = self.fr.db.execute('SELECT id, tool_use_id, source FROM events WHERE session_id=? ORDER BY id',
                                  ('m1',)).fetchall()
        self.assertEqual([r['tool_use_id'] for r in rows], ['u1', 'u2', 'u3', 'u4'])   # ids in time order
        self.assertEqual([r['source'] for r in rows], ['transcript', 'transcript', 'hook', 'hook'])
        self.assertEqual(len(self.fr.unlogged_failures('m1')), 2)

    def test_renumber_remaps_occurrences_and_marks(self):
        h = self.call('m2', False, 100, tuid='u9')
        h2 = self.call('m2', True, 110, tuid='u10')
        item = self.fr.create('x')
        self.fr.occur(item, session_id='m2', from_event=h, to_event=h2, source='backfill')
        self.fr.set_mark('m2', h2)
        self.fr.db.commit()
        path = transcript(Path(self.tmp.name) / 'n.jsonl', [
            use('u1', 'Bash', 'old', self.t + 0, 'm2'), result('u1', 'err', self.t + 1, True, 'm2')])
        session, cwd, events = backfill.parse_claude(path)
        backfill.import_events(self.fr, session, 'claude', cwd, path, events)
        occ = self.fr.db.execute('SELECT * FROM occurrences').fetchone()
        ids = {r['id']: r['tool_use_id'] for r in self.fr.db.execute('SELECT id, tool_use_id FROM events')}
        self.assertEqual((ids[occ['from_event']], ids[occ['to_event']]), ('u9', 'u10'))
        self.assertEqual(ids[self.fr.session('m2')['mark_event']], 'u10')


class JevUnit(Base):
    def questions(self):
        return {'q': ('pick one', {'a': 'option a', 'b': 'option b'})}

    def answer(self, choice='a', conf=0.9, probs=None):
        probs = probs or {'a': 0.9, 'b': 0.1}
        return {'answers': {'q': {'type': 'choice', 'choice': choice, 'probabilities': probs,
                                  'confidence': conf}}, 'usage': {'input_tokens': 5, 'output_tokens': 1,
                                                                  'junk': 'x'}, 'model': 'jev-1.13.0'}

    def test_build_request_shape(self):
        req = jev.build_request(self.questions(), {'s': 1})
        self.assertEqual(req, {'model': 'jev-1.13.0', 'state': {'s': 1}, 'questions': {
            'q': {'type': 'choice', 'instructions': 'pick one', 'criteria': {'a': 'option a', 'b': 'option b'}}}})

    def test_build_request_limits(self):
        with self.assertRaises(jev.InvalidRequest):
            jev.build_request({'q': ('i', {'a': 'only one'})}, {})
        with self.assertRaises(jev.InvalidRequest):
            jev.build_request({'q': ('i', {str(n): 'd' for n in range(256)})}, {})
        with self.assertRaises(jev.InvalidRequest):
            jev.build_request({'q': ('i', {'a': 'x' * 1001, 'b': 'y'})}, {})
        jev.build_request({'q': ('i', {str(n): 'd' for n in range(255)})}, {})

    def test_validate_answer_accepts_good(self):
        req = jev.build_request(self.questions(), {})
        ans = jev.validate_answer(req, self.answer(), 'q')
        self.assertEqual((ans.choice, ans.confidence, ans.usage), ('a', 0.9, {'input_tokens': 5, 'output_tokens': 1}))

    def test_validate_answer_rejects_bad(self):
        req = jev.build_request(self.questions(), {})
        cases = [
            {}, {'answers': []}, {'answers': {}},
            self.answer(choice='z'),
            self.answer(probs={'a': 0.9}),
            self.answer(probs={'a': 0.6, 'b': 0.1}),                 # sums to 0.7
            self.answer(choice='b', probs={'a': 0.9, 'b': 0.1}),     # choice is not the most probable
            self.answer(conf=1.5), self.answer(conf='high'),
            self.answer(probs={'a': True, 'b': 0.0}),
        ]
        bad_type = self.answer()
        bad_type['answers']['q']['type'] = 'text'
        cases.append(bad_type)
        for data in cases:
            with self.assertRaises(jev.InvalidAnswer, msg=str(data)[:80]):
                jev.validate_answer(req, data, 'q')

    def test_load_config_defaults_and_invalid(self):
        self.assertEqual(jev.load_config()['classifier'], 'claude')
        self.write_config('not json{')
        cfg = jev.load_config()
        self.assertEqual(cfg['classifier'], 'claude')
        self.assertTrue(cfg['problem'])
        self.write_config({'classifier': 'jev', 'jev': 'oops'})
        self.assertEqual(jev.load_config()['classifier'], 'claude')    # invalid config never enables Jev
        self.write_config({'classifier': 'jev', 'jev': {'key_file': '/k', 'model': 'm', 'min_confidence': 0.5,
                                                          'questions_per_request': 2}})
        cfg = jev.load_config()
        self.assertEqual((cfg['classifier'], cfg['key_file'], cfg['model'], cfg['min_confidence'],
                          cfg['questions_per_request']), ('jev', '/k', 'm', 0.5, 2))
        self.write_config({'classifier': 'jev', 'jev': {'min_confidence': 7, 'questions_per_request': 0}})
        cfg = jev.load_config()
        self.assertEqual((cfg['min_confidence'], cfg['questions_per_request']), (0.75, 2))

    def test_read_key_formats_and_errors(self):
        self.assertEqual(jev.read_key(self.write_key('apiKey=abc123\n')), 'abc123')
        self.assertEqual(jev.read_key(self.write_key('# comment\nabc456\n')), 'abc456')
        for bad in ('', 'apiKey=PASTE_API_KEY_HERE', 'apiKey=has space'):
            with self.assertRaises(jev.JevError) as cm:
                jev.read_key(self.write_key(bad))
            self.assertNotIn('space', str(cm.exception))
        with self.assertRaises(jev.JevError):
            jev.read_key(Path(self.tmp.name) / 'missing')
        self.assertEqual(jev.read_key(self.write_key('{"provider": "typesafe", "apiKey": "ts_1"}')), 'ts_1')
        with self.assertRaises(jev.JevError):
            jev.read_key(self.write_key('{"provider": "openai", "apiKey": "sk_1"}'))

    def test_falls_back_to_jevgrep_credentials(self):
        self.assertNotEqual(jev.default_key_path(), jev.JEVGREP_CREDENTIALS)
        jev.JEVGREP_CREDENTIALS.parent.mkdir(parents=True)
        jev.JEVGREP_CREDENTIALS.write_text('{"provider": "typesafe", "apiKey": "ts_jg"}')
        self.assertEqual(jev.read_key(jev.default_key_path()), 'ts_jg')

    def test_evaluator_sends_bearer_and_hides_secrets(self):
        keyfile = self.write_key('apiKey=secret-key-9\n')
        seen = {}

        class Resp:
            status = 200

            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                return self.body[:n]

        def opener(req, timeout):
            seen.update(url=req.full_url, auth=req.get_header('Authorization'), timeout=timeout,
                        body=json.loads(req.data))
            return Resp(b'{"ok": 1}')
        out = jev.evaluator(keyfile, opener=opener)({'model': 'm'})
        self.assertEqual(out, {'ok': 1})
        self.assertEqual((seen['url'], seen['auth'], seen['timeout']),
                         ('https://api.typesafe.ai/v1/systemone', 'Bearer secret-key-9', 12.0))
        for body, msg in ((b'x' * (jev.MAX_RESPONSE_BYTES + 5), 'too large'), (b'not json', 'not JSON')):
            with self.assertRaises(jev.JevError) as cm:
                jev.evaluator(keyfile, opener=lambda r, timeout, b=body: Resp(b))({})
            self.assertIn(msg, str(cm.exception))
        def boom(req, timeout):
            raise OSError('connection to secret-key-9 failed')
        with self.assertRaises(jev.JevError) as cm:
            jev.evaluator(keyfile, opener=boom)({})
        self.assertNotIn('secret-key-9', str(cm.exception))

    def test_ask_batches_per_request(self):
        qs = {'a': ('i', {'x': 'x', 'y': 'y'}), 'b': ('i', {'x': 'x', 'y': 'y'}), 'c': ('i', {'x': 'x', 'y': 'y'})}
        calls = []

        def evaluate(req):
            calls.append(sorted(req['questions']))
            return {'answers': {q: {'type': 'choice', 'choice': 'x', 'probabilities': {'x': 1.0, 'y': 0.0},
                                    'confidence': 0.9} for q in req['questions']}}
        out = jev.ask(evaluate, qs, {}, per_request=1)
        self.assertEqual(len(calls), 3)
        calls.clear()
        out = jev.ask(evaluate, qs, {}, per_request=2)
        self.assertEqual(calls, [['a', 'b'], ['c']])
        self.assertTrue(all(a.choice == 'x' for a in out.values()))


class FakeJev:
    """Evaluator that answers by question id from a script."""

    def __init__(self, kind=('friction', 0.95), match=('new', 0.9)):
        self.kind, self.match, self.requests = kind, match, []

    def __call__(self, request):
        self.requests.append(request)
        answers = {}
        for qid, q in request['questions'].items():
            choice, conf = self.kind if qid == 'kind' else self.match
            crit = q['criteria']
            if choice not in crit:
                choice = next(iter(crit))
            others = [c for c in crit if c != choice]
            probs = {choice: 1.0}
            probs.update({c: 0.0 for c in others})
            answers[qid] = {'type': 'choice', 'choice': choice, 'probabilities': probs, 'confidence': conf}
        return {'answers': answers}


class Hybrid(Base):
    def setup_session(self):
        self.call('s', False, 0, error='token expired')
        self.call('s', False, 5, error='token expired')
        self.call('s', True, 10)
        self.fr.db.commit()

    def hybrid(self, fake, **cfg):
        base = {'model': 'jev-1.13.0', 'min_confidence': 0.75, 'questions_per_request': 1}
        base.update(cfg)
        return {'evaluate': fake, 'cfg': base}

    def args(self):
        return SimpleNamespace(model='haiku')

    def test_matched_item_gets_occurrence_without_claude(self):
        self.setup_session()
        item = self.fr.create('gh auth token expired', 'gh needs re-auth')
        fake = FakeJev(match=(str(item), 0.9))
        with mock.patch.object(backfill, 'analyse', side_effect=AssertionError('claude called')):
            filed, status, complete = backfill.process_hybrid(self.fr, 's', self.args(), self.hybrid(fake))
        self.assertEqual((filed, complete), (1, True))
        occ = self.fr.occurrences(item)[0]
        self.assertEqual(occ['source'], 'backfill')
        self.assertEqual(self.fr.clusters('s'), [])
        # one question per request by default, match only after kind
        self.assertEqual([sorted(r['questions']) for r in fake.requests], [['kind'], ['match']])
        crit = fake.requests[1]['questions']['match']['criteria']
        self.assertEqual(set(crit), {'new', str(item)})
        self.assertEqual(set(fake.requests[0]['questions']['kind']['criteria']),
                         {'friction', 'task_iteration', 'probe', 'noise'})

    def test_new_problem_written_up_by_claude_unverified(self):
        self.setup_session()
        self.fr.create('something else')
        fake = FakeJev(match=('new', 0.9))
        a = self.fr.clusters('s')[0]['from_event']

        def analyse(prompt, model):
            self.assertIn('matches no existing item', prompt)
            return [finding(from_event=a, to_event=None, title='gh token expired', workaround='gh auth login')]
        with mock.patch.object(backfill, 'analyse', analyse):
            filed, status, complete = backfill.process_hybrid(self.fr, 's', self.args(), self.hybrid(fake))
        self.assertEqual(filed, 1)
        row = self.fr.db.execute("SELECT * FROM items WHERE title='gh token expired'").fetchone()
        self.assertEqual(row['verified'], 0)

    def test_no_items_means_new_without_match_question(self):
        self.setup_session()
        fake = FakeJev()
        a = self.fr.clusters('s')[0]['from_event']
        with mock.patch.object(backfill, 'analyse', lambda p, m: [finding(from_event=a, to_event=a)]):
            filed, _, _ = backfill.process_hybrid(self.fr, 's', self.args(), self.hybrid(fake))
        self.assertEqual(filed, 1)
        self.assertEqual([sorted(r['questions']) for r in fake.requests], [['kind']])

    def test_low_confidence_marks_processed_as_uncertain(self):
        self.setup_session()
        fake = FakeJev(kind=('friction', 0.5))
        with mock.patch.object(backfill, 'analyse', side_effect=AssertionError('claude called')):
            filed, status, complete = backfill.process_hybrid(self.fr, 's', self.args(), self.hybrid(fake))
        self.assertEqual((filed, complete), (0, True))
        self.assertIn('uncertain=1', status)
        self.assertEqual(self.fr.clusters('s'), [])           # marked, so not re-sent forever

    def test_low_match_confidence_uncertain(self):
        self.setup_session()
        item = self.fr.create('gh auth token expired')
        fake = FakeJev(match=(str(item), 0.4))
        filed, status, _ = backfill.process_hybrid(self.fr, 's', self.args(), self.hybrid(fake))
        self.assertEqual(filed, 0)
        self.assertIn('uncertain=1', status)

    def test_non_friction_kinds_skipped(self):
        self.setup_session()
        for kind in ('task_iteration', 'probe', 'noise'):
            self.fr.db.execute('UPDATE sessions SET mark_event=0')
            filed, status, _ = backfill.process_hybrid(self.fr, 's', self.args(), self.hybrid(FakeJev(kind=(kind, 0.99))))
            self.assertEqual(filed, 0)
            self.assertIn('skipped=1', status)

    def test_two_questions_per_request(self):
        self.setup_session()
        item = self.fr.create('gh auth token expired')
        fake = FakeJev(match=(str(item), 0.9))
        filed, _, _ = backfill.process_hybrid(self.fr, 's', self.args(), self.hybrid(fake, questions_per_request=2))
        self.assertEqual(filed, 1)
        self.assertEqual([sorted(r['questions']) for r in fake.requests], [['kind', 'match']])

    def test_invalid_jev_answer_counts_uncertain(self):
        self.setup_session()
        filed, status, _ = backfill.process_hybrid(self.fr, 's', self.args(), self.hybrid(lambda req: {'answers': {}}))
        self.assertEqual(filed, 0)
        self.assertIn('uncertain=1', status)

    def test_item_list_capped(self):
        self.setup_session()
        for i in range(300):
            self.fr.create(f'item {i}')
        fake = FakeJev(match=('new', 0.9))
        a = self.fr.clusters('s')[0]['from_event']
        with mock.patch.object(backfill, 'analyse', lambda p, m: [finding(from_event=a, to_event=a)]):
            backfill.process_hybrid(self.fr, 's', self.args(), self.hybrid(fake))
        self.assertLessEqual(len(fake.requests[1]['questions']['match']['criteria']), 255)


class Classifier(Base):
    def args(self, **kw):
        base = dict(classifier=None, jev_check=False, since='1d', dry_run=True, limit=5, min_failures=2,
                    model='haiku', redo=False)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_default_is_claude_and_no_jev_without_config(self):
        cfg = jev.load_config()
        self.assertEqual(backfill.resolve_classifier(self.args(), cfg), 'claude')

    def test_jev_without_key_errors_clearly(self):
        with mock.patch.object(backfill, 'transcripts', side_effect=AssertionError('read transcripts')):
            out = io.StringIO()
            with redirect_stdout(out):
                code = backfill.run(self.fr, self.args(classifier='jev'))
        self.assertEqual(code, 2)
        self.assertIn('Jev classifier selected but not usable', out.getvalue())

    def test_config_selects_jev_and_cli_overrides(self):
        self.write_key()
        self.write_config({'classifier': 'jev'})
        cfg = jev.load_config()
        self.assertEqual(cfg['classifier'], 'jev')
        self.assertEqual(backfill.resolve_classifier(self.args(), cfg), 'jev')
        self.assertEqual(backfill.resolve_classifier(self.args(classifier='claude'), cfg), 'claude')

    def test_invalid_config_falls_back_to_claude(self):
        self.write_config('[1,2')
        self.assertEqual(backfill.resolve_classifier(self.args(), jev.load_config()), 'claude')

    def test_claude_flow_never_builds_jev_evaluator(self):
        with mock.patch.object(jev, 'evaluator', side_effect=AssertionError('jev touched')), \
                mock.patch.object(backfill, 'transcripts', return_value=[]), redirect_stdout(io.StringIO()):
            self.assertEqual(backfill.run(self.fr, self.args()), 0)

    def test_run_hybrid_end_to_end_with_fake_evaluator(self):
        self.write_key()
        self.write_config({'classifier': 'jev'})
        now = 2_000_000.0
        tpath = transcript(Path(self.tmp.name) / 'e.jsonl', [
            use('u1', 'Bash', 'gh pr list', 0, 'e1'), result('u1', 'token expired', 2, True, 'e1'),
            use('u2', 'Bash', 'gh pr view', 5, 'e1'), result('u2', 'token expired', 7, True, 'e1'),
            use('u3', 'Bash', 'echo done', 20, 'e1'), result('u3', 'done', 21, False, 'e1')])
        item = self.fr.create('gh auth token expired')
        fake = FakeJev(match=(str(item), 0.95))
        args = self.args(dry_run=False)
        with mock.patch.object(backfill, 'transcripts', return_value=[(tpath, 'claude', 1234.0)]), \
                mock.patch.object(backfill.time, 'time', return_value=now), \
                mock.patch.object(jev, 'evaluator', return_value=fake), redirect_stdout(io.StringIO()):
            self.assertEqual(backfill.run(self.fr, args), 0)
        self.assertEqual(len(self.fr.occurrences(item)), 1)
        row = self.fr.db.execute('SELECT * FROM backfill_sessions').fetchone()
        self.assertTrue(row['status'].startswith('jev'))
        self.assertEqual(row['mtime'], 1234.0)

    def test_jev_check_sends_only_synthetic_request(self):
        self.write_key()
        self.write_config({'classifier': 'claude'})
        sent = []

        def evaluate(req):
            sent.append(req)
            return {'answers': {'check': {'type': 'choice', 'choice': 'blue',
                                          'probabilities': {'blue': 0.97, 'green': 0.03}, 'confidence': 0.97}}}
        out = io.StringIO()
        with mock.patch.object(jev, 'evaluator', return_value=evaluate), redirect_stdout(out):
            code = backfill.run(self.fr, self.args(jev_check=True))
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['state'], {'purpose': 'connectivity check'})
        self.assertIn('Jev OK', out.getvalue())

    def test_jev_check_failure_reported(self):
        self.write_key()

        def evaluate(req):
            raise jev.JevError('Jev HTTP 401')
        out = io.StringIO()
        with mock.patch.object(jev, 'evaluator', return_value=evaluate), redirect_stdout(out):
            self.assertEqual(backfill.run(self.fr, self.args(jev_check=True)), 1)
        self.assertIn('Jev HTTP 401', out.getvalue())


if __name__ == '__main__':
    unittest.main()
