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
        self.assertIn('另有 1 条置信度不高于 0.80 的发现未发布', summary)
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
