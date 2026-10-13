#!/usr/bin/env python3
"""Offline tests for codex_review.py with GitHub and codex replaced by fakes."""
import hashlib
import hmac
import http.client
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import codex_review as cr  # noqa: E402

HEAD = 'a' * 40
NEW_HEAD = 'b' * 40
SECRET = b'webhook-secret'
REPO = 'Example-Org/Example-Repo'
WORKTREE = '/var/lib/codex-review/work/7/tree'

CLEAN = 'The change is consistent with its callers; no defects found.\nVERDICT: CLEAN'


def events_jsonl(message, read=True):
    """Return a minimal `codex exec --json` stream: one successful command, the message, a completed turn."""
    events = [{'type': 'thread.started', 'thread_id': 't-1'}, {'type': 'turn.started'}]
    if read:
        events.append({'type': 'item.completed', 'item': {
            'id': 'item_0', 'type': 'command_execution', 'command': "/usr/bin/bash -lc 'git diff origin/main...HEAD'",
            'aggregated_output': 'diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n', 'exit_code': 0, 'status': 'completed'}})
    events.append({'type': 'item.completed', 'item': {'id': 'item_1', 'type': 'agent_message', 'text': message}})
    events.append({'type': 'turn.completed', 'usage': {}})
    return '\n'.join(json.dumps(event) for event in events) + '\n'
FINDINGS = f"""Two defects remain.

Full review comments:

- [P1] Close the gate after the control pass — {WORKTREE}/src/service.py:5388-5389
  The resume command opens the gate.
  A later tick treats it as known.
  Confidence: 90

- [P3] Keep the cancel obligation — {WORKTREE}/src/worker.py:45
  Closing the incident drops the order. See [AGENTS.md:56](AGENTS.md#L56).
  Confidence: 60
"""


def config(**overrides):
    values = {'CODEX_REVIEW_APP_ID': '42', 'CODEX_REVIEW_PRIVATE_KEY': '/nonexistent/key.pem',
              'CODEX_REVIEW_WEBHOOK_SECRET_FILE': '/nonexistent/secret',
              'CODEX_REVIEW_ACCOUNTS': 'example-owner,Example-Org', 'CODEX_REVIEW_AUTHORS': 'example-owner'}
    values.update(overrides)
    return cr.Config(values)


def pull_payload(action='synchronize', head=HEAD, number=7, author='Example-Owner', association='OWNER',
                 head_repo=REPO, draft=False, state='open', repo=REPO, installation_account=None, base='main'):
    owner = repo.split('/')[0]
    installation = {'id': 99}
    if installation_account is not None:
        installation['account'] = {'login': installation_account}
    return {'action': action, 'installation': installation,
            'repository': {'full_name': repo, 'owner': {'login': owner}},
            'pull_request': {'number': number, 'state': state, 'draft': draft,
                             'author_association': association, 'user': {'login': author},
                             'head': {'sha': head, 'repo': {'full_name': head_repo}},
                             'base': {'ref': base, 'repo': {'full_name': repo, 'owner': {'login': owner}}}}}


def signed(body, secret=SECRET):
    return 'sha256=' + hmac.new(secret, body, hashlib.sha256).hexdigest()


def headers_for(body, guid='d-1', event='pull_request', signature='auto'):
    headers = {'Content-Length': str(len(body)), 'X-GitHub-Event': event, 'X-GitHub-Delivery': guid}
    if signature == 'auto':
        headers['X-Hub-Signature-256'] = signed(body)
    elif signature is not None:
        headers['X-Hub-Signature-256'] = signature
    return headers


class FakeGitHub:
    def __init__(self):
        self.heads = {}
        self.state = 'open'
        self.posted = []
        self.markers = {}
        self.files = []
        self.base = 'main'
        self.draft = False
        self.issues = {}
        self.comments = {}
        self.issue_calls = []

    def review(self, full_name, number, review_id, token):
        return {'id': review_id, 'body': self.posted[review_id - 1]['body']}

    def token(self, installation):
        return 'installation-token'

    def pull(self, full_name, number, token):
        payload = pull_payload(head=self.heads.get(number, HEAD), number=number, state=self.state, base=self.base,
                               draft=self.draft)
        return dict(payload['pull_request'], base=payload['pull_request']['base'])

    def pull_files(self, full_name, number, token):
        return self.files

    def existing_marker(self, full_name, number, head, token):
        return self.markers.get((number, head))

    def issue(self, full_name, number, token):
        self.issue_calls.append((full_name, number))
        result = self.issues[(full_name, number)]
        if isinstance(result, Exception):
            raise result
        return result

    def issue_comments(self, full_name, number, token):
        return self.comments.get((full_name, number), [])

    def post_review(self, full_name, number, head, body, comments, token):
        self.posted.append({'number': number, 'head': head, 'body': body, 'comments': comments})
        self.markers[(number, head)] = (cr.MARKER_RE.search(body).group(2), len(self.posted))
        return len(self.posted), len(comments)


class FakeGit:
    def __init__(self, root):
        self.work = root / 'work'
        self.fetched = []
        self.removed = []
        self.agents = None

    def fetch(self, full_name, number, base, token):
        self.fetched.append((full_name, number, base))
        return getattr(self, 'head', HEAD)

    def changed_files(self, full_name, base, head):
        return getattr(self, 'files', ['src/app.py'])

    def delta_tree(self, full_name, base, prev, head, scratch):
        self.delta_calls = getattr(self, 'delta_calls', []) + [(prev, head)]
        if getattr(self, 'during_delta', None):
            self.during_delta()
        return getattr(self, 'delta', ('c' * 40, f'delta from reviewed head {prev[:12]}'))

    def diff_files(self, full_name, old, new):
        if old == getattr(self, 'delta', ('c' * 40,))[0]:
            return getattr(self, 'delta_files', ['src/fix.py'])
        return getattr(self, 'base_touched', [])

    def add_worktree(self, full_name, path, head):
        path.mkdir(parents=True)
        if self.agents is not None:
            (path / 'AGENTS.md').write_text(self.agents, encoding='utf-8')

    def remove_worktree(self, full_name, path):
        self.removed.append(path)


class FakeCodex:
    def __init__(self, message='', rc=0, timed_out=False, during=None):
        self.message, self.rc, self.timed_out, self.during = message, rc, timed_out, during
        self.messages = []
        self.events = None
        self.calls = []
        self.cancelled = []
        self.governance = None

    def __call__(self, job_id, worktree, sections, effort, timeout, scratch, proceed, governance=()):
        if not proceed():
            return None, '', '', '', False
        self.calls.append((job_id, worktree, cr.review_prompt(sections, governance), effort))
        if self.messages:
            self.message = self.messages.pop(0)
        self.governance = [(doc.ref, doc.path, doc.path.read_text() if doc.path else None, doc.error)
                           for doc in governance]
        if self.during:
            self.during()
        message = self.message.replace(WORKTREE, str(worktree))
        events = self.events if self.events is not None else events_jsonl(message)
        return self.rc, events, 'progress\n', message, self.timed_out

    def cancel(self, job_ids):
        self.cancelled.extend(job_ids)

    def cancel_all(self):
        pass


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = config(CODEX_REVIEW_STATE_DIR=str(self.root))
        self.store = cr.Store(self.root / 'state.sqlite')
        self.github = FakeGitHub()
        self.git = FakeGit(self.root)
        self.codex = FakeCodex(CLEAN)
        self.reviewer = cr.Reviewer(self.config, self.github, self.store, self.git, self.codex)
        self.app = cr.WebhookApp(self.config, self.store, SECRET, on_queued=self.reviewer.on_queued)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def deliver(self, payload, guid='d-1', event='pull_request', signature='auto'):
        body = json.dumps(payload).encode()
        return self.app.handle('POST', '/webhook', headers_for(body, guid, event, signature), io.BytesIO(body).read)

    def run_next(self):
        job = self.store.claim_next()
        self.assertIsNotNone(job)
        return job, self.reviewer.run(job)


class ParseTests(unittest.TestCase):
    def test_no_findings_is_clean(self):
        verdict, summary, findings = cr.parse_review(CLEAN)
        self.assertEqual((verdict, summary, findings), ('clean', CLEAN, []))

    def test_multiple_findings_with_range_and_prefix_stripped(self):
        verdict, summary, findings = cr.parse_review(FINDINGS, (WORKTREE,))
        self.assertEqual(verdict, 'findings')
        self.assertEqual(summary, 'Two defects remain.')
        self.assertEqual([(f.priority, f.path, f.start, f.end) for f in findings],
                         [('P1', 'src/service.py', 5388, 5389), ('P3', 'src/worker.py', 45, 45)])
        self.assertEqual(findings[0].text, 'The resume command opens the gate.\nA later tick treats it as known.')
        self.assertIn('[AGENTS.md:56](AGENTS.md#L56)', findings[1].text)

    def test_repeated_final_block_is_deduplicated(self):
        _, _, findings = cr.parse_review(FINDINGS + '\n' + FINDINGS, (WORKTREE,))
        self.assertEqual(len(findings), 2)

    def test_priority_tag_without_location_still_counts_as_findings(self):
        verdict, _, findings = cr.parse_review('One issue: [P2] the retry loop never ends.')
        self.assertEqual((verdict, findings), ('findings', []))
        body = cr.review_body(HEAD, verdict, '', findings, raw='One issue: [P2] the retry loop never ends.')
        self.assertIn('[P2] the retry loop never ends.', body)

    def test_final_message_prefers_output_file_then_last_agent_message(self):
        events = cr.read_events(events_jsonl('old') + events_jsonl('new'))
        self.assertEqual(cr.final_message(events, ''), 'new')
        self.assertEqual(cr.final_message(events, 'from -o file\n'), 'from -o file')
        self.assertEqual(cr.final_message(cr.read_events('codex\nnot json'), ''), '')

    def test_inline_comments_only_for_lines_in_the_diff(self):
        _, _, findings = cr.parse_review(FINDINGS, (WORKTREE,))
        files = [{'filename': 'src/service.py', 'patch': '@@ -5380,4 +5385,6 @@\n ctx\n ctx\n+a\n+b\n+c\n ctx'}]
        comments = cr.inline_comments(findings, cr.diff_lines(files))
        self.assertEqual(comments, [{'path': 'src/service.py', 'line': 5389, 'side': 'RIGHT',
                                     'start_line': 5388, 'start_side': 'RIGHT',
                                     'body': comments[0]['body']}])


class WebhookTests(Harness):
    def test_valid_signature_is_queued(self):
        self.assertEqual(self.deliver(pull_payload()), 202)
        self.assertEqual([(j.full_name, j.number, j.head) for j in self.store.queued()], [(REPO, 7, HEAD)])

    def test_wrong_and_missing_signature_are_rejected_without_queueing(self):
        self.assertEqual(self.deliver(pull_payload(), signature=signed(b'other body')), 401)
        self.assertEqual(self.deliver(pull_payload(), guid='d-2', signature=None), 401)
        self.assertEqual(self.deliver(pull_payload(), guid='d-3', signature='sha256='), 401)
        self.assertEqual(self.store.queued(), [])
        self.assertFalse(self.store.delivery_seen('d-1'))

    def test_duplicate_delivery_processed_once(self):
        self.assertEqual(self.deliver(pull_payload()), 202)
        self.store.claim_next()
        self.assertEqual(self.deliver(pull_payload(head=NEW_HEAD)), 202)
        self.assertEqual([(j.head, j.status) for j in self.store.queued()], [(HEAD, 'running')])

    def test_oversized_body_rejected_before_reading(self):
        app = cr.WebhookApp(config(CODEX_REVIEW_MAX_BODY_BYTES='1024'), self.store, SECRET)

        def reader(_):
            raise AssertionError('body must not be read')
        self.assertEqual(app.handle('POST', '/webhook', {'Content-Length': '1025'}, reader), 413)
        self.assertEqual(self.store.queued(), [])

    def test_other_paths_and_methods(self):
        self.assertEqual(self.app.handle('POST', '/', {}, None), 404)
        self.assertEqual(self.app.handle('GET', '/webhook', {}, None), 405)

    def test_owner_pr_in_same_repository_is_queued(self):
        self.assertEqual(self.deliver(pull_payload(author='example-owner', association='OWNER')), 202)
        self.assertEqual(len(self.store.queued()), 1)

    def test_repository_outside_account_allowlist_is_not_queued(self):
        self.assertEqual(self.deliver(pull_payload(repo='Stranger/Example-Repo', head_repo='Stranger/Example-Repo')), 202)
        self.assertEqual(self.store.queued(), [])

    def test_installation_account_outside_allowlist_is_not_queued(self):
        self.assertEqual(self.deliver(pull_payload(installation_account='stranger')), 202)
        self.assertEqual(self.store.queued(), [])

    def test_installation_account_matches_case_insensitively(self):
        self.assertEqual(self.deliver(pull_payload(installation_account='example-ORG')), 202)
        self.assertEqual(len(self.store.queued()), 1)

    def test_installation_event_is_logged_only(self):
        payload = {'action': 'created', 'installation': {'id': 5, 'account': {'login': 'stranger'}}}
        self.assertEqual(self.deliver(payload, event='installation'), 202)
        self.assertEqual(self.store.queued(), [])

    def test_untrusted_pull_requests_are_not_queued(self):
        cases = {'fork': pull_payload(head_repo='stranger/Ops'),
                 'none': pull_payload(association='NONE'),
                 'contributor': pull_payload(association='CONTRIBUTOR'),
                 'author': pull_payload(author='someone-else', association='COLLABORATOR')}
        for guid, payload in cases.items():
            with self.subTest(guid):
                self.assertEqual(self.deliver(payload, guid=guid), 202)
                self.assertEqual(self.store.queued(), [])

    def test_author_allowlist_accepts_added_bot(self):
        self.app.config = config(CODEX_REVIEW_AUTHORS='example-owner,owner-helper[bot]')
        self.assertEqual(self.deliver(pull_payload(author='Owner-Helper[bot]', association='MEMBER')), 202)
        self.assertEqual(len(self.store.queued()), 1)

    def test_ignored_events_and_drafts(self):
        self.assertEqual(self.deliver(pull_payload(action='closed')), 202)
        self.assertEqual(self.deliver({'zen': 'hi'}, guid='d-2', event='ping'), 202)
        self.app.config = config(CODEX_REVIEW_INCLUDE_DRAFTS='false')
        self.assertEqual(self.deliver(pull_payload(draft=True), guid='d-3'), 202)
        self.assertEqual(self.store.queued(), [])


class ServerTests(Harness):
    def request(self, body, headers):
        server = cr.make_server(self.app, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=10)
            connection.request('POST', '/webhook', body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            server.shutdown()
            server.server_close()

    def test_bad_signature_returns_401_with_empty_body(self):
        body = json.dumps(pull_payload()).encode()
        self.assertEqual(self.request(body, headers_for(body, signature='sha256=00')), (401, b''))

    def test_oversized_body_returns_413(self):
        self.app.config = config(CODEX_REVIEW_MAX_BODY_BYTES='1024')
        body = b'x' * 2048
        self.assertEqual(self.request(body, headers_for(body))[0], 413)

    def test_accepted_delivery_returns_202(self):
        body = json.dumps(pull_payload()).encode()
        self.assertEqual(self.request(body, headers_for(body)), (202, b''))


class ReviewTests(Harness):
    def test_clean_review_posts_marker(self):
        self.deliver(pull_payload())
        job, status = self.run_next()
        self.assertEqual(status, 'clean')
        post = self.github.posted[0]
        self.assertIn(f'<!-- remote-codex-review head={HEAD} verdict=clean effort=xhigh tests=read-only mode=full -->',
                      post['body'])
        self.assertEqual([call[3] for call in self.codex.calls], ['xhigh', 'xhigh'])
        self.assertIn('git diff origin/main...HEAD', self.codex.calls[0][2])
        self.assertEqual(self.git.removed, [self.git.work / str(job.id) / 'tree'])

    def test_existing_marker_on_github_skips_codex(self):
        self.github.markers[(7, HEAD)] = ('findings', 5)
        self.deliver(pull_payload())
        _, status = self.run_next()
        self.assertEqual((status, self.codex.calls, self.github.posted), ('findings', [], []))

    def test_same_head_is_not_queued_twice(self):
        self.deliver(pull_payload())
        self.run_next()
        self.deliver(pull_payload(), guid='d-2')
        self.assertEqual(self.store.queued(), [])
        self.assertIsNone(self.store.claim_next())

    def test_new_head_supersedes_queued_job(self):
        self.deliver(pull_payload())
        self.deliver(pull_payload(head=NEW_HEAD), guid='d-2')
        self.assertEqual([j.head for j in self.store.queued()], [NEW_HEAD])

    def test_new_head_cancels_running_job_and_its_result_is_discarded(self):
        self.deliver(pull_payload())

        def push():
            self.github.heads[7] = NEW_HEAD
            self.deliver(pull_payload(head=NEW_HEAD), guid='d-2')
        self.codex.during = push
        job, status = self.run_next()
        self.assertEqual(status, 'superseded')
        self.assertEqual(self.codex.cancelled, [job.id])
        self.assertEqual(self.github.posted, [])
        self.assertEqual([(j.head, j.status) for j in self.store.queued()], [(NEW_HEAD, 'queued')])

    def test_superseded_job_never_posts_even_if_github_still_shows_old_head(self):
        self.deliver(pull_payload())
        self.codex.during = lambda: self.deliver(pull_payload(head=NEW_HEAD), guid='d-2')
        job, status = self.run_next()
        self.assertEqual((status, self.github.posted, self.codex.cancelled), ('superseded', [], [job.id]))

    def test_cancel_before_codex_start_is_not_lost(self):
        stub = self.root / 'codex-stub'
        started = self.root / 'codex-started'
        stub.write_text(f'#!/bin/sh\ntouch {started}\n')
        stub.chmod(0o700)
        self.reviewer.codex = cr.CodexRunner(str(stub))
        self.app.on_queued = self.reviewer.on_queued
        add_worktree = self.git.add_worktree

        def push_then_add(full_name, path, head):
            self.deliver(pull_payload(head=NEW_HEAD), guid='d-2')
            add_worktree(full_name, path, head)
        self.git.add_worktree = push_then_add
        self.deliver(pull_payload())
        _, status = self.run_next()
        self.assertEqual((status, self.github.posted), ('superseded', []))
        self.assertFalse(started.exists(), 'codex started after its job was superseded')

    def test_head_moved_during_review_discards_result(self):
        self.deliver(pull_payload())

        def push():
            self.github.heads[7] = NEW_HEAD
        self.codex.during = push
        _, status = self.run_next()
        self.assertEqual((status, self.github.posted), ('superseded', []))
        self.assertEqual([j.head for j in self.store.queued()], [NEW_HEAD])

    def test_head_moved_before_review_skips_codex(self):
        self.deliver(pull_payload())
        self.github.heads[7] = NEW_HEAD
        _, status = self.run_next()
        self.assertEqual((status, self.codex.calls, self.github.posted), ('superseded', [], []))

    def test_review_uses_live_base_not_queued_base(self):
        self.deliver(pull_payload())
        self.github.base = 'develop'
        _, status = self.run_next()
        self.assertEqual(status, 'clean')
        self.assertEqual(self.git.fetched[0][2], 'develop')
        self.assertIn('git diff origin/develop...HEAD', self.codex.calls[0][2])

    def test_base_changed_during_review_requeues_without_posting(self):
        self.deliver(pull_payload())

        def retarget():
            self.github.base = 'develop'
        self.codex.during = retarget
        _, status = self.run_next()
        self.assertEqual((status, self.github.posted), (None, []))
        self.assertEqual([(j.head, j.status) for j in self.store.queued()], [(HEAD, 'queued')])

    def test_repository_excluded_after_queueing_is_not_reviewed(self):
        self.deliver(pull_payload())
        self.reviewer.config = config(CODEX_REVIEW_EXCLUDE_REPOS=REPO.lower())
        _, status = self.run_next()
        self.assertEqual((status, self.codex.calls, self.github.posted), ('skipped', [], []))
        self.assertEqual(self.store.queued(), [])

    def test_queued_pr_now_draft_is_not_reviewed_when_drafts_are_off(self):
        self.deliver(pull_payload())
        self.reviewer.config = config(CODEX_REVIEW_INCLUDE_DRAFTS='false')
        self.github.draft = True
        _, status = self.run_next()
        self.assertEqual((status, self.codex.calls, self.github.posted), ('skipped', [], []))

    def test_pr_turned_draft_during_review_is_not_published(self):
        self.reviewer.config = config(CODEX_REVIEW_INCLUDE_DRAFTS='false')
        self.deliver(pull_payload())

        def to_draft():
            self.github.draft = True
        self.codex.during = to_draft
        _, status = self.run_next()
        self.assertEqual((status, len(self.codex.calls), self.github.posted), ('skipped', 2, []))

    def test_findings_posted_inline_and_in_body(self):
        self.codex.message = FINDINGS
        self.github.files = [{'filename': 'src/worker.py', 'patch': '@@ -40,3 +44,3 @@\n a\n+b\n c'}]
        self.deliver(pull_payload())
        _, status = self.run_next()
        post = self.github.posted[0]
        self.assertEqual(status, 'findings')
        self.assertEqual([(c['path'], c['line']) for c in post['comments']], [('src/worker.py', 45)])
        self.assertIn('`src/service.py:5388-5389`', post['body'])
        self.assertIn('The resume command opens the gate.', post['body'])
        self.assertNotIn(str(self.git.work), post['body'])

    def test_codex_failure_posts_failed_marker(self):
        self.codex.rc = 1
        self.deliver(pull_payload())
        _, status = self.run_next()
        self.assertEqual(status, 'failed')
        self.assertIn(f'head={HEAD} verdict=failed effort=xhigh tests=read-only mode=full -->',
                      self.github.posted[0]['body'])
        self.assertEqual(len(self.codex.calls), 1)
        self.assertIn('not a pass', self.github.posted[0]['body'])

    def test_codex_timeout_posts_failed_marker(self):
        self.codex.timed_out, self.codex.rc = True, -9
        self.deliver(pull_payload())
        _, status = self.run_next()
        self.assertEqual(status, 'failed')
        self.assertIn('timed out', self.github.posted[0]['body'])

    def test_timeout_output_is_kept_private_and_referenced(self):
        self.codex.timed_out, self.codex.rc = True, -9
        self.deliver(pull_payload())
        job, _ = self.run_next()
        kept = self.root / 'output' / f'{job.id}.log'
        self.assertIn('progress', kept.read_text())
        self.assertEqual(stat.S_IMODE(kept.stat().st_mode), 0o600)
        self.assertIn(f'`$CODEX_REVIEW_STATE_DIR/output/{job.id}.log`', self.github.posted[0]['body'])

    def test_truncated_body_points_to_kept_full_text(self):
        self.codex.message = ('Summary.\n\n- [P2] Long finding — src/app.py:1\n  Confidence: 90\n'
                              + '  detail\n' * 10000 + '  END-OF-TEXT')
        self.deliver(pull_payload())
        job, status = self.run_next()
        body = self.github.posted[0]['body']
        self.assertEqual(status, 'findings')
        self.assertTrue(body.endswith(f'[truncated; full text kept on the review host at '
                                      f'`$CODEX_REVIEW_STATE_DIR/output/{job.id}.log`]'))
        self.assertNotIn('END-OF-TEXT', body)
        self.assertIn('END-OF-TEXT', (self.root / 'output' / f'{job.id}.log').read_text())

    def test_clean_review_keeps_no_output(self):
        self.deliver(pull_payload())
        self.run_next()
        self.assertFalse((self.root / 'output').exists())

    def test_empty_final_message_is_failed_not_clean(self):
        self.codex.message = ''
        self.deliver(pull_payload())
        self.assertEqual(self.run_next()[1], 'failed')

    def test_infrastructure_error_retries_then_posts_failed(self):
        def broken(*_):
            raise RuntimeError('fetch refused')
        self.git.fetch = broken
        now = [0.0]
        self.store.clock = lambda: now[0]
        self.deliver(pull_payload())
        statuses = [self.run_next()[1]]
        for _ in range(self.config.max_attempts - 1):
            self.assertIsNone(self.store.claim_next(), 'backoff must delay the retry')
            now[0] += 3600
            statuses.append(self.run_next()[1])
        self.assertEqual(statuses, [None] * (self.config.max_attempts - 1) + ['failed'])
        self.assertIn('fetch refused', self.github.posted[0]['body'])

    def test_give_up_path_skips_repository_excluded_meanwhile(self):
        def broken(*_):
            self.reviewer.config = config(CODEX_REVIEW_MAX_ATTEMPTS='1', CODEX_REVIEW_EXCLUDE_REPOS=REPO)
            raise RuntimeError('fetch refused')
        self.git.fetch = broken
        self.reviewer.config = config(CODEX_REVIEW_MAX_ATTEMPTS='1')
        self.deliver(pull_payload())
        self.assertEqual((self.run_next()[1], self.github.posted), ('skipped', []))

    def test_worker_survives_error_on_give_up_path(self):
        def broken(*_):
            raise RuntimeError('fetch refused')

        def no_token(_):
            raise OSError('network unreachable')
        self.git.fetch = broken
        self.reviewer.config = config(CODEX_REVIEW_MAX_ATTEMPTS='1')
        self.github.token = no_token
        self.deliver(pull_payload())
        self.assertTrue(self.reviewer.work_once())
        self.assertEqual([(j.head, j.status) for j in self.store.queued()], [(HEAD, 'queued')])
        self.assertEqual(self.github.posted, [])

    def test_restart_requeues_interrupted_job(self):
        self.deliver(pull_payload())
        self.store.claim_next()
        self.assertEqual(self.store.reset_running(), 1)
        self.assertEqual(self.run_next()[1], 'clean')


class EffortTests(unittest.TestCase):
    rules = {'default': cr.DEFAULT_EFFORT_RULES, 'repos': {}}

    def effort(self, files, repo=REPO, rules=None):
        return cr.choose_effort(rules or self.rules, repo, files)

    def test_wiki_and_docs_only_is_medium(self):
        self.assertEqual(self.effort(['docs/wiki/stop/flow.md', 'docs/notes.md']), 'medium')

    def test_runbook_only_is_high(self):
        self.assertEqual(self.effort(['docs/runbooks/deploy.md']), 'high')
        self.assertEqual(self.effort(['docs/runbooks/README.md']), 'high')

    def test_wiki_plus_source_is_high(self):
        self.assertEqual(self.effort(['docs/wiki/a.md', 'src/service.py']), 'high')

    def test_readme_only_is_medium(self):
        self.assertEqual(self.effort(['README.md', 'deploy/README.md']), 'medium')

    def test_no_changed_files_is_high(self):
        self.assertEqual(self.effort([]), 'high')

    def test_docs_glob_does_not_cross_directories(self):
        self.assertEqual(self.effort(['docs/notes.md']), 'medium')
        self.assertEqual(self.effort(['docs/adr/0001.md']), 'high')

    def test_repository_override_is_case_insensitive(self):
        rules = {'default': cr.DEFAULT_EFFORT_RULES, 'repos': {'example-org/example-repo': {'medium': ['runbooks/**']}}}
        self.assertEqual(self.effort(['runbooks/x.md'], rules=rules), 'medium')
        self.assertEqual(self.effort(['README.md'], rules=rules), 'high')
        self.assertEqual(self.effort(['runbooks/x.md'], repo='example-owner/other', rules=rules), 'high')

    def test_rules_file_is_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rules.json'
            path.write_text(json.dumps({'repos': {'example-owner/x': {'medium': ['*.md']}}}))
            loaded = cr.load_effort_rules(str(path))
            self.assertEqual(loaded['default'], cr.DEFAULT_EFFORT_RULES)
            path.write_text(json.dumps({'default': {'low': ['*']}}))
            with self.assertRaises(cr.ConfigError):
                cr.load_effort_rules(str(path))

    def test_medium_effort_reaches_codex_and_marker(self):
        harness = ReviewTests('test_clean_review_posts_marker')
        harness.setUp()
        try:
            harness.git.files = ['docs/wiki/a.md']
            harness.deliver(pull_payload())
            harness.run_next()
            harness.github.heads[7] = NEW_HEAD
            harness.git.head = NEW_HEAD
            harness.git.delta_files = ['docs/wiki/a.md']
            harness.deliver(pull_payload(head=NEW_HEAD), guid='d-2')
            harness.run_next()
            self.assertEqual([call[3] for call in harness.codex.calls], ['xhigh', 'xhigh', 'medium'])
            self.assertIn('verdict=clean effort=medium tests=read-only mode=delta -->',
                          harness.github.posted[1]['body'])
        finally:
            harness.tearDown()


class ReadOnlyTests(unittest.TestCase):
    def test_rules_file_drops_annotations_and_rejects_unknown_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rules.json'
            path.write_text(json.dumps({'_basis': 'x', 'default': {'medium': ['a/**'], '_why': {'a/**': 'y'}},
                                        'repos': {'example-owner/x': {'high': ['b/**'], '_why': {}}}}))
            loaded = cr.load_effort_rules(str(path))
            self.assertEqual(loaded['default'], {'medium': ['a/**']})
            self.assertEqual(loaded['repos'], {'example-owner/x': {'high': ['b/**']}})
            path.write_text(json.dumps({'default': {'tests': ['a/**']}}))
            with self.assertRaises(cr.ConfigError):
                cr.load_effort_rules(str(path))

    def test_shipped_rules_file_matches_builtin_default(self):
        shipped = cr.load_effort_rules(str(Path(__file__).resolve().parent.parent / 'deploy' / 'effort-rules.json'))
        self.assertEqual(shipped['default'], cr.DEFAULT_EFFORT_RULES)

    def test_high_risk_paths_are_reviewed_read_only(self):
        harness = ReviewTests('test_clean_review_posts_marker')
        harness.setUp()
        try:
            harness.git.files = ['src/app.py', 'deploy/install.sh', 'db/migrations/0002_add.py']
            harness.deliver(pull_payload())
            harness.run_next()
            body = harness.github.posted[0]['body']
        finally:
            harness.tearDown()
        self.assertIn('verdict=clean effort=xhigh tests=read-only mode=full -->', body)
        self.assertIn('Read-only review', body)
        self.assertEqual(cr.MARKER_RE.search(body).group(1), HEAD)

    def test_command_is_read_only_and_forbids_tests(self):
        sections = cr.first_prompt('main', cr.LENSES[0][1])
        command = cr.CodexRunner('codex').command(sections, '/w/last.txt', 'high')
        self.assertEqual(command, ['codex', 'exec', '--json', '--sandbox', 'read-only',
                                   '-c', 'approval_policy="never"', '-c', 'model_reasoning_effort=high',
                                   'review', cr.review_prompt(sections), '-o', '/w/last.txt'])
        self.assertNotIn('--base', command)
        prompt = command[-3]
        self.assertIn('git diff origin/main...HEAD', prompt)
        self.assertIn('do not run pytest, vitest or any other test suite', prompt)
        self.assertIn('本次为只读审查，未运行测试', prompt)
        self.assertIn('`VERDICT: CLEAN`', prompt)


class GitHubClientTests(unittest.TestCase):
    def test_rejected_inline_comments_fall_back_to_body(self):
        calls = []

        def transport(method, url, token, body):
            calls.append(body)
            if body.get('comments'):
                raise cr.GitHubError(422, 'Line could not be resolved')
            return 200, {'id': 11}, ''
        github = cr.GitHub(config(), transport=transport, jwt_factory=lambda: 'jwt')
        body = cr.review_body(HEAD, 'findings', '', cr.parse_review(FINDINGS, (WORKTREE,))[2])
        result = github.post_review(REPO, 7, HEAD, body, [{'path': 'x', 'line': 1, 'body': 'b'}], 't')
        self.assertEqual(result, (11, 0))
        self.assertEqual(len(calls), 2)
        self.assertNotIn('comments', calls[1])
        self.assertIn('Keep the cancel obligation', calls[1]['body'])
        self.assertEqual(calls[1]['event'], 'COMMENT')

    def test_marker_only_counts_from_this_app(self):
        reviews = [{'user': {'login': 'someone'}, 'id': 1,
                    'body': cr.MARKER.format(head=HEAD, verdict='clean', effort='', tests='', mode='')},
                   {'user': {'login': 'Remote-Review[bot]'}, 'id': 2,
                    'body': cr.MARKER.format(head=NEW_HEAD, verdict='clean', effort=' effort=high', tests='', mode='')},
                   {'user': {'login': 'remote-review[bot]'}, 'id': 3,
                    'body': cr.MARKER.format(head=HEAD, verdict='findings', effort=' effort=medium',
                                             tests=' tests=read-only', mode=' mode=delta')}]

        def transport(method, url, token, body):
            return 200, ({'slug': 'remote-review'} if url.endswith('/app') else reviews), ''
        github = cr.GitHub(config(), transport=transport, jwt_factory=lambda: 'jwt')
        self.assertEqual(github.existing_marker(REPO, 7, HEAD, 't'), ('findings', 3))

    def test_installation_token_cached_until_five_minutes_before_expiry(self):
        now = [1_000_000.0]
        issued = []

        def transport(method, url, token, body):
            if method == 'GET':
                return 200, {'id': 99, 'account': {'login': 'example-ORG'}}, ''
            issued.append(url)
            expires = cr.time.strftime('%Y-%m-%dT%H:%M:%SZ', cr.time.gmtime(now[0] + 3600))
            return 201, {'token': f'tok-{len(issued)}', 'expires_at': expires}, ''
        github = cr.GitHub(config(), transport=transport, jwt_factory=lambda: 'jwt', clock=lambda: now[0])
        self.assertEqual(github.token(99), 'tok-1')
        now[0] += 3600 - 301
        self.assertEqual(github.token(99), 'tok-1')
        now[0] += 2
        self.assertEqual(github.token(99), 'tok-2')

    def test_token_refused_for_installation_outside_account_allowlist(self):
        calls = []

        def transport(method, url, token, body):
            calls.append((method, url))
            return 200, {'id': 5, 'account': {'login': 'stranger'}}, ''
        github = cr.GitHub(config(), transport=transport, jwt_factory=lambda: 'jwt')
        with self.assertRaises(cr.InstallationRejected):
            github.token(5)
        with self.assertRaises(cr.InstallationRejected):
            github.token(5)
        self.assertEqual(calls, [('GET', cr.API + '/app/installations/5')])

    def test_app_jwt_signature_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            key, pub = Path(tmp) / 'key.pem', Path(tmp) / 'pub.pem'
            subprocess.run(['openssl', 'genrsa', '-out', str(key), '2048'], check=True, capture_output=True)
            os.chmod(key, 0o600)
            subprocess.run(['openssl', 'rsa', '-in', str(key), '-pubout', '-out', str(pub)],
                           check=True, capture_output=True)
            token = cr.app_jwt('42', key, now=1_000_000)
            header, payload, signature = token.split('.')
            raw = cr.base64.urlsafe_b64decode(signature + '=' * (-len(signature) % 4))
            (Path(tmp) / 'sig').write_bytes(raw)
            result = subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(pub), '-signature',
                                     str(Path(tmp) / 'sig')], input=f'{header}.{payload}'.encode(),
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            claims = json.loads(cr.base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
            self.assertEqual(claims, {'iat': 999_940, 'exp': 1_000_540, 'iss': '42'})
            os.chmod(key, 0o644)
            with self.assertRaises(cr.ConfigError):
                cr.app_jwt('42', key)

    def test_token_is_not_in_git_argv(self):
        env = cr.auth_env('secret-token')
        self.assertNotIn('secret-token', env['GIT_CONFIG_VALUE_0'])
        self.assertEqual(env['GIT_CONFIG_KEY_0'], 'http.https://github.com/.extraheader')


class RedeliveryTests(unittest.TestCase):
    def test_only_failed_unreceived_pull_request_deliveries_are_redelivered(self):
        stamp = '2026-10-12T10:00:00Z'
        deliveries = [
            {'id': 10, 'guid': 'ok', 'event': 'pull_request', 'status_code': 202, 'delivered_at': stamp},
            {'id': 18, 'guid': 'stranger', 'event': 'pull_request', 'status_code': 500, 'delivered_at': stamp,
             'installation_id': 5},
            {'id': 11, 'guid': 'failed', 'event': 'pull_request', 'status_code': 502, 'delivered_at': stamp},
            {'id': 12, 'guid': 'failed', 'event': 'pull_request', 'status_code': 0, 'delivered_at': stamp},
            {'id': 13, 'guid': 'retried-ok', 'event': 'pull_request', 'status_code': 202, 'delivered_at': stamp},
            {'id': 14, 'guid': 'retried-ok', 'event': 'pull_request', 'status_code': 500, 'delivered_at': stamp},
            {'id': 15, 'guid': 'ping', 'event': 'ping', 'status_code': 500, 'delivered_at': stamp},
            {'id': 16, 'guid': 'seen', 'event': 'pull_request', 'status_code': 500, 'delivered_at': stamp},
            {'id': 17, 'guid': 'asked', 'event': 'pull_request', 'status_code': 500, 'delivered_at': stamp},
            {'id': 9, 'guid': 'old', 'event': 'pull_request', 'status_code': 500,
             'delivered_at': '2026-10-10T10:00:00Z'},
        ]
        posted = []

        for delivery in deliveries:
            delivery.setdefault('installation_id', 99)

        def transport(method, url, token, body):
            if method == 'POST':
                posted.append(url)
                return 202, {}, ''
            if url.endswith('/app/installations/99'):
                return 200, {'id': 99, 'account': {'login': 'Example-Org'}}, ''
            if url.endswith('/app/installations/5'):
                return 200, {'id': 5, 'account': {'login': 'stranger'}}, ''
            return 200, deliveries, ''
        with tempfile.TemporaryDirectory() as tmp:
            store = cr.Store(Path(tmp) / 'state.sqlite')
            store.accept('seen')
            store.mark_redelivered(17)
            github = cr.GitHub(config(), transport=transport, jwt_factory=lambda: 'jwt')
            now = cr.utc_epoch(stamp) + 60
            self.assertEqual(cr.redeliver_failed(github, store, 86400, clock=lambda: now), [12])
            self.assertEqual(posted, [cr.API + '/app/hook/deliveries/12/attempts'])
            self.assertEqual(cr.redeliver_failed(github, store, 86400, clock=lambda: now), [])
            store.db.close()

    def test_github_timestamps_with_and_without_milliseconds_are_utc(self):
        self.assertEqual(cr.utc_epoch('2026-10-13T05:11:43Z'), 1791868303)
        self.assertAlmostEqual(cr.utc_epoch('2026-10-13T05:11:43.903Z'), 1791868303.903, places=6)
        with self.assertRaises(ValueError):
            cr.utc_epoch('2026-10-13T05:11:43+08:00')

    def test_millisecond_delivered_at_does_not_abort_the_scan(self):
        deliveries = [{'id': 21, 'guid': 'ms', 'event': 'pull_request', 'status_code': 502,
                       'delivered_at': '2026-10-13T05:11:43.903Z', 'installation_id': 99}]
        posted = []

        def transport(method, url, token, body):
            if method == 'POST':
                posted.append(url)
                return 202, {}, ''
            if url.endswith('/app/installations/99'):
                return 200, {'id': 99, 'account': {'login': 'Example-Org'}}, ''
            return 200, deliveries, ''
        with tempfile.TemporaryDirectory() as tmp:
            store = cr.Store(Path(tmp) / 'state.sqlite')
            github = cr.GitHub(config(), transport=transport, jwt_factory=lambda: 'jwt')
            self.assertEqual(cr.redeliver_failed(github, store, 86400, clock=lambda: 1791868303 + 60), [21])
            store.db.close()

    def test_redelivered_fork_event_is_still_filtered(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = cr.Store(Path(tmp) / 'state.sqlite')
            app = cr.WebhookApp(config(), store, SECRET)
            body = json.dumps(pull_payload(head_repo='stranger/Ops')).encode()
            self.assertEqual(app.handle('POST', '/webhook', headers_for(body, 'redelivered'),
                                        io.BytesIO(body).read), 202)
            self.assertEqual(store.queued(), [])
            store.db.close()


class ConfigTests(unittest.TestCase):
    def test_env_file_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'env'
            path.write_text('# comment\nCODEX_REVIEW_APP_ID=42\nCODEX_REVIEW_PRIVATE_KEY="/k.pem"\n'
                            'CODEX_REVIEW_WEBHOOK_SECRET_FILE=/s\nCODEX_REVIEW_ACCOUNTS=example-owner, Example-Org\n'
                            'CODEX_REVIEW_AUTHORS=example-owner\n')
            loaded = cr.Config(cr.read_env_file(path))
        self.assertEqual(loaded.accounts, ('example-owner', 'Example-Org'))
        self.assertEqual((loaded.concurrency, loaded.timeout_seconds, loaded.listen_port), (2, 2400, 8787))
        self.assertEqual(loaded.authors, ('example-owner',))
        self.assertTrue(loaded.include_drafts)
        self.assertTrue(loaded.allowed_account('example-org'))

    def test_authors_allowlist_is_required(self):
        with self.assertRaises(cr.ConfigError):
            config(CODEX_REVIEW_AUTHORS='')

    def test_secret_file_must_be_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'secret'
            path.write_bytes(b'value\n')
            os.chmod(path, 0o640)
            with self.assertRaises(cr.ConfigError):
                cr.read_private(path)
            os.chmod(path, 0o600)
            self.assertEqual(cr.read_private(path), b'value\n')

if __name__ == '__main__':
    unittest.main()
