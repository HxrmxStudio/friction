"""Fuzz the agent hook: random but well-formed sequences of hook events per session.

A stateful machine sends PreToolUse/PostToolUse pairs (matching ids, sometimes missing input),
orphan posts, permission prompts, prompts, stops and session starts, plus the odd junk event,
so the retry detection, wait tracking, nudges and timing windows all get exercised.

`main()` swallows every exception so the hook never breaks an agent; that also hides bugs, so
this calls `handle()` directly: any exception here would have silently lost data in real use."""
import json
import unittest
from unittest import mock

from hypothesis import given, strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, consumes, invariant, precondition, rule

import friction_hook
from fuzz.strategies import (json_val, maybe, short_name, temp_friction, text, tool_input,
                             tool_name, tool_response, with_optional)

SESSIONS = st.sampled_from(['s1', 's2', 's3'])
AGENTS = st.sampled_from(['claude', 'codex'])
duration = maybe(st.integers(0, 10**7), st.one_of(st.none(), st.integers(), st.floats(allow_nan=False)))


class HookMachine(RuleBasedStateMachine):
    calls = Bundle('calls')

    def __init__(self):
        super().__init__()
        self._ctx = temp_friction()
        self.fr = self._ctx.__enter__()
        self._patch = mock.patch.object(friction_hook, 'kick_sync', lambda: None)
        self._patch.start()
        self.sessions = set()
        self.ids = 0

    def teardown(self):
        self._patch.stop()
        self._ctx.__exit__(None, None, None)

    def send(self, event, agent='claude'):
        out = friction_hook.handle(event, self.fr, agent)
        if out is not None:
            assert isinstance(out, dict), out
            json.dumps(out)               # main() prints it: must serialise
        if isinstance(event.get('session_id'), str) and event['session_id']:
            self.sessions.add(event['session_id'])
        return out

    @rule(target=calls, session=SESSIONS, tool=tool_name, inp=tool_input, agent=AGENTS,
          with_id=st.booleans(), extra=st.dictionaries(short_name, json_val, max_size=2))
    def pre(self, session, tool, inp, agent, with_id, extra):
        self.ids += 1
        tid = f'toolu_{self.ids}' if with_id else None
        ev = {**extra, 'hook_event_name': 'PreToolUse', 'session_id': session, 'tool_name': tool,
              'tool_input': inp, 'tool_use_id': tid, 'cwd': '/home/x/code/proj'}
        self.send(ev, agent)
        return (session, tool, inp, tid)

    @rule(call=consumes(calls), resp=tool_response, failure=st.booleans(), dur=duration,
          keep_input=st.booleans())
    def post(self, call, resp, failure, dur, keep_input):
        session, tool, inp, tid = call
        ev = {'hook_event_name': 'PostToolUseFailure' if failure else 'PostToolUse',
              'session_id': session, 'tool_name': tool, 'tool_use_id': tid, 'tool_response': resp}
        if keep_input:
            ev['tool_input'] = inp
        if failure:
            ev['error'] = resp
        if dur is not None:
            ev['duration_ms'] = dur
        self.send(ev)

    @rule(session=SESSIONS, tool=tool_name, inp=tool_input, resp=tool_response, dur=duration)
    def orphan_post(self, session, tool, inp, resp, dur):
        self.send({'hook_event_name': 'PostToolUse', 'session_id': session, 'tool_name': tool,
                   'tool_input': inp, 'tool_response': resp, 'duration_ms': dur,
                   'tool_use_id': f'orphan_{self.ids}'})

    @rule(session=SESSIONS)
    def permission(self, session):
        self.send({'hook_event_name': 'PermissionRequest', 'session_id': session})

    @rule(session=SESSIONS, prompt=text)
    def prompt(self, session, prompt):
        self.send({'hook_event_name': 'UserPromptSubmit', 'session_id': session, 'prompt': prompt})

    @rule(session=SESSIONS, active=st.booleans(), sub=st.booleans())
    def stop(self, session, active, sub):
        ev = {'hook_event_name': 'Stop', 'session_id': session, 'stop_hook_active': active}
        if sub:
            ev['agent_id'] = 'sub1'
        out = self.send(ev)
        if out is not None:
            assert out.get('decision') == 'block' and isinstance(out.get('reason'), str), out

    @rule(session=SESSIONS, source=st.sampled_from([None, 'startup', 'clear', 'resume', 'compact', 'other']))
    def session_start(self, session, source):
        ev = {'hook_event_name': 'SessionStart', 'session_id': session, 'cwd': '/home/x/code/proj'}
        if source:
            ev['source'] = source
        out = self.send(ev)
        if out is not None:
            ctx = out['hookSpecificOutput']['additionalContext']
            assert isinstance(ctx, str) and 'friction' in ctx

    @rule(ev=with_optional({'hook_event_name': maybe(st.sampled_from(
        ['PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'Stop', 'SessionStart', 'UserPromptSubmit',
         'PermissionRequest', 'Notification']))}, {
        'session_id': SESSIONS, 'thread_id': SESSIONS, 'tool_name': tool_name, 'tool_input': tool_input,
        'tool_response': tool_response, 'tool_use_id': text, 'duration_ms': duration, 'cwd': text,
        'transcript_path': text, 'agent_id': text, 'source': text, 'error': tool_response}))
    def junk_event(self, ev):
        self.send(ev)

    @precondition(lambda self: self.sessions)
    @rule(data=st.data(), title=text, est=st.one_of(st.none(), st.floats(0, 1e5)))
    def log_occurrence(self, data, title, est):
        """What `friction log` does mid-session: time a window from the recorded events."""
        session = data.draw(st.sampled_from(sorted(self.sessions)))
        item = self.fr.create(title or 'x', '', '')
        res = self.fr.occur(item, session_id=session, estimate_s=est)
        assert res['wasted_s'] is None or res['wasted_s'] >= 0, res

    @invariant()
    def events_sane(self):
        for row in self.fr.db.execute('SELECT * FROM events'):
            assert row['ok'] in (None, 0, 1), dict(row)
            if row['kind'] == 'wait' and row['duration_ms'] is not None:
                assert row['duration_ms'] >= 0, dict(row)

    @invariant()
    def core_queries_work(self):
        for session in self.sessions:
            self.fr.clusters(session)
            self.fr.unnudged_failures(session)
        self.fr.report(days=7)
        self.fr.hot()


TestHookMachine = HookMachine.TestCase


class HookOneShot(unittest.TestCase):
    """Single events, including totally unstructured ones, against an empty DB."""

    @staticmethod
    def _handle(ev):
        with temp_friction() as fr, mock.patch.object(friction_hook, 'kick_sync', lambda: None):
            out = friction_hook.handle(ev, fr, 'claude')
            if out is not None:
                json.dumps(out)

    @given(st.dictionaries(st.sampled_from(['hook_event_name', 'session_id', 'tool_name', 'tool_input',
                                            'tool_response', 'tool_use_id', 'duration_ms']), json_val))
    def test_any_shape(self, ev):
        self._handle(ev)

    @given(name=st.sampled_from(['PostToolUse', 'PostToolUseFailure']), resp=tool_response, tool=tool_name)
    def test_failed_and_summaries(self, name, resp, tool):
        bad, err = friction_hook.failed({'hook_event_name': name, 'tool_response': resp})
        assert isinstance(bad, bool)
        assert err is None or isinstance(err, str)
        s = friction_hook.summarize(tool, resp)
        assert s is None or isinstance(s, str)

    @given(text)
    def test_safe_title(self, title):
        out = friction_hook.safe_title(title)
        assert len(out) <= friction_hook.UNVERIFIED_TITLE_MAX
        assert not any(c in out for c in '`\r\n\t')


if __name__ == '__main__':
    unittest.main()
