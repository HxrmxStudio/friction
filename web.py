#!/usr/bin/env python3
"""Read-only web view of the friction log: heatmap, totals, drill-down.

  python3 web.py --port 8798      then open http://127.0.0.1:8798

Serves web.html plus two JSON endpoints. It never writes to the database.
"""
import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from friction import Friction  # noqa: E402


def _cols(fr, table):
    return {r[1] for r in fr.db.execute(f'PRAGMA table_info({table})')}


def annotate_report(fr, rep, days, project, agent):
    """Add verified flags and hard/soft bad-call counts per item (columns may not exist yet)."""
    ids = [it['id'] for it in rep['items']]
    has_ver = 'verified' in _cols(fr, 'items')
    has_out = 'outcome' in _cols(fr, 'events')
    ver = {}
    if has_ver and ids:
        ver = {r[0]: r[1] for r in fr.db.execute(
            f"SELECT id, verified FROM items WHERE id IN ({','.join('?' * len(ids))})", ids)}
    bad = {}
    if has_out and ids:
        where, args = ['o.ts>=?'], [time.time() - days * 86400]
        for col, val in (('project', project), ('agent', agent)):
            if val:
                where.append(f'o.{col}=?')
                args.append(val)
        for r in fr.db.execute(
                "SELECT o.item_id, COALESCE(SUM(e.ok=0), 0) hard,"
                " COALESCE(SUM(e.ok=1 AND e.outcome IS NOT NULL), 0) soft"
                ' FROM occurrences o JOIN events e ON e.session_id=o.session_id'
                ' AND e.id>=o.from_event AND e.id<=COALESCE(o.to_event, o.from_event)'
                " AND e.kind='tool' WHERE o.from_event IS NOT NULL AND " + ' AND '.join(where)
                + ' GROUP BY o.item_id', args):
            bad[r[0]] = (r[1], r[2])
    for it in rep['items']:
        it['verified'] = bool(ver.get(it['id'], 1))
        it['hard_calls'], it['soft_calls'] = bad.get(it['id'], (0, 0))
        if not it['verified']:
            it['unverified_workaround'] = it.pop('workaround', '')
            it['workaround'] = ''
    return rep


def item_payload(fr, item_id):
    item = fr.item(item_id)
    occs = []
    for occ in fr.occurrences(item['id'], 100):
        row = dict(occ)
        row['events'] = [dict(ev) for ev in fr.evidence(occ)][:60]
        occs.append(row)
    item = dict(item)
    item.setdefault('verified', 1)
    return {'item': item, 'stats': dict(fr.stats(item['id'])), 'occurrences': occs}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == '/':
                return self.send(200, (HERE / 'web.html').read_bytes(), 'text/html; charset=utf-8')
            fr = Friction()
            if url.path == '/api/report':
                days = max(1, min(366, int(q.get('days', 14))))
                rep = fr.report(days=days, project=q.get('project') or None,
                                agent=q.get('agent') or None)
                rep = annotate_report(fr, rep, days, q.get('project') or None, q.get('agent') or None)
                return self.send(200, json.dumps(rep), 'application/json')
            if url.path == '/api/item':
                return self.send(200, json.dumps(item_payload(fr, int(q['id']))), 'application/json')
            self.send(404, 'not found', 'text/plain')
        except (KeyError, ValueError, OverflowError) as exc:    # OverflowError: id past SQLite's range
            self.send(400, str(exc), 'text/plain')
        except Exception as exc:
            self.send(500, str(exc), 'text/plain')


def serve(host='127.0.0.1', port=8798):
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--port', type=int, default=8798)
    ap.add_argument('--host', default='127.0.0.1')
    args = ap.parse_args()
    serve(args.host, args.port)


if __name__ == '__main__':
    main()
