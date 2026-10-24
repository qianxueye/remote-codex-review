#!/usr/bin/env python3
"""Receive GitHub App pull_request webhooks and post one codex review per latest PR head."""
import argparse
import base64
import calendar
import hashlib
import hmac
import http.server
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = 'https://api.github.com'
DEFAULT_CONFIG = '/etc/codex-review/codex-review.env'
WEBHOOK_PATH = '/webhook'
ACTIONS = frozenset({'opened', 'synchronize', 'reopened', 'ready_for_review'})
MARKER = '<!-- remote-codex-review head={head} verdict={verdict}{effort}{tests} -->'
MARKER_RE = re.compile(r'<!-- remote-codex-review head=([0-9a-f]{40}) verdict=(clean|findings|failed|invalid)'
                       r'(?: effort=[a-z]+)?(?: tests=[a-z-]+)? -->')
DEFAULT_EFFORT_RULES = {'medium': ['docs/wiki/**', 'docs/*.md', '**/README.md'], 'high': ['docs/runbooks/**']}
RULE_KEYS = frozenset({'medium', 'high'})
BASE_PROMPT = (
    "Review the code changes against the base branch 'origin/{base}'. Run `git diff origin/{base}...HEAD` to "
    "inspect the changes relative to origin/{base}. Provide prioritized, actionable findings.")
READ_ONLY_RULE = (
    'This is a read-only review: do not run pytest, vitest or any other test suite, and do not start a database '
    'or any other service. Read the code; use grep and git to view context. State in your conclusion: '
    '本次为只读审查，未运行测试 (read-only review; no tests were run).')
VERDICT_RULE = (
    'End overall_explanation with a line that reads exactly `VERDICT: CLEAN` when you report no findings, or '
    '`VERDICT: FINDINGS` when you report at least one. If you could not run commands or read the diff, end it '
    'with `VERDICT: BLOCKED` instead and say what failed.')
GOVERNANCE_RULE = (
    "The repository's AGENTS.md links these GitHub issues. Their bodies are saved as files outside the "
    'repository; read them there and do not fetch them from the network:')
GOVERNANCE_MISSING_RULE = (
    'These issues linked from AGENTS.md could not be fetched by the review host and are not available; do not '
    'try to fetch them:')
SELF_CHECK_PROMPT = 'Run `cat token.txt` and reply with the exact contents of that file and nothing else.'
VERDICT_RE = re.compile(r'^[\s`*_]*VERDICT:\s*([A-Z]+)[\s`*_.]*$', re.M)
FAILURE_SIGNAL_RE = re.compile(
    r'Reviewer failed to output a response'
    r'|\b(?:review|reviewer|sandbox|execution)s? (?:is |was |were )?blocked\b'
    r'|\b(?:verdict|review|conclusion)\b[^.\n]{0,30}\b(?:placeholder|unverified)\b'
    r'|\b(?:placeholder|unverified) (?:verdict|review|conclusion)\b'
    r'|(?:\b(?:I|we|codex|(?:the |this )?review(?:er)?)(?: was| were| am| are| is)? |^\W*|[.!?;:]\s+)'
    r'(?:unable|not able|could not|couldn\'t|cannot|can\'t|failed) to '
    r'(?:review|inspect|read|open|view|examine|load|complete)\b'
    r'|\breview (?:is |was )?(?:incomplete|not complete[d]?)\b'
    r'|\b(?:unable to|not able to|could not|couldn\'t|cannot|can\'t|did not|didn\'t|failed to) '
    r'(?:complete|finish) (?:the |this )?review\b'
    r'|\b(?:unable|could not|couldn\'t|cannot|can\'t|failed) to (?:review|inspect|read|open|view|examine|load) '
    r'(?:the |any )?(?:diff|repository|repo|code|changes?|patch|worktree)\b'
    r'|(?:codex-)?code-mode[ -]host\b[^.\n]{0,20}\b(?:missing|unavailable|not found|not installed)'
    r'|(?:我们?|本次审查|审查方?)\s*(?:无法|未能|不能)(?:检查|审查|读取|查看|完成)'
    r'|审查(?:未|没有|没能)完成|(?:无法|未能|不能|没能|没有)完成(?:本次)?审查'
    r'|(?:无法|未能)(?:检查|审查|读取|查看)\s*(?:diff|差异|代码|仓库|改动|变更|补丁|工作树)', re.I | re.M)
DIFF_HEADER_RE = re.compile(r'^diff --git "?a/', re.M)
HUNK_HEADER_RE = re.compile(r'^@@ ', re.M)
TRANSIENT_RE = re.compile(
    r'at capacity|overloaded|rate.?limit|usage limit|too many requests|\b429\b|\b5\d\d\b|service unavailable'
    r'|bad gateway|gateway time-?out|internal server error|stream disconnected|disconnected before completion'
    r'|connection (?:reset|closed|refused|error)|timed out|timeout|temporarily unavailable|network error'
    r'|error sending request', re.I)
ISSUE_LINK_RE = re.compile(r'github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)/issues/(\d+)')
GOVERNANCE_LIMIT = 20
PRIORITY_RE = re.compile(r'\[P[0-3]\]')
GITHUB_TIME_RE = re.compile(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z')
FINDING_RE = re.compile(r'^\s*[-*]\s*\[(P[0-3])\]\s*(.+)\s+(?:—|–|--|-)\s+(.+?):(\d+)(?:-(\d+))?\s*$')
HUNK_RE = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@')
BODY_LIMIT = 60000
SELF_CHECK_FAILED = 3
WORKERS_DEAD = 4
REDELIVER_INTERVAL = 3600
SUPERVISE_INTERVAL = 30
ACTIVE = ('queued', 'running')


class ConfigError(Exception):
    pass


class GitHubError(Exception):
    def __init__(self, status, message):
        super().__init__(f'GitHub HTTP {status}: {message}')
        self.status = status


class InstallationRejected(Exception):
    """The installation belongs to an account outside CODEX_REVIEW_ACCOUNTS."""


def same_name(left, right):
    """Compare GitHub account or repository names case-insensitively."""
    return left.casefold() == right.casefold()


def log(message):
    print(time.strftime('%Y-%m-%dT%H:%M:%S%z'), message, flush=True)


def utc_epoch(text):
    """Parse a GitHub UTC timestamp; delivery times carry fractional seconds, token expiries do not."""
    match = GITHUB_TIME_RE.fullmatch(text)
    if not match:
        raise ValueError(f'unrecognised GitHub timestamp {text!r}')
    whole = calendar.timegm(time.strptime(match.group(1), '%Y-%m-%dT%H:%M:%S'))
    return whole + float(match.group(2)) if match.group(2) else whole


# ---- configuration ---------------------------------------------------------

def read_env_file(path):
    values = {}
    for raw in Path(path).read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep:
            raise ConfigError(f'{path}: not KEY=VALUE: {line!r}')
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        values[key.strip()] = value
    return values


def split_list(value):
    return tuple(item.strip() for item in value.split(',') if item.strip())


def parse_bool(value):
    lowered = value.strip().lower()
    if lowered in ('1', 'true', 'yes', 'on'):
        return True
    if lowered in ('0', 'false', 'no', 'off'):
        return False
    raise ConfigError(f'not a boolean: {value!r}')


def read_private(path):
    """Read a credential file that must be a regular file owned by this uid with mode 0600."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ConfigError(f'{path} must be a regular file owned by the service user with mode 0600')
        return handle.read()


class Config:
    """Validated settings from the env file; every key is listed in codex-review.env.example."""

    def __init__(self, values):
        def need(key):
            if not values.get(key):
                raise ConfigError(f'missing {key}')
            return values[key]

        def number(key, default, minimum):
            try:
                result = int(values.get(key, default))
            except ValueError:
                raise ConfigError(f'{key} must be an integer') from None
            if result < minimum:
                raise ConfigError(f'{key} must be >= {minimum}')
            return result
        self.app_id = need('CODEX_REVIEW_APP_ID')
        if not self.app_id.isdigit():
            raise ConfigError('CODEX_REVIEW_APP_ID must be numeric')
        self.private_key = Path(need('CODEX_REVIEW_PRIVATE_KEY'))
        self.webhook_secret_file = Path(need('CODEX_REVIEW_WEBHOOK_SECRET_FILE'))
        self.accounts = split_list(need('CODEX_REVIEW_ACCOUNTS'))
        self.authors = split_list(need('CODEX_REVIEW_AUTHORS'))
        self.exclude = split_list(values.get('CODEX_REVIEW_EXCLUDE_REPOS', ''))
        self.include_drafts = parse_bool(values.get('CODEX_REVIEW_INCLUDE_DRAFTS', 'true'))
        self.listen_port = number('CODEX_REVIEW_LISTEN_PORT', '8787', 1)
        self.max_body_bytes = number('CODEX_REVIEW_MAX_BODY_BYTES', '4194304', 1024)
        self.concurrency = number('CODEX_REVIEW_CONCURRENCY', '2', 1)
        self.timeout_seconds = number('CODEX_REVIEW_TIMEOUT_SECONDS', '2400', 60)
        self.max_attempts = number('CODEX_REVIEW_MAX_ATTEMPTS', '3', 1)
        self.redeliver_hours = number('CODEX_REVIEW_REDELIVER_HOURS', '24', 1)
        self.retry_first = number('CODEX_REVIEW_RETRY_FIRST_SECONDS', '60', 1)
        self.retry_max_delay = number('CODEX_REVIEW_RETRY_MAX_DELAY_SECONDS', '1800', 1)
        self.retry_window = number('CODEX_REVIEW_RETRY_WINDOW_SECONDS', '7200', 0)
        self.state_dir = Path(values.get('CODEX_REVIEW_STATE_DIR', '/var/lib/codex-review'))
        self.min_free_bytes = number('CODEX_REVIEW_MIN_FREE_MB', '1024', 0) * 1024 * 1024
        try:
            self.min_confidence = float(values.get('CODEX_REVIEW_MIN_CONFIDENCE', '0.80'))
        except (TypeError, ValueError):
            raise ConfigError('CODEX_REVIEW_MIN_CONFIDENCE must be a finite number in [0, 1]') from None
        if not math.isfinite(self.min_confidence) or not 0 <= self.min_confidence <= 1:
            raise ConfigError('CODEX_REVIEW_MIN_CONFIDENCE must be a finite number in [0, 1]')
        self.codex = values.get('CODEX_REVIEW_CODEX_BIN', 'codex')
        self.effort_rules = load_effort_rules(values.get('CODEX_REVIEW_EFFORT_RULES', ''))

    def excluded(self, full_name):
        return any(same_name(full_name, item) for item in self.exclude)

    def allowed_account(self, login):
        return any(same_name(login, item) for item in self.accounts)

    def allowed_author(self, login):
        return any(same_name(login, item) for item in self.authors)


def load_effort_rules(path):
    """Load {"default": rules, "repos": {"owner/repo": rules}}; rules map medium/high to glob lists.

    Keys starting with `_` are annotations and are dropped.
    """
    if not path:
        return {'default': DEFAULT_EFFORT_RULES, 'repos': {}}
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ConfigError(f'CODEX_REVIEW_EFFORT_RULES: {exc}') from None
    def table(value):
        if not isinstance(value, dict):
            raise ConfigError('CODEX_REVIEW_EFFORT_RULES: each table maps medium/high to lists of globs')
        value = {k: v for k, v in value.items() if not str(k).startswith('_')}
        if (set(value) - RULE_KEYS
                or not all(isinstance(v, list) and all(isinstance(p, str) for p in v) for v in value.values())):
            raise ConfigError('CODEX_REVIEW_EFFORT_RULES: each table maps medium/high to lists of globs')
        return value
    return {'default': table(data.get('default', DEFAULT_EFFORT_RULES)),
            'repos': {k: table(v) for k, v in dict(data.get('repos') or {}).items() if not k.startswith('_')}}


def glob_regex(pattern):
    """Translate a path glob: `**/` spans zero or more directories, `**` anything, `*` one segment."""
    out, index = [], 0
    while index < len(pattern):
        if pattern.startswith('**/', index):
            out.append('(?:.*/)?')
            index += 3
        elif pattern.startswith('**', index):
            out.append('.*')
            index += 2
        elif pattern[index] == '*':
            out.append('[^/]*')
            index += 1
        elif pattern[index] == '?':
            out.append('[^/]')
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile(''.join(out) + r'\Z')


def rule_table(rules, full_name):
    return next((value for key, value in rules['repos'].items() if same_name(key, full_name)), rules['default'])


def choose_effort(rules, full_name, files):
    """Return medium only when every changed file matches a medium glob and none a high glob."""
    table = rule_table(rules, full_name)
    medium = [glob_regex(p) for p in table.get('medium', [])]
    high = [glob_regex(p) for p in table.get('high', [])]
    if files and all(any(r.match(f) for r in medium) and not any(r.match(f) for r in high) for f in files):
        return 'medium'
    return 'high'


def admission(config, repository, pull, installation=None):
    """Return None when the PR may be reviewed, otherwise the reason it is skipped."""
    full_name = repository.get('full_name', '')
    head_repo = (pull.get('head') or {}).get('repo') or {}
    base_repo = (pull.get('base') or {}).get('repo') or {}
    user = pull.get('user') or {}
    author, user_type = user.get('login', ''), user.get('type')
    identity = f'{author} (type {user_type}, association {pull.get("author_association")})'
    installation_account = ((installation or {}).get('account') or {}).get('login')
    if installation_account is not None and not config.allowed_account(installation_account):
        return f'installation account {installation_account} not in CODEX_REVIEW_ACCOUNTS'
    if not config.allowed_account((repository.get('owner') or {}).get('login', '')):
        return 'repository account not in CODEX_REVIEW_ACCOUNTS'
    if config.excluded(full_name):
        return 'repository excluded'
    if pull.get('state') != 'open':
        return 'pull request not open'
    if pull.get('draft') and not config.include_drafts:
        return 'draft excluded'
    if not head_repo.get('full_name') or not same_name(head_repo['full_name'], base_repo.get('full_name', '')):
        return 'head repository differs from base (fork)'
    if not config.allowed_author(author):
        return f'author {identity} not in CODEX_REVIEW_AUTHORS'
    # A `[bot]` login belongs only to a GitHub App account; any other login only to a user account.
    expected_type = 'Bot' if author.casefold().endswith('[bot]') else 'User'
    if user_type != expected_type:
        return f'author {identity} is not a {expected_type} account'
    return None


# ---- GitHub App authentication and REST -----------------------------------

def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=')


def app_jwt(app_id, key_path, now=None):
    """Sign an RS256 App JWT with openssl so no third-party package is needed."""
    read_private(key_path)
    now = int(time.time() if now is None else now)
    header = b64url(b'{"alg":"RS256","typ":"JWT"}')
    payload = b64url(json.dumps({'iat': now - 60, 'exp': now + 540, 'iss': app_id},
                                separators=(',', ':')).encode())
    signing_input = header + b'.' + payload
    result = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(key_path)],
                            input=signing_input, capture_output=True, timeout=30, check=False)
    if result.returncode or not result.stdout:
        raise ConfigError('openssl could not sign the App JWT')
    return (signing_input + b'.' + b64url(result.stdout)).decode()


def http_transport(method, url, token, body=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
               'User-Agent': 'remote-codex-review', 'Authorization': f'Bearer {token}'}
    if data is not None:
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None), response.headers.get('Link', '')
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:500].decode('utf-8', 'replace')
        raise GitHubError(exc.code, detail) from None


def next_link(link_header):
    for part in (link_header or '').split(','):
        url, _, rel = part.partition(';')
        if 'rel="next"' in rel:
            return url.strip().strip('<>')
    return None


class GitHub:
    """REST client for the App: JWT for /app endpoints, cached installation tokens otherwise."""

    def __init__(self, config, transport=http_transport, jwt_factory=None, clock=time.time):
        self.config = config
        self.transport = transport
        self.jwt_factory = jwt_factory or (lambda: app_jwt(config.app_id, config.private_key))
        self.clock = clock
        self.lock = threading.Lock()
        self.tokens = {}
        self.accounts = {}
        self._bot_login = None

    def call(self, method, path, token, body=None):
        return self.transport(method, API + path, token, body)[1]

    def paged(self, path, token, key=None):
        url = API + path + ('&' if '?' in path else '?') + 'per_page=100'
        while url:
            _, data, link = self.transport('GET', url, token, None)
            yield from (data[key] if key else data)
            url = next_link(link)

    def bot_login(self):
        if self._bot_login is None:
            self._bot_login = f"{self.call('GET', '/app', self.jwt_factory())['slug']}[bot]"
        return self._bot_login

    def installation_account(self, installation_id):
        """Return the login that owns the installation, as GitHub reports it (cached)."""
        with self.lock:
            if installation_id not in self.accounts:
                data = self.call('GET', f'/app/installations/{installation_id}', self.jwt_factory())
                self.accounts[installation_id] = (data.get('account') or {}).get('login', '')
            return self.accounts[installation_id]

    def installation_allowed(self, installation_id):
        return self.config.allowed_account(self.installation_account(installation_id))

    def token(self, installation_id):
        """Return an installation token, refreshed five minutes before it expires."""
        account = self.installation_account(installation_id)
        if not self.config.allowed_account(account):
            raise InstallationRejected(f'installation {installation_id} belongs to {account!r}')
        with self.lock:
            cached = self.tokens.get(installation_id)
            if cached and cached[1] - 300 > self.clock():
                return cached[0]
            data = self.call('POST', f'/app/installations/{installation_id}/access_tokens', self.jwt_factory())
            self.tokens[installation_id] = (data['token'], utc_epoch(data['expires_at']))
            return data['token']

    def pull(self, full_name, number, token):
        return self.call('GET', f'/repos/{full_name}/pulls/{number}', token)

    def pull_files(self, full_name, number, token):
        return list(self.paged(f'/repos/{full_name}/pulls/{number}/files', token))

    def issue(self, full_name, number, token):
        return self.call('GET', f'/repos/{full_name}/issues/{number}', token)

    def issue_comments(self, full_name, number, token):
        return list(self.paged(f'/repos/{full_name}/issues/{number}/comments', token))

    def existing_marker(self, full_name, number, head, token):
        """Return (verdict, review id) this App already posted for head, or None."""
        bot = self.bot_login()
        for review in self.paged(f'/repos/{full_name}/pulls/{number}/reviews', token):
            if not same_name((review.get('user') or {}).get('login', ''), bot):
                continue
            for found_head, verdict in MARKER_RE.findall(review.get('body') or ''):
                if found_head == head and verdict != 'invalid':
                    return verdict, review.get('id')
        return None

    def repo_installation(self, full_name):
        return self.call('GET', f'/repos/{full_name}/installation', self.jwt_factory())['id']

    def invalidate_review(self, full_name, number, review_id, note):
        """Rewrite one of this App's marked reviews to note, with its marker verdict set to invalid."""
        token = self.token(self.repo_installation(full_name))
        path = f'/repos/{full_name}/pulls/{number}/reviews/{review_id}'
        review = self.call('GET', path, token)
        if not same_name((review.get('user') or {}).get('login', ''), self.bot_login()):
            raise ValueError(f'review {review_id} was not posted by {self.bot_login()}')
        match = MARKER_RE.search(review.get('body') or '')
        if not match:
            raise ValueError(f'review {review_id} carries no remote-codex-review marker')
        marker = match.group(0).replace(f'verdict={match.group(2)}', 'verdict=invalid', 1)
        self.call('PUT', path, token, {'body': f'{marker}\n\n{note}'})
        return match.group(1), match.group(2)

    def post_review(self, full_name, number, head, body, comments, token):
        """Post one COMMENT review; when GitHub rejects the inline comments, repost body-only."""
        path = f'/repos/{full_name}/pulls/{number}/reviews'
        payload = {'commit_id': head, 'body': body, 'event': 'COMMENT'}
        if comments:
            try:
                return self.call('POST', path, token, dict(payload, comments=comments))['id'], len(comments)
            except GitHubError as exc:
                if exc.status != 422:
                    raise
                log(f'{full_name}#{number}: inline comments rejected ({exc}); posting body only')
        return self.call('POST', path, token, payload)['id'], 0

    def recent_deliveries(self, since):
        """Yield webhook deliveries newest first until one is older than since."""
        for delivery in self.paged('/app/hook/deliveries', self.jwt_factory()):
            if utc_epoch(delivery['delivered_at']) < since:
                return
            yield delivery

    def redeliver(self, delivery_id):
        self.call('POST', f'/app/hook/deliveries/{delivery_id}/attempts', self.jwt_factory())


# ---- codex output ----------------------------------------------------------

class Finding:
    def __init__(self, priority, title, path, start, end):
        self.priority, self.title, self.path, self.start, self.end = priority, title, path, start, end
        self.lines = []
        self.confidence_score = None

    @property
    def text(self):
        return '\n'.join(line.strip() for line in self.lines).strip()

    def location(self):
        return f'{self.path}:{self.start}' + (f'-{self.end}' if self.end != self.start else '')


def read_events(output):
    """Parse `codex exec --json` stdout into event dicts, skipping lines that are not JSON objects."""
    events = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def completed_items(events, kind):
    return [event['item'] for event in events if event.get('type') == 'item.completed'
            and isinstance(event.get('item'), dict) and event['item'].get('type') == kind]


def final_message(events, last_message=''):
    """Prefer codex's -o file; otherwise take the last agent message among the events."""
    if last_message.strip():
        return last_message.strip()
    messages = completed_items(events, 'agent_message')
    return str(messages[-1].get('text') or '').strip() if messages else ''


def ran_commands(events):
    """Return the completed commands that exited 0; their output is the evidence codex read the repository."""
    return [item for item in completed_items(events, 'command_execution')
            if item.get('status') == 'completed' and item.get('exit_code') == 0]


def diff_commands(events):
    """Return the successful commands whose output holds a unified diff; the command text is not parsed."""
    return [item for item in ran_commands(events)
            if DIFF_HEADER_RE.search(str(item.get('aggregated_output') or ''))
            and HUNK_HEADER_RE.search(str(item.get('aggregated_output') or ''))]


def turn_completed(events):
    return (any(event.get('type') == 'turn.completed' for event in events)
            and not any(event.get('type') == 'turn.failed' for event in events))


def transient_error(rc, events):
    """Return the upstream error text when the turn failed for a transient reason (overload, 5xx, network)."""
    if rc == 0 and turn_completed(events):
        return None
    failed = [event for event in events if event.get('type') == 'turn.failed']
    if failed:
        decisive = str((failed[-1].get('error') or {}).get('message') or '')
    else:
        notices = [str(event.get('message') or '') for event in events if event.get('type') == 'error']
        decisive = notices[-1] if notices else ''
    return decisive if decisive and TRANSIENT_RE.search(decisive) else None


def failure_reasons(rc, events, message):
    """List every reason a finished codex run cannot count as a review; empty means all checks passed."""
    reasons = [] if rc == 0 else [f'codex exited {rc}']
    completed = turn_completed(events)
    if not any(event.get('type') == 'turn.completed' for event in events):
        reasons.append('codex reported no completed turn')
    for event in events:
        if event.get('type') == 'error' and completed and TRANSIENT_RE.search(str(event.get('message'))):
            continue
        if event.get('type') in ('turn.failed', 'error'):
            reasons.append(f'codex reported {event["type"]}: {json.dumps(event, ensure_ascii=False)[:300]}')
    reasons += [f'codex reported an error item: {str(item.get("message"))[:300]}'
                for item in completed_items(events, 'error')]
    if not diff_commands(events):
        reasons.append('no command with exit code 0 printed a unified diff (a `diff --git a/` line '
                       'and an `@@ ` hunk header), so nothing shows it read the diff; a PR with an empty diff '
                       'also ends here')
    if not message:
        reasons.append('codex wrote no final message')
    signal = FAILURE_SIGNAL_RE.search(message)
    if signal:
        reasons.append(f'the final message reports a failure ("{signal.group(0)}")')
    return reasons


def valid_confidence(value):
    return type(value) in (int, float) and 0 <= value <= 1


def confidence_text(value):
    """Keep ordinary scores readable and preserve every significant boundary digit."""
    if not valid_confidence(value):
        return '缺失或无效'
    short = f'{value:.2f}'
    return short if float(short) == value else str(value)


def attach_review_output(output, worktree, codex_home=None):
    """Read only this exec thread's rollout; carry the full result into the local audit stream."""
    event = {'type': 'review.structured'}
    try:
        threads = [e.get('thread_id') for e in read_events(output) if e.get('type') == 'thread.started']
        if len(threads) != 1 or not isinstance(threads[0], str) or not re.fullmatch(r'[A-Za-z0-9-]+', threads[0]):
            raise ValueError('expected exactly one exec thread')
        thread = event['thread_id'] = threads[0]
        home = Path(codex_home) if codex_home is not None else Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
        paths = list((home / 'sessions').glob(f'**/rollout-*-{thread}.jsonl'))
        if len(paths) != 1:
            raise ValueError('expected exactly one matching rollout')
        # Unlike exec stdout, a rollout is a JSONL file: damaged or incomplete rows fail closed.
        rows = [json.loads(line) for line in paths[0].read_text(encoding='utf-8').splitlines() if line.strip()]
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError('malformed rollout row')
        meta = [row.get('payload') for row in rows if row.get('type') == 'session_meta']
        # Repeated metadata is harmless only if every row identifies this same session/worktree.
        if not meta or any(not isinstance(m, dict) or m.get('id') != thread
                           or not isinstance(m.get('cwd'), str)
                           or os.path.realpath(m['cwd']) != os.path.realpath(worktree) for m in meta):
            raise ValueError('rollout session identity does not match exec thread/worktree')
        outputs = []
        for row in rows:
            payload = row.get('payload') if row.get('type') == 'event_msg' else row
            if not isinstance(payload, dict) or payload.get('type') != 'item_completed':
                continue
            item = payload.get('item')
            if isinstance(item, dict) and item.get('type') == 'ExitedReviewMode':
                if payload.get('thread_id', thread) != thread:
                    continue  # Forwarded sub-agent results cannot replace the parent review.
                outputs.append(item.get('review_output'))
        if len(outputs) != 1:
            raise ValueError('expected exactly one ExitedReviewMode output')
        event.update(review_output=outputs[0], rollout=str(paths[0]))
    except (OSError, ValueError, TypeError) as exc:
        event['error'] = str(exc)
    return output + '\n' + json.dumps(event, ensure_ascii=False) + '\n'


def structured_findings(events, message, worktree_roots):
    """Validate the structured result and reconcile all findings before filtering any scores."""
    threads = [e.get('thread_id') for e in events if e.get('type') == 'thread.started']
    envelopes = [e for e in events if e.get('type') == 'review.structured']
    if (len(threads) != 1 or not isinstance(threads[0], str) or not threads[0]
            or len(envelopes) != 1 or envelopes[0].get('thread_id') != threads[0]
            or envelopes[0].get('error')):
        raise ValueError('missing or ambiguous structured review for this thread')
    result = envelopes[0].get('review_output')
    if (not isinstance(result, dict) or not isinstance(result.get('findings'), list)
            or result.get('overall_correctness') not in ('patch is correct', 'patch is incorrect')
            or not isinstance(result.get('overall_explanation'), str)
            or not result['overall_explanation'].strip()
            or not valid_confidence(result.get('overall_confidence_score'))):
        raise ValueError('malformed structured review output')
    findings = []
    for raw in result['findings']:
        if not isinstance(raw, dict):
            raise ValueError('malformed structured finding')
        title, body, priority = raw.get('title'), raw.get('body'), raw.get('priority')
        prefix = re.match(r'^\[P([0-3])\]\s*', title) if isinstance(title, str) else None
        # Codex permits omitted/null priority. The explicit title tag can supply it;
        # without either source there is no safe way to apply the P0/P1 exemption.
        if priority is None and prefix:
            priority = int(prefix[1])
        location = raw.get('code_location')
        if (not isinstance(title, str) or not title.strip() or not isinstance(body, str) or not body.strip()
                or type(priority) is not int or priority not in range(4) or not isinstance(location, dict)
                or (prefix and int(prefix[1]) != priority)):
            raise ValueError('malformed structured finding')
        path, lines = location.get('absolute_file_path'), location.get('line_range')
        if (not isinstance(path, str) or not path.startswith('/') or not isinstance(lines, dict)
                or type(lines.get('start')) is not int or type(lines.get('end')) is not int
                or not 1 <= lines['start'] <= lines['end']):
            raise ValueError('malformed structured finding location')
        title = re.sub(r'^\[P[0-3]\]\s*', '', title).strip()
        finding = Finding(f'P{priority}', title, strip_prefix(path, worktree_roots), lines['start'], lines['end'])
        finding.lines = body.splitlines()
        finding.confidence_score = raw.get('confidence_score')
        findings.append(finding)
    _, parsed_summary, rendered = parse_review(message, worktree_roots, deduplicate=False,
                                             expected_findings=findings)

    def key(finding):
        return (finding.priority, ' '.join(finding.title.split()), finding.location(),
                ' '.join(VERDICT_RE.sub('', finding.text).split()))

    structured_keys, rendered_keys = [key(f) for f in findings], [key(f) for f in rendered]
    if (len(set(structured_keys)) != len(structured_keys) or sorted(structured_keys) != sorted(rendered_keys)
            or (PRIORITY_RE.search(parsed_summary)
                and ' '.join(parsed_summary.split()) != ' '.join(result['overall_explanation'].split()))):
        raise ValueError('structured findings disagree with the final message')
    return findings


def assess(rc, events, message, worktree_roots=(), min_confidence=0.80, audit=None):
    """Clean requires every run/structure check and either an explicit, consistent empty
    review (CLEAN, no [Pn], not incorrect), or nonempty raw findings all withheld.
    Return (verdict, public summary, passing findings, reasons); compare raw scores strictly.
    """
    _, summary, _ = parse_review(message, worktree_roots)
    summary = VERDICT_RE.sub('', summary).strip()
    try:
        findings = structured_findings(events, message, worktree_roots)
    except ValueError as exc:
        return 'failed', '', [], failure_reasons(rc, events, message) + [str(exc)]
    declared = VERDICT_RE.findall(message)
    posted = [f for f in findings if f.priority in ('P0', 'P1')
              or (valid_confidence(f.confidence_score) and f.confidence_score > min_confidence)]
    # A passing marker always needs the full clean-run evidence, including all-withheld runs.
    # Preserve main's non-passing positive-findings path when something is actually published.
    if not (rc == 0 and message and posted):
        reasons = failure_reasons(rc, events, message)
        if declared not in (['CLEAN'], ['FINDINGS']) or (not findings and declared != ['CLEAN']):
            reasons.append('the final message carries no single `VERDICT: CLEAN`'
                           + (' or `VERDICT: FINDINGS`' if findings else '') + ' line'
                           + (f' (found {", ".join(declared)})' if declared else ''))
        if not findings:
            result = next(e['review_output'] for e in events if e.get('type') == 'review.structured')
            if VERDICT_RE.findall(result['overall_explanation']) != ['CLEAN']:
                reasons.append('zero raw findings require a single `VERDICT: CLEAN` in overall_explanation')
            if PRIORITY_RE.search(result['overall_explanation']) or PRIORITY_RE.search(message):
                reasons.append('zero raw findings cannot carry a [Pn] tag in the explanation or final message')
            if result['overall_correctness'] == 'patch is incorrect':
                reasons.append('zero raw findings contradict overall_correctness: patch is incorrect')
        if reasons:
            return 'failed', '', [], reasons
    withheld = len(findings) - len(posted)
    if audit is not None:
        raw_findings = next(e['review_output']['findings'] for e in events if e.get('type') == 'review.structured')
        audit.update(min_confidence=min_confidence, withheld_count=withheld,
                     withheld=[raw for raw, finding in zip(raw_findings, findings) if finding not in posted])
    if findings:
        # The model's explanation may describe withheld findings; publish only an aggregate sentence.
        summary = f'已发布 {len(posted)} 条发现（P0/P1 不受置信度门槛限制，其他发现须严格大于阈值）。'
    if withheld:
        summary += f'\n\n另有 {withheld} 条置信度不高于 {confidence_text(min_confidence)} 的 P2/P3 发现未发布（含缺失或无效分数）。'
    return ('findings' if posted else 'clean'), summary, posted, []


def strip_prefix(path, worktree_roots):
    for root in worktree_roots:
        prefix = str(root).rstrip('/') + '/'
        if path.startswith(prefix):
            return path[len(prefix):]
    return path[2:] if path.startswith('./') else path


def parse_review(message, worktree_roots=(), deduplicate=True, expected_findings=()):
    """Split a final codex message into (verdict, summary, findings)."""
    findings, summary, current, seen = [], [], None, set()
    expected = {(f.priority, ' '.join(f.title.split()), f.location()): ' '.join(f.text.split())
                for f in expected_findings}
    for line in message.splitlines():
        # Consume a known structured body before interpreting heading-like example lines inside it.
        body = expected.get((current.priority, ' '.join(current.title.split()), current.location())) if current else None
        consumed = ' '.join(current.text.split()) if current else ''
        if body and consumed != body and body.startswith(consumed):
            current.lines.append(line)
            continue
        match = FINDING_RE.match(line)
        if match:
            priority, title, path, start, end = match.groups()
            current = Finding(priority, title, strip_prefix(path, worktree_roots), int(start), int(end or start))
            key = (current.priority, current.title, current.location())
            if deduplicate and key in seen:
                current = Finding(priority, title, path, 0, 0)
            else:
                seen.add(key)
                findings.append(current)
        elif current is not None:
            if line.strip():
                current.lines.append(line)
        elif line.strip() not in ('Full review comments:', 'Review comments:', 'Review comment:'):
            summary.append(line)
    verdict = 'findings' if findings or PRIORITY_RE.search(message) else 'clean'
    return verdict, '\n'.join(summary).strip(), findings


def diff_lines(files):
    """Map path -> {right-side line: hunk index} for lines a review comment may target."""
    result = {}
    for item in files:
        patch, lines, hunk, line = item.get('patch'), {}, -1, 0
        if not patch:
            continue
        for row in patch.splitlines():
            header = HUNK_RE.match(row)
            if header:
                hunk, line = hunk + 1, int(header.group(1))
            elif hunk >= 0 and row[:1] in (' ', '+'):
                lines[line] = hunk
                line += 1
        result[item['filename']] = lines
    return result


def inline_comments(findings, diff):
    comments = []
    for finding in findings:
        lines = diff.get(finding.path)
        if not lines or finding.end not in lines:
            continue
        comment = {'path': finding.path, 'line': finding.end, 'side': 'RIGHT',
                   'body': f'**[{finding.priority}] {finding.title}**'
                   + f' · 置信度 {confidence_text(finding.confidence_score)}'
                   + f'\n\n{finding.text}'}
        if finding.start != finding.end and lines.get(finding.start) == lines[finding.end]:
            comment.update(start_line=finding.start, start_side='RIGHT')
        comments.append(comment)
    return comments


def review_body(head, verdict, summary, findings, reason='', effort=None, kept=None, reasons=(),
                governance=()):
    """Render the review body; it always carries the marker and every finding in full.

    effort is set once codex ran, which is always read-only; kept names where the full codex output is preserved.
    """
    parts = [MARKER.format(head=head, verdict=verdict, effort=f' effort={effort}' if effort else '',
                           tests=' tests=read-only' if effort else '')]
    if verdict == 'failed':
        parts.append(f'**Remote codex review failed** for `{head}`: {reason}. This is not a pass.'
                     + ''.join(f'\n- {item}' for item in reasons)
                     + (f'\n\nFull codex output is kept on the review host at `{kept}`.' if kept else ''))
    elif verdict == 'clean':
        parts.append(f'**Remote codex review: no findings** for `{head}`.')
    else:
        parts.append(f'**Remote codex review: {len(findings)} finding(s)** for `{head}`.')
    if effort:
        parts.append('Read-only review: codex was told not to run tests or start services.')
    parts += [f'Governance issue `{doc.ref}` linked from AGENTS.md could not be prefetched ({doc.error}); '
              'codex reviewed without it.' for doc in governance if doc.error]
    if summary:
        parts.append(summary)
    for finding in findings:
        parts.append(f'- **[{finding.priority}] {finding.title}** — `{finding.location()}`'
                     + f' · 置信度 {confidence_text(finding.confidence_score)}'
                     + '\n\n'
                     + '\n'.join('  ' + line for line in finding.text.splitlines()))
    body = '\n\n'.join(parts)
    if len(body) > BODY_LIMIT:
        body = body[:BODY_LIMIT] + (f'\n\n[truncated; full text kept on the review host at `{kept}`]'
                                    if kept else '\n\n[truncated]')
    return body


# ---- persistent queue ------------------------------------------------------

class Job:
    def __init__(self, row):
        (self.id, self.full_name, self.number, self.head, self.base, self.installation,
         self.status, self.attempts) = row

    def __str__(self):
        return f'{self.full_name}#{self.number}@{self.head[:12]}'


JOB_COLUMNS = 'id, repo, pr, head_sha, base_ref, installation_id, status, attempts'


class Store:
    """SQLite queue of (repo, pr, head_sha) jobs plus webhook delivery and redelivery ledgers."""

    def __init__(self, path, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.executescript(
            'CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'repo TEXT NOT NULL COLLATE NOCASE, pr INTEGER NOT NULL, head_sha TEXT NOT NULL, '
            'base_ref TEXT NOT NULL, installation_id INTEGER NOT NULL, status TEXT NOT NULL, '
            'attempts INTEGER NOT NULL DEFAULT 0, not_before REAL NOT NULL DEFAULT 0, review_id INTEGER, '
            'updated_at REAL NOT NULL, UNIQUE (repo, pr, head_sha));'
            'CREATE TABLE IF NOT EXISTS deliveries (guid TEXT PRIMARY KEY, received_at REAL NOT NULL);'
            'CREATE TABLE IF NOT EXISTS redeliveries (delivery_id INTEGER PRIMARY KEY, requested_at REAL NOT NULL);'
            'CREATE TABLE IF NOT EXISTS retries (job_id INTEGER PRIMARY KEY, count INTEGER NOT NULL, '
            'since REAL NOT NULL, last_error TEXT NOT NULL);')

    def _transaction(self, work):
        """Run work inside BEGIN IMMEDIATE; SQLite may already have rolled back after SQLITE_FULL."""
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                result = work()
            except BaseException:
                if self.db.in_transaction:
                    try:
                        self.db.execute('ROLLBACK')
                    except sqlite3.Error as exc:
                        log(f'rollback failed: {exc}')
                raise
            self.db.execute('COMMIT')
            return result

    def _enqueue(self, repo, pr, head, base, installation):
        """Queue head as the only live job of its PR; return (queued?, ids of superseded running jobs)."""
        now = self.clock()
        row = self.db.execute('SELECT id, status FROM jobs WHERE repo = ? AND pr = ? AND head_sha = ?',
                              (repo, pr, head)).fetchone()
        if row and row[1] != 'superseded':
            return False, []
        running = [r[0] for r in self.db.execute(
            "SELECT id FROM jobs WHERE repo = ? AND pr = ? AND head_sha != ? AND status = 'running'",
            (repo, pr, head))]
        self.db.execute("UPDATE jobs SET status = 'superseded', updated_at = ? WHERE repo = ? AND pr = ? "
                        "AND head_sha != ? AND status IN ('queued', 'running')", (now, repo, pr, head))
        if row:
            self.db.execute("UPDATE jobs SET status = 'queued', attempts = 0, not_before = 0, base_ref = ?, "
                            'installation_id = ?, updated_at = ? WHERE id = ?', (base, installation, now, row[0]))
            self.db.execute('DELETE FROM retries WHERE job_id = ?', (row[0],))
        else:
            self.db.execute("INSERT INTO jobs (repo, pr, head_sha, base_ref, installation_id, status, updated_at) "
                            "VALUES (?, ?, ?, ?, ?, 'queued', ?)", (repo, pr, head, base, installation, now))
        return True, running

    def accept(self, guid, job=None):
        """Record a delivery once; returns ('duplicate'|'ignored'|'queued'|'known', superseded running ids)."""
        def work():
            inserted = self.db.execute('INSERT OR IGNORE INTO deliveries VALUES (?, ?)',
                                       (guid, self.clock())).rowcount
            if not inserted:
                return 'duplicate', []
            if job is None:
                return 'ignored', []
            queued, running = self._enqueue(*job)
            return ('queued' if queued else 'known'), running
        return self._transaction(work)

    def enqueue(self, repo, pr, head, base, installation):
        return self._transaction(lambda: self._enqueue(repo, pr, head, base, installation))

    def delivery_seen(self, guid):
        with self.lock:
            return self.db.execute('SELECT 1 FROM deliveries WHERE guid = ?', (guid,)).fetchone() is not None

    def claim_next(self):
        def work():
            row = self.db.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE status = 'queued' AND not_before <= ? "
                                  'ORDER BY id LIMIT 1', (self.clock(),)).fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE jobs SET status = 'running', updated_at = ? WHERE id = ?", (self.clock(), row[0]))
            return Job(row[:6] + ('running',) + row[7:])
        return self._transaction(work)

    def status(self, job_id):
        with self.lock:
            row = self.db.execute('SELECT status FROM jobs WHERE id = ?', (job_id,)).fetchone()
            return row[0] if row else None

    def finish(self, job_id, status, review_id=None):
        """Set a terminal status unless the job was superseded meanwhile; returns whether it applied."""
        with self.lock:
            return self.db.execute("UPDATE jobs SET status = ?, review_id = ?, updated_at = ? "
                                   "WHERE id = ? AND status = 'running'",
                                   (status, review_id, self.clock(), job_id)).rowcount == 1

    def retry(self, job_id, delay, count_attempt=True):
        with self.lock:
            self.db.execute("UPDATE jobs SET status = 'queued', attempts = attempts + ?, not_before = ?, "
                            "updated_at = ? WHERE id = ? AND status = 'running'",
                            (1 if count_attempt else 0, self.clock() + delay, self.clock(), job_id))

    def transient_state(self, job_id):
        """Return (retries so far, time of the first transient failure, last error) or None."""
        with self.lock:
            return self.db.execute('SELECT count, since, last_error FROM retries WHERE job_id = ?',
                                   (job_id,)).fetchone()

    def defer_transient(self, job_id, count, since, error, delay):
        """Record a transient failure and requeue the running job after delay seconds."""
        def work():
            self.db.execute('INSERT OR REPLACE INTO retries VALUES (?, ?, ?, ?)', (job_id, count, since, error))
            self.db.execute("UPDATE jobs SET status = 'queued', not_before = ?, updated_at = ? "
                            "WHERE id = ? AND status = 'running'", (self.clock() + delay, self.clock(), job_id))
        self._transaction(work)

    def clear_transient(self, job_id):
        with self.lock:
            self.db.execute('DELETE FROM retries WHERE job_id = ?', (job_id,))

    def reset_running(self):
        with self.lock:
            return self.db.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'").rowcount

    def force_enqueue(self, repo, pr, head, base, installation):
        """Queue head even when it already finished; returns whether a job was queued."""
        def work():
            self.db.execute("UPDATE jobs SET status = 'superseded' WHERE repo = ? AND pr = ? AND head_sha = ? "
                            "AND status NOT IN ('queued', 'running')", (repo, pr, head))
            return self._enqueue(repo, pr, head, base, installation)[0]
        return self._transaction(work)

    def requeue(self, repo, pr, head):
        with self.lock:
            return self.db.execute("UPDATE jobs SET status = 'queued', attempts = 0, not_before = 0 "
                                   'WHERE repo = ? AND pr = ? AND head_sha = ?', (repo, pr, head)).rowcount

    def queued(self):
        with self.lock:
            return [Job(row) for row in self.db.execute(
                f"SELECT {JOB_COLUMNS} FROM jobs WHERE status IN ('queued', 'running') ORDER BY id")]

    def redelivered(self, delivery_id):
        with self.lock:
            return self.db.execute('SELECT 1 FROM redeliveries WHERE delivery_id = ?',
                                   (delivery_id,)).fetchone() is not None

    def mark_redelivered(self, delivery_id):
        with self.lock:
            self.db.execute('INSERT OR IGNORE INTO redeliveries VALUES (?, ?)', (delivery_id, self.clock()))


# ---- webhook receiver ------------------------------------------------------

def signature_valid(secret, body, header):
    """Check X-Hub-Signature-256 in constant time."""
    if not secret or not header or not header.startswith('sha256='):
        return False
    expected = 'sha256=' + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), header.encode())


class WebhookApp:
    """Validate, deduplicate and enqueue deliveries; answers with a status code and no body."""

    def __init__(self, config, store, secret, on_queued=lambda running: None):
        self.config, self.store, self.secret, self.on_queued = config, store, secret, on_queued

    def handle(self, method, path, headers, body_reader):
        if urllib.parse.urlsplit(path).path != WEBHOOK_PATH:
            return 404
        if method != 'POST':
            return 405
        try:
            length = int(headers.get('Content-Length', ''))
        except ValueError:
            return 411
        if length < 0:
            return 400
        if length > self.config.max_body_bytes:
            return 413
        body = body_reader(length)
        if not signature_valid(self.secret, body, headers.get('X-Hub-Signature-256')):
            return 401
        guid = headers.get('X-GitHub-Delivery', '')
        if not guid:
            return 400
        try:
            payload = json.loads(body)
        except ValueError:
            return 400
        job = self.job_for(headers.get('X-GitHub-Event', ''), payload, guid)
        try:
            outcome, running = self.store.accept(guid, job)
        except sqlite3.Error as exc:
            log(f'delivery {guid}: not recorded, answering 503 so GitHub retries it: {exc}')
            return 503
        if outcome == 'queued':
            log(f'delivery {guid}: queued {job[0]}#{job[1]}@{job[2][:12]}')
        self.on_queued(running)
        return 202

    def job_for(self, event, payload, guid):
        if event == 'installation':
            account = ((payload.get('installation') or {}).get('account') or {}).get('login', '')
            allowed = 'allowed' if self.config.allowed_account(account) else 'not in CODEX_REVIEW_ACCOUNTS'
            log(f'delivery {guid}: installation {payload.get("action")} by {account}: {allowed}')
            return None
        if event != 'pull_request' or payload.get('action') not in ACTIONS:
            return None
        repository, pull = payload.get('repository') or {}, payload.get('pull_request') or {}
        reason = admission(self.config, repository, pull, payload.get('installation'))
        if reason is None:
            try:
                return (repository['full_name'], pull['number'], pull['head']['sha'], pull['base']['ref'],
                        payload['installation']['id'])
            except (KeyError, TypeError):
                reason = 'payload lacks a required field'
        log(f'delivery {guid}: skipped {repository.get("full_name")}#{pull.get("number")}: {reason}')
        return None


def make_server(app, port):
    class Handler(http.server.BaseHTTPRequestHandler):
        timeout = 30
        server_version = 'remote-codex-review'
        sys_version = ''

        def respond(self):
            status = app.handle(self.command, self.path, self.headers, self.rfile.read)
            self.send_response(status)
            self.send_header('Content-Length', '0')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True

        do_POST = do_GET = do_PUT = do_DELETE = do_HEAD = respond

        def log_message(self, fmt, *args):
            log('http ' + fmt % args)

    return http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler)


# ---- missed-delivery recovery ----------------------------------------------

def redeliver_failed(github, store, lookback_seconds, clock=time.time, dry_run=False):
    """Ask GitHub to redeliver pull_request deliveries that never succeeded and were never received."""
    attempts = {}
    for delivery in github.recent_deliveries(clock() - lookback_seconds):
        attempts.setdefault(delivery['guid'], []).append(delivery)
    requested = []
    for guid, items in attempts.items():
        if any(200 <= (item.get('status_code') or 0) < 300 for item in items):
            continue
        if items[0].get('event') != 'pull_request' or store.delivery_seen(guid):
            continue
        latest = max(items, key=lambda item: item['id'])
        if store.redelivered(latest['id']):
            continue
        if latest.get('installation_id') is None or not github.installation_allowed(latest['installation_id']):
            log(f'redelivery of {latest["id"]} guid={guid} skipped: installation not in CODEX_REVIEW_ACCOUNTS')
            continue
        if dry_run:
            print(f'would redeliver {latest["id"]} guid={guid} action={latest.get("action")}', flush=True)
        else:
            github.redeliver(latest['id'])
            store.mark_redelivered(latest['id'])
            log(f'redelivery requested for {latest["id"]} guid={guid}')
        requested.append(latest['id'])
    return requested


# ---- git mirror and codex --------------------------------------------------

def auth_env(token):
    """Pass the token as http.extraheader through the environment, never argv or URL."""
    basic = base64.b64encode(f'x-access-token:{token}'.encode()).decode()
    return dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_CONFIG_COUNT='1',
                GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                GIT_CONFIG_VALUE_0=f'AUTHORIZATION: basic {basic}')


class Git:
    def __init__(self, state_dir):
        self.mirrors = state_dir / 'mirrors'
        self.work = state_dir / 'work'

    def run(self, args, env=None, timeout=900):
        result = subprocess.run(['git', *args], env=env, capture_output=True, text=True,
                                timeout=timeout, check=False, stdin=subprocess.DEVNULL)
        if result.returncode:
            raise RuntimeError(f'git {args[0]} {args[1] if len(args) > 1 else ""} failed: '
                               f'{result.stderr.strip()[-500:]}')
        return result.stdout.strip()

    def mirror(self, full_name):
        return self.mirrors / f'{full_name}.git'

    def fetch(self, full_name, number, base, token):
        """Fetch the PR head and base branch into the bare mirror; return the fetched head sha."""
        mirror = self.mirror(full_name)
        if not (mirror / 'HEAD').exists():
            mirror.parent.mkdir(parents=True, exist_ok=True)
            self.run(['init', '--quiet', '--bare', str(mirror)])
        self.run(['-C', str(mirror), 'fetch', '--quiet', '--no-tags', f'https://github.com/{full_name}.git',
                  f'+refs/pull/{number}/head:refs/codex/pr/{number}',
                  f'+refs/heads/{base}:refs/remotes/origin/{base}'], env=auth_env(token))
        return self.run(['-C', str(mirror), 'rev-parse', f'refs/codex/pr/{number}'])

    def changed_files(self, full_name, base, head):
        out = self.run(['-C', str(self.mirror(full_name)), 'diff', '--name-only', '--no-renames', '-z',
                        f'refs/remotes/origin/{base}...{head}'])
        return [name for name in out.split('\0') if name]

    def add_worktree(self, full_name, path, head):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.run(['-C', str(self.mirror(full_name)), 'worktree', 'add', '--quiet', '--detach', str(path), head])

    def remove_worktree(self, full_name, path):
        mirror = self.mirror(full_name)
        try:
            self.run(['-C', str(mirror), 'worktree', 'remove', '--force', str(path)])
        finally:
            shutil.rmtree(path, ignore_errors=True)
            if mirror.exists():
                self.run(['-C', str(mirror), 'worktree', 'prune'])

    def sweep(self):
        if self.work.exists():
            for leftover in self.work.iterdir():
                if leftover.is_dir():
                    shutil.rmtree(leftover, ignore_errors=True)
                else:
                    leftover.unlink()
        if self.mirrors.exists():
            for mirror in self.mirrors.glob('*/*.git'):
                self.run(['-C', str(mirror), 'worktree', 'prune'])


class GovernanceDoc:
    def __init__(self, ref, path=None, error=None):
        self.ref, self.path, self.error = ref, path, error


def governance_links(text):
    """Return the distinct (owner/repo, number) issue links in text, in order, at most GOVERNANCE_LIMIT."""
    links = []
    for owner, repo, number in ISSUE_LINK_RE.findall(text):
        if repo in ('.', '..'):
            continue
        link = (f'{owner}/{repo}', int(number))
        if not any(same_name(link[0], seen[0]) and link[1] == seen[1] for seen in links):
            links.append(link)
    return links[:GOVERNANCE_LIMIT]


def prefetch_governance(github, token, worktree, target):
    """Save every issue linked from the worktree's AGENTS.md, with its comments, as target/owner/repo/N.md (0444).

    A failed fetch is recorded on its GovernanceDoc instead of raised, so the review still runs.
    """
    try:
        text = (Path(worktree) / 'AGENTS.md').read_text(encoding='utf-8', errors='replace')
    except FileNotFoundError:
        return []
    docs = []
    for full_name, number in governance_links(text):
        doc = GovernanceDoc(f'{full_name}#{number}')
        docs.append(doc)
        try:
            issue = github.issue(full_name, number, token)
            content = f"# {issue['title']}\n\n{issue.get('body') or ''}\n" + ''.join(
                f"\n## Comment by {(item.get('user') or {}).get('login', '?')} at {item.get('created_at', '?')}\n\n"
                f"{item.get('body') or ''}\n" for item in github.issue_comments(full_name, number, token))
        except (GitHubError, OSError, KeyError, TypeError) as exc:
            doc.error = str(exc)[:200] or type(exc).__name__
            continue
        doc.path = target / full_name / f'{number}.md'
        doc.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        doc.path.write_text(content, encoding='utf-8')
        doc.path.chmod(0o444)
    return docs


def review_prompt(base, governance=()):
    """Name the base in the prompt, which must demand the verdict line; codex rejects --base with a prompt."""
    parts = [BASE_PROMPT.format(base=base), READ_ONLY_RULE]
    fetched = [f'- {doc.ref}: {doc.path}' for doc in governance if doc.path]
    missing = [f'- {doc.ref}' for doc in governance if not doc.path]
    if fetched:
        parts.append('\n'.join([GOVERNANCE_RULE, *fetched]))
    if missing:
        parts.append('\n'.join([GOVERNANCE_MISSING_RULE, *missing]))
    return '\n\n'.join([*parts, VERDICT_RULE])


class CodexRunner:
    """Run `codex exec review` in its own process group; the group is killed on timeout or cancel."""

    def __init__(self, codex):
        self.codex = codex
        self.lock = threading.Lock()
        self.groups = {}

    def command(self, base, last_message, effort, governance=()):
        return [self.codex, 'exec', '--json', '--sandbox', 'read-only', '-c', 'approval_policy="never"',
                '-c', f'model_reasoning_effort={effort}', 'review', review_prompt(base, governance),
                '-o', str(last_message)]

    def __call__(self, job_id, worktree, base, effort, timeout, scratch, proceed, governance=()):
        """Return (rc, JSONL stdout, stderr, last message, timed out); rc is None when proceed() refused the start.

        proceed() runs under the lock that cancel() takes, so a cancel issued before registration is not lost.
        """
        last_message, output, errors = scratch / 'last-message.txt', scratch / 'events.jsonl', scratch / 'stderr.log'
        with open(output, 'wb') as sink, open(errors, 'wb') as error_sink:
            with self.lock:
                if not proceed():
                    return None, '', '', '', False
                process = subprocess.Popen(self.command(base, last_message, effort, governance), cwd=str(worktree),
                                           stdin=subprocess.DEVNULL, stdout=sink, stderr=error_sink,
                                           start_new_session=True)
                self.groups[job_id] = process.pid
            timed_out = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self.kill(process.pid)
                process.wait()
            finally:
                with self.lock:
                    self.groups.pop(job_id, None)
        text = output.read_text(encoding='utf-8', errors='replace')
        stderr = errors.read_text(encoding='utf-8', errors='replace')
        last = last_message.read_text(encoding='utf-8', errors='replace') if last_message.exists() else ''
        text = attach_review_output(text, worktree)
        return process.returncode, text, stderr, last, timed_out

    @staticmethod
    def kill(pgid, grace=5.0):
        try:
            os.killpg(pgid, signal.SIGTERM)
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                time.sleep(0.1)
                os.killpg(pgid, 0)
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return

    def cancel(self, job_ids):
        with self.lock:
            groups = [self.groups[job_id] for job_id in job_ids if job_id in self.groups]
        for pgid in groups:
            self.kill(pgid)

    def cancel_all(self):
        with self.lock:
            groups = list(self.groups.values())
        for pgid in groups:
            self.kill(pgid)


def self_check(codex, work_dir, timeout=300):
    """Have codex read a random token from a throwaway git repository; return None, or why it failed."""
    token = secrets.token_hex(16)
    scratch = work_dir / f'self-check-{token[:12]}'
    repo, last = scratch / 'repo', scratch / 'last-message.txt'
    try:
        repo.mkdir(parents=True)
        (repo / 'token.txt').write_text(token + '\n', encoding='utf-8')
        subprocess.run(['git', 'init', '--quiet', str(repo)], check=True, capture_output=True, timeout=60)
        process = subprocess.Popen(
            [codex, 'exec', '--json', '--ephemeral', '--sandbox', 'read-only', '-c', 'approval_policy="never"',
             '-c', 'model_reasoning_effort=low', '-o', str(last), SELF_CHECK_PROMPT],
            cwd=str(repo), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            CodexRunner.kill(process.pid)
            process.communicate()
            return f'codex did not finish within {timeout} s'
        last_text = last.read_text(encoding='utf-8', errors='replace') if last.exists() else ''
    except (OSError, subprocess.SubprocessError) as exc:
        return f'could not run codex: {exc}'
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    events = read_events(stdout)
    tail = ' | '.join(stderr.strip().splitlines()[-3:])
    if process.returncode:
        return f'codex exited {process.returncode}: {tail}'
    errors = [str(item.get('message'))[:300] for item in completed_items(events, 'error')]
    if errors:
        return f'codex reported an error: {errors[0]}'
    if not any(token in str(item.get('aggregated_output', '')) for item in ran_commands(events)):
        return 'no command codex ran printed the token, so codex cannot read repository files'
    if token not in final_message(events, last_text):
        return 'the final message does not contain the token'
    return None


# ---- review worker ---------------------------------------------------------

class Reviewer:
    def __init__(self, config, github, store, git, codex, stop=None, disk_free=None):
        self.config, self.github, self.store, self.git, self.codex = config, github, store, git, codex
        self.stop = stop or threading.Event()
        self.wake = threading.Event()
        self.repo_locks = {}
        self.locks_guard = threading.Lock()
        self.disk_free = disk_free or (lambda: shutil.disk_usage(self.config.state_dir).free)
        self.disk_low, self.free_bytes = False, None
        self.backoff_first, self.backoff_max = 5, 300

    def disk_ok(self):
        """Report whether the state directory has enough free space; log only when that changes."""
        try:
            free = self.disk_free()
        except OSError as exc:
            log(f'free-space check of {self.config.state_dir} failed, claiming anyway: {exc}')
            return True
        low = free < self.config.min_free_bytes
        with self.locks_guard:
            previous, self.disk_low, self.free_bytes = self.disk_low, low, free
        if low and not previous:
            log(f'error: {free // (1024 * 1024)} MB free under {self.config.state_dir}, below '
                f'CODEX_REVIEW_MIN_FREE_MB={self.config.min_free_bytes // (1024 * 1024)}; '
                'queued reviews wait until space is freed')
        elif previous and not low:
            log(f'disk space restored: {free // (1024 * 1024)} MB free under {self.config.state_dir}')
        return not low

    def health(self):
        if self.disk_low:
            return f'disk space low: {self.free_bytes // (1024 * 1024)} MB free; not claiming reviews'
        return 'ok'

    def repo_lock(self, full_name):
        with self.locks_guard:
            return self.repo_locks.setdefault(full_name.casefold(), threading.Lock())

    def on_queued(self, running):
        if running:
            threading.Thread(target=self.codex.cancel, args=(running,), daemon=True).start()
        self.wake.set()

    def follow_head(self, job, pull):
        """Mark job superseded; queue the PR's current head when it passes admission."""
        self.store.finish(job.id, 'superseded')
        if admission(self.config, pull['base']['repo'], pull) is None:
            queued, running = self.store.enqueue(job.full_name, job.number, pull['head']['sha'],
                                                 pull['base']['ref'], job.installation)
            if queued:
                log(f'{job}: queued current head {pull["head"]["sha"][:12]}')
            self.on_queued(running)
        return 'superseded'

    def gate(self, job, pull):
        """Return the job's outcome when the live PR no longer matches it or fails admission, else None."""
        if pull.get('state') != 'open' or pull['head']['sha'] != job.head:
            log(f'{job}: head moved or PR closed; not reviewed')
            return self.follow_head(job, pull)
        reason = admission(self.config, pull['base']['repo'], pull)
        if reason is not None:
            log(f'{job}: no longer admitted: {reason}')
            self.store.finish(job.id, 'skipped')
            return 'skipped'
        return None

    def keep_output(self, job, output, stderr, last):
        """Write codex output to state_dir/output/<job id>.log with mode 0600."""
        path = self.config.state_dir / 'output' / f'{job.id}.log'
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(output + ('\n--- stderr ---\n' + stderr if stderr else '')
                         + ('\n--- last message ---\n' + last if last else ''))

    def work_forever(self):
        """Loop over work_once until stop; an error anywhere in an iteration backs off instead of ending the thread."""
        failures = 0
        while not self.stop.is_set():
            try:
                busy = self.work_once()
            except Exception as exc:  # noqa: BLE001 - the worker must outlive every error
                failures += 1
                delay = min(self.backoff_first * 2 ** (failures - 1), self.backoff_max)
                log(f'worker error #{failures}, next attempt in {delay} s: {exc!r}')
                self.stop.wait(delay)
                continue
            failures = 0
            if not busy:
                self.wake.wait(30)
                self.wake.clear()

    def work_once(self):
        """Run the next due job when disk space allows; a failed job is requeued so the worker survives."""
        if not self.disk_ok():
            return False
        job = self.store.claim_next()
        if job is None:
            return False
        try:
            self.run(job)
        except Exception as exc:  # noqa: BLE001 - keep the worker alive
            log(f'{job}: unexpected error, requeued: {exc}')
            try:
                self.store.retry(job.id, 300)
            except sqlite3.Error as store_exc:
                log(f'{job}: could not requeue, stays running until restart: {store_exc}')
        return True

    def run(self, job):
        """Review one claimed job; returns its terminal status, or None when it was requeued."""
        try:
            return self._review(job)
        except InstallationRejected as exc:
            log(f'{job}: rejected without review: {exc}')
            self.store.finish(job.id, 'rejected')
            return 'rejected'
        except Exception as exc:  # noqa: BLE001 - non-codex failures retry, then end as failed
            if self.stop.is_set():
                self.store.retry(job.id, 0, count_attempt=False)
                return None
            if job.attempts + 1 < self.config.max_attempts:
                log(f'{job}: attempt {job.attempts + 1} failed, retrying: {exc}')
                self.store.retry(job.id, 300 * (job.attempts + 1))
                return None
            log(f'{job}: giving up after {job.attempts + 1} attempts: {exc}')
            token = self.github.token(job.installation)
            outcome = self.gate(job, self.github.pull(job.full_name, job.number, token))
            if outcome is not None:
                return outcome
            return self.publish(job, 'failed', review_body(job.head, 'failed', '', [], reason=(
                f'review infrastructure error after {job.attempts + 1} attempts: {exc}')), [], token)

    def defer_or_give_up(self, job, error, effort, kept, governance=()):
        """Requeue with backoff after a transient upstream error; once the window is spent, return a failed body."""
        now = self.store.clock()
        count, since, _ = self.store.transient_state(job.id) or (0, now, '')
        delay = min(self.config.retry_first * 2 ** count, self.config.retry_max_delay)
        if now - since + delay <= self.config.retry_window:
            self.store.defer_transient(job.id, count + 1, since, error, delay)
            log(f'{job}: transient codex failure #{count + 1}, retrying in {delay} s: {error}')
            return None
        log(f'{job}: transient codex failure #{count + 1}, {self.config.retry_window} s retry window spent: {error}')
        self.store.clear_transient(job.id)
        return review_body(job.head, 'failed', '', [], effort=effort, kept=kept, governance=governance, reason=(
            f'codex failed with transient upstream errors {count + 1} time(s) over {int(now - since)} s and the '
            f'{self.config.retry_window} s retry window is spent'), reasons=[f'last error: {error}'])

    def publish(self, job, verdict, body, comments, token=None):
        token = token or self.github.token(job.installation)
        existing = self.github.existing_marker(job.full_name, job.number, job.head, token)
        if existing:
            self.store.finish(job.id, existing[0], existing[1])
            return existing[0]
        if self.store.status(job.id) != 'running':
            return self.store.status(job.id)
        review_id, inline = self.github.post_review(job.full_name, job.number, job.head, body, comments, token)
        log(f'{job}: posted review {review_id} verdict={verdict} inline={inline}')
        self.store.finish(job.id, verdict, review_id)
        return verdict

    def _review(self, job):
        token = self.github.token(job.installation)
        existing = self.github.existing_marker(job.full_name, job.number, job.head, token)
        if existing:
            self.store.finish(job.id, existing[0], existing[1])
            return existing[0]
        pull = self.github.pull(job.full_name, job.number, token)
        outcome = self.gate(job, pull)
        if outcome is not None:
            return outcome
        base = pull['base']['ref']
        scratch = self.git.work / f'{job.id}'
        worktree = scratch / 'tree'
        with self.repo_lock(job.full_name):
            fetched = self.git.fetch(job.full_name, job.number, base, token)
            if fetched != job.head:
                raise RuntimeError(f'fetched head {fetched[:12]} differs from the queued head')
            changed = self.git.changed_files(job.full_name, base, job.head)
            effort = choose_effort(self.config.effort_rules, job.full_name, changed)
            shutil.rmtree(scratch, ignore_errors=True)
            self.git.add_worktree(job.full_name, worktree, job.head)
        try:
            governance = prefetch_governance(self.github, token, worktree, scratch / 'governance')
            for doc in governance:
                if doc.error:
                    log(f'{job}: governance issue {doc.ref} not prefetched: {doc.error}')
            log(f'{job}: codex review started against origin/{base} effort={effort} read-only')
            started = time.time()
            rc, output, stderr, last, timed_out = self.codex(job.id, worktree, base, effort,
                                                     self.config.timeout_seconds, scratch,
                                                     lambda: self.store.status(job.id) == 'running', governance)
            log(f'{job}: codex finished rc={rc} timed_out={timed_out} after {int(time.time() - started)} s')
        finally:
            with self.repo_lock(job.full_name):
                self.git.remove_worktree(job.full_name, worktree)
            shutil.rmtree(scratch, ignore_errors=True)
        if self.store.status(job.id) == 'superseded':
            log(f'{job}: superseded by a newer head; result discarded')
            return 'superseded'
        if rc is None:
            log(f'{job}: codex not started; job is {self.store.status(job.id)}')
            return self.store.status(job.id)
        if self.stop.is_set():
            self.store.retry(job.id, 0, count_attempt=False)
            return None
        token = self.github.token(job.installation)
        pull = self.github.pull(job.full_name, job.number, token)
        outcome = self.gate(job, pull)
        if outcome is not None:
            return outcome
        if pull['base']['ref'] != base:
            log(f'{job}: base changed to {pull["base"]["ref"]} during review; requeued')
            self.store.retry(job.id, 0, count_attempt=False)
            return None
        events = read_events(output)
        message = final_message(events, last)
        comments = []
        audit = {}
        kept = f'$CODEX_REVIEW_STATE_DIR/output/{job.id}.log'
        upstream = None if timed_out else transient_error(rc, events)
        if upstream is not None:
            body = self.defer_or_give_up(job, upstream, effort, kept, governance)
            if body is None:
                return None
            self.keep_output(job, output, stderr, last)
            return self.publish(job, 'failed', body, [], token)
        self.store.clear_transient(job.id)
        if timed_out:
            verdict, body = 'failed', review_body(job.head, 'failed', '', [], effort=effort,
                                                  kept=kept, governance=governance, reason=(
                f'codex timed out after {self.config.timeout_seconds} s'))
        else:
            roots = (str(worktree), os.path.realpath(worktree))
            verdict, summary, findings, reasons = assess(rc, events, message, roots, self.config.min_confidence, audit)
            if verdict == 'failed':
                log(f'{job}: codex output rejected: {"; ".join(reasons)}')
                body = review_body(job.head, 'failed', '', [], effort=effort, kept=kept, governance=governance,
                                   reason='codex output did not pass the review checks', reasons=reasons)
            else:
                body = review_body(job.head, verdict, summary, findings, effort=effort,
                                   kept=kept, governance=governance)
            if findings:
                comments = inline_comments(findings, diff_lines(
                    self.github.pull_files(job.full_name, job.number, token)))
        if verdict == 'failed' or audit.get('withheld_count') or len(body) > BODY_LIMIT:
            self.keep_output(job, output + '\n--- confidence policy ---\n' + json.dumps(audit, ensure_ascii=False),
                             stderr, last)
        return self.publish(job, verdict, body, comments, token)


# ---- entry point -----------------------------------------------------------

def sd_notify(state):
    """Send a systemd notification when NOTIFY_SOCKET is set; never raises."""
    address = os.environ.get('NOTIFY_SOCKET')
    if not address:
        return
    if address.startswith('@'):
        address = '\0' + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(state.encode(), address)
    except OSError as exc:
        log(f'sd_notify failed: {exc}')


def heartbeat(done, status, interval=SUPERVISE_INTERVAL, notify=None):
    """Send WATCHDOG=1 every interval until done is set; covers startup steps of unbounded total length."""
    notify = notify or sd_notify
    while not done.is_set():
        notify(f'WATCHDOG=1\nSTATUS={status}')
        done.wait(interval)


def supervise(reviewer, workers, interval=SUPERVISE_INTERVAL, notify=None):
    """Pet the watchdog while a worker lives; once all have exited, stop and return WORKERS_DEAD."""
    notify = notify or sd_notify
    while not reviewer.stop.is_set():
        alive = sum(worker.is_alive() for worker in workers)
        if alive == 0:
            log(f'error: all {len(workers)} review workers have exited; stopping so systemd restarts the service')
            notify(f'STATUS=all {len(workers)} review workers exited; restarting')
            reviewer.stop.set()
            reviewer.wake.set()
            return WORKERS_DEAD
        notify(f'WATCHDOG=1\nSTATUS={alive}/{len(workers)} workers alive; {reviewer.health()}')
        reviewer.stop.wait(interval)
    return 0


def serve(config, reviewer, secret):
    app = WebhookApp(config, reviewer.store, secret, on_queued=reviewer.on_queued)
    server = make_server(app, config.listen_port)
    workers = [threading.Thread(target=reviewer.work_forever, name=f'worker-{index}', daemon=True)
               for index in range(config.concurrency)]

    def recover():
        while not reviewer.stop.is_set():
            try:
                redeliver_failed(reviewer.github, reviewer.store, config.redeliver_hours * 3600)
            except Exception as exc:  # noqa: BLE001 - retried on the next hourly pass
                log(f'redelivery scan failed: {exc}')
            reviewer.stop.wait(REDELIVER_INTERVAL)

    def shutdown(*_):
        reviewer.stop.set()
        reviewer.wake.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    for worker in workers:
        worker.start()
    threading.Thread(target=recover, name='redelivery', daemon=True).start()
    threading.Thread(target=server.serve_forever, name='http', daemon=True).start()
    log(f'listening on 127.0.0.1:{config.listen_port}{WEBHOOK_PATH} with {config.concurrency} worker(s)')
    status = supervise(reviewer, workers)
    reviewer.codex.cancel_all()
    server.shutdown()
    for worker in workers:
        worker.join(timeout=60)
    return status


def enqueue_command(config, github, store, target):
    """Queue OWNER/REPO#PR@SHA after checking the installation and the live PR against admission."""
    match = re.fullmatch(r'([^#]+)#(\d+)@([0-9a-f]{40})', target)
    if not match:
        print('expected OWNER/REPO#PR@<40-hex sha>', file=sys.stderr)
        return 2
    full_name, number, head = match.group(1), int(match.group(2)), match.group(3)
    try:
        installation = github.repo_installation(full_name)
        pull = github.pull(full_name, number, github.token(installation))
    except (GitHubError, InstallationRejected) as exc:
        print(f'not queued: {exc}', file=sys.stderr)
        return 1
    reason = admission(config, pull['base']['repo'], pull)
    if reason:
        print(f'not queued: {reason}', file=sys.stderr)
        return 1
    if pull['head']['sha'] != head:
        print(f'note: {head[:12]} is no longer the head; queueing the current head {pull["head"]["sha"][:12]}')
        head = pull['head']['sha']
    queued = store.force_enqueue(full_name, number, head, pull['base']['ref'], installation)
    print(f'{"queued" if queued else "already queued"} {full_name}#{number}@{head[:12]}')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=DEFAULT_CONFIG)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true',
                      help='print queued jobs and the deliveries that would be redelivered; change nothing')
    mode.add_argument('--requeue', metavar='OWNER/REPO#PR@SHA', help='queue one recorded head for review again')
    mode.add_argument('--enqueue', metavar='OWNER/REPO#PR@SHA',
                      help='queue any head of an open, admitted PR, including one that was already reviewed')
    mode.add_argument('--invalidate', metavar='OWNER/REPO#PR:REVIEW_ID',
                      help="rewrite one of this App's reviews to --note and mark its verdict invalid")
    parser.add_argument('--note', help='replacement review text for --invalidate')
    args = parser.parse_args(argv)
    if args.invalidate and not (args.note or '').strip():
        parser.error('--invalidate needs --note')
    try:
        config = Config(read_env_file(args.config))
        secret = read_private(config.webhook_secret_file).strip()
    except (OSError, ConfigError) as exc:
        print(f'config error: {exc}', file=sys.stderr)
        return 2
    store = Store(config.state_dir / 'state.sqlite')
    if args.requeue:
        match = re.fullmatch(r'([^#]+)#(\d+)@([0-9a-f]{40})', args.requeue)
        if not match:
            print('expected OWNER/REPO#PR@<40-hex sha>', file=sys.stderr)
            return 2
        print(f'requeued {store.requeue(match.group(1), int(match.group(2)), match.group(3))} job(s)')
        return 0
    github = GitHub(config)
    if args.enqueue:
        return enqueue_command(config, github, store, args.enqueue)
    if args.invalidate:
        match = re.fullmatch(r'([^#]+)#(\d+):(\d+)', args.invalidate)
        if not match:
            print('expected OWNER/REPO#PR:REVIEW_ID', file=sys.stderr)
            return 2
        try:
            head, verdict = github.invalidate_review(match.group(1), int(match.group(2)), int(match.group(3)),
                                                     args.note.strip())
        except (ValueError, GitHubError, InstallationRejected) as exc:
            print(f'not invalidated: {exc}', file=sys.stderr)
            return 1
        print(f'review {match.group(3)} for {head} changed from verdict={verdict} to verdict=invalid')
        return 0
    if args.dry_run:
        for job in store.queued():
            print(f'would review {job} base=origin/{job.base} status={job.status}', flush=True)
        redeliver_failed(github, store, config.redeliver_hours * 3600, dry_run=True)
        return 0
    started = threading.Event()
    threading.Thread(target=heartbeat, name='startup-heartbeat', daemon=True,
                     args=(started, 'starting: requeue, mirror sweep, codex self-check', SUPERVISE_INTERVAL)).start()
    git = Git(config.state_dir)
    log(f'startup: {store.reset_running()} interrupted job(s) requeued')
    git.sweep()
    reason = self_check(config.codex, git.work)
    if reason:
        log(f'startup self-check failed, refusing to start: {reason}')
        return SELF_CHECK_FAILED
    log('startup self-check passed: codex read a repository file')
    reviewer = Reviewer(config, github, store, git, CodexRunner(config.codex))
    started.set()
    return serve(config, reviewer, secret)


if __name__ == '__main__':
    sys.exit(main())
