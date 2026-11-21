"""Fuzz the parsing of analyser (LLM) replies: the least trustworthy input friction reads.

Replies are built like `claude -p --output-format json` returns them: structured_output, or a
`result` string holding a findings array that may be wrapped in prose, code fences, or cut off,
with findings whose fields are sometimes the wrong type."""
import json
import unittest

from hypothesis import given, strategies as st

import backfill
from fuzz.strategies import json_val, maybe, mostly, text

finding = st.fixed_dictionaries({
    'item_id': mostly(st.one_of(st.none(), st.integers(1, 50))),
    'title': mostly(text), 'detail': mostly(text), 'workaround': mostly(text), 'note': mostly(text),
    'from_event': mostly(st.integers(1, 500)),
    'to_event': mostly(st.one_of(st.none(), st.integers(1, 500))),
}, optional={'extra': json_val})
findings = st.one_of(st.lists(finding, max_size=30), st.lists(finding, min_size=81, max_size=90),
                     st.fixed_dictionaries({'findings': st.lists(finding, max_size=5)}), json_val)


@st.composite
def reply(draw):
    value = draw(findings)
    body = json.dumps(value)
    wrap = draw(st.sampled_from(['plain', 'prose', 'fence', 'cut', 'garbage']))
    if wrap == 'prose':
        body = draw(text).replace('[', '').replace(']', '') + '\nHere you go:\n' + body + '\n' + draw(text)
    elif wrap == 'fence':
        body = '```json\n' + body + '\n```'
    elif wrap == 'cut':
        body = body[:draw(st.integers(0, len(body)))]
    elif wrap == 'garbage':
        body = draw(text)
    if draw(st.booleans()):
        return {'result': body}
    return {'structured_output': value, 'result': draw(maybe(st.just(body)))}


def is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


class AnalyserReplies(unittest.TestCase):
    @given(reply())
    def test_parse_then_validate(self, r):
        try:
            raw = backfill.parse_reply(r)
        except (RuntimeError, ValueError):
            return                    # rejected cleanly: the session is retried next run
        try:
            valid, dropped = backfill.validate_findings(raw)
        except RuntimeError:
            return
        assert dropped >= 0 and len(valid) <= backfill.MAX_FINDINGS
        for f in valid:
            assert is_int(f['from_event'])
            assert f['to_event'] is None or is_int(f['to_event'])
            assert f['item_id'] is None or is_int(f['item_id'])
            assert all(isinstance(f[k], str) for k in ('title', 'detail', 'workaround', 'note'))
            assert len(f['title']) <= 200 and len(f['detail']) <= 2000
            assert f['item_id'] is not None or f['title']

    @given(st.lists(finding, max_size=backfill.MAX_FINDINGS), text, text)
    def test_prose_wrapped_array_roundtrips(self, fs, before, after):
        """A valid array survives any prose around it (that has no brackets of its own)."""
        clean = lambda s: s.replace('[', '').replace(']', '')
        r = {'result': clean(before) + json.dumps(fs) + clean(after)}
        self.assertEqual(backfill.validate_findings(backfill.parse_reply(r)),
                         backfill.validate_findings(fs))


if __name__ == '__main__':
    unittest.main()
