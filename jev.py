#!/usr/bin/env python3
"""Optional Jev (TypeSafe) classifier for `friction backfill`. Stdlib only, off by default.

Jev answers closed multiple-choice questions with a probability per option, which
is a cheaper and more consistent way to sort failure clusters than a free-text
analyser. Nothing here runs unless the config (classifier.json next to the friction
config) or `friction backfill --classifier jev` selects it.

Config (`~/.config/friction/classifier.json`; same directory as config.ini):

    {"classifier": "claude",                 # "claude" (default) or "jev"
     "jev": {"key_file": "~/.config/friction/jev.properties",   # `apiKey=...` or a bare key
             "model": "jev-1.13.0",
             "min_confidence": 0.75,
             "questions_per_request": 2}}   # TypeSafe docs: ask independent questions together

A missing, unreadable or invalid file means the claude flow. Errors raised here never
contain the API key, request body or response body.
"""
import json
import math
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple

ENDPOINT = 'https://api.typesafe.ai/v1/systemone'
DEFAULT_MODEL = 'jev-1.13.0'
DEFAULT_MIN_CONFIDENCE = 0.75
MAX_CRITERIA = 255
MAX_DESCRIPTION = 1000
MAX_RESPONSE_BYTES = 65536
MAX_KEY_FILE = 16384
PROBABILITY_TOLERANCE = 0.02
TIMEOUT_S = 12.0
MAX_QUESTIONS_PER_REQUEST = 8


class JevError(RuntimeError):
    """Configuration, transport or HTTP failure. The message is safe to print."""


class InvalidRequest(ValueError):
    pass


class InvalidAnswer(ValueError):
    pass


class Answer(NamedTuple):
    choice: str
    probabilities: dict
    confidence: float
    model: str
    usage: dict


# ---- configuration -------------------------------------------------------

def config_file():
    from tracker import config_path
    return config_path().parent / 'classifier.json'


def load_config():
    """-> dict(classifier, key_file, model, min_confidence, questions_per_request, problem).

    Never raises: anything wrong gives the default claude flow, with `problem` saying why."""
    cfg = {'classifier': 'claude', 'key_file': None, 'model': DEFAULT_MODEL,
           'min_confidence': DEFAULT_MIN_CONFIDENCE, 'questions_per_request': 2, 'problem': None}
    try:
        path = config_file()
        if not path.exists():
            return cfg
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError('top level must be an object')
    except (OSError, ValueError) as exc:
        cfg['problem'] = f'classifier config ignored: {exc}'[:200]
        return cfg
    if data.get('classifier') in ('claude', 'jev'):
        cfg['classifier'] = data['classifier']
    elif 'classifier' in data:
        cfg['problem'] = 'classifier config ignored: "classifier" must be "claude" or "jev"'
    section = data.get('jev')
    if isinstance(section, dict):
        if isinstance(section.get('key_file'), str) and section['key_file'].strip():
            cfg['key_file'] = section['key_file'].strip()
        if isinstance(section.get('model'), str) and section['model'].strip():
            cfg['model'] = section['model'].strip()[:80]
        conf = section.get('min_confidence')
        if type(conf) in (int, float) and 0 <= conf <= 1:
            cfg['min_confidence'] = float(conf)
        qpr = section.get('questions_per_request')
        if type(qpr) is int and 1 <= qpr <= MAX_QUESTIONS_PER_REQUEST:
            cfg['questions_per_request'] = qpr
    elif section is not None:
        cfg['problem'] = 'jev section ignored: must be an object'
    if cfg['classifier'] == 'jev' and cfg['problem']:
        cfg['classifier'] = 'claude'        # invalid config never turns Jev on
    return cfg


JEVGREP_CREDENTIALS = Path('~/.config/jevgrep/credentials.json')   # saved by `jg auth`


def default_key_path():
    """jev.properties beside the config, else the TypeSafe key jevgrep (`jg auth`) saved."""
    own = config_file().parent / 'jev.properties'
    shared = JEVGREP_CREDENTIALS.expanduser()
    return own if own.exists() or not shared.exists() else shared


def read_key(path=None):
    path = Path(path).expanduser() if path else default_key_path()
    try:
        if not path.is_file() or path.stat().st_size > MAX_KEY_FILE:
            raise JevError('Jev key file missing or too large')
        text = path.read_text()
    except OSError:
        raise JevError('Jev key file unreadable') from None
    key = ''
    if text.lstrip().startswith('{'):          # JSON, e.g. jevgrep's {"provider", "apiKey"}
        try:
            data = json.loads(text)
        except ValueError:
            raise JevError('Jev key file is not valid JSON') from None
        if not isinstance(data, dict) or data.get('provider') not in (None, 'typesafe'):
            raise JevError('Jev key file is not a TypeSafe credential')
        key = data.get('apiKey') if isinstance(data.get('apiKey'), str) else ''
        text = ''
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('apiKey='):
            key = line[7:].strip()
            break
    else:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith('#')]
        if len(lines) == 1 and '=' not in lines[0]:
            key = lines[0]
    if not key or key == 'PASTE_API_KEY_HERE' or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise JevError('Jev API key missing or malformed')
    return key


def evaluator(key_path=None, timeout=TIMEOUT_S, opener=urllib.request.urlopen):
    """-> evaluate(request) -> decoded response. One attempt, no retry."""
    def evaluate(request):
        key = read_key(key_path)
        body = json.dumps(request, separators=(',', ':')).encode()
        req = urllib.request.Request(ENDPOINT, body, {'Authorization': 'Bearer ' + key,
                                                      'Content-Type': 'application/json'}, method='POST')
        try:
            with opener(req, timeout=timeout) as response:
                if response.status != 200:
                    raise JevError(f'Jev HTTP {response.status}')
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            error.close()                   # discard the provider body unread
            raise JevError(f'Jev HTTP {error.code}') from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
            raise JevError('Jev request failed') from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise JevError('Jev response too large')
        try:
            return json.loads(raw)
        except ValueError:
            raise JevError('Jev response not JSON') from None
    return evaluate


# ---- request / answer ----------------------------------------------------

def _probability(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def build_request(questions, state, model=DEFAULT_MODEL):
    """questions: {qid: (instructions, {option_id: description})} -> request body.

    Each question needs 2..255 options (one option needs no inference)."""
    if not isinstance(questions, dict) or not questions:
        raise InvalidRequest('no questions')
    body = {}
    for qid, (instructions, criteria) in questions.items():
        if not isinstance(qid, str) or not qid:
            raise InvalidRequest('question id must be a non-empty string')
        if not isinstance(instructions, str) or not instructions.strip():
            raise InvalidRequest('instructions missing: ' + qid)
        if not isinstance(criteria, dict) or not 2 <= len(criteria) <= MAX_CRITERIA:
            raise InvalidRequest('a question needs 2 to 255 options: ' + qid)
        for cid, text in criteria.items():
            if not isinstance(cid, str) or not cid:
                raise InvalidRequest('option ids must be non-empty strings: ' + qid)
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_DESCRIPTION:
                raise InvalidRequest(f'option description invalid: {qid}/{cid}')
        body[qid] = {'type': 'choice', 'instructions': instructions, 'criteria': dict(criteria)}
    if not isinstance(state, dict):
        raise InvalidRequest('state must be an object')
    return {'model': model, 'state': state, 'questions': body}


def validate_answer(request, data, qid):
    """Validate one decoded answer against the request's options. -> Answer"""
    criteria = request['questions'][qid]['criteria']
    if not isinstance(data, dict) or not isinstance(data.get('answers'), dict):
        raise InvalidAnswer('answers missing')
    answer = data['answers'].get(qid)
    if not isinstance(answer, dict) or answer.get('type') != 'choice':
        raise InvalidAnswer('choice answer missing')
    choice = answer.get('choice')
    if not isinstance(choice, str) or choice not in criteria:
        raise InvalidAnswer('choice not among options')
    probs = answer.get('probabilities')
    if not isinstance(probs, dict) or set(probs) != set(criteria):
        raise InvalidAnswer('probabilities must cover exactly the options')
    if not all(_probability(v) for v in probs.values()):
        raise InvalidAnswer('probability out of range')
    if abs(sum(probs.values()) - 1) > PROBABILITY_TOLERANCE:
        raise InvalidAnswer('probabilities do not sum to 1')
    if probs[choice] < max(probs.values()):
        raise InvalidAnswer('choice is not the most probable option')
    confidence = answer.get('confidence')
    if not _probability(confidence):
        raise InvalidAnswer('confidence out of range')
    usage = data.get('usage') if isinstance(data.get('usage'), dict) else {}
    usage = {k: usage[k] for k in ('input_tokens', 'output_tokens') if type(usage.get(k)) is int}
    return Answer(choice, dict(probs), float(confidence), str(data.get('model', ''))[:80], usage)


def ask(evaluate, questions, state, model=DEFAULT_MODEL, per_request=1):
    """Ask every question. -> {qid: Answer or None (answer failed validation)}.

    Raises JevError when a request fails; no partial retries."""
    out, ids = {}, list(questions)
    per_request = max(1, min(per_request, MAX_QUESTIONS_PER_REQUEST))
    for i in range(0, len(ids), per_request):
        chunk = {qid: questions[qid] for qid in ids[i:i + per_request]}
        request = build_request(chunk, state, model)
        data = evaluate(request)
        for qid in chunk:
            try:
                out[qid] = validate_answer(request, data, qid)
            except InvalidAnswer:
                out[qid] = None
    return out


def check(evaluate, model=DEFAULT_MODEL):
    """Send a synthetic two-option question (no session data). -> Answer"""
    questions = {'check': ('Which option is the colour of a clear daytime sky? Answer with one option.',
                           {'blue': 'blue', 'green': 'green'})}
    return ask(evaluate, questions, {'purpose': 'connectivity check'}, model)['check']
