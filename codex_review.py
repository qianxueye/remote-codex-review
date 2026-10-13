#!/usr/bin/env python3
"""Receive GitHub App pull_request webhooks and post one codex review per latest PR head."""
import argparse
import base64
import calendar
import hashlib
import hmac
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
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
TRUSTED_ASSOCIATIONS = frozenset({'OWNER', 'MEMBER', 'COLLABORATOR'})
MARKER = '<!-- remote-codex-review head={head} verdict={verdict}{effort}{tests} -->'
MARKER_RE = re.compile(r'<!-- remote-codex-review head=([0-9a-f]{40}) verdict=(clean|findings|failed|invalid)'
                       r'(?: effort=[a-z]+)?(?: tests=[a-z-]+)? -->')
DEFAULT_EFFORT_RULES = {'medium': ['docs/wiki/**', 'docs/*.md', '**/README.md'], 'high': ['docs/runbooks/**'],
                        'tests': ['**/migrations/**', 'deploy/**', '.github/workflows/**', '**/*secret*',
                                  '**/*credential*']}
RULE_KEYS = frozenset({'medium', 'high', 'tests'})
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
SELF_CHECK_PROMPT = 'Run `cat token.txt` and reply with the exact contents of that file and nothing else.'
VERDICT_RE = re.compile(r'^[\s`*_]*VERDICT:\s*([A-Z]+)[\s`*_.]*$', re.M)
FAILURE_SIGNAL_RE = re.compile(
    r'\bblocked\b|placeholder|\bunverified\b|Reviewer failed to output a response|no such file or directory'
    r'|failed to spawn|\b(?:unable|could not|couldn\'t|cannot|can\'t|failed) to '
    r'(?:review|inspect|read|access|open|view|examine|load)\b'
    r'|\b(?:tool|command|executable|binary|host|helper|sandbox|component|program|mode)s? '
    r'(?:is |was |are |were )?(?:missing|unavailable|not found|not installed)'
    r'|(?:无法|未能)(?:检查|审查|读取|查看|访问)|工具缺失|缺少(?:组件|工具|程序)', re.I)
TRANSIENT_RE = re.compile(
    r'at capacity|overloaded|rate.?limit|usage limit|too many requests|\b429\b|\b5\d\d\b|service unavailable'
    r'|bad gateway|gateway time-?out|internal server error|stream disconnected|disconnected before completion'
    r'|connection (?:reset|closed|refused|error)|timed out|timeout|temporarily unavailable|network error'
    r'|error sending request', re.I)
PRIORITY_RE = re.compile(r'\[P[0-3]\]')
GITHUB_TIME_RE = re.compile(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z')
FINDING_RE = re.compile(r'^\s*[-*]\s*\[(P[0-3])\]\s*(.+?)\s+(?:—|–|--|-)\s+(\S+?):(\d+)(?:-(\d+))?\s*$')
HUNK_RE = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@')
BODY_LIMIT = 60000
SELF_CHECK_FAILED = 3
REDELIVER_INTERVAL = 3600
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
        self.codex = values.get('CODEX_REVIEW_CODEX_BIN', 'codex')
        self.effort_rules = load_effort_rules(values.get('CODEX_REVIEW_EFFORT_RULES', ''))
        self.tier = values.get('CODEX_REVIEW_TIER', 'auto')
        if self.tier not in ('auto', 'read-only'):
            raise ConfigError('CODEX_REVIEW_TIER must be auto or read-only')

    def excluded(self, full_name):
        return any(same_name(full_name, item) for item in self.exclude)

    def allowed_account(self, login):
        return any(same_name(login, item) for item in self.accounts)

    def allowed_author(self, login):
        return any(same_name(login, item) for item in self.authors)


def load_effort_rules(path):
    """Load {"default": rules, "repos": {"owner/repo": rules}}; rules map medium/high/tests to glob lists.

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
            raise ConfigError('CODEX_REVIEW_EFFORT_RULES: each table maps medium/high/tests to lists of globs')
        value = {k: v for k, v in value.items() if not str(k).startswith('_')}
        if (set(value) - RULE_KEYS
                or not all(isinstance(v, list) and all(isinstance(p, str) for p in v) for v in value.values())):
            raise ConfigError('CODEX_REVIEW_EFFORT_RULES: each table maps medium/high/tests to lists of globs')
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


def choose_tests(rules, full_name, files):
    """Return read-only only when the table lists tests globs and no changed file matches one."""
    table = rule_table(rules, full_name)
    if 'tests' not in table or not files:
        return 'tests'
    patterns = [glob_regex(p) for p in table['tests']]
    return 'tests' if any(r.match(f) for f in files for r in patterns) else 'read-only'


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
    author = (pull.get('user') or {}).get('login', '')
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
    if pull.get('author_association') not in TRUSTED_ASSOCIATIONS:
        return f'author_association {pull.get("author_association")} not trusted'
    if not config.allowed_author(author):
        return f'author {author} not in CODEX_REVIEW_AUTHORS'
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
    if not ran_commands(events):
        reasons.append('codex completed no command with exit code 0, so nothing shows it read the diff')
    if not message:
        reasons.append('codex wrote no final message')
    signal = FAILURE_SIGNAL_RE.search(message)
    if signal:
        reasons.append(f'the final message reports a failure ("{signal.group(0)}")')
    return reasons


def assess(rc, events, message, worktree_roots=()):
    """Return (verdict, summary, findings, reasons); clean needs every check to pass and `VERDICT: CLEAN`."""
    verdict, summary, findings = parse_review(message, worktree_roots)
    declared = VERDICT_RE.findall(message)
    summary = VERDICT_RE.sub('', summary).strip()
    if rc == 0 and message and (verdict == 'findings' or 'FINDINGS' in declared):
        return 'findings', summary, findings, []
    reasons = failure_reasons(rc, events, message)
    if declared != ['CLEAN']:
        reasons.append('the final message carries no single `VERDICT: CLEAN` line'
                       + (f' (found {", ".join(declared)})' if declared else ''))
    return ('failed' if reasons else 'clean'), summary, [], reasons


def strip_prefix(path, worktree_roots):
    for root in worktree_roots:
        prefix = str(root).rstrip('/') + '/'
        if path.startswith(prefix):
            return path[len(prefix):]
    return path[2:] if path.startswith('./') else path


def parse_review(message, worktree_roots=()):
    """Split a final codex message into (verdict, summary, findings)."""
    findings, summary, current, seen = [], [], None, set()
    for line in message.splitlines():
        match = FINDING_RE.match(line)
        if match:
            priority, title, path, start, end = match.groups()
            current = Finding(priority, title, strip_prefix(path, worktree_roots), int(start), int(end or start))
            key = (current.priority, current.title, current.location())
            if key in seen:
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
                   'body': f'**[{finding.priority}] {finding.title}**\n\n{finding.text}'}
        if finding.start != finding.end and lines.get(finding.start) == lines[finding.end]:
            comment.update(start_line=finding.start, start_side='RIGHT')
        comments.append(comment)
    return comments


def review_body(head, verdict, summary, findings, raw='', reason='', effort=None, kept=None, tests=None,
                reasons=()):
    """Render the review body; it always carries the marker and every finding in full.

    kept names where the full codex output is preserved; failed and truncated bodies point to it.
    """
    parts = [MARKER.format(head=head, verdict=verdict, effort=f' effort={effort}' if effort else '',
                           tests=f' tests={tests}' if tests else '')]
    if verdict == 'failed':
        parts.append(f'**Remote codex review failed** for `{head}`: {reason}. This is not a pass.'
                     + ''.join(f'\n- {item}' for item in reasons)
                     + (f'\n\nFull codex output is kept on the review host at `{kept}`.' if kept else ''))
    elif verdict == 'clean':
        parts.append(f'**Remote codex review: no findings** for `{head}`.')
    else:
        parts.append(f'**Remote codex review: {len(findings) or "unparsed"} finding(s)** for `{head}`.')
    if tests == 'read-only':
        parts.append('Read-only review: codex was told not to run tests or start services.')
    if summary:
        parts.append(summary)
    for finding in findings:
        parts.append(f'- **[{finding.priority}] {finding.title}** — `{finding.location()}`\n\n'
                     + '\n'.join('  ' + line for line in finding.text.splitlines()))
    if verdict == 'findings' and not findings and raw:
        parts.append(raw)
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
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                result = work()
            except BaseException:
                self.db.execute('ROLLBACK')
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
        outcome, running = self.store.accept(guid, job)
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


def review_prompt(base, tier):
    """Both tiers pass a prompt so it can demand the verdict line; codex rejects --base with a prompt."""
    rules = [READ_ONLY_RULE] if tier == 'read-only' else []
    return '\n\n'.join([BASE_PROMPT.format(base=base), *rules, VERDICT_RULE])


class CodexRunner:
    """Run `codex exec review` in its own process group; the group is killed on timeout or cancel."""

    def __init__(self, codex):
        self.codex = codex
        self.lock = threading.Lock()
        self.groups = {}

    def command(self, base, last_message, effort, tier='tests'):
        sandbox = 'workspace-write' if tier == 'tests' else 'read-only'
        return [self.codex, 'exec', '--json', '--sandbox', sandbox,
                '-c', 'sandbox_workspace_write.network_access=false', '-c', 'approval_policy="never"',
                '-c', f'model_reasoning_effort={effort}',
                'review', review_prompt(base, tier), '-o', str(last_message)]

    def __call__(self, job_id, worktree, base, effort, tier, timeout, scratch, proceed):
        """Return (rc, JSONL stdout, stderr, last message, timed out); rc is None when proceed() refused the start.

        proceed() runs under the lock that cancel() takes, so a cancel issued before registration is not lost.
        """
        last_message, output, errors = scratch / 'last-message.txt', scratch / 'events.jsonl', scratch / 'stderr.log'
        with open(output, 'wb') as sink, open(errors, 'wb') as error_sink:
            with self.lock:
                if not proceed():
                    return None, '', '', '', False
                process = subprocess.Popen(self.command(base, last_message, effort, tier), cwd=str(worktree),
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
    def __init__(self, config, github, store, git, codex, stop=None):
        self.config, self.github, self.store, self.git, self.codex = config, github, store, git, codex
        self.stop = stop or threading.Event()
        self.wake = threading.Event()
        self.repo_locks = {}
        self.locks_guard = threading.Lock()

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
        while not self.stop.is_set():
            if not self.work_once():
                self.wake.wait(30)
                self.wake.clear()

    def work_once(self):
        """Run the next due job; any escaping error requeues it so the worker thread survives."""
        job = self.store.claim_next()
        if job is None:
            return False
        try:
            self.run(job)
        except Exception as exc:  # noqa: BLE001 - keep the worker alive
            log(f'{job}: unexpected error, requeued: {exc}')
            self.store.retry(job.id, 300)
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

    def defer_or_give_up(self, job, error, effort, tier, kept):
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
        return review_body(job.head, 'failed', '', [], effort=effort, tests=tier, kept=kept, reason=(
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
            tier = ('read-only' if self.config.tier == 'read-only'
                    else choose_tests(self.config.effort_rules, job.full_name, changed))
            shutil.rmtree(scratch, ignore_errors=True)
            self.git.add_worktree(job.full_name, worktree, job.head)
        try:
            log(f'{job}: codex review started against origin/{base} effort={effort} tests={tier}')
            started = time.time()
            rc, output, stderr, last, timed_out = self.codex(job.id, worktree, base, effort, tier,
                                                     self.config.timeout_seconds, scratch,
                                                     lambda: self.store.status(job.id) == 'running')
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
        kept = f'$CODEX_REVIEW_STATE_DIR/output/{job.id}.log'
        upstream = None if timed_out else transient_error(rc, events)
        if upstream is not None:
            body = self.defer_or_give_up(job, upstream, effort, tier, kept)
            if body is None:
                return None
            self.keep_output(job, output, stderr, last)
            return self.publish(job, 'failed', body, [], token)
        self.store.clear_transient(job.id)
        if timed_out:
            verdict, body = 'failed', review_body(job.head, 'failed', '', [], effort=effort, tests=tier,
                                                  kept=kept, reason=(
                f'codex timed out after {self.config.timeout_seconds} s'))
        else:
            roots = (str(worktree), os.path.realpath(worktree))
            verdict, summary, findings, reasons = assess(rc, events, message, roots)
            if verdict == 'failed':
                log(f'{job}: codex output rejected: {"; ".join(reasons)}')
                body = review_body(job.head, 'failed', '', [], effort=effort, tests=tier, kept=kept,
                                   reason='codex output did not pass the review checks', reasons=reasons)
            else:
                body = review_body(job.head, verdict, summary, findings, raw=message, effort=effort, tests=tier,
                                   kept=kept)
            if findings:
                comments = inline_comments(findings, diff_lines(
                    self.github.pull_files(job.full_name, job.number, token)))
        if verdict == 'failed' or len(body) > BODY_LIMIT:
            self.keep_output(job, output, stderr, last)
        return self.publish(job, verdict, body, comments, token)


# ---- entry point -----------------------------------------------------------

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
        reviewer.codex.cancel_all()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    for worker in workers:
        worker.start()
    threading.Thread(target=recover, name='redelivery', daemon=True).start()
    log(f'listening on 127.0.0.1:{config.listen_port}{WEBHOOK_PATH} with {config.concurrency} worker(s)')
    server.serve_forever()
    for worker in workers:
        worker.join(timeout=60)


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
    queued = store.force_enqueue(full_name, number, head, pull['base']['ref'], installation)
    print(f'{"queued" if queued else "already queued"} {full_name}#{number}@{head[:12]}')
    if pull['head']['sha'] != head:
        print(f'note: the current head is {pull["head"]["sha"][:12]}; the worker will review that head instead')
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
    git = Git(config.state_dir)
    log(f'startup: {store.reset_running()} interrupted job(s) requeued')
    git.sweep()
    reason = self_check(config.codex, git.work)
    if reason:
        log(f'startup self-check failed, refusing to start: {reason}')
        return SELF_CHECK_FAILED
    log('startup self-check passed: codex read a repository file')
    reviewer = Reviewer(config, github, store, git, CodexRunner(config.codex))
    serve(config, reviewer, secret)
    return 0


if __name__ == '__main__':
    sys.exit(main())
