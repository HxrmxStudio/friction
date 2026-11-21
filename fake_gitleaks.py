"""Test support: a stand-in for `gitleaks stdin` so the tests do not need gitleaks installed.

    fake_gitleaks.install()     # point FRICTION_GITLEAKS at a fake; returns a cleanup function

The fake reads stdin and prints gitleaks-shaped JSON for a few token shapes (GitHub tokens,
AWS access keys, anything starting SECRETVAL). FAKE_GL_MODE=timeout|error|garbage makes it
misbehave; FAKE_GL_LOG=<file> appends one line per run (to count processes).
"""
import os
import stat
import sys
import tempfile
import textwrap

SCRIPT = textwrap.dedent('''\
    import json, os, re, sys, time
    mode = os.environ.get('FAKE_GL_MODE', '')
    if os.environ.get('FAKE_GL_LOG'):
        with open(os.environ['FAKE_GL_LOG'], 'a') as f:
            f.write(' '.join(sys.argv[1:]) + '\\n')
    if sys.argv[1:2] == ['version']:
        print('8.30.1')
        sys.exit(0)
    data = sys.stdin.read()
    if mode == 'timeout':
        time.sleep(30)
    if mode == 'error':
        sys.exit(2)
    if mode == 'garbage':
        print('not json')
        sys.exit(0)
    rules = [('github-pat', r'ghp_[A-Za-z0-9]{20,}'), ('aws-access-token', r'AKIA[0-9A-Z]{16}'),
             ('generic-api-key', r'SECRETVAL[A-Za-z0-9]*')]
    out = []
    for n, line in enumerate(data.split('\\n'), 1):
        for rule, pat in rules:
            for m in re.finditer(pat, line):
                out.append({'RuleID': rule, 'StartLine': n, 'EndLine': n, 'Match': 'x=' + m.group(0),
                            'Secret': m.group(0), 'File': ''})
    print(json.dumps(out, indent=1))
''')


def install():
    d = tempfile.mkdtemp(prefix='fake-gitleaks-')
    path = os.path.join(d, 'gitleaks')
    with open(path, 'w') as f:
        f.write(f'#!{sys.executable}\n{SCRIPT}')
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    old = os.environ.get('FRICTION_GITLEAKS')
    os.environ['FRICTION_GITLEAKS'] = path

    def cleanup():
        if old is None:
            os.environ.pop('FRICTION_GITLEAKS', None)
        else:
            os.environ['FRICTION_GITLEAKS'] = old
        for name in os.listdir(d):
            os.unlink(os.path.join(d, name))
        os.rmdir(d)
    return cleanup
