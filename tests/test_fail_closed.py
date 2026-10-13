#!/usr/bin/env python3
"""Tests for the fail-closed verdict, startup self-check, invalidation and manual enqueue."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import codex_review as cr  # noqa: E402
from test_codex_review import HEAD, NEW_HEAD, REPO, WORKTREE, FakeCodex, Harness, config, pull_payload  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
ROOT = Path(__file__).resolve().parent.parent
INCIDENT_MESSAGE = ('Review blocked: the execution tool failed because /usr/local/bin/codex-code-mode-host is missing, '
                    'so I could not inspect the diff or repository instructions. The verdict is a placeholder.')


def fixture(name):
    """Return (JSONL stdout, last message) of a real codex 0.160.1 run recorded on the review host."""
    return (FIXTURES / f'{name}.jsonl').read_text(encoding='utf-8'), (FIXTURES / f'{name}.last').read_text(
        encoding='utf-8')


def judge_all(stdout, last, rc=0):
    """Return assess()'s (verdict, summary, posted, unposted, reasons)."""
    events = cr.read_events(stdout)
    return cr.assess(rc, events, cr.final_message(events, last), (WORKTREE,))


def judge(stdout, last, rc=0):
    """Return (verdict, summary, posted findings, reasons)."""
    verdict, summary, posted, _, reasons = judge_all(stdout, last, rc)
    return verdict, summary, posted, reasons


def with_confidence(last, score):
    """Append `Confidence: <score>` to every finding block of a recorded final message."""
    out, inside = [], False
    for line in last.splitlines():
        if inside and not line.startswith('  '):
            out.append(f'  Confidence: {score}')
            inside = False
        out.append(line)
        inside = inside or bool(cr.FINDING_RE.match(line))
    return '\n'.join(out + ([f'  Confidence: {score}'] if inside else [])) + '\n'


def without(stdout, predicate):
    return ''.join(line + '\n' for line in stdout.splitlines() if not predicate(json.loads(line)))


def is_command(event):
    return (event.get('item') or {}).get('type') == 'command_execution'


class RealOutputTests(unittest.TestCase):
    def test_real_clean_runs_are_clean(self):
        for name in ('clean-tests', 'clean-ro'):
            with self.subTest(name):
                verdict, summary, findings, reasons = judge(*fixture(name))
                self.assertEqual((verdict, findings, reasons), ('clean', [], []))
                self.assertNotIn('VERDICT', summary)
                self.assertIn('pagination helper', summary)

    def test_real_findings_runs_are_findings(self):
        for name in ('findings-tests', 'findings-ro'):
            with self.subTest(name):
                stdout, last = fixture(name)
                verdict, _, findings, _ = judge(stdout, with_confidence(last, 80))
                self.assertEqual(verdict, 'findings')
                self.assertEqual([(f.priority, f.path, f.start) for f in findings],
                                 [('P2', 'paging.py', 8), ('P2', 'paging.py', 9)])

    def test_real_single_finding_read_only_run_drops_the_block_header(self):
        last = (FIXTURES / 'findings-ro-single.last').read_text(encoding='utf-8')
        verdict, summary, findings, _ = judge(fixture('findings-ro')[0], with_confidence(last, 50))
        self.assertEqual(verdict, 'findings')
        self.assertEqual([(f.priority, f.title, f.path, f.start, f.end) for f in findings],
                         [('P2', 'Correct both pagination slice bounds', 'paging.py', 8, 9)])
        self.assertIn('page([0, 1, 2, 3, 4], 0, 2)', findings[0].text)
        self.assertNotIn('Review comment', summary)
        self.assertNotIn('VERDICT', summary)

    def test_real_findings_without_confidence_are_not_posted_and_the_run_is_clean(self):
        for name in ('findings-tests', 'findings-ro'):
            with self.subTest(name):
                verdict, _, posted, unposted, reasons = judge_all(*fixture(name))
                self.assertEqual((verdict, posted, reasons), ('clean', [], []))
                self.assertEqual([(f.priority, f.confidence) for f in unposted], [('P2', None), ('P2', None)])

    def test_real_run_without_code_mode_host_is_failed(self):
        verdict, _, findings, reasons = judge(*fixture('broken-tests'))
        self.assertEqual((verdict, findings), ('failed', []))
        text = '\n'.join(reasons)
        self.assertIn('codex-code-mode-host', text)
        self.assertIn('no command with exit code 0', text)
        self.assertIn('found BLOCKED', text)

    def test_incident_message_with_no_priority_tag_is_failed_not_clean(self):
        stdout, _ = fixture('clean-ro')
        self.assertEqual(cr.parse_review(INCIDENT_MESSAGE)[0], 'clean')
        verdict, _, _, reasons = judge(stdout, INCIDENT_MESSAGE)
        self.assertEqual(verdict, 'failed')
        self.assertTrue(any('reports a failure' in reason for reason in reasons))


# Real `git diff` output for a non-ASCII path under the default core.quotePath=true.
QUOTED_PATH_DIFF = (
    'diff --git "a/\\346\\226\\207\\346\\241\\243/\\350\\257\\264\\346\\230\\216.md"'
    ' "b/\\346\\226\\207\\346\\241\\243/\\350\\257\\264\\346\\230\\216.md"\n'
    'index f2ad6c7..6178079 100644\n'
    '--- "a/\\346\\226\\207\\346\\241\\243/\\350\\257\\264\\346\\230\\216.md"\n'
    '+++ "b/\\346\\226\\207\\346\\241\\243/\\350\\257\\264\\346\\230\\216.md"\n'
    '@@ -1 +1 @@\n'
    '-c\n'
    '+b\n')
# Real `git show` output of a merge commit: a combined diff, not the PR diff.
MERGE_COMBINED_DIFF = (
    'diff --cc "\\346\\226\\207\\346\\241\\243/\\350\\257\\264\\346\\230\\216.md"\n'
    'index f2ad6c7,6178079..4bcfe98\n'
    '--- "a/\\346\\226\\207\\346\\241\\243/\\350\\257\\264\\346\\230\\216.md"\n'
    '+++ "b/\\346\\226\\207\\346\\241\\243/\\350\\257\\264\\346\\230\\216.md"\n'
    '@@@ -1,1 -1,1 +1,1 @@@\n'
    '- c\n'
    ' -b\n'
    '++d\n')


class SingleConditionTests(unittest.TestCase):
    """Each case breaks exactly one clean-verdict condition of a real clean run."""

    def setUp(self):
        self.stdout, self.last = fixture('clean-tests')
        self.assertEqual(judge(self.stdout, self.last)[0], 'clean')

    def assert_failed(self, verdict_tuple, phrase):
        verdict, _, _, reasons = verdict_tuple
        self.assertEqual(verdict, 'failed')
        self.assertEqual(len(reasons), 1, reasons)
        self.assertIn(phrase, reasons[0])

    def test_nonzero_exit(self):
        self.assert_failed(judge(self.stdout, self.last, rc=1), 'codex exited 1')

    def test_no_successful_command(self):
        self.assert_failed(judge(without(self.stdout, is_command), self.last), 'no command with exit code 0')

    def test_only_failed_commands(self):
        stdout = without(self.stdout, lambda e: is_command(e) and e['item'].get('exit_code') == 0)
        self.assertIn('"exit_code":127', stdout)
        self.assert_failed(judge(stdout, self.last), 'no command with exit code 0')

    def command_only(self, command, output, exit_code=0):
        """Return the clean run with its commands replaced by one command with this output."""
        event = {'type': 'item.completed', 'item': {'type': 'command_execution', 'command': command,
                                                    'aggregated_output': output, 'exit_code': exit_code,
                                                    'status': 'completed'}}
        return json.dumps(event) + '\n' + without(self.stdout, is_command)

    def test_commands_whose_output_holds_no_unified_diff_are_not_evidence(self):
        diff_text = 'diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n'
        for command, output, exit_code in (
                ("bash -lc 'git status --short'", ' M x\n', 0), ("bash -lc 'ls -la'", 'x\n', 0),
                ("bash -lc 'git -v diff'", 'git version 2.34.1\n', 0),
                ("bash -lc \"echo '&&' git diff\"", '&& git diff\n', 0),
                ("bash -lc 'git diff --stat'", ' x | 2 +-\n 1 file changed\n', 0),
                ("bash -lc 'git diff origin/main...HEAD'", '', 0),
                ("bash -lc 'git diff --name-only'", 'diff --git a/x b/x\n', 0),
                ("bash -lc 'git diff origin/main...HEAD'", diff_text, 128),
                ("bash -lc 'git show HEAD'", MERGE_COMBINED_DIFF, 0),
                ("bash -lc 'git show -c HEAD'", MERGE_COMBINED_DIFF.replace('diff --cc', 'diff --combined'), 0)):
            with self.subTest(command=command, output=output):
                self.assert_failed(judge(self.command_only(command, output, exit_code), self.last),
                                   'printed a unified diff')

    def test_any_command_whose_output_holds_a_unified_diff_is_evidence(self):
        diff_text = 'diff --git a/x b/x\nindex 1..2 100644\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n'
        for command, output in (
                ("bash -lc 'git status --short\ngit diff HEAD'", ' M x\n' + diff_text),
                ('git -C "/tmp/repo with spaces" diff HEAD', diff_text),
                ("bash -lc 'git diff origin/main...HEAD | head -c 60'", diff_text[:60]),
                ("bash -lc 'git log -p -1'", 'commit abc\n\n' + diff_text),
                ("bash -lc 'git diff origin/main...HEAD'", QUOTED_PATH_DIFF)):
            with self.subTest(command=command):
                self.assertTrue(cr.diff_commands(cr.read_events(self.command_only(command, output))))
                self.assertEqual(judge(self.command_only(command, output), self.last)[0], 'clean')

    def test_error_item(self):
        error = {'type': 'item.completed', 'item': {'id': 'e', 'type': 'error', 'message': 'Code Mode is off'}}
        self.assert_failed(judge(json.dumps(error) + '\n' + self.stdout, self.last), 'error item: Code Mode is off')

    def test_turn_failed_event(self):
        failed = {'type': 'turn.failed', 'error': {'message': 'stream disconnected'}}
        self.assert_failed(judge(self.stdout + json.dumps(failed) + '\n', self.last), 'turn.failed')

    def test_no_completed_turn(self):
        self.assert_failed(judge(without(self.stdout, lambda e: e['type'] == 'turn.completed'), self.last),
                           'no completed turn')

    def test_failure_signal_in_message(self):
        for text in ('The verdict is a placeholder.', 'I was unable to read the diff.',
                     'The code-mode host is missing.', 'Reviewer failed to output a response.',
                     '工具缺失，无法检查代码。'):
            with self.subTest(text):
                self.assert_failed(judge(self.stdout, text + '\nVERDICT: CLEAN'), 'reports a failure')

    def test_review_failure_phrasings_still_fail(self):
        for text in ('Review blocked: the execution tool failed.',
                     'The correctness verdict is an unverified placeholder.',
                     'Unable to review or verify correctness.', 'The sandbox was blocked.',
                     'Codex failed to read the diff.', '审查中断：未能读取 diff。', '我无法读取仓库。',
                     '本次审查无法读取文件，因此无法完成审查。',
                     'The reviewer was unable to read the files, so the review is incomplete.',
                     'I could not complete the review.', 'The review was incomplete.', '审查未完成。',
                     'I did not finish the review.', 'Codex failed to complete this review.', '没有完成审查。'):
            with self.subTest(text):
                self.assert_failed(judge(self.stdout, text + '\nVERDICT: CLEAN'), 'reports a failure')

    def test_business_descriptions_and_negations_stay_clean(self):
        for text in ('No defects found. Requests are blocked for untrusted authors as intended.',
                     'The fixture keeps placeholder credentials, which the loader rejects at startup.',
                     'Unverified webhook signatures are refused with 401.',
                     'Non-admin users cannot view the settings page, as the spec requires.',
                     'The client is unable to read the body after a timeout and retries; that path is covered.',
                     'No tool is missing and the review was not blocked.',
                     'The handler correctly returns no such file or directory for absent paths.',
                     'The installer fails if the helper binary is missing, which is intended.',
                     '普通用户无法查看他人订单，与设计一致。', '没有无法读取的文件。', '没有工具缺失。',
                     'I was able to complete the review. No defects found.', 'I finished the review of every file.',
                     '已完成审查，未发现缺陷。', '本次审查已完成。'):
            with self.subTest(text):
                self.assertEqual(judge(self.stdout, text + '\nVERDICT: CLEAN')[:1], ('clean',))

    def test_missing_verdict_line(self):
        message = self.last.replace('VERDICT: CLEAN', '')
        self.assert_failed(judge(self.stdout, message), 'no single `VERDICT: CLEAN`')

    def test_blocked_verdict_line(self):
        message = self.last.replace('VERDICT: CLEAN', 'VERDICT: BLOCKED')
        verdict, _, _, reasons = judge(self.stdout, message)
        self.assertEqual(verdict, 'failed')
        self.assertTrue(any('found BLOCKED' in reason for reason in reasons))

    def test_empty_message(self):
        stdout = without(self.stdout, lambda e: (e.get('item') or {}).get('type') == 'agent_message')
        verdict, _, _, reasons = judge(stdout, '')
        self.assertEqual(verdict, 'failed')
        self.assertIn('codex wrote no final message', reasons)

    def test_stderr_noise_in_stdout_is_skipped(self):
        noise = (FIXTURES / 'clean-tests.stderr').read_text(encoding='utf-8')
        self.assertEqual(judge(noise + self.stdout + noise, self.last)[0], 'clean')

    def test_last_agent_message_is_used_without_output_file(self):
        self.assertEqual(judge(self.stdout, '')[0], 'clean')


class ReviewerFailClosedTests(Harness):
    def test_broken_codex_output_posts_failed_with_reasons(self):
        self.codex.events, self.codex.message = fixture('broken-tests')
        self.deliver(pull_payload())
        job, status = self.run_next()
        body = self.github.posted[0]['body']
        self.assertEqual(status, 'failed')
        self.assertIn(f'head={HEAD} verdict=failed', body)
        self.assertIn('not a pass', body)
        self.assertIn('codex-code-mode-host', body)
        kept = (self.root / 'output' / f'{job.id}.log').read_text()
        self.assertIn('--- stderr ---', kept)

    def test_incident_message_is_not_posted_as_clean(self):
        self.codex.message = INCIDENT_MESSAGE
        self.deliver(pull_payload())
        self.assertEqual(self.run_next()[1], 'failed')


RECONNECT = ('{"type":"error","message":"Reconnecting... 2/5 (unexpected status 503 Service Unavailable: An error '
             'occurred while processing your request.)"}\n')
CAPACITY = 'Selected model is at capacity. Please try a different model.'


class TransientClassifierTests(unittest.TestCase):
    def test_real_capacity_failures_are_transient(self):
        for name in ('capacity-after-command', 'capacity-after-503'):
            with self.subTest(name):
                stdout = (FIXTURES / f'{name}.jsonl').read_text(encoding='utf-8')
                self.assertEqual(cr.transient_error(1, cr.read_events(stdout)), CAPACITY)

    def test_completed_and_deterministic_runs_are_not_transient(self):
        for name in ('clean-tests', 'findings-ro', 'broken-tests'):
            with self.subTest(name):
                self.assertIsNone(cr.transient_error(0, cr.read_events(fixture(name)[0])))
        auth = '{"type":"turn.failed","error":{"message":"401 Unauthorized: invalid credentials"}}\n'
        self.assertIsNone(cr.transient_error(1, cr.read_events(auth)))
        self.assertIsNone(cr.transient_error(1, []))

    def test_reconnect_notices_of_a_completed_turn_do_not_fail_the_review(self):
        stdout, last = fixture('clean-tests')
        stdout = RECONNECT + stdout
        self.assertIsNone(cr.transient_error(0, cr.read_events(stdout)))
        self.assertEqual(judge(stdout, last)[0], 'clean')


class TransientRetryTests(Harness):
    def setUp(self):
        super().setUp()
        self.now = [1_000_000.0]
        self.store.clock = lambda: self.now[0]
        self.capacity = (FIXTURES / 'capacity-after-503.jsonl').read_text(encoding='utf-8')
        self.codex.events, self.codex.rc, self.codex.message = self.capacity, 1, ''

    def run_due(self):
        due = self.store.db.execute("SELECT not_before FROM jobs WHERE status = 'queued' ORDER BY id").fetchone()
        self.now[0] = max(self.now[0], due[0])
        return self.run_next()

    def test_capacity_failure_posts_nothing_and_requeues_with_backoff(self):
        self.deliver(pull_payload())
        job, status = self.run_next()
        self.assertIsNone(status)
        self.assertEqual(self.github.posted, [])
        self.assertEqual(self.store.status(job.id), 'queued')
        self.assertEqual(self.store.transient_state(job.id), (1, 1_000_000.0, CAPACITY))
        first = self.store.db.execute('SELECT not_before FROM jobs WHERE id = ?', (job.id,)).fetchone()[0]
        self.assertEqual(first - self.now[0], 60)
        delays = []
        for _ in range(6):
            self.run_due()
            due = self.store.db.execute('SELECT not_before FROM jobs WHERE id = ?', (job.id,)).fetchone()[0]
            delays.append(int(due - self.now[0]))
        self.assertEqual(delays, [120, 240, 480, 960, 1800, 1800])
        self.assertEqual(self.github.posted, [])

    def test_window_spent_posts_failed_with_count_and_last_error(self):
        self.deliver(pull_payload())
        statuses = [self.run_next()[1]]
        while statuses[-1] is None and len(statuses) < 20:
            statuses.append(self.run_due()[1])
        self.assertEqual(statuses, [None] * 7 + ['failed'])
        body = self.github.posted[0]['body']
        self.assertIn(f'head={HEAD} verdict=failed', body)
        self.assertIn('transient upstream errors 8 time(s) over 5460 s', body)
        self.assertIn(f'last error: {CAPACITY}', body)
        self.assertEqual(len(self.codex.calls), 8)

    def test_success_after_transient_failure_posts_the_real_verdict(self):
        self.deliver(pull_payload())
        job, _ = self.run_next()
        self.codex.events, self.codex.rc, self.codex.message = None, 0, cr_clean()
        self.assertEqual(self.run_due()[1], 'clean')
        self.assertIn('verdict=clean', self.github.posted[0]['body'])
        self.assertIsNone(self.store.transient_state(job.id))

    def test_deterministic_failure_posts_failed_at_once(self):
        for events, rc in ((fixture('broken-tests')[0], 0),
                           ('{"type":"turn.failed","error":{"message":"401 Unauthorized"}}\n', 1)):
            with self.subTest(rc=rc):
                self.github.posted.clear()
                self.github.markers.clear()
                self.codex.events, self.codex.rc = events, rc
                self.store.force_enqueue(REPO, 7, HEAD, 'main', 99)
                job, status = self.run_next()
                self.assertEqual(status, 'failed')
                self.assertEqual(len(self.github.posted), 1)
                self.assertIsNone(self.store.transient_state(job.id))

    def test_head_change_during_retry_supersedes_the_old_job(self):
        self.deliver(pull_payload())
        job, _ = self.run_next()
        self.github.heads[7] = NEW_HEAD
        self.assertEqual(self.run_due()[1], 'superseded')
        self.assertEqual(self.store.status(job.id), 'superseded')
        self.assertEqual([j.head for j in self.store.queued()], [NEW_HEAD])
        self.assertEqual(self.github.posted, [])

    def test_new_head_delivery_supersedes_a_job_waiting_to_retry(self):
        self.deliver(pull_payload())
        job, _ = self.run_next()
        self.deliver(pull_payload(head=NEW_HEAD), guid='d-2')
        self.assertEqual(self.store.status(job.id), 'superseded')
        self.assertEqual([j.head for j in self.store.queued()], [NEW_HEAD])

    def test_pending_retry_survives_a_restart(self):
        self.deliver(pull_payload())
        job, _ = self.run_next()
        self.store.db.close()
        self.store = cr.Store(self.root / 'state.sqlite', clock=lambda: self.now[0])
        self.reviewer = cr.Reviewer(self.config, self.github, self.store, self.git, self.codex)
        self.assertEqual(self.store.reset_running(), 0)
        self.assertEqual(self.store.transient_state(job.id), (1, 1_000_000.0, CAPACITY))
        self.assertIsNone(self.store.claim_next())
        self.assertIsNone(self.run_due()[1])
        self.assertEqual(self.store.transient_state(job.id)[0], 2)

    def test_requeued_job_starts_a_fresh_retry_window(self):
        self.deliver(pull_payload())
        job, _ = self.run_next()
        self.deliver(pull_payload(head=NEW_HEAD), guid='d-2')
        self.deliver(pull_payload(head=HEAD), guid='d-3')
        self.assertEqual(self.store.status(job.id), 'queued')
        self.assertIsNone(self.store.transient_state(job.id))


def cr_clean():
    return 'The change is consistent with its callers.\nVERDICT: CLEAN'


REAL_CLEAN_WITHOUT_PYTEST = (
    '未发现符合报告标准的新缺陷；diff 检查和合约清单校验通过。环境仅有 Python 3.10 且缺少 pytest，未能执行项目测试。\n'
    'VERDICT: CLEAN',
    '未发现此次变更引入的明确缺陷。合约清单校验通过；当前环境缺少 pytest，未能运行测试。\nVERDICT: CLEAN',
    'No defects found. I was unable to run the test suite because pytest is not installed.\nVERDICT: CLEAN',
    '未发现缺陷。沙箱禁止联网，无法访问 GitHub API，未核对 issue 正文。\nVERDICT: CLEAN',
    'No defects found. I was unable to access the GitHub API from the sandbox, so the issue text was not checked.\n'
    'VERDICT: CLEAN')


class TestRunLimitTests(unittest.TestCase):
    def test_not_running_tests_is_not_a_tool_failure(self):
        stdout, _ = fixture('clean-tests')
        for message in REAL_CLEAN_WITHOUT_PYTEST:
            with self.subTest(message[:20]):
                self.assertEqual(judge(stdout, message)[0], 'clean')

class FakeTransport:
    def __init__(self, review):
        self.review, self.calls = review, []

    def __call__(self, method, url, token, body):
        self.calls.append((method, url.replace(cr.API, ''), body))
        if url.endswith('/app'):
            return 200, {'slug': 'remote-review'}, ''
        if url.endswith('/installation'):
            return 200, {'id': 99}, ''
        if '/app/installations/' in url and method == 'GET':
            return 200, {'id': 99, 'account': {'login': 'Example-Org'}}, ''
        if url.endswith('/access_tokens'):
            return 201, {'token': 'tok', 'expires_at': '2099-01-01T00:00:00Z'}, ''
        if method == 'GET':
            return 200, self.review, ''
        return 200, dict(self.review, body=body['body']), ''


class InvalidateTests(unittest.TestCase):
    def review(self, login='remote-review[bot]', verdict='clean'):
        marker = cr.MARKER.format(head=HEAD, verdict=verdict, effort=' effort=high', tests=' tests=read-only', mode='')
        return {'id': 5, 'user': {'login': login}, 'body': f'{marker}\n\n**Remote codex review: no findings**'}

    def test_rewrites_body_and_marks_verdict_invalid(self):
        transport = FakeTransport(self.review())
        github = cr.GitHub(config(), transport=transport, jwt_factory=lambda: 'jwt')
        self.assertEqual(github.invalidate_review(REPO, 7, 5, 'Void.'), (HEAD, 'clean'))
        method, path, body = transport.calls[-1]
        self.assertEqual((method, path), ('PUT', f'/repos/{REPO}/pulls/7/reviews/5'))
        self.assertEqual(body['body'], f'<!-- remote-codex-review head={HEAD} verdict=invalid effort=high '
                                       'tests=read-only -->\n\nVoid.')

    def test_refuses_reviews_of_other_authors_and_unmarked_reviews(self):
        for review in (self.review(login='someone'), dict(self.review(), body='plain text')):
            transport = FakeTransport(review)
            github = cr.GitHub(config(), transport=transport, jwt_factory=lambda: 'jwt')
            with self.assertRaises(ValueError):
                github.invalidate_review(REPO, 7, 5, 'Void.')
            self.assertNotIn('PUT', [call[0] for call in transport.calls])

    def test_invalid_marker_does_not_count_as_reviewed(self):
        reviews = [{'user': {'login': 'remote-review[bot]'}, 'id': 3,
                    'body': cr.MARKER.format(head=HEAD, verdict='invalid', effort='', tests='', mode='')}]

        def transport(method, url, token, body):
            return 200, ({'slug': 'remote-review'} if url.endswith('/app') else reviews), ''
        github = cr.GitHub(config(), transport=transport, jwt_factory=lambda: 'jwt')
        self.assertIsNone(github.existing_marker(REPO, 7, HEAD, 't'))
        reviews.append({'user': {'login': 'remote-review[bot]'}, 'id': 4,
                        'body': cr.MARKER.format(head=HEAD, verdict='clean', effort='', tests='', mode='')})
        self.assertEqual(github.existing_marker(REPO, 7, HEAD, 't'), ('clean', 4))


class EnqueueTests(Harness):
    def finished_job(self, head=HEAD):
        self.store.enqueue(REPO, 7, head, 'main', 99)
        job = self.store.claim_next()
        self.store.finish(job.id, 'clean', 5)
        return job

    def test_force_enqueue_requeues_a_finished_head(self):
        job = self.finished_job()
        self.assertFalse(self.store.enqueue(REPO, 7, HEAD, 'main', 99)[0])
        self.assertTrue(self.store.force_enqueue(REPO, 7, HEAD, 'main', 99))
        self.assertEqual(self.store.status(job.id), 'queued')
        self.assertEqual([j.id for j in self.store.queued()], [job.id])

    def test_force_enqueue_supersedes_other_live_heads_and_keeps_a_live_one(self):
        self.store.enqueue(REPO, 7, NEW_HEAD, 'main', 99)
        self.assertTrue(self.store.force_enqueue(REPO, 7, HEAD, 'main', 99))
        self.assertEqual([j.head for j in self.store.queued()], [HEAD])
        self.assertFalse(self.store.force_enqueue(REPO, 7, HEAD, 'main', 99))

    def test_enqueue_command_uses_live_base_and_checks_admission(self):
        github = mock.Mock()
        github.repo_installation.return_value = 99
        github.pull.return_value = dict(pull_payload(base='release')['pull_request'])
        self.assertEqual(cr.enqueue_command(self.config, github, self.store, f'{REPO}#7@{HEAD}'), 0)
        self.assertEqual([(j.head, j.base, j.installation) for j in self.store.queued()], [(HEAD, 'release', 99)])

    def test_enqueue_command_with_a_stale_sha_queues_the_live_head(self):
        self.finished_job(HEAD)
        self.store.enqueue(REPO, 7, NEW_HEAD, 'main', 99)
        github = mock.Mock()
        github.repo_installation.return_value = 99
        github.pull.return_value = dict(pull_payload(head=NEW_HEAD)['pull_request'])
        self.assertEqual(cr.enqueue_command(self.config, github, self.store, f'{REPO}#7@{HEAD}'), 0)
        self.assertEqual([(j.head, j.status) for j in self.store.queued()], [(NEW_HEAD, 'queued')])

    def test_enqueue_command_refuses_a_pr_that_fails_admission(self):
        github = mock.Mock()
        github.repo_installation.return_value = 99
        github.pull.return_value = dict(pull_payload(state='closed')['pull_request'])
        self.assertEqual(cr.enqueue_command(self.config, github, self.store, f'{REPO}#7@{HEAD}'), 1)
        self.assertEqual(self.store.queued(), [])

    def test_reenqueued_head_is_reviewed_after_invalidation(self):
        self.finished_job()
        self.github.markers[(7, HEAD)] = None
        self.store.force_enqueue(REPO, 7, HEAD, 'main', 99)
        self.assertEqual(self.run_next()[1], 'clean')
        self.assertEqual(len(self.codex.calls), 2)
        self.assertIn('mode=full -->', self.github.posted[-1]['body'])


FAKE_CODEX = '''#!{python}
import json, os, sys
args = sys.argv[1:]
last = args[args.index('-o') + 1]
mode = {mode!r}
if mode == 'exit':
    sys.exit(4)
events = [{{'type': 'thread.started'}}, {{'type': 'turn.started'}}]
token = open('token.txt').read().strip() if mode != 'blind' else ''
if mode == 'error':
    events.append({{'type': 'item.completed', 'item': {{'type': 'error', 'message': 'host executable was not found'}}}})
else:
    events.append({{'type': 'item.completed', 'item': {{'type': 'command_execution', 'command': 'cat token.txt',
                   'aggregated_output': token + '\\n', 'exit_code': 0, 'status': 'completed'}}}})
reply = token if mode != 'mute' else 'done'
events.append({{'type': 'item.completed', 'item': {{'type': 'agent_message', 'text': reply}}}})
events.append({{'type': 'turn.completed'}})
open(last, 'w').write(reply)
print('2026-01-01T00:00:00Z ERROR rmcp: noise', file=sys.stderr)
print('\\n'.join(json.dumps(e) for e in events))
'''


class SelfCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def codex(self, mode):
        path = self.root / f'codex-{mode}'
        path.write_text(FAKE_CODEX.format(python=sys.executable, mode=mode))
        path.chmod(0o755)
        return str(path)

    def test_passes_when_codex_reads_the_token(self):
        self.assertIsNone(cr.self_check(self.codex('ok'), self.root / 'work'))
        self.assertEqual(list((self.root / 'work').iterdir()), [])

    def test_fails_for_each_missing_piece(self):
        for mode, phrase in (('exit', 'codex exited 4'), ('error', 'host executable was not found'),
                             ('blind', 'no command codex ran printed the token'),
                             ('mute', 'final message does not contain the token')):
            with self.subTest(mode):
                reason = cr.self_check(self.codex(mode), self.root / 'work')
                self.assertIsNotNone(reason)
                self.assertIn(phrase, reason)

    def test_missing_codex_binary_fails(self):
        self.assertIn('could not run codex', cr.self_check(str(self.root / 'absent'), self.root / 'work'))

    def test_service_refuses_to_start_and_systemd_does_not_restart_it(self):
        secret = self.root / 'secret'
        secret.write_text('s')
        secret.chmod(0o600)
        env = self.root / 'env'
        env.write_text(textwrap.dedent(f'''\
            CODEX_REVIEW_APP_ID=42
            CODEX_REVIEW_PRIVATE_KEY={self.root / 'key'}
            CODEX_REVIEW_WEBHOOK_SECRET_FILE={secret}
            CODEX_REVIEW_ACCOUNTS=example-owner
            CODEX_REVIEW_AUTHORS=example-owner
            CODEX_REVIEW_STATE_DIR={self.root}
            CODEX_REVIEW_CODEX_BIN={self.codex('blind')}
            '''))
        (self.root / 'work').mkdir()
        (self.root / 'mirrors').mkdir()
        with mock.patch.object(cr, 'serve') as serve:
            self.assertEqual(cr.main(['--config', str(env)]), cr.SELF_CHECK_FAILED)
        serve.assert_not_called()
        unit = (ROOT / 'deploy' / 'codex-review.service').read_text()
        self.assertIn(f'RestartPreventExitStatus={cr.SELF_CHECK_FAILED}\n', unit)


class InstallScriptTests(unittest.TestCase):
    def package_ok(self, directory):
        script = (ROOT / 'deploy' / 'install.sh').read_text()
        assignments = '\n'.join(line for line in script.splitlines()
                                if line.startswith(('CODEX_VERSION=', 'CODEX_REQUIRED=')))
        function = script[script.index('package_ok() {'):script.index('\n}\n', script.index('package_ok() {')) + 3]
        return subprocess.run(['bash', '-c', f'{assignments}\n{function}\npackage_ok "$1"', 'x', str(directory)],
                              capture_output=True, text=True)

    def test_package_check_requires_every_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'codex-package.json').write_text('{"version": "0.160.1"}')
            files = ['bin/codex', 'bin/codex-code-mode-host', 'codex-resources/bwrap', 'codex-resources/zsh/bin/zsh',
                     'codex-path/rg']
            for name in files:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text('')
                (root / name).chmod(0o755)
            self.assertEqual(self.package_ok(root).returncode, 0)
            for name in files:
                with self.subTest(name):
                    (root / name).chmod(0o644)
                    result = self.package_ok(root)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(name, result.stderr)
                    (root / name).chmod(0o755)
            (root / 'codex-package.json').write_text('{"version": "0.159.0"}')
            self.assertNotEqual(self.package_ok(root).returncode, 0)

    def shell_function(self, name, call, *args):
        script = (ROOT / 'deploy' / 'install.sh').read_text()
        start = script.index(f'{name}() {{')
        function = script[start:script.index('\n}\n', start) + 3]
        return subprocess.run(['bash', '-c', f'{function}\n{call}', 'x', *map(str, args)],
                              capture_output=True, text=True)

    def drop_tests_tier(self, path):
        return self.shell_function('drop_tests_tier', 'drop_tests_tier "$1"', path)

    def test_configured_rules_path_is_read_from_the_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / 'codex-review.env'
            for line, expected in (('CODEX_REVIEW_EFFORT_RULES=/srv/rules.json', '/srv/rules.json'),
                                   ('CODEX_REVIEW_EFFORT_RULES = "/srv/my rules.json"  ', '/srv/my rules.json'),
                                   ("CODEX_REVIEW_EFFORT_RULES='/srv/q.json'", '/srv/q.json'),
                                   ('# CODEX_REVIEW_EFFORT_RULES=/x', '')):
                with self.subTest(line):
                    env.write_text(f'CODEX_REVIEW_APP_ID=1\n{line}\n')
                    result = self.shell_function('configured_rules', 'configured_rules "$1"', env)
                    self.assertEqual(result.stdout.strip('\n'), expected)

    def test_package_modes_do_not_depend_on_the_umask(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'pkg'
            result = self.shell_function(
                'fix_package_modes', 'umask 077; mkdir -p "$1/bin"; : > "$1/bin/codex"; : > "$1/data.json"; '
                'chmod 700 "$1/bin/codex"; chmod 600 "$1/data.json"; fix_package_modes "$1"', root)
            self.assertEqual(result.returncode, 0, result.stderr)
            modes = {name: stat.S_IMODE((root / name).stat().st_mode)
                     for name in ('.', 'bin', 'bin/codex', 'data.json')}
            self.assertEqual(modes, {'.': 0o755, 'bin': 0o755, 'bin/codex': 0o755, 'data.json': 0o644})

    def test_upgrade_drops_the_retired_tests_tier_so_the_service_starts(self):
        rules = {'default': {'medium': ['docs/wiki/**', 'docs/*.md', '**/README.md'], 'high': ['docs/runbooks/**'],
                             'tests': ['**/migrations/**', 'deploy/**']},
                 'repos': {'o/r': {'medium': ['docs/**'], 'tests': ['x/**']}}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'effort-rules.json'
            path.write_text(json.dumps(rules))
            path.chmod(0o640)
            with self.assertRaises(cr.ConfigError):
                cr.load_effort_rules(str(path))
            result = self.drop_tests_tier(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('removed 2 retired tests table(s)', result.stdout)
            self.assertEqual(cr.load_effort_rules(str(path)), {
                'default': {'medium': ['docs/wiki/**', 'docs/*.md', '**/README.md'], 'high': ['docs/runbooks/**']},
                'repos': {'o/r': {'medium': ['docs/**']}}})
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            before = path.read_text()
            self.assertEqual((self.drop_tests_tier(path).stdout, path.read_text()), ('', before))


if __name__ == '__main__':
    unittest.main()
