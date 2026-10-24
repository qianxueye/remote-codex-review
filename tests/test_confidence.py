"""Offline confidence policy and exact-rollout regression tests."""
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

import codex_review as cr
from test_codex_review import HEAD, WORKTREE, Harness, config, events_jsonl, pull_payload


def finding(score=0.87, title='Keep the gate closed', line=1):
    result = {'title': '[P2] ' + title, 'body': 'Secret detail for ' + title,
              'priority': 2, 'code_location': {'absolute_file_path': WORKTREE + '/src/app.py',
                                             'line_range': {'start': line, 'end': line}},
              'confidence_score': score}
    return result


def review_output(findings):
    return {'findings': findings, 'overall_correctness': 'patch is incorrect' if findings else 'patch is correct',
            'overall_explanation': 'Private overall explanation.\nVERDICT: ' + ('FINDINGS' if findings else 'CLEAN'),
            'overall_confidence_score': 0.94}


def message(findings):
    intro = 'Private overall explanation.\nVERDICT: ' + ('FINDINGS' if findings else 'CLEAN')
    return intro + ''.join('\n\n- ' + f['title'] + ' — ' + f['code_location']['absolute_file_path'] + ':'
                           + str(f['code_location']['line_range']['start']) + '\n  ' + f['body'] for f in findings)


def envelope(output, thread='t-1'):
    return {'type': 'review.structured', 'thread_id': thread, 'review_output': output}


def stream(findings):
    return cr.read_events(events_jsonl(message(findings))) + [envelope(review_output(findings))]


def assess(findings, **kwargs):
    return cr.assess(0, stream(findings), message(findings), (WORKTREE,), **kwargs)


class ConfidenceTests(unittest.TestCase):
    def test_all_withheld_broken_recording_stays_failed(self):
        fixtures = Path(__file__).parent / 'fixtures'
        items = [finding(0.3)]
        final = (fixtures / 'broken-tests.last').read_text() + '\n\n' + message(items).split('\n\n', 1)[1]
        events = cr.read_events((fixtures / 'broken-tests.jsonl').read_text())
        events.append(envelope(review_output(items), events[0]['thread_id']))
        verdict, _, posted, reasons = cr.assess(0, events, final, (WORKTREE,))
        self.assertEqual((verdict, posted), ('failed', []))
        for reason in ('error item', 'no command with exit code 0', 'reports a failure', 'found BLOCKED'):
            self.assertIn(reason, '\n'.join(reasons))

    def test_all_withheld_requires_each_clean_evidence_check(self):
        items = [finding(0.3)]
        healthy = stream(items)
        cases = [(1, healthy, message(items)),
                 (0, [e for e in healthy if e.get('type') != 'turn.completed'], message(items)),
                 (0, healthy + [{'type': 'turn.failed'}], message(items)),
                 (0, healthy + [{'type': 'error', 'message': 'fatal error'}], message(items)),
                 (0, healthy + [{'type': 'item.completed', 'item': {'type': 'error', 'message': 'tool failed'}}], message(items)),
                 (0, [e for e in healthy if (e.get('item') or {}).get('type') != 'command_execution'], message(items)),
                 (0, healthy, message(items).replace('FINDINGS', 'BLOCKED')),
                 (0, healthy, message(items).replace('VERDICT: FINDINGS', 'Review incomplete.')),
                 (0, healthy, message(items).replace('VERDICT: FINDINGS', '')),
                 (0, healthy, 'VERDICT: CLEAN\n' + message(items))]
        for rc, events, final in cases:
            with self.subTest(rc=rc, events=events[-1], final=final):
                self.assertEqual(cr.assess(rc, events, final, (WORKTREE,))[0], 'failed')

    def test_empty_posted_accepts_only_single_clean_or_findings_verdict(self):
        for items in ([], [finding(0.3)]):
            for verdict in ('CLEAN', 'FINDINGS'):
                final = message(items).replace('VERDICT: CLEAN', 'VERDICT: ' + verdict).replace('VERDICT: FINDINGS', 'VERDICT: ' + verdict)
                self.assertEqual(cr.assess(0, stream(items), final, (WORKTREE,))[0], 'clean')

    def test_published_finding_keeps_existing_nonpassing_failure_path(self):
        items = [finding(0.9)]
        final = message(items).replace('VERDICT: FINDINGS', 'Review incomplete.\nVERDICT: BLOCKED')
        events = [e for e in stream(items) if (e.get('item') or {}).get('type') != 'command_execution']
        self.assertEqual(cr.assess(0, events, final, (WORKTREE,))[0], 'findings')

    def test_p1_bypasses_threshold_and_keeps_score(self):
        for score in (0.0, 0.3, 0.8, None, '0.9', True, float('nan'), float('inf'), -1, 2, {}, [], 10 ** 500):
            item = dict(finding(score), priority=1, title='[P1] Urgent defect')
            verdict, summary, posted, reasons = assess([item], min_confidence=1)
            self.assertEqual((verdict, len(posted), reasons), ('findings', 1, []))
            body = cr.review_body(HEAD, verdict, summary, posted)
            inline = cr.inline_comments(posted, {'src/app.py': {1: 0}})[0]['body']
            for text in (body, inline):
                self.assertIn('置信度 ' + (str(score) if cr.valid_confidence(score) else '缺失或无效'), text)
        for priority in (0, 2, 3):
            self.assertEqual(assess([dict(finding(0.8), priority=priority, title=f'[P{priority}] Other')])[2], [])
            self.assertEqual(assess([dict(finding(0.81), priority=priority, title=f'[P{priority}] Other')])[0], 'findings')
        missing = dict(finding(), priority=1, title='[P1] Missing score')
        del missing['confidence_score']
        self.assertEqual(assess([missing])[0], 'findings')

    def test_optional_priority_uses_title_and_rejects_ambiguity(self):
        for priority in (None, 'absent'):
            item = finding()
            if priority == 'absent':
                del item['priority']
            else:
                item['priority'] = priority
            self.assertEqual(assess([item])[0], 'findings')
            item['title'] = 'No priority available'
            self.assertEqual(assess([item])[0], 'failed')
        self.assertEqual(assess([dict(finding(), priority=1)])[0], 'failed')

    def test_relative_location_and_capitalized_correctness_fail_closed(self):
        for field in ('relative', 'correctness'):
            events = stream([finding()])
            output = events[-1]['review_output']
            if field == 'relative':
                output['findings'][0]['code_location']['absolute_file_path'] = 'src/app.py'
            else:
                output['overall_correctness'] = 'Patch is incorrect'
            self.assertEqual(cr.assess(0, events, message([finding()]), (WORKTREE,))[0], 'failed')

    def test_explanation_priority_tag_is_not_an_extra_finding(self):
        items = [finding()]
        events = stream(items)
        explanation = 'One [P1] regression in the queue.\nVERDICT: FINDINGS'
        events[-1]['review_output']['overall_explanation'] = explanation
        final = explanation + '\n\n' + message(items).split('\n\n', 1)[1]
        self.assertEqual(cr.assess(0, events, final, (WORKTREE,))[0], 'findings')

    def test_whitespace_path_reconciles(self):
        item = finding()
        item['code_location']['absolute_file_path'] = WORKTREE + '/source dir/app file.py'
        self.assertEqual(assess([item])[0], 'findings')

    def test_body_heading_is_not_an_extra_finding(self):
        item = finding()
        item['body'] += '\n- [P3] Example heading — /example.py:3\nExample continuation.'
        verdict, _, posted, _ = assess([item])
        self.assertEqual(verdict, 'findings')
        self.assertEqual(posted[0].text, item['body'])
        final = message([item]).replace('Keep the gate closed', 'Keep   the gate closed')
        self.assertEqual(cr.assess(0, stream([item]), final, (WORKTREE,))[0], 'findings')

    def test_display_preserves_scores_and_threshold_precision(self):
        for score, threshold in ((0.8000001, 0.8), (0.801, 0.8), (0.80500001, 0.805)):
            items = [finding(score), finding(threshold, 'Withheld', 2)]
            verdict, summary, posted, _ = assess(items, min_confidence=threshold)
            body = cr.review_body(HEAD, verdict, summary, posted)
            inline = cr.inline_comments(posted, {'src/app.py': {1: 0}})[0]['body']
            for text in (body, inline):
                displayed = float(text.split('置信度 ')[-1].split()[0])
                self.assertGreater(displayed, threshold)
                self.assertEqual(displayed, score)
            self.assertIn('不高于 ' + ('0.80' if threshold == 0.8 else str(threshold)), summary)

    def test_exactly_080_is_withheld(self):
        self.assertEqual(assess([finding(0.80)])[2], [])

    def test_raw_08000001_passes(self):
        self.assertEqual(len(assess([finding(0.8000001)])[2]), 1)
        self.assertEqual(assess([finding(0.8000001)])[2][0].confidence_score, 0.8000001)

    def test_081_passes(self):
        posted = assess([finding(0.81)])[2]
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0].confidence_score, 0.81)

    def test_invalid_confidences_are_withheld(self):
        for score in (None, '0.99', float('nan'), float('inf'), -0.1, 1.1, 10 ** 500, True, {}, []):
            with self.subTest(score=score):
                self.assertEqual(assess([finding(score)])[2], [])
        item = finding()
        del item['confidence_score']
        self.assertEqual(assess([item])[2], [])

    def test_all_withheld_is_clean_with_count_and_no_details(self):
        verdict, summary, posted, reasons = assess([finding(0.8)])
        self.assertEqual((verdict, posted, reasons), ('clean', [], []))
        self.assertIn('另有 1 条置信度不高于 0.80 的非 P1 发现未发布', summary)
        self.assertNotIn('Private', summary)
        self.assertNotIn('Secret', summary)

    def test_mixed_publishes_only_high_and_score(self):
        verdict, summary, posted, _ = assess([finding(0.8, 'Withheld'), finding(0.87, 'Published', 2)])
        body = cr.review_body(HEAD, verdict, summary, posted)
        comments = cr.inline_comments(posted, {'src/app.py': {1: 1, 2: 1}})
        self.assertEqual(len(posted), 1)
        self.assertEqual(len(comments), 1)
        for text in (body, comments[0]['body']):
            self.assertIn('置信度 0.87', text)
            self.assertIn('Published', text)
            self.assertNotIn('Withheld', text)
            self.assertNotIn('Private', text)

    def test_config_validated_and_used(self):
        self.assertEqual(config().min_confidence, 0.8)
        for value in ('', 'wat', 'nan', 'inf', '-0.1', '1.01'):
            with self.subTest(value=value), self.assertRaises(cr.ConfigError):
                config(CODEX_REVIEW_MIN_CONFIDENCE=value)
        for value in ('0', '0.9', '1'):
            self.assertEqual(config(CODEX_REVIEW_MIN_CONFIDENCE=value).min_confidence, float(value))
        self.assertEqual(assess([finding(0.87)], min_confidence=0.9)[0], 'clean')

    def test_missing_or_malformed_structured_output_fails(self):
        for output in (None, [], {}, {'findings': {}}, review_output([{'title': 'broken'}])):
            with self.subTest(output=output):
                events = cr.read_events(events_jsonl(message([]))) + [envelope(output)]
                self.assertEqual(cr.assess(0, events, message([]), (WORKTREE,))[0], 'failed')
        events = cr.read_events(events_jsonl(message([])))
        self.assertEqual(cr.assess(0, events, message([]), (WORKTREE,))[0], 'failed')

    def test_disagreeing_final_findings_fail_without_leak(self):
        items = [finding()]
        variants = [[], [finding(title='Other')], [dict(finding(), body='Other body')]]
        for other in variants:
            with self.subTest(other=other):
                verdict, summary, posted, reasons = cr.assess(0, stream(items), message(other), (WORKTREE,))
                self.assertEqual((verdict, posted), ('failed', []))
                self.assertNotIn('Secret', summary + str(reasons))

    def test_thread_mismatch_and_duplicate_envelopes_fail(self):
        for extra in ([envelope(review_output([]), 'other')], [envelope(review_output([]))] * 2):
            events = cr.read_events(events_jsonl(message([]))) + extra
            self.assertEqual(cr.assess(0, events, message([]), (WORKTREE,))[0], 'failed')

    def test_reordered_final_and_whitespace_reconcile(self):
        items = [finding(), finding(0.95, 'Second', 2)]
        final = message(list(reversed(items))).replace('Secret detail', 'Secret   detail')
        verdict, _, posted, _ = cr.assess(0, stream(items), final, (WORKTREE,))
        self.assertEqual(verdict, 'findings')
        self.assertEqual([f.confidence_score for f in posted], [0.87, 0.95])

    def test_duplicate_final_with_conflicting_body_fails(self):
        items = [finding()]
        final = message(items) + '\n\n' + message([dict(finding(), body='Contradictory body')]).split('\n\n', 1)[1]
        self.assertEqual(cr.assess(0, stream(items), final, (WORKTREE,))[0], 'failed')

    def test_unparsed_extra_final_finding_fails(self):
        items = [finding()]
        final = 'Another issue: [P1] an unlocated extra finding.\n\n' + message(items)
        self.assertEqual(cr.assess(0, stream(items), final, (WORKTREE,))[0], 'failed')

    def test_exec_only_recording_has_no_implicit_confidence(self):
        path = Path(__file__).parent / 'fixtures/findings-ro.jsonl'
        last = path.with_suffix('.last').read_text()
        self.assertEqual(cr.assess(0, cr.read_events(path.read_text()), last, (WORKTREE,))[0], 'failed')

    def test_malformed_finding_schema_fails(self):
        for mutation in ({'body': []}, {'priority': True}, {'code_location': None},
                         {'code_location': {'absolute_file_path': '/x', 'line_range': {'start': 2, 'end': 1}}}):
            with self.subTest(mutation=mutation):
                events = stream([finding()])
                events[-1]['review_output']['findings'][0].update(mutation)
                self.assertEqual(cr.assess(0, events, message([finding()]), (WORKTREE,))[0], 'failed')


class RolloutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.path = self.home / 'sessions/2026/10/24/rollout-2026-10-24-t-1.jsonl'
        self.path.parent.mkdir(parents=True)
        self.output = events_jsonl(message([finding()]))
        self.write_rollout()

    def write_rollout(self, thread='t-1', cwd=WORKTREE, payload=None):
        rows = [{'type': 'session_meta', 'payload': {'id': thread, 'cwd': cwd}},
                {'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': thread,
                 'item': {'type': 'ExitedReviewMode', 'review_output': review_output([finding()])}}}]
        if payload is not None:
            rows[1]['payload'] = payload
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def load(self):
        return cr.attach_review_output(self.output, Path(WORKTREE), self.home)

    def test_exact_thread_rollout_is_loaded(self):
        events = cr.read_events(self.load())
        self.assertEqual(cr.assess(0, events, message([finding()]), (WORKTREE,))[0], 'findings')
        self.assertEqual(events[-1]['review_output']['findings'][0]['confidence_score'], 0.87)

    def test_sanitized_real_rollout_reconciles(self):
        """Original rollout ordinals 0/63/65, codex 0.160.1; neutral prose, unchanged schema."""
        rows = [json.loads(line) for line in (Path(__file__).parent / 'fixtures/real-review-rollout.jsonl').read_text().splitlines()]
        thread, cwd = rows[0]['payload']['id'], rows[0]['payload']['cwd']
        final = rows[-1]['payload']['item']['content'][0]['text']
        self.path.unlink()
        path = self.path.with_name('rollout-2026-10-24-' + thread + '.jsonl')
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        output = events_jsonl(final).replace('t-1', thread)
        events = cr.read_events(cr.attach_review_output(output, Path(cwd), self.home))
        verdict, _, posted, reasons = cr.assess(0, events, final, (cwd,))
        self.assertEqual((verdict, reasons), ('findings', []))
        self.assertEqual([f.confidence_score for f in posted], [0.97, 0.99])
        self.assertEqual([f.start for f in posted], [7409, 3616])
        self.assertTrue(all(f.path == 'src/main/java/us/q3q/fido2/FIDO2Applet.java' for f in posted))

    def test_forwarded_other_thread_event_is_ignored(self):
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        forwarded = json.loads(json.dumps(rows[-1]))
        forwarded['payload']['thread_id'] = 'child-thread'
        forwarded['payload']['item']['review_output'] = review_output([])
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in rows + [forwarded]))
        self.assertEqual(cr.assess(0, cr.read_events(self.load()), message([finding()]), (WORKTREE,))[0], 'findings')

    def test_repeated_matching_session_metadata_is_accepted(self):
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in rows + [rows[0]]))
        self.assertEqual(cr.assess(0, cr.read_events(self.load()), message([finding()]), (WORKTREE,))[0], 'findings')

    def test_conflicting_session_metadata_fails_closed(self):
        for field, value in (('id', 'other'), ('cwd', '/other/worktree')):
            self.write_rollout()
            row = {'type': 'session_meta', 'payload': {'id': 't-1', 'cwd': WORKTREE}}
            row['payload'][field] = value
            with self.path.open('a') as handle:
                handle.write(json.dumps(row) + '\n')
            self.assertEqual(cr.assess(0, cr.read_events(self.load()), message([finding()]), (WORKTREE,))[0], 'failed')

    def test_missing_wrong_or_ambiguous_rollout_fails(self):
        for thread, cwd in (('other', WORKTREE), ('t-1', '/wrong/worktree')):
            self.write_rollout(thread, cwd)
            # Keep the event identity correct to independently exercise the metadata guard.
            rows = self.path.read_text().splitlines()
            event = json.loads(rows[1])
            event['payload']['thread_id'] = 't-1'
            self.path.write_text(rows[0] + '\n' + json.dumps(event) + '\n')
            self.assertEqual(cr.assess(0, cr.read_events(self.load()), message([finding()]), (WORKTREE,))[0], 'failed')
        self.path.unlink()
        self.assertEqual(cr.assess(0, cr.read_events(self.load()), message([finding()]), (WORKTREE,))[0], 'failed')
        self.write_rollout()
        second = self.path.with_name('rollout-other-t-1.jsonl')
        second.write_text(self.path.read_text())
        self.assertEqual(cr.assess(0, cr.read_events(self.load()), message([finding()]), (WORKTREE,))[0], 'failed')

    def test_wrong_event_thread_malformed_or_multiple_outputs_fail(self):
        self.write_rollout(payload={'type': 'item_completed', 'thread_id': 'other',
                                   'item': {'type': 'ExitedReviewMode', 'review_output': review_output([finding()])}})
        self.assertEqual(cr.assess(0, cr.read_events(self.load()), message([finding()]), (WORKTREE,))[0], 'failed')
        self.write_rollout()
        with self.path.open('a') as handle:
            handle.write('{broken json\n')
        self.assertEqual(cr.assess(0, cr.read_events(self.load()), message([finding()]), (WORKTREE,))[0], 'failed')
        self.write_rollout()
        with self.path.open('a') as handle:
            handle.write(self.path.read_text().splitlines()[-1] + '\n')
        self.assertEqual(cr.assess(0, cr.read_events(self.load()), message([finding()]), (WORKTREE,))[0], 'failed')

    def test_runner_attaches_structured_output(self):
        scratch = self.home / 'scratch'
        scratch.mkdir()
        class Process:
            pid, returncode = 4242, 0
            def wait(inner, timeout=None):
                return 0
        def spawn(*args, **kwargs):
            kwargs['stdout'].write(self.output.encode())
            return Process()
        with mock.patch.dict('os.environ', {'CODEX_HOME': str(self.home)}), mock.patch.object(cr.subprocess, 'Popen', spawn):
            result = cr.CodexRunner('unused')(1, Path(WORKTREE), 'main', 'high', 60, scratch, lambda: True)
        events = cr.read_events(result[1])
        self.assertEqual(events[-1]['type'], 'review.structured')
        self.assertEqual(cr.assess(0, events, message([finding()]), (WORKTREE,))[0], 'findings')


class AuditTests(Harness):
    def test_broken_withheld_run_publishes_failed_marker(self):
        fixtures = Path(__file__).parent / 'fixtures'
        items = [finding(0.3)]
        self.codex.message = (fixtures / 'broken-tests.last').read_text() + '\n\n' + message(items).split('\n\n', 1)[1]
        stdout = (fixtures / 'broken-tests.jsonl').read_text()
        thread = cr.read_events(stdout)[0]['thread_id']
        self.codex.events = stdout + '\n' + json.dumps(envelope(review_output(items), thread))
        self.deliver(pull_payload())
        job, status = self.run_next()
        self.assertEqual(status, 'failed')
        body = self.github.posted[0]['body']
        self.assertIn(f'head={HEAD} verdict=failed', body)
        self.assertIn('found BLOCKED', body)
        self.assertNotIn('Secret', body)
        self.assertEqual(self.github.posted[0]['comments'], [])
        self.assertTrue((self.root / 'output' / f'{job.id}.log').is_file())

    def test_withheld_details_saved_privately_before_publication(self):
        items = [finding(0.8, 'Withheld'), finding(0.87, 'Published', 2)]
        self.codex.message = message(items)
        self.codex.events = events_jsonl(message(items)) + json.dumps(envelope(review_output(items))) + '\n'
        self.github.files = [{'filename': 'src/app.py', 'patch': '@@ -1,2 +1,2 @@\n+a\n+b'}]
        self.deliver(pull_payload())
        job, status = self.run_next()
        self.assertEqual(status, 'findings')
        post = self.github.posted[0]
        self.assertIn('另有 1 条', post['body'])
        self.assertNotIn('Withheld', post['body'] + str(post['comments']))
        kept = self.root / 'output' / f'{job.id}.log'
        self.assertTrue(kept.is_file())
        self.assertIn('Secret detail for Withheld', kept.read_text())
        self.assertIn('"withheld": [{"title": "[P2] Withheld"', kept.read_text())
        self.assertEqual(stat.S_IMODE(kept.stat().st_mode), 0o600)

    def test_config_threshold_reaches_reviewer(self):
        self.config.min_confidence = 0.9
        self.codex.message = message([finding(0.87)])
        self.codex.events = events_jsonl(self.codex.message) + json.dumps(envelope(review_output([finding(0.87)])))
        self.deliver(pull_payload())
        self.assertEqual(self.run_next()[1], 'clean')
        self.assertIn('不高于 0.90', self.github.posted[0]['body'])

    def test_all_withheld_has_clean_marker_and_audit_before_post(self):
        items = [finding(0.8)]
        self.codex.message = message(items)
        self.codex.events = events_jsonl(self.codex.message) + json.dumps(envelope(review_output(items)))
        self.deliver(pull_payload())
        original = self.github.post_review
        def post(*args, **kwargs):
            kept = self.root / 'output/1.log'
            self.assertIn('"withheld_count": 1', kept.read_text())
            return original(*args, **kwargs)
        with mock.patch.object(self.github, 'post_review', post):
            _, status = self.run_next()
        self.assertEqual(status, 'clean')
        body = self.github.posted[0]['body']
        self.assertIn(f'head={HEAD} verdict=clean', body)
        self.assertIn('另有 1 条', body)
        self.assertNotIn('Secret', body)
        self.assertEqual(self.github.posted[0]['comments'], [])
