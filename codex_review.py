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
MARKER = '<!-- remote-codex-review head={head} verdict={verdict}{effort} -->'
MARKER_RE = re.compile(r'<!-- remote-codex-review head=([0-9a-f]{40}) verdict=(clean|findings|failed)'
                       r'(?: effort=[a-z]+)? -->')
DEFAULT_EFFORT_RULES = {'medium': ['docs/wiki/**', 'docs/*.md', '**/README.md'], 'high': ['docs/runbooks/**']}
PRIORITY_RE = re.compile(r'\[P[0-3]\]')
FINDING_RE = re.compile(r'^\s*[-*]\s*\[(P[0-3])\]\s*(.+?)\s+(?:—|–|--|-)\s+(\S+?):(\d+)(?:-(\d+))?\s*$')
HUNK_RE = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@')
BODY_LIMIT = 60000
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
    return calendar.timegm(time.strptime(text, '%Y-%m-%dT%H:%M:%SZ'))


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
        self.state_dir = Path(values.get('CODEX_REVIEW_STATE_DIR', '/var/lib/codex-review'))
        self.codex = values.get('CODEX_REVIEW_CODEX_BIN', 'codex')
        self.effort_rules = load_effort_rules(values.get('CODEX_REVIEW_EFFORT_RULES', ''))

    def excluded(self, full_name):
        return any(same_name(full_name, item) for item in self.exclude)

    def allowed_account(self, login):
        return any(same_name(login, item) for item in self.accounts)

    def allowed_author(self, login):
        return any(same_name(login, item) for item in self.authors)


def load_effort_rules(path):
    """Load {"default": rules, "repos": {"owner/repo": rules}}; rules are {"medium": [...], "high": [...]}."""
    if not path:
        return {'default': DEFAULT_EFFORT_RULES, 'repos': {}}
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ConfigError(f'CODEX_REVIEW_EFFORT_RULES: {exc}') from None
    tables = [data.get('default', DEFAULT_EFFORT_RULES), *dict(data.get('repos') or {}).values()]
    for table in tables:
        if (not isinstance(table, dict) or set(table) - {'medium', 'high'}
                or not all(isinstance(v, list) and all(isinstance(p, str) for p in v) for v in table.values())):
            raise ConfigError('CODEX_REVIEW_EFFORT_RULES: each table maps medium/high to lists of globs')
    return {'default': data.get('default', DEFAULT_EFFORT_RULES), 'repos': dict(data.get('repos') or {})}


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


def choose_effort(rules, full_name, files):
    """Return medium only when every changed file matches a medium glob and none a high glob."""
    table = next((value for key, value in rules['repos'].items() if same_name(key, full_name)), rules['default'])
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
                if found_head == head:
                    return verdict, review.get('id')
        return None

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


def final_message(output, last_message=''):
    """Prefer codex's -o file; otherwise take the text after the last bare `codex` line."""
    if last_message.strip():
        return last_message.strip()
    lines = output.splitlines()
    marks = [index for index, line in enumerate(lines) if line.strip() == 'codex']
    return '\n'.join(lines[marks[-1] + 1:]).strip() if marks else ''


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
        elif line.strip() not in ('Full review comments:', 'Review comments:'):
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


def review_body(head, verdict, summary, findings, raw='', reason='', effort=None):
    """Render the review body; it always carries the marker and every finding in full."""
    parts = [MARKER.format(head=head, verdict=verdict, effort=f' effort={effort}' if effort else '')]
    if verdict == 'failed':
        parts.append(f'**Remote codex review failed** for `{head}`: {reason}. This is not a pass.')
    elif verdict == 'clean':
        parts.append(f'**Remote codex review: no findings** for `{head}`.')
    else:
        parts.append(f'**Remote codex review: {len(findings) or "unparsed"} finding(s)** for `{head}`.')
    if summary:
        parts.append(summary)
    for finding in findings:
        parts.append(f'- **[{finding.priority}] {finding.title}** — `{finding.location()}`\n\n'
                     + '\n'.join('  ' + line for line in finding.text.splitlines()))
    if verdict == 'findings' and not findings and raw:
        parts.append(raw)
    body = '\n\n'.join(parts)
    if len(body) > BODY_LIMIT:
        body = body[:BODY_LIMIT] + '\n\n[truncated; full text in the service journal]'
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
            'CREATE TABLE IF NOT EXISTS redeliveries (delivery_id INTEGER PRIMARY KEY, requested_at REAL NOT NULL);')

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

    def reset_running(self):
        with self.lock:
            return self.db.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'").rowcount

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


class CodexRunner:
    """Run `codex exec review` in its own process group; the group is killed on timeout or cancel."""

    def __init__(self, codex):
        self.codex = codex
        self.lock = threading.Lock()
        self.groups = {}

    def command(self, base, last_message, effort):
        return [self.codex, 'exec', '--sandbox', 'workspace-write',
                '-c', 'sandbox_workspace_write.network_access=false', '-c', 'approval_policy="never"',
                '-c', f'model_reasoning_effort={effort}',
                'review', '--base', f'origin/{base}', '-o', str(last_message)]

    def __call__(self, job_id, worktree, base, effort, timeout, scratch):
        last_message, output = scratch / 'last-message.txt', scratch / 'codex.log'
        with open(output, 'wb') as sink:
            process = subprocess.Popen(self.command(base, last_message, effort), cwd=str(worktree),
                                       stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            with self.lock:
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
        last = last_message.read_text(encoding='utf-8', errors='replace') if last_message.exists() else ''
        return process.returncode, text, last, timed_out

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
            return self.publish(job, 'failed', review_body(job.head, 'failed', '', [], reason=(
                f'review infrastructure error after {job.attempts + 1} attempts: {exc}')), [])

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
        if pull.get('state') != 'open' or pull['head']['sha'] != job.head:
            log(f'{job}: head moved or PR closed before review')
            return self.follow_head(job, pull)
        scratch = self.git.work / f'{job.id}'
        worktree = scratch / 'tree'
        with self.repo_lock(job.full_name):
            fetched = self.git.fetch(job.full_name, job.number, job.base, token)
            if fetched != job.head:
                raise RuntimeError(f'fetched head {fetched[:12]} differs from the queued head')
            effort = choose_effort(self.config.effort_rules, job.full_name,
                                   self.git.changed_files(job.full_name, job.base, job.head))
            shutil.rmtree(scratch, ignore_errors=True)
            self.git.add_worktree(job.full_name, worktree, job.head)
        try:
            log(f'{job}: codex review started against origin/{job.base} effort={effort}')
            started = time.time()
            rc, output, last, timed_out = self.codex(job.id, worktree, job.base, effort,
                                                     self.config.timeout_seconds, scratch)
            log(f'{job}: codex finished rc={rc} timed_out={timed_out} after {int(time.time() - started)} s')
        finally:
            with self.repo_lock(job.full_name):
                self.git.remove_worktree(job.full_name, worktree)
            shutil.rmtree(scratch, ignore_errors=True)
        if self.store.status(job.id) == 'superseded':
            log(f'{job}: superseded by a newer head; result discarded')
            return 'superseded'
        if self.stop.is_set():
            self.store.retry(job.id, 0, count_attempt=False)
            return None
        token = self.github.token(job.installation)
        pull = self.github.pull(job.full_name, job.number, token)
        if pull.get('state') != 'open' or pull['head']['sha'] != job.head:
            log(f'{job}: head moved or PR closed during review; result discarded')
            return self.follow_head(job, pull)
        message = final_message(output, last)
        comments = []
        if timed_out:
            verdict, body = 'failed', review_body(job.head, 'failed', '', [], effort=effort, reason=(
                f'codex timed out after {self.config.timeout_seconds} s'))
        elif rc != 0 or not message:
            tail = '\n'.join(output.strip().splitlines()[-5:])
            verdict, body = 'failed', review_body(job.head, 'failed', '', [], effort=effort, reason=(
                f'codex exited {rc}' + (' with no final message' if rc == 0 else '')
                + (f'\n\n```\n{tail}\n```' if tail else '')))
        else:
            roots = (str(worktree), os.path.realpath(worktree))
            verdict, summary, findings = parse_review(message, roots)
            body = review_body(job.head, verdict, summary, findings, raw=message, effort=effort)
            if findings:
                comments = inline_comments(findings, diff_lines(
                    self.github.pull_files(job.full_name, job.number, token)))
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=DEFAULT_CONFIG)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true',
                      help='print queued jobs and the deliveries that would be redelivered; change nothing')
    mode.add_argument('--requeue', metavar='OWNER/REPO#PR@SHA', help='queue one recorded head for review again')
    args = parser.parse_args(argv)
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
    if args.dry_run:
        for job in store.queued():
            print(f'would review {job} base=origin/{job.base} status={job.status}', flush=True)
        redeliver_failed(github, store, config.redeliver_hours * 3600, dry_run=True)
        return 0
    git = Git(config.state_dir)
    log(f'startup: {store.reset_running()} interrupted job(s) requeued')
    git.sweep()
    reviewer = Reviewer(config, github, store, git, CodexRunner(config.codex))
    serve(config, reviewer, secret)
    return 0


if __name__ == '__main__':
    sys.exit(main())
