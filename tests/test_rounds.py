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
from test_codex_review import file_diff as cr_file_diff, session_item  # noqa: E402
import json  # noqa: E402

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
        self.assertEqual(self.codex.manifests, ['src/app.py\nsrc/log.py\n'])
        self.assertIn('listed one per line in `', prompt)
        self.assertIn(cr.EXHAUSTIVE, prompt)
        self.assertIn('Confidence: <0-100>', prompt)
        self.assertIn(f'head={HEAD} verdict=findings effort=xhigh tests=read-only mode=full -->', body)
        self.assertIn('首轮全面审查：2 个文件，effort=xhigh。', body)
        self.assertIn('**[P2] Retry never stops** — `src/app.py:10-12` · 置信度 80', body)
        self.assertIn('**[P3] Log is noisy** — `src/log.py:3` · 置信度 70', body)
        self.assertEqual(self.github.posted[-1]['comments'][0]['path'], 'src/app.py')


class ReadCoverageTests(Rounds):
    """Every changed file's diff must appear, untruncated, in what the model saw from successful commands."""

    def setUp(self):
        super().setUp()
        self.git.files = ['src/app.py', 'src/b.py']

    def test_all_files_read_is_clean(self):
        self.deliver(pull_payload())
        _, status = self.run_next()
        self.assertEqual(status, 'clean')
        self.assertEqual(self.git.ranges, [[f'refs/remotes/origin/main...{HEAD}']])
        self.assertEqual(self.git.diff_worktrees, [self.root / 'work' / '1' / 'tree'])
        self.assertEqual(self.sessions.calls[0][0], 't-1')
        self.assertNotIn('阅读不完整', self.github.posted[-1]['body'])

    def test_one_unread_file_after_three_reviews_is_failed_and_listed(self):
        self.assertEqual(cr.READ_ATTEMPTS, 3)
        self.codex.read = ['src/app.py']
        self.deliver(pull_payload())
        _, status = self.run_next()
        body = self.github.posted[-1]['body']
        self.assertEqual((status, len(self.codex.calls)), ('failed', 3))
        self.assertIn('verdict=failed', body)
        self.assertIn('1 个改动文件（共 2 个）没有在模型看到的合格命令输出中读全：`src/b.py`（未读）', body)
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
        self.assertNotIn('This is review', self.codex.calls[0][2])
        self.assertIn('This is review 2 of at most 3 of this head', self.codex.calls[1][2])
        self.assertIn('of 1 changed file(s), listed one per line in `', self.codex.calls[1][2])
        self.assertEqual(self.codex.unread_lists, [None, 'src/b.py\n'])
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
        self.codex.outputs = [cr_file_diff('src/app.py').rsplit('\n', 2)[0] + '\n', cr_file_diff('src/b.py')]
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
        self.assertIn('**阅读不完整**：1 个改动文件（共 2 个）没有在模型看到的合格命令输出中读全：`src/b.py`（未读）。', body)
        self.assertIn('本 head 审查 3 次（上限 3）；各次未读全的文件数：1、1、1。', body)
        self.assertIn('Retry never stops', body)
        self.assertTrue((self.root / 'output' / '1.log').exists())

    def test_output_truncated_for_the_model_is_not_evidence(self):
        self.codex.truncate = True
        self.deliver(pull_payload())
        _, status = self.run_next()
        self.assertEqual((status, len(self.codex.calls)), ('failed', 3))
        self.assertIn('`src/app.py`（未读）、`src/b.py`（未读）', self.github.posted[-1]['body'])

    def test_unreadable_session_record_is_not_evidence_and_is_explained(self):
        self.sessions.error = 'no session record for thread t-1 under /nonexistent/sessions'
        self.deliver(pull_payload())
        _, status = self.run_next()
        body = self.github.posted[-1]['body']
        self.assertEqual((status, len(self.codex.calls)), ('failed', 3))
        self.assertIn('2 个改动文件（共 2 个）没有在模型看到的合格命令输出中读全：`src/app.py`（未读）、`src/b.py`（未读）'
                      '（codex 会话记录不可用，一律不算证据：no session record for thread t-1 under /nonexistent/sessions）',
                      body)

    def test_output_of_a_failed_command_is_not_evidence(self):
        self.codex.exit_code = 1
        self.deliver(pull_payload())
        _, status = self.run_next()
        self.assertEqual(status, 'failed')
        self.assertIn('`src/app.py`（未读）', self.github.posted[-1]['body'])

    def test_a_diff_read_in_parts_counts(self):
        lines = cr_file_diff('src/app.py').splitlines(keepends=True)
        self.codex.outputs = [''.join(lines[:3]), ''.join(lines[3:]), cr_file_diff('src/b.py')]
        self.deliver(pull_payload())
        self.assertEqual(self.run_next()[1], 'clean')

    def test_a_line_longer_than_the_fold_width_counts_when_every_segment_was_seen(self):
        self.assertEqual(cr.FOLD_WIDTH, 4000)
        long_line = '+' + 'é' * 6000
        expected = {'big.min.js': f'diff --git a/big.min.js b/big.min.js\n@@ -1 +1 @@\n{long_line}\n'}
        folded = cr.fold(long_line)
        self.assertEqual(len(folded), 4)
        self.assertIn('\ufffd', folded[0])
        shown = 'diff --git a/big.min.js b/big.min.js\n@@ -1 +1 @@\n' + '\n'.join(folded) + '\n'
        lines = cr.evidence_lines([session_item(shown)], cr.read_events(events_jsonl('', outputs=[shown])))
        self.assertEqual(cr.unread_files(expected, lines), [])
        self.assertEqual(cr.unread_files(expected, [lines[0][:3] + lines[0][4:]]), [('big.min.js', '部分')])

    def test_delta_review_requires_only_the_delta_files(self):
        self.first_round(message(verdict='CLEAN'))
        self.git.delta_files = ['src/fix.py']
        status, body = self.second_head(message(verdict='CLEAN'))
        self.assertEqual(status, 'clean')
        self.assertEqual(self.git.ranges[-1], [TREE, NEW_HEAD])
        self.assertIn(f'git diff --name-only --no-renames {TREE} HEAD', self.codex.calls[-1][2])
        self.assertEqual(self.codex.manifests[-1], 'src/fix.py\n')
        self.assertEqual(list(self.git.expected), ['src/fix.py'])
        self.codex.read = []
        calls = len(self.codex.calls)
        status, body = self.second_head(message(verdict='CLEAN'), guid='d-3', head=THIRD_HEAD)
        self.assertEqual((status, len(self.codex.calls) - calls), ('failed', 3))
        self.assertIn('1 个改动文件（共 1 个）没有在模型看到的合格命令输出中读全：`src/fix.py`（未读）', body)
        self.assertIn(f'git diff --name-only --no-renames {TREE} HEAD', self.codex.calls[-1][2])
        self.assertEqual(self.codex.unread_lists[-1], 'src/fix.py\n')


class SessionLogTests(unittest.TestCase):
    """SessionLog reads `$CODEX_HOME/sessions/**/rollout-*.jsonl`; visible_texts decodes its records."""

    def rollout(self, home, name, meta, items):
        path = home / 'sessions' / '2026' / '10' / '14' / f'rollout-2026-10-14T00-00-00-{name}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({'timestamp': 't', 'type': 'session_meta', 'payload': meta})]
        lines += [json.dumps({'timestamp': 't', 'type': 'event_msg' if item['type'] == 'item_completed' else 'response_item',
                              'payload': item}) for item in items]
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    def test_outputs_of_the_thread_and_its_child_are_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.rollout(home, 'parent', {'id': 'thread-1', 'cwd': '/w'},
                         [{'type': 'custom_tool_call_output', 'call_id': 'c1', 'output': session_item('one\n')},
                          {'type': 'item_completed', 'item': {'type': 'CommandExecution', 'process_id': '7',
                                                               'exit_code': 0, 'aggregated_output': 'one\n'}},
                          {'type': 'item_completed', 'item': {'type': 'AgentMessage', 'id': 'm1'}}])
            self.rollout(home, 'child', {'id': 'thread-2', 'parent_thread_id': 'thread-1'},
                         [{'type': 'function_call_output', 'call_id': 'c2', 'output': 'two\n'},
                          {'type': 'message', 'role': 'assistant', 'content': []}])
            self.rollout(home, 'other', {'id': 'thread-9'},
                         [{'type': 'custom_tool_call_output', 'call_id': 'c3', 'output': session_item('nine\n')}])
            outputs, error = cr.SessionLog(home).outputs('thread-1', 0)
            self.assertIsNone(error)
            self.assertEqual(sorted(cr.visible_texts(outputs)), ['one\n', 'two\n'])
            self.assertEqual(outputs.count(cr.ROLLOUT_BOUNDARY), 2)
            self.assertEqual([o for o in outputs if isinstance(o, dict) and o.get('type') != 'rollout_boundary'],
                             [{'type': 'CommandExecution', 'process_id': '7', 'exit_code': 0, 'aggregated_output': 'one\n'}])
            self.assertEqual(cr.SessionLog(home).outputs('thread-7', 0), ([], f'no session record for thread thread-7 under {home / "sessions"}'))
            self.assertEqual(cr.SessionLog(home).outputs('', 0), ([], 'the codex events carry no thread id'))
            self.assertEqual(cr.SessionLog(home / 'missing').outputs('thread-1', 0),
                             ([], f'no sessions directory at {home / "missing" / "sessions"}'))

    def test_visible_texts_reject_truncated_failed_and_unreadable_items(self):
        self.assertEqual(cr.visible_texts([session_item('a\nb\n')]), ['a\nb\n'])
        self.assertEqual(cr.visible_texts([session_item('a\nb\n', truncated=True)]), [])
        self.assertEqual(cr.visible_texts([session_item('a\nb\n', exit_code=1)]), [])
        self.assertEqual(cr.visible_texts(['Warning: truncated output (original token count: 5)\na\n']), [])
        self.assertEqual(cr.visible_texts(['plain shell output\n']), ['plain shell output\n'])
        broken = session_item('a\n')
        broken[1]['text'] = broken[1]['text'][:-5]
        self.assertEqual(cr.visible_texts([broken]), [])
        self.assertEqual(cr.visible_texts([[{'type': 'input_text', 'text': 'tool listing without a chunk'}]]), [])
        self.assertEqual(cr.visible_texts([{'unexpected': 'shape'}]), [])
        self.assertEqual(cr.visible_texts([None]), [])
        two = session_item('a\n')
        two[1]['text'] += '\n' + session_item('b\n')[1]['text']
        self.assertEqual(cr.visible_texts([two]), ['a\n', 'b\n'])

    def test_evidence_needs_both_the_model_view_and_a_successful_command(self):
        events = cr.read_events(events_jsonl('', outputs=['x\ny\n']))
        self.assertEqual(cr.evidence_lines([session_item('x\nz\n')], events), [['x', None, '']])
        self.assertEqual(cr.evidence_lines([session_item('x\ny\n')], cr.read_events(events_jsonl('', outputs=['x\ny\n'], exit_code=1))), [[None, None, None]])


class CoverageEvidenceTests(unittest.TestCase):
    """Coverage is per file and per occurrence; truncation is read from the tool wrapper; streamed chunks count."""

    WARNING = 'Warning: truncated output (original token count: 12)\nTotal output lines: 3\n\n'

    def unread(self, expected, items, printed):
        return cr.unread_files(expected, cr.evidence_lines(items, cr.read_events(events_jsonl('', outputs=printed))))

    def diff(self, path, *rows):
        return f'diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1,{len(rows)} @@\n' + ''.join(
            f'+{row}\n' for row in rows)

    def test_a_diff_quoting_the_truncation_warning_read_in_full_counts(self):
        text = self.diff('codex_review.py', "TRUNCATION_RE = re.compile(r'Warning: truncated output|x')",
                         "    warning = 'Warning: truncated output (original token count: 9999)\\nTotal output lines: 1'",
                         'Warning: truncated output (original token count: 12)')
        self.assertEqual(self.unread({'codex_review.py': text}, [session_item(text)], [text]), [])

    def test_a_chunk_whose_output_the_tool_truncated_counts_only_outside_the_cut(self):
        text = self.diff('a.py', 'one', 'two')
        cut = self.WARNING + text.replace('+one\n+two', '+o…3 tokens truncated…wo')
        self.assertEqual(self.unread({'a.py': text}, [session_item(cut)], [text]), [('a.py', '部分')])
        self.assertEqual(self.unread({'a.py': text}, [session_item(self.WARNING + text)], [text]), [])
        self.assertEqual(self.unread({'a.py': text}, [session_item(text, truncated=True)], [text]), [('a.py', '未读')])
        bare = 'Warning: truncated output (original token count: 12)\n' + text
        self.assertEqual(self.unread({'a.py': text}, [session_item(bare)], [text]), [('a.py', '未读')])

    def test_repeated_rows_need_every_occurrence(self):
        text = self.diff('rows.csv', *['same'] * 100)
        shown = self.diff('rows.csv', 'same').replace('+1,1', '+1,100')
        self.assertEqual(self.unread({'rows.csv': text}, [session_item(shown)], [shown]), [('rows.csv', '部分')])

    def test_lines_read_in_another_file_do_not_cover_this_one(self):
        a, b = self.diff('a.py', 'shared', 'tail'), self.diff('b.py', 'shared', 'tail')
        header = ''.join(b.splitlines(keepends=True)[:4])
        items = [session_item(a), session_item(header), session_item('+tail\n')]
        self.assertEqual(self.unread({'a.py': a, 'b.py': b}, items, [a, header, '+tail\n']), [('b.py', '部分')])

    def test_pages_across_outputs_with_repeated_rows_and_a_folded_line_count(self):
        long_row = 'x' * 9000
        text = self.diff('data.txt', *['same'] * 6, long_row, *['same'] * 3)
        lines = text.splitlines(keepends=True)
        pages = [''.join(lines[:7]), ''.join(lines[7:10]),
                 '\n'.join(cr.fold('+' + long_row)) + '\n', ''.join(lines[11:])]
        self.assertEqual(self.unread({'data.txt': text}, [session_item(page) for page in pages], pages), [])

    def test_a_streamed_command_counts_once_it_exits_zero(self):
        text = self.diff('s.py', 'first row', 'second row')
        cut = text.index('second') + 3
        items = [session_item(text[:cut], exit_code=None, session_id=41),
                 {'type': 'CommandExecution', 'process_id': '41', 'exit_code': 0, 'aggregated_output': text},
                 session_item(text[cut:])]
        self.assertEqual(self.unread({'s.py': text}, items, [text]), [])
        failed = [items[0], {**items[1], 'exit_code': 1}, items[2]]
        self.assertEqual(self.unread({'s.py': text}, failed, [text]), [('s.py', '未读')])


class RolloutEvidenceTests(unittest.TestCase):
    """codex 0.160 `exec review`: CommandExecution items, plus the review child's code-mode outputs when it has any."""

    rollout = SessionLogTests.rollout

    WARNING = 'Warning: truncated output (original token count: 11082)\nTotal output lines: 352\n\n'

    def diff(self, path, *rows):
        return f'diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1,{len(rows)} @@\n' + ''.join(
            f'+{row}\n' for row in rows)

    def command(self, aggregated, formatted=None, exit_code=0, number=1):
        return {'type': 'item_completed', 'item': {
            'type': 'CommandExecution', 'id': f'call_{number}', 'process_id': str(number), 'status': 'completed',
            'command': ['/usr/bin/bash', '-lc', 'git diff origin/main...HEAD'], 'exit_code': exit_code,
            'aggregated_output': aggregated, 'stdout': aggregated, 'stderr': '',
            'formatted_output': aggregated if formatted is None else formatted}}

    def review(self, expected, parent, child=None):
        """Write a review thread's rollouts and return the unread files the checker reports for it."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.rollout(home, 'parent', {'id': 'thread-1', 'cwd': '/w', 'cli_version': '0.160.1'}, [
                {'type': 'item_completed', 'item': {'type': 'EnteredReviewMode', 'id': 'r1'}}, *parent,
                {'type': 'item_completed', 'item': {'type': 'AgentMessage', 'id': 'm1'}}])
            if child is not None:
                self.rollout(home, 'child', {'id': 'thread-2', 'parent_thread_id': 'thread-1',
                                             'source': {'subagent': 'review'}}, child)
            records, error = cr.SessionLog(home).outputs('thread-1', 0)
        self.assertIsNone(error)
        printed = [item['item']['aggregated_output'] for item in [*parent, *(child or [])]
                   if item.get('item', {}).get('type') == 'CommandExecution' and item['item']['exit_code'] == 0]
        events = cr.read_events(events_jsonl('', outputs=printed))
        return cr.unread_files(expected, cr.evidence_lines(records, events))

    def three_files(self):
        expected = {'a.py': self.diff('a.py', 'alpha one', 'alpha two'),
                    'b.py': self.diff('b.py', *[f'beta {n}' for n in range(6)]),
                    'c.py': self.diff('c.py', 'gamma one', 'gamma two')}
        return expected, ''.join(expected.values())

    def test_full_diff_in_formatted_output_reads_every_file(self):
        expected, full = self.three_files()
        failed = self.command('fatal: bad revision\n', exit_code=2, number=2)
        self.assertEqual(self.review(expected, [self.command('pwd\n/w\n'), self.command(full, number=3), failed]), [])

    def test_lines_in_the_elided_middle_are_unread_and_the_cut_line_is_not_seen(self):
        expected, full = self.three_files()
        cut = full[:full.index('+beta 1')] + '+beta …1082 tokens truncated…a 4\n' + full[full.index('+beta 5'):]
        command = self.command(full, self.WARNING + cut)
        self.assertEqual(self.review(expected, [command]), [('b.py', '部分')])
        texts = cr.visible_texts([command['item'], cr.ROLLOUT_BOUNDARY])
        self.assertNotIn('Total output lines: 352', texts[0])
        self.assertNotIn('tokens truncated', texts[0])
        dropped = full[:full.index('diff --git a/b.py')] + '+beta …900 tokens truncated…\n' + expected['c.py']
        self.assertEqual(self.review(expected, [self.command(full, self.WARNING + dropped)]), [('b.py', '未读')])

    def test_a_file_only_in_aggregated_output_is_unread(self):
        expected, full = self.three_files()
        shown = expected['a.py'] + expected['c.py']
        self.assertEqual(self.review(expected, [self.command(full, shown)]), [('b.py', '未读')])

    def test_plain_code_mode_text_counts_when_it_is_a_successful_commands_output(self):
        expected, full = self.three_files()
        a, b, c = expected.values()
        header = {'type': 'input_text', 'text': 'Script completed\nWall time 0.2 seconds\nOutput:\n'}

        def output(*texts, number=1):
            return {'type': 'custom_tool_call_output', 'call_id': f'call_{number}',
                    'output': [header, *({'type': 'input_text', 'text': text} for text in texts)]}
        child = [self.command(a), self.command(b, number=2), output(a, b),
                 self.command(c, exit_code=1, number=3), output(c, number=3),
                 output('{"content":[{"type":"text","text":"Action completed."}]}', number=4)]
        self.assertEqual(self.review(expected, [], child), [('c.py', '未读')])
        partly = [self.command(full, a), output(a), self.command(c, exit_code=1, number=2), output(c, number=2)]
        self.assertEqual(self.review(expected, [], partly), [('b.py', '未读'), ('c.py', '未读')])
        wrapped = [self.command(full), output(self.WARNING + full[:40])]
        self.assertEqual(self.review(expected, [self.command(full)], wrapped),
                         [('a.py', '未读'), ('b.py', '未读'), ('c.py', '未读')])
        cut = self.WARNING + a + '+beta …1082 tokens truncated…\n' + c
        self.assertEqual(self.review(expected, [], [self.command(full, cut), output(cut)]), [('b.py', '未读')])


class ManifestTests(unittest.TestCase):
    """The changed-file list reaches codex through a file, so a huge PR cannot overflow one argv entry."""

    def test_five_thousand_files_start_codex_with_bounded_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stub = root / 'codex-stub'
            stub.write_text('#!/usr/bin/env python3\nimport sys\n'
                            "print(max(len(a.encode()) for a in sys.argv))\n"
                            "open(sys.argv[sys.argv.index('-o') + 1], 'w').write('ok')\n", encoding='utf-8')
            stub.chmod(0o700)
            files = [f'src/package/module_{n:05d}/implementation_file_{n:05d}.py' for n in range(5000)]
            manifest = root / 'changed-files.txt'
            manifest.write_text(''.join(f'{f}\n' for f in files), encoding='utf-8')
            sections = cr.first_prompt('main') + [cr.read_rule('origin/main...HEAD', manifest, len(files))]
            scratch = root / 'review1'
            scratch.mkdir()
            rc, output, _, last, timed_out = cr.CodexRunner(str(stub))(1, root, sections, 'xhigh', 60, scratch,
                                                                        lambda: True)
            self.assertEqual((rc, last, timed_out), (0, 'ok', False))
            self.assertLess(int(output.strip()), 131072)
            self.assertIn(f'listed one per line in `{manifest}`', cr.review_prompt(sections))
            self.assertNotIn('implementation_file_04999.py', cr.review_prompt(sections))
            self.assertEqual(manifest.read_text(encoding='utf-8').count('\n'), 5000)


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
        self.assertEqual(self.codex.manifests[-1], 'src/fix.py\n')
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


class RealDiffTests(unittest.TestCase):
    """Real git: reference diffs come from codex's worktree, keep CRLF bytes, and honour `.gitattributes`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src, self.store = root / 'src', cr.Git(root / 'state')
        self.src.mkdir()
        git(self.src, 'init', '-q', '-b', 'main')
        (self.src / '.gitattributes').write_text('*.dat -diff\n')
        (self.src / 'bin.dat').write_bytes(b'\x00\x01\x02')
        (self.src / 'data.dat').write_text('alpha\nbeta\n')
        (self.src / 'win.txt').write_bytes(b'one\r\ntwo\r\n')
        (self.src / 'old.py').write_text('print(1)\n' * 10)
        (self.src / 'gone.py').write_text('x = 1\n')
        (self.src / 'run.sh').write_text('echo\n')
        git(self.src, 'add', '-A')
        git(self.src, 'commit', '-qm', 'base')
        git(self.src, 'switch', '-qc', 'pr')
        (self.src / 'bin.dat').write_bytes(b'\x00\x01\x03')
        (self.src / 'data.dat').write_text('alpha\ngamma\n')
        (self.src / 'win.txt').write_bytes(b'one\r\nthree\r\n')
        git(self.src, 'mv', 'old.py', 'new.py')
        (self.src / 'gone.py').unlink()
        (self.src / 'run.sh').chmod(0o755)
        git(self.src, 'add', '-A')
        git(self.src, 'commit', '-qm', 'pr')
        self.head = git(self.src, 'rev-parse', 'HEAD')
        mirror = self.store.mirror(REPO)
        mirror.parent.mkdir(parents=True)
        git(root, 'init', '-q', '--bare', str(mirror))
        git(self.src, 'push', '-q', str(mirror), 'main:refs/remotes/origin/main', 'pr:refs/codex/pr/7')
        self.worktree = root / 'state' / 'work' / '1' / 'tree'
        self.store.add_worktree(REPO, self.worktree, self.head)
        self.files = self.store.changed_files(REPO, 'main', self.head)
        self.expected = self.store.file_diffs(self.worktree, [f'refs/remotes/origin/main...{self.head}'], self.files)

    def tearDown(self):
        self.store.remove_worktree(REPO, self.worktree)
        self.tmp.cleanup()

    def codex_output(self, path):
        """The bytes codex's own `git diff` prints for path in the worktree, decoded as codex does."""
        return subprocess.run(['git', '-C', str(self.worktree), 'diff', '--no-renames', 'origin/main...HEAD', '--', path],
                              check=True, capture_output=True).stdout.decode('utf-8', 'replace')

    def test_file_diffs_cover_every_kind_of_change(self):
        self.assertEqual(sorted(self.expected), ['bin.dat', 'data.dat', 'gone.py', 'new.py', 'old.py', 'run.sh', 'win.txt'])
        self.assertIn('Binary files a/bin.dat and b/bin.dat differ', self.expected['bin.dat'])
        self.assertIn('old mode 100644\nnew mode 100755', self.expected['run.sh'])
        self.assertIn('deleted file mode', self.expected['gone.py'])
        self.assertIn('new file mode', self.expected['new.py'])
        self.assertNotIn('rename', self.expected['new.py'])
        shown = [self.codex_output(path) for path in self.files]
        lines = cr.evidence_lines([session_item(text) for text in shown], cr.read_events(events_jsonl('', outputs=shown)))
        self.assertEqual(cr.unread_files(self.expected, lines), [])
        without_binary = [self.codex_output(path) for path in self.files if path != 'bin.dat']
        lines = cr.evidence_lines([session_item(t) for t in without_binary], cr.read_events(events_jsonl('', outputs=without_binary)))
        self.assertEqual(cr.unread_files(self.expected, lines), [('bin.dat', '未读')])

    def test_crlf_lines_keep_their_bytes_on_both_sides(self):
        self.assertIn('-two\r\n+three\r\n', self.expected['win.txt'])
        self.assertEqual(self.expected['win.txt'], self.codex_output('win.txt'))

    def test_minus_diff_attribute_is_applied_to_the_reference(self):
        self.assertIn('Binary files a/data.dat and b/data.dat differ', self.expected['data.dat'])
        self.assertNotIn('+gamma', self.expected['data.dat'])
        self.assertEqual(self.expected['data.dat'], self.codex_output('data.dat'))


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
