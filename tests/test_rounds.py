#!/usr/bin/env python3
"""First-round, re-review and confidence-floor tests; codex and GitHub are fakes, the delta tree uses real git."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_review as cr  # noqa: E402
from test_codex_review import HEAD, NEW_HEAD, REPO, WORKTREE, Harness, pull_payload  # noqa: E402

THIRD_HEAD = 'd' * 40
TREE = 'c' * 40


def finding(priority, title, path, line, score=None, body='The branch skips the check.'):
    text = f'- [{priority}] {title} — {WORKTREE}/{path}:{line}\n  {body}\n'
    return text + (f'  Confidence: {score}\n' if score is not None else '')


def message(*findings, verdict='FINDINGS', register=()):
    head = 'Summary of the change.\n'
    if register:
        head += cr.REGISTER_HEADING + '\n' + ''.join(f'{item}\n' for item in register)
    tail = '\nFull review comments:\n\n' + '\n'.join(findings) if findings else ''
    return head + f'VERDICT: {verdict}\n' + tail


class Rounds(Harness):
    def first_round(self, *messages):
        """Review HEAD as the PR's first head and return the posted body."""
        self.codex.messages = list(messages)
        self.deliver(pull_payload())
        self.run_next()
        return self.github.posted[-1]['body']

    def second_head(self, msg, guid='d-2', head=NEW_HEAD):
        """Push a new head after a reviewed one and review it; returns (status, posted body)."""
        self.github.heads[7] = head
        self.git.head = head
        self.codex.messages = [msg]
        self.deliver(pull_payload(head=head), guid=guid)
        _, status = self.run_next()
        return status, self.github.posted[-1]['body']


class FirstRoundTests(Rounds):
    def test_first_round_runs_two_xhigh_passes_and_merges_their_findings(self):
        body = self.first_round(
            message(finding('P2', 'Retry never stops', 'src/app.py', '10-12', 80),
                    finding('P3', 'Log is noisy', 'src/log.py', 3, 70)),
            message(finding('P1', 'Retry loop has no bound', 'src/app.py', 11, 90),
                    finding('P2', 'Caller ignores the error', 'src/caller.py', 7, 60)))
        self.assertEqual([call[3] for call in self.codex.calls], ['xhigh', 'xhigh'])
        self.assertIn(cr.LENSES[0][1], self.codex.calls[0][2])
        self.assertIn(cr.LENSES[1][1], self.codex.calls[1][2])
        for call in self.codex.calls:
            self.assertIn('git diff origin/main...HEAD', call[2])
            self.assertIn(cr.EXHAUSTIVE, call[2])
            self.assertIn('Confidence: <0-100>', call[2])
        self.assertIn(f'head={HEAD} verdict=findings effort=xhigh tests=read-only mode=full -->', body)
        self.assertEqual(body.count('Retry never stops'), 1)
        self.assertIn('同一位置另一遍（调用方与边界）的报告：**[P1] Retry loop has no bound**', body)
        self.assertLess(body.index('Retry never stops'), body.index('Caller ignores the error'))
        self.assertLess(body.index('Caller ignores the error'), body.index('Log is noisy'))

    def test_a_failed_second_pass_fails_the_round(self):
        self.codex.messages = [message(verdict='CLEAN'), 'I could not read the diff.\nVERDICT: BLOCKED']
        self.deliver(pull_payload())
        _, status = self.run_next()
        self.assertEqual(status, 'failed')
        self.assertIn('调用方与边界: ', self.github.posted[0]['body'])


class DeltaTests(Rounds):
    def test_rereview_gives_codex_only_the_delta(self):
        self.first_round(message(finding('P2', 'Retry never stops', 'src/app.py', 10, 80)), message(verdict='CLEAN'))
        self.git.delta_files = ['src/fix.py']
        status, body = self.second_head(message(verdict='CLEAN'))
        self.assertEqual(status, 'clean')
        self.assertEqual(len(self.codex.calls), 3)
        prompt, effort = self.codex.calls[2][2], self.codex.calls[2][3]
        self.assertEqual(self.git.delta_calls, [(HEAD, NEW_HEAD)])
        self.assertIn(f'run `git diff {TREE} HEAD`', prompt)
        self.assertIn('- src/fix.py', prompt)
        self.assertNotIn('origin/main...HEAD', prompt)
        self.assertIn('Retry never stops', prompt)
        self.assertEqual(effort, 'high')
        self.assertIn(f'head={NEW_HEAD} verdict=clean effort=high tests=read-only mode=delta -->', body)
        self.assertIn(f'`{HEAD[:12]}..{NEW_HEAD[:12]}`', body)

    def test_out_of_delta_findings_are_registered_and_do_not_count(self):
        self.first_round(message(verdict='CLEAN'), message(verdict='CLEAN'))
        item = '- [P2] Stale comment — src/old.py:4: the comment names a removed flag. Confidence: 80'
        status, body = self.second_head(message(verdict='FINDINGS', register=[item]))
        self.assertEqual(status, 'clean')
        self.assertIn(f'verdict=clean effort=high tests=read-only mode=delta -->', body)
        self.assertIn('## 登记项', body)
        self.assertIn(item, body)
        self.assertEqual(self.github.posted[-1]['comments'], [])

    def test_out_of_delta_p1_and_security_findings_still_block(self):
        for guid, item in (('d-2', '- [P1] Order is sent twice — src/old.py:4: no idempotency key. Confidence: 80'),
                           ('d-3', '- [P2] Token logged — src/old.py:9: the credential reaches the log. '
                                   'Confidence: 70')):
            with self.subTest(item):
                head = NEW_HEAD if guid == 'd-2' else THIRD_HEAD
                if guid == 'd-2':
                    self.first_round(message(verdict='CLEAN'), message(verdict='CLEAN'))
                status, body = self.second_head(message(verdict='CLEAN', register=[item]), guid=guid, head=head)
                self.assertEqual(status, 'findings')
                self.assertIn('## 增量外但阻塞的问题', body)
                self.assertIn(item, body)
                self.assertNotIn('## 登记项', body)

    def test_force_push_falls_back_to_a_full_first_round(self):
        self.first_round(message(verdict='CLEAN'), message(verdict='CLEAN'))
        self.git.delta = (None, f'last reviewed head {HEAD[:12]} is not an ancestor of {NEW_HEAD[:12]}')
        status, body = self.second_head(message(verdict='CLEAN'))
        self.assertEqual(status, 'clean')
        self.assertEqual([call[3] for call in self.codex.calls], ['xhigh'] * 4)
        self.assertIn('git diff origin/main...HEAD', self.codex.calls[2][2])
        self.assertIn('mode=full -->', body)
        self.assertIn('is not an ancestor', body)

    def test_previous_review_marked_invalid_is_not_a_delta_base(self):
        self.first_round(message(verdict='CLEAN'), message(verdict='CLEAN'))
        first = self.github.posted[0]
        first['body'] = first['body'].replace('verdict=clean', 'verdict=invalid', 1)
        status, body = self.second_head(message(verdict='CLEAN'))
        self.assertEqual(getattr(self.git, 'delta_calls', []), [])
        self.assertIn('mode=full -->', body)

    def test_empty_delta_after_a_clean_review_is_clean_without_codex(self):
        self.first_round(message(verdict='CLEAN'), message(verdict='CLEAN'))
        self.git.delta_files = []
        status, body = self.second_head(message(verdict='CLEAN'))
        self.assertEqual((status, len(self.codex.calls)), ('clean', 2))
        self.assertIn(f'head={NEW_HEAD} verdict=clean effort=none tests=read-only mode=delta -->', body)
        self.assertNotIn('codex was told', body)

    def assert_full_after_findings(self, previous):
        self.first_round(message(previous), message(verdict='CLEAN'))
        self.assertIn('verdict=findings', self.github.posted[0]['body'])
        self.git.delta_files = []
        self.codex.messages = [message(verdict='CLEAN'), message(verdict='CLEAN')]
        status, body = self.second_head(message(verdict='CLEAN'))
        self.assertEqual(status, 'clean')
        self.assertEqual([call[3] for call in self.codex.calls], ['xhigh'] * 4)
        self.assertIn(f'head={NEW_HEAD} verdict=clean effort=xhigh tests=read-only mode=full -->', body)
        self.assertIn('was findings', body)

    def test_empty_delta_after_a_p1_review_is_reviewed_in_full(self):
        self.assert_full_after_findings(finding('P1', 'Order is sent twice', 'src/order.py', 7, 90))

    def test_empty_delta_after_a_p3_only_review_is_reviewed_in_full(self):
        self.assert_full_after_findings(finding('P3', 'Typo in log', 'src/log.py', 2, 80))

    def test_empty_delta_after_a_truncated_clean_review_is_reviewed_in_full(self):
        self.first_round(message(verdict='CLEAN'), message(verdict='CLEAN'))
        self.github.posted[0]['body'] += '\n\n[truncated]'
        self.git.delta_files = []
        status, body = self.second_head(message(verdict='CLEAN'))
        self.assertEqual([call[3] for call in self.codex.calls], ['xhigh'] * 4)
        self.assertIn('mode=full -->', body)
        self.assertIn('was clean and truncated', body)

    def test_base_change_falls_back_to_a_full_round(self):
        self.first_round(message(verdict='CLEAN'), message(verdict='CLEAN'))
        self.github.base = 'release'
        status, body = self.second_head(message(verdict='CLEAN'))
        self.assertEqual(status, 'clean')
        self.assertEqual(getattr(self.git, 'delta_calls', []), [])
        self.assertEqual([call[3] for call in self.codex.calls], ['xhigh'] * 4)
        self.assertIn('git diff origin/release...HEAD', self.codex.calls[2][2])
        self.assertIn('mode=full -->', body)
        self.assertIn('the base branch changed from main to release', body)

    def test_the_recorded_base_is_the_live_base_the_review_used(self):
        self.github.base = 'release'
        self.first_round(message(verdict='CLEAN'), message(verdict='CLEAN'))
        status, body = self.second_head(message(verdict='CLEAN'))
        self.assertEqual(self.store.concluded(REPO, 7, NEW_HEAD)[0][2], 'release')
        self.assertIn('mode=delta -->', body)

    def test_empty_delta_rechecks_the_live_pr_before_publishing(self):
        def close():
            self.github.state = 'closed'

        def retarget():
            self.github.base = 'release'
        for guid, head, change, expected in (('d-2', NEW_HEAD, close, 'superseded'),
                                             ('d-3', THIRD_HEAD, retarget, None)):
            with self.subTest(expected):
                if guid == 'd-2':
                    self.first_round(message(verdict='CLEAN'), message(verdict='CLEAN'))
                self.github.state, self.github.base = 'open', 'main'
                self.git.delta_files, self.git.during_delta = [], change
                self.github.heads[7], self.git.head = head, head
                self.deliver(pull_payload(head=head), guid=guid)
                _, status = self.run_next()
                self.assertEqual(status, expected)
                self.assertEqual(len(self.github.posted), 1)


class ConfidenceTests(Rounds):
    def review_once(self, msg):
        self.codex.messages = [msg, message(verdict='CLEAN')]
        self.deliver(pull_payload())
        _, status = self.run_next()
        return status, self.github.posted[-1]

    def test_49_is_not_posted(self):
        status, post = self.review_once(message(finding('P1', 'Weak suspicion', 'src/app.py', 3, 49)))
        self.assertEqual(status, 'clean')
        self.assertNotIn('Weak suspicion', post['body'])
        self.assertEqual(post['comments'], [])
        self.assertTrue(post['body'].endswith('另有 1 条置信度低于 50 的发现未提交。'))

    def test_50_is_posted_with_its_confidence(self):
        self.github.files = [{'filename': 'src/app.py', 'patch': '@@ -1,4 +1,5 @@\n a\n b\n+c\n d\n e'}]
        status, post = self.review_once(message(finding('P2', 'Real defect', 'src/app.py', 3, 50)))
        self.assertEqual(status, 'findings')
        self.assertIn('**[P2] Real defect** — `src/app.py:3` · 置信度 50', post['body'])
        self.assertNotIn('Confidence: 50', post['body'])
        self.assertIn('置信度 50', post['comments'][0]['body'])
        self.assertNotIn('置信度低于', post['body'])

    def test_missing_confidence_is_not_posted(self):
        status, post = self.review_once(message(finding('P0', 'Unscored', 'src/app.py', 3)))
        self.assertEqual(status, 'clean')
        self.assertNotIn('Unscored', post['body'])
        self.assertTrue(post['body'].endswith('另有 1 条置信度低于 50 的发现未提交。'))
        self.assertIn('Unscored', (self.root / 'output' / '1.log').read_text())

    def test_all_below_50_is_clean(self):
        status, post = self.review_once(message(finding('P1', 'Guess one', 'src/a.py', 3, 10),
                                                finding('P2', 'Guess two', 'src/b.py', 4, 49),
                                                finding('P3', 'Guess three', 'src/c.py', 5)))
        self.assertEqual(status, 'clean')
        self.assertIn(f'head={HEAD} verdict=clean effort=xhigh tests=read-only mode=full -->', post['body'])
        self.assertTrue(post['body'].endswith('另有 3 条置信度低于 50 的发现未提交。'))

    def test_all_below_50_still_needs_evidence_of_reading(self):
        self.codex.events = cr.json.dumps({'type': 'turn.completed'}) + '\n'
        status, _ = self.review_once(message(finding('P1', 'Guess', 'src/a.py', 3, 10)))
        self.assertEqual(status, 'failed')

    def test_floor_is_compared_before_rounding(self):
        for line, posted in (('Confidence: 49.6', False), ('confidence_score: 0.496', False),
                             ('Confidence: 50.0', True), ('confidence_score: 0.5', True)):
            with self.subTest(line):
                self.github.posted.clear()
                self.github.markers.clear()
                self.store.db.execute('DELETE FROM jobs')
                self.store.db.execute('DELETE FROM deliveries')
                msg = message(finding('P2', 'Edge', 'src/app.py', 3, body=f'Off by one.\n  {line}'))
                status, post = self.review_once(msg)
                self.assertEqual(status, 'findings' if posted else 'clean')
                self.assertEqual('Edge' in post['body'], posted)
                if posted:
                    self.assertIn('· 置信度 50', post['body'])

    def test_confidence_scales(self):
        self.assertEqual(cr.confidence('Confidence: 50'), 50)
        self.assertEqual(cr.confidence('**Confidence:** 49'), 49)
        self.assertEqual(cr.confidence('confidence_score: 0.8'), 80)
        self.assertEqual(cr.confidence('Confidence: 49.6'), cr.Decimal('49.6'))
        self.assertLess(cr.confidence('confidence_score: 0.496'), cr.CONFIDENCE_FLOOR)
        self.assertIsNone(cr.confidence('confidence_score: 1.5'))
        self.assertIsNone(cr.confidence('confidence_score: 80'))
        self.assertIsNone(cr.confidence('Confidence: 101'))
        self.assertIsNone(cr.confidence('no score here'))


class MarkerTests(unittest.TestCase):
    def test_marker_with_mode_parses_and_old_markers_still_parse(self):
        for marker in (f'<!-- remote-codex-review head={HEAD} verdict=clean effort=xhigh tests=read-only mode=full -->',
                       f'<!-- remote-codex-review head={HEAD} verdict=findings effort=high tests=read-only -->',
                       f'<!-- remote-codex-review head={HEAD} verdict=failed -->'):
            with self.subTest(marker):
                self.assertEqual(cr.MARKER_RE.search(marker).group(1), HEAD)


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
               GIT_COMMITTER_EMAIL='t@t', GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_SYSTEM='/dev/null')
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


class DeltaTreeTests(unittest.TestCase):
    """Real git: the delta tree drops base-branch changes and keeps the conflict resolution."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src, self.git = root / 'src', cr.Git(root / 'state')
        self.src.mkdir()
        git(self.src, 'init', '-q', '-b', 'main')
        for name, text in (('a.txt', 'a\n'), ('b.txt', 'base\n'), ('c.txt', 'base\n')):
            (self.src / name).write_text(text)
        git(self.src, 'add', '-A')
        git(self.src, 'commit', '-qm', 'A')
        git(self.src, 'switch', '-qc', 'pr')
        (self.src / 'c.txt').write_text('pr\n')
        git(self.src, 'commit', '-qam', 'P')
        self.prev = git(self.src, 'rev-parse', 'HEAD')
        git(self.src, 'switch', '-q', 'main')
        (self.src / 'b.txt').write_text('main\n')
        (self.src / 'c.txt').write_text('main\n')
        git(self.src, 'commit', '-qam', 'M')
        git(self.src, 'switch', '-q', 'pr')
        subprocess.run(['git', '-C', str(self.src), 'merge', '-q', 'main'], capture_output=True,
                       env=dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
                                GIT_COMMITTER_EMAIL='t@t'))
        (self.src / 'c.txt').write_text('resolved\n')
        git(self.src, 'commit', '-qam', 'merge main')
        (self.src / 'd.txt').write_text('fix\n')
        git(self.src, 'add', '-A')
        git(self.src, 'commit', '-qm', 'fix')
        self.head = git(self.src, 'rev-parse', 'HEAD')
        mirror = self.git.mirror(REPO)
        mirror.parent.mkdir(parents=True)
        git(root, 'init', '-q', '--bare', str(mirror))
        git(self.src, 'push', '-q', str(mirror), 'main:refs/remotes/origin/main', 'pr:refs/codex/pr/7')

    def tearDown(self):
        self.tmp.cleanup()

    def test_delta_excludes_base_changes_and_keeps_the_conflict_resolution(self):
        tree, why = self.git.delta_tree(REPO, 'main', self.prev, self.head, self.git.work / '1' / 'delta-base')
        self.assertIsNotNone(tree, why)
        self.assertIn('conflicts', why)
        self.assertEqual(self.git.diff_files(REPO, tree, self.head), ['c.txt', 'd.txt'])
        self.assertFalse((self.git.work / '1' / 'delta-base').exists())

    def test_modify_delete_conflict_falls_back_to_a_full_round(self):
        git(self.src, 'switch', '-q', '-c', 'pr2', self.prev)
        (self.src / 'a.txt').write_text('a changed on the branch\n')
        git(self.src, 'commit', '-qam', 'modify a')
        prev = git(self.src, 'rev-parse', 'HEAD')
        git(self.src, 'switch', '-q', 'main')
        git(self.src, 'rm', '-q', 'a.txt')
        git(self.src, 'commit', '-qm', 'delete a')
        git(self.src, 'switch', '-q', 'pr2')
        subprocess.run(['git', '-C', str(self.src), 'merge', '-q', 'main'], capture_output=True,
                       env=dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
                                GIT_COMMITTER_EMAIL='t@t'))
        (self.src / 'c.txt').write_text('resolved\n')
        git(self.src, 'add', '-A')
        git(self.src, 'commit', '-qm', 'keep a, resolve c')
        head = git(self.src, 'rev-parse', 'HEAD')
        git(self.src, 'push', '-q', '-f', str(self.git.mirror(REPO)), 'main:refs/remotes/origin/main',
            'pr2:refs/codex/pr/7')
        tree, why = self.git.delta_tree(REPO, 'main', prev, head, self.git.work / '1' / 'delta-base')
        self.assertIsNone(tree)
        self.assertIn('conflicts without text markers (a.txt)', why)
        self.assertFalse((self.git.work / '1' / 'delta-base').exists())

    def test_unmarked_conflict_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'text.txt').write_text('<<<<<<< HEAD\na\n=======\nb\n>>>>>>> main\n')
            Path(tmp, 'bin.dat').write_bytes(b'\x00ours')
            listing = ''.join(f'{mode} {"0" * 40} {stage}\t{path}\0' for mode, stage, path in (
                ('100644', 1, 'text.txt'), ('100644', 2, 'text.txt'), ('100644', 3, 'text.txt'),
                ('100644', 1, 'bin.dat'), ('100644', 2, 'bin.dat'), ('100644', 3, 'bin.dat'),
                ('120000', 2, 'link'), ('120000', 3, 'link'), ('100644', 1, 'gone.txt'), ('100644', 2, 'gone.txt')))
            self.assertEqual(cr.unmarked_conflicts(listing, tmp), ['bin.dat', 'gone.txt', 'link'])

    def test_force_pushed_head_has_no_delta(self):
        git(self.src, 'switch', '-q', '-c', 'rewrite', 'main')
        (self.src / 'e.txt').write_text('e\n')
        git(self.src, 'add', '-A')
        git(self.src, 'commit', '-qm', 'rewrite')
        rewritten = git(self.src, 'rev-parse', 'HEAD')
        git(self.src, 'push', '-q', '-f', str(self.git.mirror(REPO)), 'rewrite:refs/codex/pr/7')
        tree, why = self.git.delta_tree(REPO, 'main', self.prev, rewritten, self.git.work / '1' / 'delta-base')
        self.assertIsNone(tree)
        self.assertIn('not an ancestor', why)


if __name__ == '__main__':
    unittest.main()
