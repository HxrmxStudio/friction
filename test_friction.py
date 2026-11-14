#!/usr/bin/env python3
"""Tests for agent-friction. Run: python3 -m unittest -v test_friction"""
import json
import tempfile
import unittest
from pathlib import Path

import backfill
import friction
import friction_hook
from friction import Friction


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fr = Friction(self.tmp.name)
        self.t = 1_000_000.0

    def tearDown(self):
        self.fr.db.close()
        self.tmp.cleanup()

    def call(self, session, ok, at, dur=1.0, tool='Bash', summary='cmd', error=None, source='hook'):
        self.fr.touch_session(session, 'claude', '/home/x/code/proj', ts=self.t + at)
        return self.fr.add_event(session, 'tool', tool=tool, ok=1 if ok else 0,
                                 duration_ms=int(dur * 1000), summary=summary, error=error,
                                 ts=self.t + at, source=source)


class Helpers(unittest.TestCase):
    def test_redact(self):
        out = friction.redact('curl -H "Authorization: Bearer abcdefghijklmnop123" x ghp_abcdefghijklmnopqrstuvwxyz1 '
                              'API_KEY=supersecretvalue https://user:hunter2@host/')
        for secret in ('abcdefghijklmnop123', 'ghp_abc', 'supersecretvalue', 'hunter2'):
            self.assertNotIn(secret, out)
        self.assertLessEqual(len(friction.redact('x' * 5000)), friction.SUMMARY_MAX)

    def test_durations(self):
        self.assertEqual(friction.parse_duration('90'), 90)
        self.assertEqual(friction.parse_duration('1h30m'), 5400)
        self.assertEqual(friction.parse_duration('2d'), 172800)
        with self.assertRaises(ValueError):
            friction.parse_duration('soon')
        self.assertEqual(friction.fmt_duration(125), '2m05s')


class Items(Base):
    def test_search_finds_reworded_item(self):
        a = self.fr.create('gh auth token expired', 'GitHub CLI needs re-auth')
        self.fr.create('npm not installed')
        hits = self.fr.search('GitHub auth timed out, token expired')
        self.assertEqual(hits[0]['id'], a)
        self.assertEqual(self.fr.search('completely unrelated zebra'), [])

    def test_window_is_current_failure_streak(self):
        self.call('s', False, 0)                 # old streak, more than STREAK_GAP_S earlier
        self.call('s', False, 2000)
        self.call('s', True, 2010)
        last = self.call('s', False, 2030)
        item = self.fr.create('x')
        res = self.fr.occur(item, session_id='s', now=self.t + 2100)
        self.assertEqual(res['timing'], 'hook')
        self.assertAlmostEqual(res['wasted_s'], 100)
        self.assertEqual((res['tool_calls'], res['failed_calls']), (3, 2))
        self.assertEqual(self.fr.session('s')['mark_event'], last)
        self.assertEqual(self.fr.unlogged_failures('s'), [])

    def test_estimate_fallback_is_flagged(self):
        item = self.fr.create('x')
        res = self.fr.occur(item, session_id='unknown', estimate_s=90, now=self.t)
        self.assertEqual((res['timing'], res['wasted_s']), ('estimate', 90))
        self.call('s', True, 0)                  # events but no failures and no --from
        self.assertEqual(self.fr.occur(item, session_id='s', estimate_s=30)['timing'], 'estimate')
        self.assertEqual(self.fr.occur(item, session_id='nope')['timing'], 'none')
        rep = self.fr.report(days=36500)
        self.assertEqual(rep['total']['estimated'], 2)
        self.assertEqual(rep['total']['wasted_estimated_s'], 120)
        self.assertEqual(rep['total']['untimed'], 1)

    def test_hook_timing_beats_estimate(self):
        self.call('s', False, 0)
        item = self.fr.create('x')
        res = self.fr.occur(item, session_id='s', estimate_s=999, now=self.t + 50)
        self.assertEqual((res['timing'], round(res['wasted_s'])), ('hook', 50))

    def test_idle_gaps_are_not_wasted_time(self):
        first = self.call('s', False, 0, dur=2)
        self.call('s', False, 30000, dur=4)       # user away for hours in between
        item = self.fr.create('x')
        res = self.fr.occur(item, session_id='s', from_event=first, now=self.t + 30010)
        self.assertEqual(round(res['wasted_s']), 12)   # 2s call + 10s after the second
        slow = self.call('t', False, 0, dur=900)       # one long hanging call does count
        res = self.fr.occur(item, session_id='t', from_event=slow, now=self.t + 905)
        self.assertEqual(round(res['wasted_s']), 905)

    def test_explicit_from_event(self):
        first = self.call('s', True, 0)
        self.call('s', True, 10)
        item = self.fr.create('x')
        res = self.fr.occur(item, session_id='s', from_event=first, now=self.t + 40)
        self.assertEqual(round(res['wasted_s']), 40)
        with self.assertRaises(ValueError):
            self.fr.occur(item, session_id='s', from_event=9999)

    def test_resolve_then_hit_is_regression(self):
        item = self.fr.create('x')
        self.fr.set_status(item, 'resolved', 'fixed it', now=self.t)
        res = self.fr.occur(item, now=self.t + 10)
        self.assertTrue(res['regression'])
        row = self.fr.item(item)
        self.assertEqual((row['status'], row['regressions'], row['fix_note']), ('open', 1, 'fixed it'))

    def test_backfilled_hit_before_fix_is_not_regression(self):
        ev = self.call('s', False, 0)
        item = self.fr.create('x')
        self.fr.set_status(item, 'resolved', now=self.t + 500)
        res = self.fr.occur(item, session_id='s', from_event=ev, to_event=ev, source='backfill')
        self.assertFalse(res['regression'])
        self.assertEqual(self.fr.item(item)['status'], 'resolved')

    def test_merge(self):
        a = self.fr.create('gh auth expired', workaround='gh auth refresh')
        b = self.fr.create('github token timeout')
        self.fr.occur(a, estimate_s=10)
        self.fr.occur(b, estimate_s=20)
        self.fr.merge(b, a)
        self.assertEqual(self.fr.stats(a)['n'], 2)
        self.assertEqual(self.fr.item(b)['id'], a)            # follows the merge
        self.assertEqual(self.fr.search('github token timeout')[0]['id'], a)
        self.fr.occur(b, estimate_s=5)                        # bumping the old id lands on a
        self.assertEqual(self.fr.stats(a)['n'], 3)

    def test_hot_skips_resolved(self):
        a, b = self.fr.create('a'), self.fr.create('b')
        self.fr.occur(a, estimate_s=10)
        self.fr.occur(b, estimate_s=500)
        self.assertEqual([r['id'] for r in self.fr.hot()], [b, a])
        self.fr.set_status(b, 'resolved')
        self.assertEqual([r['id'] for r in self.fr.hot()], [a])

    def test_prune_keeps_items(self):
        self.call('s', False, 0)
        item = self.fr.create('x')
        self.fr.occur(item, session_id='s', now=self.t + 5)
        self.assertEqual(self.fr.prune_events(days=1, now=self.t + 10 * 86400), 1)
        self.assertEqual(self.fr.stats(item)['n'], 1)


class Hook(Base):
    def ev(self, name, **kw):
        return friction_hook.handle(dict(hook_event_name=name, session_id='s', **kw), self.fr, 'claude')

    def test_tool_call_lifecycle(self):
        self.ev('PreToolUse', tool_name='Bash', tool_use_id='t1', cwd='/home/x/code/proj',
                tool_input={'command': 'gh pr list --token ghp_abcdefghijklmnopqrstuvwxyz1'})
        self.ev('PostToolUseFailure', tool_name='Bash', tool_use_id='t1', error='auth expired')
        self.ev('PreToolUse', tool_name='Read', tool_use_id='t2', tool_input={'file_path': '/a'})
        self.ev('PostToolUse', tool_name='Read', tool_use_id='t2', tool_response={'file': {}})
        rows = self.fr.timeline('s')
        self.assertEqual([r['ok'] for r in rows], [0, 1])
        self.assertEqual(rows[0]['error'], 'auth expired')
        self.assertNotIn('ghp_', rows[0]['summary'])
        self.assertIsNotNone(rows[0]['duration_ms'])

    def test_failure_detection(self):
        f = friction_hook.failed
        self.assertTrue(f({'tool_response': {'is_error': True, 'stderr': 'x'}})[0])
        self.assertTrue(f({'tool_response': {'metadata': {'exit_code': 2}, 'output': 'x'}})[0])
        self.assertTrue(f({'tool_response': 'Exit code 1\nboom'})[0])
        self.assertFalse(f({'tool_response': {'stdout': 'error: in the output only'}})[0])
        self.assertFalse(f({'tool_response': 'Script completed\nexit code 0'})[0])

    def test_stop_nudges_once_per_streak(self):
        self.assertIsNone(self.ev('Stop'))
        self.call('s', False, 0)
        self.assertIsNone(self.ev('Stop'))                    # one failure is not enough
        self.call('s', False, 1)
        out = self.ev('Stop')
        self.assertEqual(out['decision'], 'block')
        self.assertIsNone(self.ev('Stop'))                    # same streak: quiet
        self.call('s', False, 2)
        self.call('s', False, 3)
        self.assertIsNone(self.ev('Stop', stop_hook_active=True))
        self.assertIsNotNone(self.ev('Stop'))
        # a nudge does not consume the failures: logging afterwards is still hook-timed
        res = self.fr.occur(self.fr.create('x'), session_id='s', now=self.t + 10)
        self.assertEqual(res['timing'], 'hook')
        self.call('s', True, 20)
        self.assertIsNone(self.ev('Stop'))

    def test_session_start_lists_hot_items(self):
        item = self.fr.create('gh auth expired', workaround='gh auth refresh')
        self.fr.occur(item, estimate_s=60)
        ctx = self.ev('SessionStart', source='startup')['hookSpecificOutput']['additionalContext']
        self.assertIn('friction log', ctx)
        self.assertIn('gh auth refresh', ctx)


class Backfill(Base):
    def test_parse_claude_and_file(self):
        path = Path(self.tmp.name) / 'sess.jsonl'
        lines = [
            {'type': 'assistant', 'sessionId': 'b1', 'cwd': '/home/x/code/proj',
             'timestamp': '2026-11-01T00:00:00Z',
             'message': {'content': [{'type': 'tool_use', 'id': 'u1', 'name': 'Bash',
                                      'input': {'command': 'gh pr list'}}]}},
            {'type': 'user', 'sessionId': 'b1', 'timestamp': '2026-11-01T00:00:05Z',
             'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'u1', 'is_error': True,
                                      'content': 'token expired'}]}},
            {'type': 'assistant', 'sessionId': 'b1', 'timestamp': '2026-11-01T00:01:00Z',
             'message': {'content': [{'type': 'tool_use', 'id': 'u2', 'name': 'Bash',
                                      'input': {'command': 'gh pr list'}}]}},
            {'type': 'user', 'sessionId': 'b1', 'timestamp': '2026-11-01T00:01:10Z',
             'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'u2', 'is_error': True,
                                      'content': [{'type': 'text', 'text': 'token expired'}]}]}},
        ]
        path.write_text('\n'.join(json.dumps(x) for x in lines))
        session, cwd, events = backfill.parse_claude(path)
        self.assertEqual((session, len(events), events[0]['duration_ms']), ('b1', 2, 5000))
        self.assertEqual(backfill.import_events(self.fr, session, 'claude', cwd, path, events), 2)
        self.assertEqual(backfill.import_events(self.fr, session, 'claude', cwd, path, events), 0)
        text, valid = backfill.excerpt(self.fr, 'b1')
        self.assertIn('FAIL', text)
        ids = sorted(valid)
        existing = self.fr.create('gh auth token expired')
        n = backfill.file_findings(self.fr, 'b1', [
            {'item_id': existing, 'from_event': ids[0], 'to_event': ids[1], 'note': 'n'},
            {'item_id': None, 'title': 'bogus window', 'from_event': 424242, 'to_event': 1},
        ], valid)
        self.assertEqual(n, 1)
        occ = self.fr.occurrences(existing)[0]
        self.assertEqual((occ['timing'], occ['source'], round(occ['wasted_s'])), ('transcript', 'backfill', 70))


if __name__ == '__main__':
    unittest.main()
