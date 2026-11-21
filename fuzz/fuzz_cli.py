"""Fuzz the `friction` CLI: sequences of real subcommands with hostile arguments on one database.

Each run seeds a session with a few recorded tool calls (some failed), then drives log, bump,
search, show, list, workaround, verify, resolve, wontfix, reopen, merge, timeline, hot, report
and prune through `friction.main(argv)`, so items get created, merged into each other, resolved,
regressed and timed against real event windows. The only acceptable ways out are a return
code or argparse's usage exit (2); anything else is a crash an agent would see as a traceback."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from hypothesis import given, strategies as st

import friction
from fuzz.strategies import command, mostly, nasty_text, output, text

arg = st.one_of(text, nasty_text).filter(lambda s: not s.startswith('-'))   # not an option flag
item_id = mostly(st.integers(1, 6), st.sampled_from(['0', '-1', '99999', '2.5', 'x', '9' * 30]))
dur = st.one_of(st.sampled_from(['90s', '5m', '1h30m', '2d', '0', '1.5h']), arg)
event_id = mostly(st.integers(1, 12), st.integers(-3, 10**6))


def occur_opts(draw):
    opts = []
    if draw(st.booleans()):
        opts += ['--session', draw(mostly(st.sampled_from(['s1', 's2']), arg))]
    if draw(st.booleans()):
        opts += ['--from', str(draw(event_id))]
    if draw(st.booleans()):
        opts += ['--to', str(draw(event_id))]
    if draw(st.booleans()):
        opts += ['--wasted', draw(dur)]
    if draw(st.booleans()):
        opts += ['--note', draw(arg)]
    return opts


@st.composite
def cli_call(draw):
    cmd = draw(st.sampled_from(['log', 'log', 'log', 'bump', 'bump', 'search', 'show', 'list', 'workaround',
                                'verify', 'resolve', 'wontfix', 'reopen', 'merge', 'timeline', 'hot',
                                'report', 'prune']))
    i = lambda: str(draw(item_id))
    if cmd == 'log':
        argv = ['log', draw(arg)]
        for flag in ('--detail', '--workaround'):
            if draw(st.booleans()):
                argv += [flag, draw(arg)]
        if draw(st.booleans()):
            argv.append('--new')
        return argv + occur_opts(draw)
    if cmd == 'bump':
        return ['bump', i()] + occur_opts(draw)
    if cmd == 'search':
        return ['search', draw(arg)] + draw(st.lists(arg, max_size=2))
    if cmd == 'show':
        return ['show', i()] + (['--events'] if draw(st.booleans()) else [])
    if cmd == 'list':
        return ['list'] + (['--all'] if draw(st.booleans()) else [])
    if cmd == 'workaround':
        return ['workaround', i(), draw(arg)] + (['--append'] if draw(st.booleans()) else [])
    if cmd == 'resolve':
        return ['resolve', i()] + (['--fix', draw(arg)] if draw(st.booleans()) else [])
    if cmd == 'wontfix':
        return ['wontfix', i()] + (['--reason', draw(arg)] if draw(st.booleans()) else [])
    if cmd in ('verify', 'reopen'):
        return [cmd, i()]
    if cmd == 'merge':
        return ['merge', i(), i()]
    if cmd == 'timeline':
        return ['timeline', '--session', draw(mostly(st.sampled_from(['s1', 's2']), arg))]
    if cmd == 'hot':
        return ['hot', '--days', str(draw(st.integers(-5, 4000)))]
    if cmd == 'report':
        return ['report', '--days', str(draw(st.integers(-5, 4000)))] + (['--json'] if draw(st.booleans()) else [])
    return ['prune', '--days', str(draw(st.integers(-5, 4000)))]


@st.composite
def seeded_calls(draw):
    """A few finished tool calls in session s1, so `--session s1` windows have something to time."""
    calls, t = [], 1_750_000_000.0
    for _ in range(draw(st.integers(0, 10))):
        t += draw(st.floats(0, 400))
        calls.append((t, draw(command), draw(st.sampled_from([0, 1, 1])), draw(output)))
    return calls


SESSION_ENV = ('FRICTION_SESSION', 'CLAUDE_CODE_SESSION_ID', 'CODEX_SESSION_ID', 'CODEX_THREAD_ID', 'AI_AGENT')


class CliSequences(unittest.TestCase):
    @given(seeded_calls(), st.lists(cli_call(), min_size=1, max_size=12))
    def test_commands(self, seed, calls):
        clean_env = {k: v for k, v in os.environ.items() if k not in SESSION_ENV}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {**clean_env, 'FRICTION_DIR': tmp}, clear=True), \
                mock.patch.object(friction, 'kick_sync', lambda: None):
            fr = friction.Friction(tmp)
            fr.touch_session('s1', 'claude', '/home/x/code/proj', ts=seed[0][0] if seed else None)
            for ts, cmd, ok, out in seed:
                fr.add_event('s1', 'tool', tool='Bash', ok=ok, duration_ms=1000, summary=cmd,
                             error=None if ok else out, ts=ts)
            fr.db.commit()
            fr.db.close()
            for argv in calls:
                stdout = io.StringIO()
                try:
                    with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                        code = friction.main(argv)
                except SystemExit as exc:
                    assert exc.code == 2, (argv, exc.code)     # argparse rejected the arguments
                    continue
                assert code in (None, 0, 1, 3), (argv, code)
                if argv[0] == 'report' and '--json' in argv:
                    json.loads(stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
