"""Linear owns Ghidorah cards and status. Friction owns the local event log."""

import collections
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime

import ghidorah
from tracker import TrackerError, read_token

ENDPOINT = 'https://api.linear.app/graphql'
ISSUE_FIELDS = 'id identifier title description url archivedAt completedAt state { id type } team { id key }'
PAGE = 'pageInfo { hasNextPage endCursor }'
FINGERPRINT_LINE = re.compile(r'^Ghidorah fingerprint: `(' + ghidorah.FINGERPRINT.pattern + r')`$', re.M)
UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
HIT = re.compile(
    rf'`ghidorah-hit-v2:({ghidorah.FINGERPRINT.pattern}):({UUID}):({UUID}):([^:`]+):([^:`]+):([^:`]+):'
    r'([01]):([01]):([0-9]{1,10}\.[0-9]{6})`\s*\Z'
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GraphQL:
    def __init__(self, token):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def call(self, operation, body, *, missing=None, **variables):
        request = urllib.request.Request(
            ENDPOINT,
            data=json.dumps({'query': body, 'variables': variables}).encode(),
            headers={'Authorization': self.token, 'Content-Type': 'application/json'},
        )
        try:
            with self.opener.open(request, timeout=12) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise TrackerError(f'Linear {operation}: HTTP {exc.code}') from None
        except (OSError, ValueError):
            raise TrackerError(f'Linear unreachable or unreadable during {operation}') from None
        if not isinstance(result, dict):
            raise TrackerError(f'Linear {operation}: unexpected API response')
        errors = result.get('errors')
        if errors and missing and len(errors) == 1 and errors[0].get('message') == f'Entity not found: {missing}':
            return None
        data = result.get('data')
        if errors or not isinstance(data, dict):
            raise TrackerError(f'Linear {operation}: GraphQL error')
        return data

    def issue(self, key):
        result = self.call(
            'read issue', f'query($key: String!) {{ issue(id:$key) {{ {ISSUE_FIELDS} }} }}',
            key=key, missing='Issue',
        )
        return result and result['issue']

    def issues(self, filters, *, comments=False):
        cursor = None
        while True:
            page = self.call(
                'read issues',
                'query($filter: IssueFilter!, $after: String, $comments: Boolean!) {'
                'issues(first:50, after:$after, includeArchived:true, filter:$filter) {'
                f'nodes {{ {ISSUE_FIELDS} comments(first:10) @include(if:$comments) {{'
                f'nodes {{ id body }} {PAGE} }} }} {PAGE} }} }}',
                filter=filters, comments=comments, after=cursor,
            )['issues']
            yield from page['nodes']
            if not page['pageInfo']['hasNextPage']:
                return
            cursor = page['pageInfo']['endCursor']

    def comments(self, issue):
        page, cursor = issue.get('comments'), None
        while True:
            if page is None:
                page = self.call(
                    'read comments', 'query($key: String!, $after: String) { issue(id:$key) { '
                    f'comments(first:100, after:$after) {{ nodes {{ id body }} {PAGE} }} }} }}',
                    key=issue['id'], after=cursor,
                )['issue']['comments']
            yield from page['nodes']
            if not page['pageInfo']['hasNextPage']:
                return
            cursor, page = page['pageInfo']['endCursor'], None


def issue_id(team_id, fingerprint):
    return ghidorah.occurrence_id('linear-issue', team_id, fingerprint)


def hit_token(hit):
    attribution = ':'.join(hit[key] for key in ('uid', 'session', 'machine', 'account', 'engine'))
    return f'`ghidorah-hit-v2:{hit["fingerprint"]}:{attribution}:{hit["correction"]:d}:{hit["silent"]:d}:{hit["ts"]:.6f}`'


def comment_id(issue, uid):
    return ghidorah.occurrence_id('linear-comment', issue['id'], uid)


def attribution_text(hit):
    if not ghidorah.valid_hit({key: hit[key] for key in ghidorah.FIELDS}) or uuid.UUID(hit['uid']).version != 4:
        raise TrackerError('invalid Ghidorah attribution')
    session, machine, account, engine = (hit[key] for key in ('session', 'machine', 'account', 'engine'))
    correction = 'sí' if hit['correction'] else 'no'
    silent = 'sí' if hit['silent'] else 'no'
    return (
        f'Ocurrencia de Ghidorah en `{machine}` con `{engine}`, cuenta `{account}`.\n\n'
        f'Sesión `{session}`. Corrección de Emiliano: {correction}. Daño silencioso: {silent}.'
    )


def parsed_hit(issue, comment):
    match = HIT.search(comment['body'] or '')
    if not match:
        return None
    fingerprint, uid, session, machine, account, engine, correction, silent, ts = match.groups()
    hit = dict(
        fingerprint=fingerprint, uid=uid, session=session, machine=machine, account=account,
        engine=engine, correction=correction == '1', silent=silent == '1', ts=float(ts),
    )
    if ghidorah.valid_hit(hit) and uuid.UUID(uid).version == 4 and comment['id'] == comment_id(issue, uid):
        return hit
    return None


def pull_hit(fr, item, issue, comment):
    hit = parsed_hit(issue, comment)
    if not hit or hit['fingerprint'] != item['fingerprint']:
        return
    uid = hit['uid']
    existing = fr.db.execute(
        'SELECT pushed FROM occurrences WHERE uid=? AND item_id=?', (uid, item['item_id']),
    ).fetchone()
    if existing:
        if not existing['pushed']:
            mark_pushed(fr, uid)
        return
    with fr.db:
        fr.db.execute('BEGIN IMMEDIATE')
        ghidorah.insert_hit(fr, hit, local=False)
        fr.db.execute('UPDATE occurrences SET pushed=1 WHERE uid=? AND item_id=?', (uid, item['item_id']))


def mark_pushed(fr, uid):
    with fr.db:
        fr.db.execute('UPDATE occurrences SET pushed=1 WHERE uid=?', (uid,))


def sync_rows(fr):
    return fr.db.execute(
        'SELECT g.*, i.remote_key, i.status, COUNT(DISTINCT h.session) AS sessions,'
        ' COALESCE(MAX(h.correction OR h.silent), 0) AS priority, COALESCE(SUM(o.pushed=0), 0) AS pending'
        ' FROM ghidorah_items g JOIN items i ON i.id=g.item_id'
        ' LEFT JOIN ghidorah_hits h ON h.fingerprint=g.fingerprint'
        ' LEFT JOIN occurrences o ON o.uid=h.uid'
        ' GROUP BY g.fingerprint ORDER BY g.item_id',
    ).fetchall()


def pending_hits(fr, fingerprint):
    return fr.db.execute(
        'SELECT h.* FROM ghidorah_hits h JOIN occurrences o ON o.uid=h.uid'
        ' WHERE h.fingerprint=? AND o.pushed=0 ORDER BY h.ts, h.uid',
        (fingerprint,),
    ).fetchall()


def eligible(item):
    return item['status'] == 'open' and (item['sessions'] >= 2 or item['priority'])


def pending(fr):
    ghidorah.ensure_schema(fr)
    creations, comments, local = 0, 0, 0
    for row in sync_rows(fr):
        key = row['link'] or row['remote_key']
        if key and key.startswith('MAI-'):
            continue
        if not key and not eligible(row):
            local += 1
            continue
        creations += not key
        comments += row['pending']
    return {'items to create': creations, 'hits to push': comments, 'items remaining local': local}


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() if value else None


def issue_numbers(rows, prefix):
    keys = (row['link'] or row['remote_key'] for row in rows)
    return sorted({
        int(key.split('-')[1]) for key in keys
        if key and key.startswith(prefix + '-') and ghidorah.ISSUE.fullmatch(key)
    })


def apply_status(fr, item_id, issue, *, regression=False):
    state = issue['state']['type']
    status = 'resolved' if state == 'completed' else 'open'
    if state in ('canceled', 'duplicate') or (issue['archivedAt'] and status == 'open'):
        status = 'wontfix'
    key = issue['identifier']
    with fr.db:
        fr.db.execute(
            'UPDATE items SET remote_key=?, remote_url=?, status=?, resolved_at=?, dirty=0,'
            ' regressions=regressions+CASE WHEN id=? THEN ? ELSE 0 END'
            ' WHERE id=? OR EXISTS (SELECT 1 FROM ghidorah_items g WHERE g.item_id=items.id'
            ' AND COALESCE(g.link, items.remote_key)=?)',
            (key, issue['url'], status, timestamp(issue['completedAt']) if status == 'resolved' else None,
             item_id, int(regression), item_id, key),
        )


class Linear:
    name = 'Linear'

    def __init__(self, cfg, *, token=None, linked_token=None):
        self.cfg = cfg
        self.project = cfg.get('project') or 'HXR'
        self.label = cfg.get('label') or 'ghidorah'
        self.workspace = cfg.get('workspace') or 'hxrmx'
        self.direct_credentials = token is not None
        if token == '':
            raise TrackerError('no Linear API token')
        self.api = GraphQL(token if self.direct_credentials else read_token(cfg))
        self.linked_token = linked_token
        self.team = None
        self.linked_api = None

    def check(self):
        if self.team is None:
            data = self.api.call('read team and label', '''
                query($team: String!, $label: String!) {
                    organization { urlKey }
                    teams(filter:{key:{eq:$team}}) {
                        nodes { id key states { nodes { id type position } } }
                    }
                    issueLabels(first:100, filter:{name:{eq:$label}}) {
                        nodes { id team { id } } pageInfo { hasNextPage }
                    }
                }
                ''', team=self.project, label=self.label)
            teams = data['teams']['nodes']
            if data['organization']['urlKey'] != self.workspace or len(teams) != 1:
                raise TrackerError('Linear key does not match the configured workspace and team')
            team = teams[0]
            labels = data['issueLabels']
            scoped = [label['id'] for label in labels['nodes'] if (label['team'] or {}).get('id') == team['id']]
            if len(scoped) != 1 or labels['pageInfo']['hasNextPage']:
                raise TrackerError(f'Linear requires one team-scoped {self.label} label on {self.project}')
            self.label_id = scoped[0]
            active = [state for state in team['states']['nodes'] if state['type'] in ('backlog', 'unstarted')]
            if not active:
                raise TrackerError(f'Linear team {self.project} has no backlog or unstarted state')
            self.reopen_state = min(active, key=lambda state: state['position'])['id']
            self.team = team
        return f'{self.workspace}, team {self.project}, label {self.label}'

    def hxr_issues(self, rows):
        labels = {'labels': {'some': {'id': {'eq': self.label_id}}}}
        numbers = issue_numbers(rows, self.project)
        filters = {'team': {'id': {'eq': self.team['id']}}}
        filters.update({'or': [labels, {'number': {'in': numbers}}]} if numbers else labels)
        return {issue['identifier']: issue for issue in self.api.issues(filters, comments=True)}

    def linked_issues(self, rows):
        numbers = issue_numbers(rows, 'MAI')
        if not numbers:
            return None
        if self.linked_api is None:
            token = self.linked_token
            if not token and not self.direct_credentials:
                command = self.cfg.get('linked_token_cmd')
                token_env = self.cfg.get('linked_token_env') or 'FRICTION_LINKED_LINEAR_TOKEN'
                if command or (self.cfg.get('linked_token_env') and os.environ.get(token_env, '').strip()):
                    token = read_token({'type': 'linear', 'token_cmd': command, 'token_env': token_env})
            if not token:
                return None
            linked_api = GraphQL(token)
            data = linked_api.call('read MAI workspace', '{ organization { urlKey } }')
            if data['organization']['urlKey'] != 'mainder':
                raise TrackerError('Linear key for existing MAI links does not match the Mainder workspace')
            self.linked_api = linked_api
        filters = {'team': {'key': {'eq': 'MAI'}}, 'number': {'in': numbers}}
        return {
            issue['identifier']: issue for issue in self.linked_api.issues(filters)
            if issue['team']['key'] == 'MAI'
        }

    def create(self, item, first):
        fingerprint = item['fingerprint']
        key = issue_id(self.team['id'], fingerprint)
        title = item['title'] if item['seed'] else fingerprint
        payload = {
            'id': key, 'teamId': self.team['id'], 'labelIds': [self.label_id],
            'title': f'Ghidorah: {title}'[:200],
            'description': f'{attribution_text(first)}\n\nGhidorah fingerprint: `{fingerprint}`',
        }
        try:
            result = self.api.call(
                'create issue', 'mutation($input: IssueCreateInput!) {'
                f'issueCreate(input:$input) {{ success issue {{ {ISSUE_FIELDS} }} }} }}',
                input=payload,
            )['issueCreate']
            if result['success'] and result['issue']['id'] == key:
                return result['issue'], True
            raise TrackerError('Linear did not confirm issue creation')
        except TrackerError:
            # The server may have committed the write before the response was lost.
            existing = self.api.issue(key)
            if existing and existing['team']['id'] == self.team['id']:
                return existing, False
            raise

    def comment(self, issue, hit):
        summary = attribution_text(hit)
        token = hit_token(hit)
        key = comment_id(issue, hit['uid'])
        body = f'{summary}\n\n{token}'
        payload = {'id': key, 'issueId': issue['id'], 'body': body, 'doNotSubscribeToIssue': True}
        try:
            result = self.api.call(
                'create comment', 'mutation($input: CommentCreateInput!) {'
                'commentCreate(input:$input) { success comment { id issueId } } }',
                input=payload,
            )['commentCreate']
            if result['success'] and result['comment']['id'] == key:
                return True
            raise TrackerError('Linear did not confirm comment creation')
        except TrackerError:
            present = self.api.call(
                'check comment', 'query($id: String!) { comment(id:$id) { id issueId } }',
                id=key, missing='Comment',
            )
            if present and present['comment']['issueId'] == issue['id']:
                return False
            raise

    def reopen(self, issue, hits):
        if not qualifies_as_regression(issue, hits):
            return issue, False
        current = self.api.issue(issue['id'])
        if not current:
            raise TrackerError('Linear issue disappeared before reopening')
        if not qualifies_as_regression(current, hits):
            return current, False
        result = self.api.call(
            'reopen issue', 'mutation($id:String!, $input:IssueUpdateInput!) {'
            f'issueUpdate(id:$id, input:$input) {{ success issue {{ {ISSUE_FIELDS} }} }} }}',
            id=current['id'], input={'stateId': self.reopen_state},
        )['issueUpdate']
        if not result['success']:
            raise TrackerError('Linear did not confirm reopening')
        return result['issue'], True

    def find_issue(self, item, by_key, discovered, hits, counts):
        key = item['link'] or item['remote_key']
        fingerprint = item['fingerprint']
        issue = by_key.get(key) if key else discovered.get(fingerprint)
        if key and not issue:
            raise TrackerError('linked Linear issue is unavailable in the configured team')
        if not issue and hits and eligible(item):
            if not ghidorah.FINGERPRINT.fullmatch(fingerprint):
                raise TrackerError('invalid Ghidorah fingerprint')
            issue = self.api.issue(issue_id(self.team['id'], fingerprint))
            if not issue:
                issue, made = self.create(item, hits[0])
                counts['items created'] += made
        if issue and issue['team']['id'] != self.team['id']:
            raise TrackerError('Linear issue is outside the configured team')
        return issue

    def comments_for(self, issue):
        if issue['id'] not in self.comments_by_issue:
            self.comments_by_issue[issue['id']] = list(self.api.comments(issue))
        return self.comments_by_issue[issue['id']]

    def discover_fingerprint(self, issue, known):
        if issue['id'] in known:
            return known[issue['id']]
        marker = FINGERPRINT_LINE.search(issue['description'] or '')
        if marker and issue['id'] == issue_id(self.team['id'], marker[1]):
            return marker[1]
        for comment in self.comments_for(issue):
            hit = parsed_hit(issue, comment)
            if hit and issue['id'] == issue_id(self.team['id'], hit['fingerprint']):
                return hit['fingerprint']
        return None

    def sync_item(self, fr, item, by_key, discovered, counts):
        fingerprint = item['fingerprint']
        issue = self.find_issue(item, by_key, discovered, pending_hits(fr, fingerprint), counts)
        if not issue:
            return
        apply_status(fr, item['item_id'], issue)
        for comment in self.comments_for(issue):
            pull_hit(fr, item, issue, comment)
        if issue['archivedAt']:
            return
        hits = pending_hits(fr, fingerprint)
        current, reopened = self.reopen(issue, hits)
        if current is not issue:
            issue.update(current)
        if reopened:
            counts['items reopened'] += 1
        apply_status(fr, item['item_id'], issue, regression=reopened)
        if issue['archivedAt']:
            return
        for hit in hits:
            counts['hits pushed'] += self.comment(issue, hit)
            mark_pushed(fr, hit['uid'])

    def sync(self, fr):
        self.check()
        ghidorah.ensure_schema(fr)
        counts, errors, discovered = collections.Counter(), [], {}
        rows = sync_rows(fr)
        by_key = self.hxr_issues(rows)
        self.comments_by_issue = {}
        known = {issue_id(self.team['id'], row['fingerprint']): row['fingerprint'] for row in rows}
        linked = None
        try:
            linked = self.linked_issues(rows)
        except TrackerError as exc:
            errors.append(str(exc))
        for issue in by_key.values():
            fingerprint = self.discover_fingerprint(issue, known)
            if not fingerprint:
                continue
            discovered[fingerprint] = issue
            if issue['id'] not in known:
                with fr.db:
                    fr.db.execute('BEGIN IMMEDIATE')
                    ghidorah.ensure_item(fr, fingerprint)
        for item in sync_rows(fr):
            try:
                key = item['link'] or item['remote_key'] or ''
                if key.startswith('MAI-'):
                    if linked is not None:
                        if key not in linked:
                            raise TrackerError('linked Linear issue is unavailable in the Mainder workspace')
                        apply_status(fr, item['item_id'], linked[key])
                else:
                    self.sync_item(fr, item, by_key, discovered, counts)
            except TrackerError as exc:
                errors.append(f'#{item["item_id"]}: {exc}')
                if 'unreachable' in str(exc):
                    break
        return counts, errors


def qualifies_as_regression(issue, hits):
    completed = timestamp(issue['completedAt'])
    return (
        issue['state']['type'] == 'completed' and not issue['archivedAt'] and completed is not None
        and any(hit['ts'] > completed for hit in hits)
    )
