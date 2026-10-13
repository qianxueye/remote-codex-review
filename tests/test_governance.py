#!/usr/bin/env python3
"""Tests for prefetching the governance issues that AGENTS.md links."""
from pathlib import Path
import stat
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import codex_review as cr  # noqa: E402
from test_codex_review import Harness, pull_payload  # noqa: E402
from test_fail_closed import fixture, judge  # noqa: E402

AGENTS = ('# Guidance\n\nBefore any work, read https://github.com/qianxueye/llm-trading/issues/1859 and\n'
          'https://github.com/Example-Org/Rules/issues/12;\n'
          'again: https://github.com/qianxueye/llm-trading/issues/1859\n')
ISSUE = {'title': '代理工作流约束', 'body': '先读本 issue，再动手。'}
REF = 'qianxueye/llm-trading#1859'
INCIDENT_TAILS = (
    '未发现达到报告标准的新增缺陷，拒绝码、API 契约及前端映射一致。工作流 issue #1859 因访问限制未能读取；'
    '本次为只读审查，未运行测试 (read-only review; no tests were run)。\nVERDICT: CLEAN',
    '审查中断：未能读取 diff，无法确认改动内容。\nVERDICT: CLEAN')


class GovernancePrefetchTests(Harness):
    def setUp(self):
        super().setUp()
        self.git.agents = AGENTS
        self.github.issues[('qianxueye/llm-trading', 1859)] = ISSUE
        self.github.issues[('Example-Org/Rules', 12)] = {'title': 'Rules', 'body': None}

    def test_linked_issues_are_saved_outside_the_worktree_and_named_in_the_prompt(self):
        self.deliver(pull_payload())
        job, status = self.run_next()
        self.assertEqual(status, 'clean')
        self.assertEqual(self.github.issue_calls, [('qianxueye/llm-trading', 1859), ('Example-Org/Rules', 12)])
        worktree = self.codex.calls[0][1]
        (ref, path, text, error), second = self.codex.governance
        self.assertEqual((ref, error), (REF, None))
        self.assertEqual(text, '# 代理工作流约束\n\n先读本 issue，再动手。\n')
        self.assertFalse(path.is_relative_to(worktree))
        self.assertEqual(second[2], '# Rules\n\n\n')
        prompt = cr.review_prompt('main', [cr.GovernanceDoc(ref, path)])
        self.assertIn(f'- {REF}: {path}', prompt)
        self.assertIn('do not fetch them from the network', prompt)
        self.assertNotIn('could not be prefetched', self.github.posted[0]['body'])

    def test_saved_issue_file_is_read_only(self):
        docs = cr.prefetch_governance(self.github, 'token', self._worktree(), self.root / 'gov')
        self.assertEqual(stat.S_IMODE(docs[0].path.stat().st_mode), 0o444)

    def test_forbidden_or_missing_issue_is_reported_and_the_review_still_runs(self):
        for status, detail in ((403, 'Resource not accessible by integration'), (404, 'Not Found')):
            with self.subTest(status):
                self.github.posted.clear()
                self.github.markers.clear()
                self.github.issues[('qianxueye/llm-trading', 1859)] = cr.GitHubError(status, detail)
                self.deliver(pull_payload(), guid=f'd-{status}')
                self.store.db.execute("UPDATE jobs SET status = 'queued'")
                _, verdict = self.run_next()
                body = self.github.posted[0]['body']
                self.assertEqual(verdict, 'clean')
                self.assertIn(f'Governance issue `{REF}` linked from AGENTS.md could not be prefetched '
                              f'(GitHub HTTP {status}: {detail}); codex reviewed without it.', body)
                self.assertEqual(self.codex.governance[0][1:], (None, None, f'GitHub HTTP {status}: {detail}'))
                prompt = cr.review_prompt('main', [cr.GovernanceDoc(REF, error='x')])
                self.assertIn(f'not available; do not try to fetch them:\n- {REF}', prompt)

    def test_network_error_is_reported_not_raised(self):
        self.github.issues[('qianxueye/llm-trading', 1859)] = OSError('timed out')
        docs = cr.prefetch_governance(self.github, 'token', self._worktree(), self.root / 'gov')
        self.assertEqual((docs[0].path, docs[0].error), (None, 'timed out'))
        self.assertIsNotNone(docs[1].path)

    def test_no_links_means_no_fetch_no_files_and_no_prompt_paragraph(self):
        self.git.agents = '# Guidance\n\nRead docs/wiki/index.md first.\n'
        self.deliver(pull_payload())
        self.assertEqual(self.run_next()[1], 'clean')
        self.assertEqual((self.github.issue_calls, self.codex.governance), ([], []))
        self.assertEqual(cr.review_prompt('main', []), cr.review_prompt('main'))
        self.assertNotIn('AGENTS.md', cr.review_prompt('main'))

    def test_no_agents_file_means_no_fetch(self):
        self.git.agents = None
        self.deliver(pull_payload())
        self.assertEqual(self.run_next()[1], 'clean')
        self.assertEqual(self.github.issue_calls, [])

    def test_links_with_dot_repository_names_are_ignored(self):
        self.assertEqual(cr.governance_links('github.com/a/../issues/1 github.com/a/.x/issues/2'), [])

    def _worktree(self):
        worktree = self.root / 'tree'
        worktree.mkdir()
        (worktree / 'AGENTS.md').write_text(AGENTS, encoding='utf-8')
        return worktree


class FailureSignalUnchangedTests(unittest.TestCase):
    def test_message_saying_it_could_not_read_is_still_failed(self):
        stdout, _ = fixture('clean-ro')
        for message in INCIDENT_TAILS:
            with self.subTest(message[:20]):
                verdict, _, _, reasons = judge(stdout, message)
                self.assertEqual(verdict, 'failed')
                self.assertTrue(any('未能读取' in reason for reason in reasons))


if __name__ == '__main__':
    unittest.main()
