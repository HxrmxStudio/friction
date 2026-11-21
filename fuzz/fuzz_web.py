"""Fuzz the web view's HTTP API with structured and hostile query strings.

A real server on a random port serves a seeded database (items, a merge, occurrences with and
without timing). Requests mix valid parameters with junk; the server must answer 200 with
JSON, or 400/404, never 500 (a crash in a handler)."""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from hypothesis import given, strategies as st

import web
from friction import Friction
from fuzz.strategies import mostly, nasty_text, text

param_value = st.one_of(text, nasty_text, st.integers(-10**20, 10**20).map(str))
query = st.fixed_dictionaries({}, optional={
    'days': mostly(st.integers(-5, 400).map(str), param_value, odds=3),
    'project': mostly(st.sampled_from(['proj', 'other', '']), param_value, odds=3),
    'agent': mostly(st.sampled_from(['claude', 'codex', '']), param_value, odds=3),
    'id': mostly(st.integers(0, 6).map(str), param_value, odds=3),
})
path = st.sampled_from(['/api/report', '/api/report', '/api/item', '/api/item', '/', '/api', '/api/item/',
                        '/../etc/passwd', '/api/report;x'])


class WebApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {'FRICTION_DIR': cls.tmp.name})
        cls.env.start()
        fr = Friction(cls.tmp.name)
        fr.touch_session('s1', 'claude', '/home/x/code/proj')
        for ok in (0, 0, 1, 0, 1):
            fr.add_event('s1', 'tool', tool='Bash', ok=ok, duration_ms=1500, summary='gh pr list',
                         error=None if ok else 'HTTP 401')
        a = fr.create('gh auth expired', 'detail', 'gh auth login')
        b = fr.create('gh token stale', verified=False)
        c = fr.create('flaky dns')
        fr.occur(a, session_id='s1')
        fr.occur(b, estimate_s=90)
        fr.occur(c)
        fr.merge(b, a)
        fr.db.commit()
        fr.db.close()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.env.stop()
        cls.tmp.cleanup()

    def get(self, url):
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return resp.status, resp.headers.get('Content-Type', ''), resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            exc.close()
            return exc.code, exc.headers.get('Content-Type', ''), body

    @given(path, query, st.booleans())
    def test_never_500(self, p, q, raw):
        qs = urllib.parse.urlencode(q)
        if raw:       # hand-built: stray %, repeated keys, missing values
            qs = urllib.parse.quote('&'.join(f'{k}={v}' for k, v in q.items()), safe='=&%') + '&days&id=%zz&id=1'
        status, ctype, body = self.get(f'{self.base}{p}?{qs}')
        assert status in (200, 400, 404), (p, qs, status, body[:300])
        if status == 200 and p.startswith('/api/'):
            assert ctype.startswith('application/json')
            json.loads(body)


if __name__ == '__main__':
    unittest.main()
