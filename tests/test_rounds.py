#!/usr/bin/env python3
"""First-round, re-review and confidence-display tests; codex and GitHub are fakes, the delta tree uses real git."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_review as cr  # noqa: E402
from test_codex_review import HEAD, NEW_HEAD, REPO, WORKTREE, Harness, events_jsonl, pull_payload  # noqa: E402
from test_codex_review import file_diff as cr_file_diff  # noqa: E402

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
    def test_first_round_is_one_xhigh_review_covering_both_lenses(self):
        self.git.files = ['src/app.py', 'src/log.py']
        self.github.files = [{'filename': 'src/app.py', 'patch': '@@ -8,6 +8,6 @@\n a\n b\n+c\n+d\n+e\n f'}]
        body = self.first_round(message(finding('P2', 'Retry never stops', 'src/app.py', '10-12', 80),
                                        finding('P3', 'Log is noisy', 'src/log.py', 3, 70)))
        self.assertEqual([call[3] for call in self.codex.calls], ['xhigh'])
        prompt = self.codex.calls[0][2]
        self.assertIn(cr.FIRST_ROUND_LENS, prompt)
        self.assertIn('git diff origin/main...HEAD', prompt)
        self.assertIn('git diff --name-only --no-renames origin/main...HEAD', prompt)
        self.assertIn('- src/app.py\n- src/log.py', prompt)
        self.assertIn(cr.EXHAUSTIVE, prompt)
        self.assertIn('Confidence: <0-100>', prompt)
        self.assertIn(f'head={HEAD} verdict=findings effort=xhigh tests=read-only mode=full -->', body)
        self.assertIn('首轮全面审查：2 个文件，effort=xhigh。', body)
        self.assertIn('**[P2] Retry never stops** — `src/app.py:10-12` · 置信度 80', body)
        self.assertIn('**[P3] Log is noisy** — `src/log.py:3` · 置信度 70', body)
        self.assertEqual(self.github.posted[-1]['comments'][0]['path'], 'src/app.py')


class ReadCoverageTests(Rounds):
    """Every changed file's diff must appear in the output of a successful command of at most READ_LIMIT bytes."""

    def setUp(self):
        super().setUp()
        self.git.files = ['src/app.py', 'src/b.py']

    def events(self, outputs, exit_codes=None):
        text = events_jsonl(message(verdict='CLEAN'), outputs=outputs)
        for index, code in enumerate(exit_codes or []):
            text = text.replace(f'"id": "item_{index}"', f'"id": "item_{index}", "rc": {code}')
            text = text.replace(f'"id": "item_{index}", "rc": {code}', f'"id": "item_{index}"').replace(
                f'"item_{index}", "type": "command_execution", "command": "/usr/bin/bash -lc \'git diff\'", '
                f'"aggregated_output": {cr.json.dumps(outputs[index])}, "exit_code": 0',
                f'"item_{index}", "type": "command_execution", "command": "/usr/bin/bash -lc \'git diff\'", '
                f'"aggregated_output": {cr.json.dumps(outputs[index])}, "exit_code": {code}')
        return text

    def test_all_files_read_is_clean(self):
        self.deliver(pull_payload())
        _, status = self.run_next()
        self.assertEqual(status, 'clean')
        self.assertEqual(self.git.ranges, [[f'refs/remotes/origin/main...{HEAD}']])
        self.assertNotIn('阅读不完整', self.github.posted[-1]['body'])

    def test_one_unread_file_after_three_reviews_is_failed_and_listed(self):
        self.assertEqual(cr.READ_ATTEMPTS, 3)
        self.codex.read = ['src/app.py']
        self.deliver(pull_payload())
        _, status = self.run_next()
        body = self.github.posted[-1]['body']
        self.assertEqual((status, len(self.codex.calls)), ('failed', 3))
        self.assertIn('verdict=failed', body)
        self.assertIn('1 个改动文件（共 2 个）没有在合格的命令输出中读全：`src/b.py`（未读）', body)
        self.assertIn('本 head 审查 3 次（上限 3）；各次未读全的文件数：1、1、1。', body)
        self.assertNotIn('`src/app.py`', body)
        kept = (self.root / 'output' / '1.log').read_text(encoding='utf-8')
        self.assertEqual(kept.count('===== review '), 3)

    def test_a_file_read_in_the_second_review_is_clean(self):
        self.codex.reads = [['src/app.py'], ['src/app.py', 'src/b.py']]
        self.deliver(pull_payload())
        _, status = self.run_next()
        body = self.github.posted[-1]['body']
        self.assertEqual((status, len(self.codex.calls)), ('clean', 2))
        self.assertNotIn('review 2 of at most 3', self.codex.calls[0][2])
        self.assertIn('This is review 2 of at most 3 of this head', self.codex.calls[1][2])
        self.assertIn('when needed:\n- src/b.py', self.codex.calls[1][2])
        self.assertNotIn('- src/app.py\n- src/b.py\n\nThis is review', self.codex.calls[1][2].replace(
            cr.read_rule('origin/main...HEAD', ['src/app.py', 'src/b.py']), ''))
        self.assertIn('本 head 审查 2 次（上限 3）；各次未读全的文件数：1、0。', body)
        self.assertNotIn('阅读不完整', body)
        self.assertIn('verdict=clean', body)

    def test_the_third_review_is_the_last(self):
        self.codex.reads = [[], ['src/app.py'], [], ['src/app.py', 'src/b.py']]
        self.deliver(pull_payload())
        _, status = self.run_next()
        self.assertEqual((status, len(self.codex.calls)), ('failed', 3))
        self.assertIn('各次未读全的文件数：2、1、2。', self.github.posted[-1]['body'])

    def test_partly_read_file_is_failed_as_partial(self):
        partial = cr_file_diff('src/app.py').rsplit('\n', 2)[0] + '\n'
        self.codex.events = self.events([partial, cr_file_diff('src/b.py')])
        self.deliver(pull_payload())
        _, status = self.run_next()
        self.assertEqual(status, 'failed')
        self.assertIn('`src/app.py`（部分）', self.github.posted[-1]['body'])

    def test_findings_with_an_unread_file_are_published_with_a_note(self):
        self.codex.read = ['src/app.py']
        self.codex.messages = [message(finding('P2', 'Retry never stops', 'src/app.py', 10, 80))] * 3
        self.deliver(pull_payload())
        _, status = self.run_next()
        body = self.github.posted[-1]['body']
        self.assertEqual((status, len(self.codex.calls)), ('findings', 3))
        self.assertIn('verdict=findings', body)
        self.assertIn('**阅读不完整**：1 个改动文件（共 2 个）没有在合格的命令输出中读全：`src/b.py`（未读）。', body)
        self.assertIn('本 head 审查 3 次（上限 3）；各次未读全的文件数：1、1、1。', body)
        self.assertIn('Retry never stops', body)
        self.assertTrue((self.root / 'output' / '1.log').exists())

    def test_output_over_20000_bytes_is_not_evidence(self):
        self.assertEqual(cr.READ_LIMIT, 20000)
        padded = cr_file_diff('src/app.py') + '+' + 'x' * 20000 + '\n'
        self.codex.events = self.events([padded, cr_file_diff('src/b.py')])
        self.deliver(pull_payload())
        _, status = self.run_next()
        self.assertEqual(status, 'failed')
        self.assertIn('`src/app.py`（未读）', self.github.posted[-1]['body'])

    def test_output_of_a_failed_command_is_not_evidence(self):
        self.codex.events = self.events([cr_file_diff('src/app.py'), cr_file_diff('src/b.py')], exit_codes=[1])
        self.assertIn('"exit_code": 1', self.codex.events)
        self.deliver(pull_payload())
        _, status = self.run_next()
        self.assertEqual(status, 'failed')
        self.assertIn('`src/app.py`（未读）', self.github.posted[-1]['body'])

    def test_a_diff_read_in_parts_counts(self):
        lines = cr_file_diff('src/app.py').splitlines(keepends=True)
        self.codex.events = self.events([''.join(lines[:3]), ''.join(lines[3:]), cr_file_diff('src/b.py')])
        self.deliver(pull_payload())
        self.assertEqual(self.run_next()[1], 'clean')

    def test_delta_review_requires_only_the_delta_files(self):
        self.first_round(message(verdict='CLEAN'))
        self.git.delta_files = ['src/fix.py']
        status, body = self.second_head(message(verdict='CLEAN'))
        self.assertEqual(status, 'clean')
        self.assertEqual(self.git.ranges[-1], [TREE, NEW_HEAD])
        self.assertIn(f'git diff --name-only --no-renames {TREE} HEAD', self.codex.calls[-1][2])
        self.assertEqual(list(self.git.expected), ['src/fix.py'])
        self.codex.read = []
        calls = len(self.codex.calls)
        status, body = self.second_head(message(verdict='CLEAN'), guid='d-3', head=THIRD_HEAD)
        self.assertEqual((status, len(self.codex.calls) - calls), ('failed', 3))
        self.assertIn('1 个改动文件（共 1 个）没有在合格的命令输出中读全：`src/fix.py`（未读）', body)
        self.assertIn(f'git diff --name-only --no-renames {TREE} HEAD', self.codex.calls[-1][2])
        self.assertIn('when needed:\n- src/fix.py', self.codex.calls[-1][2])


class RealDiffTests(unittest.TestCase):
    """Real git: binary, mode-only, deleted and renamed files have diffs without hunks and must still be shown."""

    def test_file_diffs_cover_every_kind_of_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, store = root / 'src', cr.Git(root / 'state')
            src.mkdir()
            git(src, 'init', '-q', '-b', 'main')
            (src / 'bin.dat').write_bytes(b'\x00\x01\x02')
            (src / 'old.py').write_text('print(1)\n' * 10)
            (src / 'gone.py').write_text('x = 1\n')
            (src / 'run.sh').write_text('echo\n')
            git(src, 'add', '-A')
            git(src, 'commit', '-qm', 'base')
            git(src, 'switch', '-qc', 'pr')
            (src / 'bin.dat').write_bytes(b'\x00\x01\x03')
            git(src, 'mv', 'old.py', 'new.py')
            (src / 'gone.py').unlink()
            (src / 'run.sh').chmod(0o755)
            git(src, 'add', '-A')
            git(src, 'commit', '-qm', 'pr')
            head = git(src, 'rev-parse', 'HEAD')
            mirror = store.mirror(REPO)
            mirror.parent.mkdir(parents=True)
            git(root, 'init', '-q', '--bare', str(mirror))
            git(src, 'push', '-q', str(mirror), 'main:refs/remotes/origin/main', 'pr:refs/codex/pr/7')
            files = store.changed_files(REPO, 'main', head)
            expected = store.file_diffs(REPO, [f'refs/remotes/origin/main...{head}'], files)
        self.assertEqual(sorted(expected), ['bin.dat', 'gone.py', 'new.py', 'old.py', 'run.sh'])
        self.assertIn('Binary files a/bin.dat and b/bin.dat differ', expected['bin.dat'])
        self.assertIn('old mode 100644\nnew mode 100755', expected['run.sh'])
        self.assertIn('deleted file mode', expected['gone.py'])
        self.assertIn('new file mode', expected['new.py'])
        self.assertNotIn('rename', expected['new.py'])
        self.assertFalse(any('@@' in expected[name] for name in ('bin.dat', 'run.sh')))
        shown = cr.read_events(events_jsonl('', outputs=list(expected.values())))
        self.assertEqual(cr.unread_files(expected, shown), [])
        without_binary = cr.read_events(events_jsonl('', outputs=[t for n, t in expected.items() if n != 'bin.dat']))
        self.assertEqual(cr.unread_files(expected, without_binary), [('bin.dat', '未读')])


class DeltaTests(Rounds):
    def test_rereview_gives_codex_only_the_delta(self):
        self.first_round(message(finding('P2', 'Retry never stops', 'src/app.py', 10, 80)))
        self.git.delta_files = ['src/fix.py']
        status, body = self.second_head(message(verdict='CLEAN'))
        self.assertEqual(status, 'clean')
        self.assertEqual(len(self.codex.calls), 2)
        prompt, effort = self.codex.calls[1][2], self.codex.calls[1][3]
        self.assertEqual(self.git.delta_calls, [(HEAD, NEW_HEAD)])
        self.assertIn(f'run `git diff {TREE} HEAD`', prompt)
        self.assertIn('- src/fix.py', prompt)
        self.assertNotIn('origin/main...HEAD', prompt)
        self.assertIn('Retry never stops', prompt)
        self.assertEqual(effort, 'high')
        self.assertIn(f'head={NEW_HEAD} verdict=clean effort=high tests=read-only mode=delta -->', body)
        self.assertIn(f'`{HEAD[:12]}..{NEW_HEAD[:12]}`', body)

    def test_out_of_delta_findings_are_registered_and_do_not_count(self):
        self.first_round(message(verdict='CLEAN'))
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
                    self.first_round(message(verdict='CLEAN'))
                status, body = self.second_head(message(verdict='CLEAN', register=[item]), guid=guid, head=head)
                self.assertEqual(status, 'findings')
                self.assertIn('## 增量外但阻塞的问题', body)
                self.assertIn(item, body)
                self.assertNotIn('## 登记项', body)

    def test_register_heading_inside_a_finding_body_is_not_a_section(self):
        self.first_round(message(verdict='CLEAN'))
        mention = finding('P2', 'Prompt text drifted', 'src/prompt.py', 4, 80,
                          body=f'The prompt still names {cr.REGISTER_HEADING} twice.')
        status, body = self.second_head(message(mention))
        self.assertEqual(status, 'findings')
        self.assertIn('**[P2] Prompt text drifted** — `src/prompt.py:4` · 置信度 80', body)

    def test_decorated_standalone_register_heading_is_a_section(self):
        self.first_round(message(verdict='CLEAN'))
        item = '- [P2] Stale comment — src/old.py:4: names a removed flag. Confidence: 80'
        msg = message(verdict='FINDINGS', register=[item]).replace(cr.REGISTER_HEADING, f'**{cr.REGISTER_HEADING}**')
        status, body = self.second_head(msg)
        self.assertEqual(status, 'clean')
        self.assertIn('## 登记项', body)

    def test_force_push_falls_back_to_a_full_first_round(self):
        self.first_round(message(verdict='CLEAN'))
        self.git.delta = (None, f'last reviewed head {HEAD[:12]} is not an ancestor of {NEW_HEAD[:12]}')
        status, body = self.second_head(message(verdict='CLEAN'))
        self.assertEqual(status, 'clean')
        self.assertEqual([call[3] for call in self.codex.calls], ['xhigh'] * 2)
        self.assertIn('git diff origin/main...HEAD', self.codex.calls[1][2])
        self.assertIn('mode=full -->', body)
        self.assertIn('is not an ancestor', body)

    def test_previous_review_marked_invalid_is_not_a_delta_base(self):
        self.first_round(message(verdict='CLEAN'))
        first = self.github.posted[0]
        first['body'] = first['body'].replace('verdict=clean', 'verdict=invalid', 1)
        status, body = self.second_head(message(verdict='CLEAN'))
        self.assertEqual(getattr(self.git, 'delta_calls', []), [])
        self.assertIn('mode=full -->', body)

    def test_empty_delta_after_a_clean_review_is_clean_without_codex(self):
        self.first_round(message(verdict='CLEAN'))
        self.git.delta_files = []
        status, body = self.second_head(message(verdict='CLEAN'))
        self.assertEqual((status, len(self.codex.calls)), ('clean', 1))
        self.assertIn(f'head={NEW_HEAD} verdict=clean effort=none tests=read-only mode=delta -->', body)
        self.assertNotIn('codex was told', body)

    def assert_full_after_findings(self, previous):
        self.first_round(message(previous))
        self.assertIn('verdict=findings', self.github.posted[0]['body'])
        self.git.delta_files = []
        self.codex.messages = [message(verdict='CLEAN')]
        status, body = self.second_head(message(verdict='CLEAN'))
        self.assertEqual(status, 'clean')
        self.assertEqual([call[3] for call in self.codex.calls], ['xhigh'] * 2)
        self.assertIn(f'head={NEW_HEAD} verdict=clean effort=xhigh tests=read-only mode=full -->', body)
        self.assertIn('was findings', body)

    def test_empty_delta_after_a_p1_review_is_reviewed_in_full(self):
        self.assert_full_after_findings(finding('P1', 'Order is sent twice', 'src/order.py', 7, 90))

    def test_empty_delta_after_a_p3_only_review_is_reviewed_in_full(self):
        self.assert_full_after_findings(finding('P3', 'Typo in log', 'src/log.py', 2, 80))

    def test_empty_delta_after_a_truncated_clean_review_is_reviewed_in_full(self):
        self.first_round(message(verdict='CLEAN'))
        self.github.posted[0]['body'] += '\n\n[truncated]'
        self.git.delta_files = []
        status, body = self.second_head(message(verdict='CLEAN'))
        self.assertEqual([call[3] for call in self.codex.calls], ['xhigh'] * 2)
        self.assertIn('mode=full -->', body)
        self.assertIn('was clean and truncated', body)

    def test_base_change_falls_back_to_a_full_round(self):
        self.first_round(message(verdict='CLEAN'))
        self.github.base = 'release'
        status, body = self.second_head(message(verdict='CLEAN'))
        self.assertEqual(status, 'clean')
        self.assertEqual(getattr(self.git, 'delta_calls', []), [])
        self.assertEqual([call[3] for call in self.codex.calls], ['xhigh'] * 2)
        self.assertIn('git diff origin/release...HEAD', self.codex.calls[1][2])
        self.assertIn('mode=full -->', body)
        self.assertIn('the base branch changed from main to release', body)

    def test_the_recorded_base_is_the_live_base_the_review_used(self):
        self.github.base = 'release'
        self.first_round(message(verdict='CLEAN'))
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
                    self.first_round(message(verdict='CLEAN'))
                self.github.state, self.github.base = 'open', 'main'
                self.git.delta_files, self.git.during_delta = [], change
                self.github.heads[7], self.git.head = head, head
                self.deliver(pull_payload(head=head), guid=guid)
                _, status = self.run_next()
                self.assertEqual(status, expected)
                self.assertEqual(len(self.github.posted), 1)


class ConfidenceTests(Rounds):
    def review_once(self, msg):
        self.codex.messages = [msg]
        self.deliver(pull_payload())
        _, status = self.run_next()
        return status, self.github.posted[-1]

    def test_a_low_score_is_posted_and_counted(self):
        self.github.files = [{'filename': 'src/app.py', 'patch': '@@ -1,4 +1,5 @@\n a\n b\n+c\n d\n e'}]
        status, post = self.review_once(message(finding('P1', 'Weak suspicion', 'src/app.py', 3, 49)))
        self.assertEqual(status, 'findings')
        self.assertIn(f'head={HEAD} verdict=findings effort=xhigh tests=read-only mode=full -->', post['body'])
        self.assertIn('**[P1] Weak suspicion** — `src/app.py:3` · 置信度 49', post['body'])
        self.assertIn('置信度 49', post['comments'][0]['body'])
        self.assertNotIn('Confidence: 49', post['body'])
        self.assertNotIn('未提交', post['body'])

    def test_missing_confidence_is_posted_as_not_given(self):
        status, post = self.review_once(message(finding('P0', 'Unscored', 'src/app.py', 3)))
        self.assertEqual(status, 'findings')
        self.assertIn('**[P0] Unscored** — `src/app.py:3` · 置信度未给出', post['body'])

    def test_every_score_is_shown_rounded_only_in_the_text(self):
        for line, text in (('Confidence: 49.6', '置信度 50'), ('confidence_score: 0.496', '置信度 50'),
                           ('Confidence: 50.0', '置信度 50'), ('confidence_score: 0.8', '置信度 80')):
            with self.subTest(line):
                self.github.posted.clear()
                self.github.markers.clear()
                self.store.db.execute('DELETE FROM jobs')
                self.store.db.execute('DELETE FROM deliveries')
                msg = message(finding('P2', 'Edge', 'src/app.py', 3, body=f'Off by one.\n  {line}'))
                status, post = self.review_once(msg)
                self.assertEqual(status, 'findings')
                self.assertIn(f'**[P2] Edge** — `src/app.py:3` · {text}', post['body'])

    def test_spaced_backticked_path_is_parsed(self):
        p1 = '- [P1] Order is sent twice — `src/order handler.py:12-14`\n  No idempotency key.\n  Confidence: 90\n'
        self.github.files = [{'filename': 'src/order handler.py', 'patch': '@@ -10,6 +10,6 @@\n a\n b\n+c\n+d\n+e\n f'}]
        status, post = self.review_once(message(p1, finding('P3', 'Weak guess', 'src/a.py', 3, 20)))
        self.assertEqual(status, 'findings')
        self.assertIn('**[P1] Order is sent twice** — `src/order handler.py:12-14` · 置信度 90', post['body'])
        self.assertIn('**[P3] Weak guess** — `src/a.py:3` · 置信度 20', post['body'])
        self.assertEqual(post['comments'][0]['path'], 'src/order handler.py')

    def test_unparseable_header_is_findings_with_the_raw_text(self):
        broken = '- [P1] Order is sent twice — src/order handler.py\n  No line number.\n  Confidence: 90\n'
        status, post = self.review_once(message(broken, finding('P3', 'Weak guess', 'src/a.py', 3, 20)))
        self.assertEqual(status, 'findings')
        self.assertIn('## 未能解析的发现（按发现处理）\n\n- [P1] Order is sent twice — src/order handler.py\n'
                      '  No line number.\n  Confidence: 90', post['body'])
        self.assertIn('**[P3] Weak guess** — `src/a.py:3` · 置信度 20', post['body'])
        self.assertEqual(post['body'].count('Order is sent twice'), 1)

    def test_register_line_without_a_priority_tag_blocks(self):
        harness = DeltaTests('test_out_of_delta_findings_are_registered_and_do_not_count')
        harness.setUp()
        try:
            harness.first_round(message(verdict='CLEAN'))
            item = '- Retry bound is off by one — src/hook.py:9: the loop runs once more. Confidence: 30'
            status, body = harness.second_head(message(verdict='CLEAN', register=[item]))
        finally:
            harness.tearDown()
        self.assertEqual(status, 'findings')
        self.assertIn('## 增量外但阻塞的问题', body)

    def test_confidence_scales(self):
        self.assertEqual(cr.confidence('Confidence: 50'), 50)
        self.assertEqual(cr.confidence('**Confidence:** 49'), 49)
        self.assertEqual(cr.confidence('confidence_score: 0.8'), 80)
        self.assertEqual(cr.confidence('Confidence: 49.6'), cr.Decimal('49.6'))
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


class ShutdownTests(Rounds):
    def test_shutdown_during_the_review_requeues_it_without_a_post(self):
        invoked = []
        self.codex.before_proceed = lambda: invoked.append(1)
        self.codex.during = self.reviewer.stop.set
        self.deliver(pull_payload())
        job, status = self.run_next()
        self.assertEqual((status, len(invoked), len(self.codex.calls), self.github.posted), (None, 1, 1, []))
        self.assertEqual(self.store.status(job.id), 'queued')

    def test_proceed_refuses_the_review_once_shutdown_began(self):
        self.codex.before_proceed = self.reviewer.stop.set
        self.deliver(pull_payload())
        job, status = self.run_next()
        self.assertEqual((status, self.codex.calls, self.github.posted), (None, [], []))
        self.assertEqual(self.store.status(job.id), 'queued')
