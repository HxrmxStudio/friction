"""Shared Hypothesis profiles and structured strategies.

Pure random input rarely gets past the first `if` of a parser, so most strategies here build
the shapes the real code expects (hook events, transcript lines, tool responses, ADF docs)
and only mutate parts of them: known-good values mixed with junk, keys sometimes missing,
types sometimes wrong. That way runs reach the core logic instead of the input checks."""
import os
import string
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

from hypothesis import HealthCheck, settings, strategies as st

# ci: fixed seeds, so a push or PR fails only on a real change (and the same way every rerun).
# nightly: fresh random examples, kept in .hypothesis/ (cached by the workflow) so a failure
# found one night is replayed first the next. HYPOTHESIS_MAX_EXAMPLES overrides either.
# Friction stores no text without a secret scanner, so every layer runs with one: the repo's
# fake_gitleaks binary (so gitleaks_path() finds it), with the subprocess call itself swapped for
# the same rules run in-process (a process per scan would make stateful runs crawl). Everything
# around that call (batching, separators, replacement, truncation) is friction's real code;
# fuzz_helpers also drives the real subprocess. Same on every machine as on CI, independent of a
# real gitleaks' rules, and FRICTION_SCRUB_SYNC keeps the hook from spawning `friction.py scrub`.
import atexit
import re

import fake_gitleaks
import friction

atexit.register(fake_gitleaks.install())
os.environ['FRICTION_SCRUB_SYNC'] = '1'
FAKE_RULES = [('github-pat', re.compile(r'ghp_[A-Za-z0-9]{20,}')),
              ('aws-access-token', re.compile(r'AKIA[0-9A-Z]{16}')),
              ('generic-api-key', re.compile(r'SECRETVAL[A-Za-z0-9]*'))]
REAL_RUN_GITLEAKS = friction._run_gitleaks


def _fast_run_gitleaks(path, text, timeout):
    return [(m.group(0), rule) for rule, pat in FAKE_RULES for m in pat.finditer(text)]


friction._run_gitleaks = _fast_run_gitleaks

settings.register_profile('dev', max_examples=100, deadline=None)
settings.register_profile('ci', max_examples=300, deadline=None, derandomize=True, print_blob=True,
                          suppress_health_check=[HealthCheck.too_slow])
settings.register_profile('nightly', max_examples=5_000, deadline=None, print_blob=True,
                          suppress_health_check=[HealthCheck.too_slow])
settings.load_profile(os.environ.get('HYPOTHESIS_PROFILE', 'dev'))
if os.environ.get('HYPOTHESIS_MAX_EXAMPLES'):
    settings.register_profile('override', settings(), max_examples=int(os.environ['HYPOTHESIS_MAX_EXAMPLES']))
    settings.load_profile('override')


def named(name, strategy):
    """`strategy` with a repr of just `name()`.

    A strategy's repr normally inlines everything it is built from, so the shared ones here
    (which nest `text`, with its 5 kB sample, dozens of times) reprs run to hundreds of kB.
    Hypothesis's stateful runner rebuilds the repr of the whole rule set on every filtered rule
    draw (precondition, empty bundle), i.e. most steps: megabytes per step, and the nightly
    HookMachine run grew to 10 GB and got the CI runner killed. Draws are unchanged."""
    def draw_it(draw):
        return draw(strategy)
    draw_it.__name__ = draw_it.__qualname__ = name
    return st.composite(draw_it)()


@contextmanager
def temp_friction():
    """A Friction on a throwaway database."""
    from friction import Friction
    with tempfile.TemporaryDirectory() as tmp:
        fr = Friction(tmp)
        try:
            yield fr
        finally:
            fr.db.close()


# ---- text ----------------------------------------------------------------

# Everything except surrogates (not encodable) and NUL (never in argv or JSON from agents).
any_text = st.text(st.characters(blacklist_categories=('Cs',), blacklist_characters='\x00'), max_size=80)
nasty_text = st.sampled_from([
    '', ' ', '\n', '\r\n', '\t', '…', '‮', '日本語', '🔥' * 3, 'x' * 5000, '[REDACTED]',
    '"quoted"', "it's", '`backtick`', '$(rm -rf /)', '; DROP TABLE items; --', '%s %d {0}',
    '[friction:hit uid=1 ts=1]', 'null', '{}', '[]', 'NaN', '-1', '0', '1e309',
])
text = named('text', st.one_of(any_text, nasty_text))
short_name = st.text(string.ascii_letters + string.digits + '_-', min_size=1, max_size=12)

# ---- JSON values -----------------------------------------------------------

json_leaf = st.one_of(st.none(), st.booleans(), st.integers(-2**63, 2**63),
                      st.floats(allow_nan=False, allow_infinity=False), text)
json_val = named('json_val', st.recursive(json_leaf, lambda c: st.one_of(st.lists(c, max_size=4),
                                                       st.dictionaries(text, c, max_size=4)),
                        max_leaves=12))


def maybe(strategy, junk=json_val):
    """Usually `strategy`, sometimes a wrong-typed value."""
    return st.one_of(strategy, strategy, strategy, junk)


def mostly(strategy, junk=json_val, odds=9):
    """`strategy` about odds-in-(odds+1) times. For fields that gate deep code: a run that
    mutates several of them at once never gets past the first check."""
    return st.one_of(*[strategy] * odds, junk)


def with_optional(required, optional):
    """fixed_dictionaries where every optional key can also be missing or junk-valued."""
    return st.fixed_dictionaries(required, optional={k: maybe(v) for k, v in optional.items()})


# ---- tool calls ------------------------------------------------------------

TOOLS = ['Bash', 'Read', 'Edit', 'Write', 'Grep', 'Glob', 'WebFetch', 'Agent', 'TodoWrite',
         'AskUserQuestion', 'ExitPlanMode', 'exec_command', 'shell', 'apply_patch',
         'mcp__github__get_issue', 'request_user_input']
tool_name = st.one_of(st.sampled_from(TOOLS), short_name, st.none())

COMMANDS = ['ls', 'ls missing', 'find . -name x', 'grep -r foo .', 'rg foo', 'cat a.txt', 'which nope',
            'curl -fsS https://api.example.com', 'gh pr list', 'ssh host true', 'git push',
            'kubectl get pods', 'python3 -m unittest', 'make', 'cd ~/code/friction && ls',
            'friction log "x" --detail y', 'python3 friction.py bump 3', 'friction timeline',
            'echo hi | grep -q hi', 'ls | head', 'npm test']
command = named('command', st.one_of(st.sampled_from(COMMANDS),
                    st.builds(lambda c, n: c.replace('x', str(n)), st.sampled_from(COMMANDS),
                              st.integers(0, 9)),
                    st.builds(lambda c, t: f'{c} {t}', st.sampled_from(COMMANDS), text)))

OUTPUTS = ['', '(no output)', '(Bash completed with no output)', 'ok', 'No matches found',
           'Found 0 files', 'Error: something broke', 'Exit code 1', 'Exit code: 127\nzsh: command not found: foo',
           'zsh: command not found: foo', 'ls: cannot access \'x\': No such file or directory',
           'cat: a.txt: Permission denied', 'Permission denied (publickey).', 'HTTP 401 Unauthorized',
           'HTTP/1.1 403 Forbidden', 'Bad credentials', 'curl: (28) Operation timed out',
           'error: 404 Not Found', 'Process exited with code 2', 'context deadline exceeded',
           'token=supersecretvalue123 ghp_abcdefghijklmnopqrstuvwxyz1']
output = named('output', st.one_of(st.sampled_from(OUTPUTS), text,
                   st.builds(lambda a, b: a + '\n' + b, st.sampled_from(OUTPUTS), text)))

tool_input = named('tool_input', st.one_of(
    st.fixed_dictionaries({'command': command}, optional={'description': text, 'timeout': st.integers()}),
    st.fixed_dictionaries({'cmd': st.one_of(command, st.lists(text, max_size=4))}),
    st.fixed_dictionaries({'file_path': text}, optional={'old_string': text, 'new_string': text}),
    st.fixed_dictionaries({'pattern': text}, optional={'path': text}),
    st.fixed_dictionaries({'url': text, 'prompt': text}),
    st.dictionaries(text, json_val, max_size=3),
    text, json_val,
))

tool_response = named('tool_response', st.one_of(
    output,
    with_optional({}, {'stdout': output, 'stderr': output, 'exit_code': st.integers(-1, 255),
                       'exitCode': st.integers(-1, 255), 'is_error': st.booleans(),
                       'isError': st.booleans(), 'success': st.booleans(), 'interrupted': st.booleans(),
                       'metadata': with_optional({}, {'exit_code': st.integers(-1, 255)}),
                       'error': output, 'content': output}),
    st.lists(with_optional({'type': st.just('text')}, {'text': output}), max_size=3),
    json_val,
))

# ---- timestamps ------------------------------------------------------------

BASE_TS = 1_750_000_000.0
epoch = st.floats(BASE_TS, BASE_TS + 86400 * 3, allow_nan=False)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace('+00:00', 'Z')


iso_ts = st.one_of(epoch.map(iso), epoch.map(iso), st.sampled_from(['', 'not a date', '2025-13-40T99:99:99Z']),
                   st.none(), st.integers())
