#!/usr/bin/env python3
"""Tests for prefetching the governance issues that AGENTS.md links."""
import json
from pathlib import Path
import stat
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import codex_review as cr  # noqa: E402
from test_codex_review import Harness, pull_payload  # noqa: E402
from test_fail_closed import CAPACITY, fixture, judge  # noqa: E402

AGENTS = ('# Guidance\n\nBefore any work, read https://github.com/qianxueye/llm-trading/issues/1859 and\n'
          'https://github.com/Example-Org/Rules/issues/12;\n'
          'again: https://github.com/qianxueye/llm-trading/issues/1859\n')
ISSUE = {'title': '代理工作流约束', 'body': '先读本 issue，再动手。'}
REF = 'qianxueye/llm-trading#1859'
JOB_85_TAIL = ('未发现达到报告标准的新增缺陷，拒绝码、API 契约及前端映射一致。工作流 issue #1859 因访问限制未能读取；'
               '本次为只读审查，未运行测试 (read-only review; no tests were run)。\nVERDICT: CLEAN')


class GovernancePrefetchTests(Harness):
    def setUp(self):
        super().setUp()
        self.git.agents = AGENTS
        self.github.issues[('qianxueye/llm-trading', 1859)] = ISSUE
        self.github.issues[('Example-Org/Rules', 12)] = {'title': 'Rules', 'body': None}
        self.github.comments[('qianxueye/llm-trading', 1859)] = [
            {'user': {'login': 'qianxueye'}, 'created_at': '2026-10-01T00:00:00Z', 'body': '补充：先跑测试。'}]

    def test_linked_issues_are_saved_outside_the_worktree_and_named_in_the_prompt(self):
        self.deliver(pull_payload())
        job, status = self.run_next()
        self.assertEqual(status, 'clean')
        self.assertEqual(self.github.issue_calls, [('qianxueye/llm-trading', 1859), ('Example-Org/Rules', 12)])
        worktree = self.codex.calls[0][1]
        (ref, path, text, error), second = self.codex.governance
        self.assertEqual((ref, error), (REF, None))
        self.assertEqual(text, '# 代理工作流约束\n\n先读本 issue，再动手。\n'
                               '\n## Comment by qianxueye at 2026-10-01T00:00:00Z\n\n补充：先跑测试。\n')
        self.assertEqual(path.relative_to(self.root / 'work').parts[1:], ('governance', 'qianxueye', 'llm-trading',
                                                                          '1859.md'))
        self.assertFalse(path.is_relative_to(worktree))
        self.assertEqual(second[2], '# Rules\n\n\n')
        prompt = cr.review_prompt(cr.first_prompt('main', cr.LENSES[0][1]), [cr.GovernanceDoc(ref, path)])
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
                prompt = cr.review_prompt(cr.first_prompt('main', cr.LENSES[0][1]), [cr.GovernanceDoc(REF, error='x')])
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
        self.assertEqual(cr.review_prompt(cr.first_prompt('main', cr.LENSES[0][1]), []), cr.review_prompt(cr.first_prompt('main', cr.LENSES[0][1])))
        self.assertNotIn('AGENTS.md', cr.review_prompt(cr.first_prompt('main', cr.LENSES[0][1])))

    def test_no_agents_file_means_no_fetch(self):
        self.git.agents = None
        self.deliver(pull_payload())
        self.assertEqual(self.run_next()[1], 'clean')
        self.assertEqual(self.github.issue_calls, [])

    def test_issues_whose_dashed_names_coincide_get_distinct_files(self):
        self.git.agents = 'https://github.com/a-b/c/issues/12 https://github.com/a/b-c/issues/12\n'
        self.github.issues[('a-b/c', 12)] = {'title': 'first', 'body': ''}
        self.github.issues[('a/b-c', 12)] = {'title': 'second', 'body': ''}
        self.deliver(pull_payload())
        self.assertEqual(self.run_next()[1], 'clean')
        (_, first, first_text, _), (_, second, second_text, _) = self.codex.governance
        self.assertNotEqual(first, second)
        self.assertEqual((first_text[:8], second_text[:9]), ('# first\n', '# second\n'))

    def test_dot_and_dotdot_are_rejected_but_dot_prefixed_repositories_are_kept(self):
        links = cr.governance_links('github.com/a/../issues/1 github.com/a/./issues/2 github.com/a/.github/issues/3')
        self.assertEqual(links, [('a/.github', 3)])

    def test_dot_github_issue_is_saved_under_its_own_directory(self):
        self.git.agents = 'Read https://github.com/Example-Org/.github/issues/4 first.\n'
        self.github.issues[('Example-Org/.github', 4)] = {'title': 'org rules', 'body': 'x'}
        self.deliver(pull_payload())
        self.assertEqual(self.run_next()[1], 'clean')
        ((ref, path, text, error),) = self.codex.governance
        self.assertEqual((ref, path.parts[-3:], text[:12], error),
                         ('Example-Org/.github#4', ('Example-Org', '.github', '4.md'), '# org rules\n', None))

    def _worktree(self):
        worktree = self.root / 'tree'
        worktree.mkdir()
        (worktree / 'AGENTS.md').write_text(AGENTS, encoding='utf-8')
        return worktree


class TransientGiveUpTests(Harness):
    def test_window_spent_body_names_the_issue_that_was_not_prefetched(self):
        self.config.retry_window = 0
        self.git.agents = AGENTS
        self.github.issues[('qianxueye/llm-trading', 1859)] = cr.GitHubError(403, 'Resource not accessible')
        self.github.issues[('Example-Org/Rules', 12)] = {'title': 'Rules', 'body': ''}
        self.codex.rc = 1
        self.codex.events = json.dumps({'type': 'turn.failed', 'error': {'message': CAPACITY}}) + '\n'
        self.deliver(pull_payload())
        self.assertEqual(self.run_next()[1], 'failed')
        body = self.github.posted[0]['body']
        self.assertIn('transient upstream errors', body)
        self.assertIn(f'Governance issue `{REF}` linked from AGENTS.md could not be prefetched', body)


class FailureSignalTests(unittest.TestCase):
    def test_unread_diff_is_failed_and_an_unread_governance_issue_is_not(self):
        stdout, _ = fixture('clean-ro')
        verdict, _, _, reasons = judge(stdout, '审查中断：未能读取 diff，无法确认改动内容。\nVERDICT: CLEAN')
        self.assertEqual(verdict, 'failed')
        self.assertTrue(any('未能读取 diff' in reason for reason in reasons))
        self.assertEqual(judge(stdout, JOB_85_TAIL)[0], 'clean')


if __name__ == '__main__':
    unittest.main()
