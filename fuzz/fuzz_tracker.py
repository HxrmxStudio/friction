"""Fuzz the issue-tracker encoding: what friction writes to Jira must read back the same, and
whatever Jira (or a person editing the issue) hands back must not break a sync."""
import unittest

from hypothesis import given, strategies as st

import tracker
from fuzz.strategies import json_val, maybe, mostly, short_name, text

# ---- ADF documents as Jira returns them -------------------------------------

adf_inline = st.one_of(
    st.fixed_dictionaries({'type': st.just('text'), 'text': mostly(text)},
                          optional={'marks': st.lists(st.fixed_dictionaries({'type': st.sampled_from(
                              ['strong', 'em', 'code', 'link'])}), max_size=2)}),
    st.just({'type': 'hardBreak'}),
    st.fixed_dictionaries({'type': st.sampled_from(['mention', 'emoji', 'inlineCard'])},
                          optional={'attrs': json_val}),
)
adf_node = st.recursive(
    st.one_of(adf_inline, st.just({'type': 'rule'})),
    lambda kids: st.one_of(
        st.fixed_dictionaries({'type': st.sampled_from(['paragraph', 'bulletList', 'orderedList',
                                                        'listItem', 'codeBlock', 'panel']),
                               'content': mostly(st.lists(kids, max_size=4))}),
        st.fixed_dictionaries({'type': st.just('heading'), 'attrs': st.just({'level': 3}),
                               'content': st.lists(st.one_of(
                                   st.sampled_from([{'type': 'text', 'text': w} for w in
                                                    ('Workaround', 'Fix', 'fix ', 'Other')]),
                                   adf_inline), max_size=2)}),
    ),
    max_leaves=15)
adf_doc = st.one_of(
    st.fixed_dictionaries({'type': st.just('doc'), 'version': st.just(1),
                           'content': mostly(st.lists(mostly(adf_node), max_size=8))}),
    st.none(), json_val)

occurrence = st.fixed_dictionaries({
    'uid': short_name, 'ts': st.floats(0, 4e9), 'timing': st.sampled_from(['hook', 'transcript', 'estimate', 'none']),
    'wasted_s': st.one_of(st.none(), st.floats(0, 1e6)),
    'agent': st.one_of(st.none(), text), 'project': st.one_of(st.none(), text), 'note': text,
})


class TrackerEncoding(unittest.TestCase):
    @given(text, text, text, text)
    def test_description_roundtrip(self, detail, workaround, fix, footer):
        doc = tracker.adf_description(detail, workaround, fix, footer or 'friction: 1 hit')
        back = tracker.parse_description(doc)
        self.assertEqual(back['detail'], detail.strip())
        self.assertEqual(back['workaround'], workaround.strip())
        self.assertEqual(back['fix'], fix.strip())

    @given(adf_doc)
    def test_parse_any_remote_description(self, doc):
        back = tracker.parse_description(doc)
        assert set(back) == {'detail', 'workaround', 'fix', 'footer'}
        assert all(isinstance(v, str) for v in back.values())

    @given(occurrence)
    def test_hit_comment_roundtrip(self, occ):
        hit = tracker.parse_hit(tracker.hit_text(occ))
        assert hit is not None
        self.assertEqual(hit['uid'], occ['uid'])
        self.assertEqual(int(hit['ts']), int(occ['ts']))
        self.assertEqual(hit['agent'], occ['agent'] or '')
        self.assertEqual(hit['project'], occ['project'] or '')
        self.assertEqual(hit['note'], occ['note'].strip())

    @given(maybe(text, st.one_of(st.none(), text)))
    def test_parse_any_comment(self, body):
        hit = tracker.parse_hit(body)
        assert hit is None or (hit['uid'] and hit['ts'].isdigit())


if __name__ == '__main__':
    unittest.main()
