"""Fuzz the transcript backfill: generated Claude and Codex transcripts, through the whole pipeline.

Transcripts are built line by line from the real shapes (assistant tool_use, user tool_result
with matching ids, typed prompts, Codex function_call / *_output / turn events), with junk lines,
truncated JSON and wrong-typed fields mixed in. Each one is parsed, imported into a database,
cut into clusters and excerpts, and "analysed" by a fake analyser that answers with findings
pointing at real (and sometimes bogus) event ids, which are validated and filed like the
real `claude -p` flow does."""
import json
import tempfile
import unittest
from pathlib import Path

from hypothesis import assume, event, given, strategies as st

import backfill
from fuzz.strategies import (BASE_TS, iso, json_val, maybe, mostly, short_name, temp_friction, text, tool_input,
                             tool_name, output)


@st.composite
def claude_transcript(draw):
    """-> list of JSONL lines for a Claude Code session."""
    session = draw(st.sampled_from(['sess-a', 'sess-b']))
    ts = BASE_TS
    lines, open_ids = [], []
    for n in range(draw(st.integers(2, 30))):
        ts += draw(st.floats(0, 900))
        stamp = draw(mostly(st.just(iso(ts)), st.sampled_from([None, '', 'garbage', 12])))
        base = {'sessionId': session, 'cwd': '/home/x/code/proj', 'timestamp': stamp}
        kind = draw(st.sampled_from(['call'] * 4 + ['result'] * 4 + ['prompt', 'junk', 'raw']))
        if kind == 'call':
            tid = f'toolu_{n}'
            open_ids.append(tid)
            block = {'type': 'tool_use', 'id': tid, 'name': draw(tool_name), 'input': draw(tool_input)}
            line = {**base, 'type': 'assistant', 'message': {'role': 'assistant', 'content': [block]}}
        elif kind == 'result' and open_ids:
            tid = open_ids.pop(draw(st.integers(0, len(open_ids) - 1)))
            content = draw(st.one_of(output, st.lists(st.fixed_dictionaries(
                {'type': st.just('text'), 'text': output}), max_size=2), json_val))
            block = {'type': 'tool_result', 'tool_use_id': draw(mostly(st.just(tid))), 'content': content,
                     'is_error': draw(mostly(st.booleans()))}
            line = {**base, 'type': 'user', 'message': {'role': 'user', 'content': [block]}}
        elif kind == 'prompt':
            line = {**base, 'type': 'user', 'message': {'role': 'user', 'content': draw(
                st.one_of(text, st.lists(st.fixed_dictionaries({'type': st.just('text'), 'text': text}),
                                         max_size=2)))}}
            for flag in ('isMeta', 'isSidechain', 'isCompactSummary'):
                if draw(st.integers(0, 9)) == 0:
                    line[flag] = True
        elif kind == 'junk':
            line = draw(st.dictionaries(st.sampled_from(['type', 'message', 'timestamp', 'sessionId']),
                                        maybe(st.sampled_from(['user', 'assistant'])), max_size=4))
        else:
            lines.append(draw(st.sampled_from(['', '{', '{"type": "user"', 'not json "user"', '[]',
                                               '"assistant"', 'null'])))
            continue
        raw = json.dumps(line)
        if draw(st.integers(0, 19)) == 0:
            raw = raw[:draw(st.integers(0, len(raw)))]        # a line cut off mid-write
        lines.append(raw)
    return lines


@st.composite
def codex_transcript(draw):
    ts = BASE_TS
    lines = [json.dumps({'type': 'session_meta', 'timestamp': iso(ts),
                         'payload': {'id': draw(st.sampled_from(['cx-1', 'cx-2'])), 'cwd': '/home/x/code/p'}})]
    open_ids = []
    for n in range(draw(st.integers(2, 30))):
        ts += draw(st.floats(0, 900))
        stamp = draw(mostly(st.just(iso(ts)), st.sampled_from([None, 'bad'])))
        kind = draw(st.sampled_from(['call'] * 4 + ['out'] * 4 + ['turn', 'junk']))
        if kind == 'call':
            cid = f'call_{n}'
            open_ids.append(cid)
            args = draw(tool_input)
            payload = {'type': draw(st.sampled_from(['function_call', 'custom_tool_call', 'local_shell_call'])),
                       'name': draw(st.one_of(st.sampled_from(['exec_command', 'shell', 'apply_patch']), tool_name)),
                       'call_id': cid,
                       'arguments': draw(st.one_of(st.just(json.dumps(args)), st.just(args), text))}
            line = {'type': 'response_item', 'timestamp': stamp, 'payload': payload}
        elif kind == 'out' and open_ids:
            cid = open_ids.pop(draw(st.integers(0, len(open_ids) - 1)))
            out = draw(st.one_of(
                output,
                st.builds(lambda code, body: f'Exit code: {code}\nWall time: 0.1 seconds\nOutput:\n{body}',
                          st.integers(0, 2), output),
                st.builds(lambda o, c: json.dumps({'output': o, 'metadata': {'exit_code': c}}),
                          output, st.integers(0, 2)),
                json_val))
            payload = {'type': draw(st.sampled_from(['function_call_output', 'custom_tool_call_output',
                                                     'local_shell_call_output'])),
                       'call_id': draw(mostly(st.just(cid))), 'output': out}
            line = {'type': 'response_item', 'timestamp': stamp, 'payload': payload}
        elif kind == 'turn':
            line = {'type': 'event_msg', 'timestamp': stamp, 'payload': {'type': draw(st.sampled_from(
                ['task_complete', 'turn_aborted', 'task_started', 'user_message', 'token_count']))}}
        else:
            line = draw(st.dictionaries(st.sampled_from(['type', 'payload', 'timestamp']), json_val, max_size=3))
        lines.append(json.dumps(line))
    return lines


def write(lines):
    tmp = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False, encoding='utf-8')
    tmp.write('\n'.join(lines) + '\n')
    tmp.close()
    return Path(tmp.name)


def check_events(events):
    tss = [ev['ts'] for ev in events]
    assert tss == sorted(tss), 'events not in time order'
    for ev in events:
        assert ev['ok'] in (0, 1), ev
        assert ev['duration_ms'] is None or ev['duration_ms'] >= 0, ev
        if ev.get('kind') == 'wait':
            assert ev['duration_ms'] > 0, ev


def finding_for(draw, scope, ids):
    """A finding like an analyser writes: mostly a real bad call, sometimes nonsense."""
    bad = sorted(scope.bad_ids)
    start = draw(mostly(st.sampled_from(bad), st.one_of(st.integers(-5, 500), st.none(), text), odds=4)) \
        if bad else draw(json_val)
    end = draw(mostly(st.sampled_from(sorted(ids)), st.one_of(st.none(), st.integers(-5, 500), st.booleans()),
                      odds=3)) if ids else None
    return draw(st.fixed_dictionaries(
        {'item_id': st.one_of(st.none(), st.integers(0, 5)), 'title': text, 'detail': text,
         'workaround': text, 'from_event': st.just(start), 'to_event': st.just(end), 'note': text},
        optional={'junk': json_val}))


def run_pipeline(draw, parse, lines, agent):
    path = write(lines)
    try:
        session, cwd, events = parse(path)
    finally:
        path.unlink()
    check_events(events)
    assume(session and isinstance(session, str))
    with temp_friction() as fr:
        added = backfill.import_events(fr, session, agent, cwd, path, events)
        assert added == len(events)
        assert backfill.import_events(fr, session, agent, cwd, path, events) == 0, 'not idempotent'
        for _ in range(backfill.MAX_CHUNKS + 2):
            clusters = fr.clusters(session)
            if not clusters:
                break
            text_, scope, included = backfill.excerpt_clusters(fr, session, clusters)
            assert included and isinstance(text_, str)
            backfill.build_prompt(fr, session, text_, backfill.nudged_ids(fr, session, scope))
            raw = [finding_for(draw, scope, scope.event_ids) for _ in range(draw(st.integers(0, 4)))]
            valid, dropped = backfill.validate_findings(raw)
            assert len(valid) + dropped == len(raw)
            filed = backfill.file_findings(fr, session, valid, scope)
            assert 0 <= filed <= len(valid)
            event('reached: clusters')
            if filed:
                event('reached: findings filed')
            fr.set_mark(session, included[-1]['to_event'])
            fr.db.commit()
            assert fr.clusters(session) != clusters, 'mark did not advance'
        for occ in fr.db.execute('SELECT * FROM occurrences'):
            assert occ['wasted_s'] is None or occ['wasted_s'] >= 0, dict(occ)
            if occ['from_event'] is not None and occ['to_event'] is not None:
                assert occ['from_event'] <= occ['to_event'], dict(occ)
        fr.report(days=3650)


class BackfillClaude(unittest.TestCase):
    @given(st.data(), claude_transcript())
    def test_pipeline(self, data, lines):
        run_pipeline(data.draw, backfill.parse_claude, lines, 'claude')


class BackfillCodex(unittest.TestCase):
    @given(st.data(), codex_transcript())
    def test_pipeline(self, data, lines):
        run_pipeline(data.draw, backfill.parse_codex, lines, 'codex')


class BackfillRawBytes(unittest.TestCase):
    """Unstructured bytes: invalid UTF-8, binary junk. Parsers must just skip it."""

    @given(st.lists(st.binary(max_size=200), max_size=10))
    def test_binary(self, chunks):
        with tempfile.NamedTemporaryFile('wb', suffix='.jsonl', delete=False) as fh:
            fh.write(b'\n'.join(chunks))
        path = Path(fh.name)
        try:
            for parse in (backfill.parse_claude, backfill.parse_codex):
                session, cwd, events = parse(path)
                check_events(events)
        finally:
            path.unlink()


if __name__ == '__main__':
    unittest.main()
