"""Fuzz the helpers every layer leans on: redaction plumbing, durations, outcome and retry rules.

Redaction runs against fake_gitleaks (the repo's test stand-in, which flags ghp_…, AKIA… and
SECRETVAL… tokens). What is fuzzed is friction's side: feeding text in, mapping findings back
onto the right text in a batch, truncating, and never handing back text that was not scanned."""
import os
import unittest
from unittest import mock

from hypothesis import given, settings, strategies as st

import friction
import friction_hook
from fuzz.strategies import (COMMANDS, REAL_RUN_GITLEAKS, command, epoch, json_val, output, temp_friction,
                             text, tool_name)

ALNUM = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
secret = st.one_of(
    st.text(ALNUM, min_size=20, max_size=40).map(lambda s: 'ghp_' + s),
    st.text('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', min_size=16, max_size=16).map(lambda s: 'AKIA' + s),
    st.text(ALNUM, min_size=1, max_size=20).map(lambda s: 'SECRETVAL' + s),
)
SEP = st.sampled_from([' ', '\n', '\t', ' "', "='", ', ', '\n\n'])
# Text around a secret must not run into it (that would make a longer, different token).
filler = st.text(st.characters(blacklist_categories=('Cs',), blacklist_characters='\x00' + ALNUM + '_'),
                 max_size=40)
scanned = settings(max_examples=min(settings().max_examples, 150))   # each example runs the fake


class Redaction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):          # the real subprocess call to the fake binary, not the shortcut
        cls._patch = mock.patch.object(friction, '_run_gitleaks', REAL_RUN_GITLEAKS)
        cls._patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._patch.stop()

    def setUp(self):
        friction._SCAN_CACHE.clear()

    @scanned
    @given(st.lists(st.tuples(filler, secret, SEP, filler), min_size=1, max_size=6))
    def test_secrets_never_survive_a_batch(self, parts):
        texts = [f'{a}{sep}{sec}{sep}{b}' for a, sec, sep, b in parts]
        outs = friction.redact_many(texts, limit=100_000)
        for (_, sec, _, _), out in zip(parts, outs):
            assert out is not None
            self.assertNotIn(sec, out)

    @scanned
    @given(st.lists(st.one_of(text, json_val, secret), max_size=6), st.integers(2, 600))
    def test_batch_matches_one_by_one_and_is_bounded(self, values, limit):
        friction._SCAN_CACHE.clear()
        batch = friction.redact_many(values, limit)
        friction._SCAN_CACHE.clear()
        self.assertEqual(batch, [friction.redact(v, limit) for v in values])
        for v, out in zip(values, batch):
            if v is None:
                assert out is None
            else:
                assert isinstance(out, str) and len(out) <= limit

    @scanned
    @given(st.one_of(command, text), secret, output)
    def test_hook_stores_only_scrubbed_text(self, cmd, sec, out):
        with temp_friction() as fr, mock.patch.object(friction_hook, 'kick_sync', lambda: None):
            base = {'session_id': 's', 'tool_name': 'Bash', 'tool_use_id': 't',
                    'tool_input': {'command': f'{cmd} {sec}'}}
            friction_hook.handle({**base, 'hook_event_name': 'PreToolUse'}, fr, 'claude')
            friction_hook.handle({**base, 'hook_event_name': 'PostToolUseFailure',
                                  'error': f'{out}\n{sec}'}, fr, 'claude')
            for row in fr.db.execute('SELECT summary, error FROM events'):
                for col in ('summary', 'error'):
                    assert row[col] is None or sec not in row[col], (col, row[col])

    @given(st.one_of(text, json_val).filter(lambda v: v is not None and str(v).strip()))
    def test_no_scanner_stores_nothing(self, value):
        with mock.patch.dict(os.environ, {'FRICTION_GITLEAKS': '/nonexistent/gitleaks'}):
            self.assertIsNone(friction.redact(value))


class Durations(unittest.TestCase):
    @given(st.floats(0, 10 * 365 * 86400))
    def test_format_parses_back(self, secs):
        back = friction.parse_duration(friction.fmt_duration(secs))
        tolerance = 60 if secs >= 3600 else 0.5
        assert abs(back - secs) <= tolerance, (secs, back)

    @given(st.one_of(text, st.builds(lambda parts: ''.join(parts), st.lists(st.sampled_from(
        ['1', '2.5', 'd', 'h', 'm', 's', ' ', '-', '0', 'x', '1e3']), max_size=8))))
    def test_parse_only_raises_value_error(self, s):
        try:
            v = friction.parse_duration(s)
        except ValueError:
            return
        assert v >= 0 or v != v


class Outcomes(unittest.TestCase):
    @given(tool_name, st.one_of(command, text, st.none()), st.one_of(output, st.none()))
    def test_unexpected_outcome_vocabulary(self, tool, summary, out):
        r = friction.unexpected_outcome(tool, summary, out)
        assert r in (None, 'empty', 'not_found', 'denied', 'auth', 'timeout'), r

    @given(st.lists(st.fixed_dictionaries({
        'tool': tool_name, 'summary': st.one_of(st.sampled_from(COMMANDS), text, st.none()),
        'ok': st.sampled_from([None, 0, 1]), 'outcome': st.sampled_from([None, 'empty', 'auth']),
        'ts': st.one_of(epoch, st.none())}), max_size=12))
    def test_retry_flags_indexes(self, events):
        events.sort(key=lambda e: e['ts'] or 0)
        flags = friction.retry_flags(events)
        assert all(0 <= i < len(events) - 1 for i in flags)
        assert all(events[i]['ok'] is not None for i in flags)

    @given(st.one_of(command, text, st.none()))
    def test_self_call_is_bool(self, s):
        assert friction.is_self_call(s) in (True, False)

    @given(st.one_of(text, st.none()))
    def test_project_of(self, cwd):
        p = friction.project_of(cwd)
        assert p is None or isinstance(p, str)


if __name__ == '__main__':
    unittest.main()
