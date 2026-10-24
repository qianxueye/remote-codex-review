#!/usr/bin/env python3
"""Prove confidence regressions fail, using disposable copies; never edit the checkout.

Run: python3 tests/mutate_confidence.py [--baseline] [--baseline-ref REF] [--logs /tmp/rcr-mutations]
The baseline option checks confidence tests against a local commit (default 9690ba7).
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
C = 'test_confidence.ConfidenceTests.'
R = 'test_confidence.RolloutTests.'
A = 'test_confidence.AuditTests.'
SCORE = "finding.confidence_score = raw.get('confidence_score')"
POST = 'f.confidence_score > min_confidence'
MUTATIONS = [
    ('inclusive-boundary', C + 'test_exactly_080_is_withheld', POST, 'f.confidence_score >= min_confidence'),
    ('rounded-comparison', C + 'test_raw_08000001_passes', POST, 'round(f.confidence_score, 2) > min_confidence'),
    ('reject-081', C + 'test_081_passes', POST, 'f.confidence_score > 0.9'),
    ('missing-default-high', C + 'test_invalid_confidences_are_withheld', SCORE,
     "finding.confidence_score = raw.get('confidence_score', 0.99)"),
    ('accept-string', C + 'test_invalid_confidences_are_withheld', SCORE,
     "finding.confidence_score = float(raw['confidence_score']) if isinstance(raw.get('confidence_score'), str) else raw.get('confidence_score')"),
    ('accept-nan', C + 'test_invalid_confidences_are_withheld', SCORE,
     "finding.confidence_score = 0.99 if isinstance(raw.get('confidence_score'), float) and math.isnan(raw['confidence_score']) else raw.get('confidence_score')"),
    ('accept-all-invalid', C + 'test_invalid_confidences_are_withheld', SCORE,
     "finding.confidence_score = raw.get('confidence_score') if valid_confidence(raw.get('confidence_score')) else 0.99"),
    ('all-withheld-findings', C + 'test_all_withheld_is_clean_with_count_and_no_details',
     "return ('findings' if posted else 'clean'), summary, posted, []", "return 'findings', summary, posted, []"),
    ('leak-overall-summary', C + 'test_all_withheld_is_clean_with_count_and_no_details',
     "summary = f'已发布 {len(posted)} 条发现（P1 不受置信度门槛限制，其他发现须严格大于阈值）。'", "summary = message"),
    ('omit-score-inline', C + 'test_mixed_publishes_only_high_and_score',
     "+ f' · 置信度 {confidence_text(finding.confidence_score)}'", "+ ''"),
    ('omit-score-summary', C + 'test_mixed_publishes_only_high_and_score',
     "parts.append(f'- **[{finding.priority}] {finding.title}** — `{finding.location()}`'\n"
     "                     + f' · 置信度 {confidence_text(finding.confidence_score)}'",
     "parts.append(f'- **[{finding.priority}] {finding.title}** — `{finding.location()}`'"),
    ('ignore-threshold', C + 'test_config_validated_and_used', POST, 'f.confidence_score > 0.8'),
    ('unvalidated-config', C + 'test_config_validated_and_used',
     'if not math.isfinite(self.min_confidence) or not 0 <= self.min_confidence <= 1:', 'if False:'),
    ('coerce-malformed-output', C + 'test_missing_or_malformed_structured_output_fails',
     "result = envelopes[0].get('review_output')", "result = {'findings': [], 'overall_correctness': 'patch is correct', 'overall_explanation': 'VERDICT: CLEAN', 'overall_confidence_score': 0.9}"),
    ('ignore-disagreement', C + 'test_disagreeing_final_findings_fail_without_leak',
     'sorted(structured_keys) != sorted(rendered_keys)', 'False'),
    ('allow-duplicate-envelope', C + 'test_thread_mismatch_and_duplicate_envelopes_fail',
     'len(envelopes) != 1', 'not envelopes'),
    ('overwrite-structured-score', C + 'test_reordered_final_and_whitespace_reconcile', SCORE,
     'finding.confidence_score = 0.99'),
    ('hide-duplicate-conflict', C + 'test_duplicate_final_with_conflicting_body_fails',
     'deduplicate=False,', 'deduplicate=True,'),
    ('ignore-unparsed-extra', C + 'test_unparsed_extra_final_finding_fails',
     'PRIORITY_RE.search(parsed_summary)', 'False'),
    ('exec-only-fallback', C + 'test_exec_only_recording_has_no_implicit_confidence',
     '    try:\n        findings = structured_findings(events, message, worktree_roots)',
     "    if not any(e.get('type') == 'review.structured' for e in events):\n        return 'clean', '', [], []\n"
     '    try:\n        findings = structured_findings(events, message, worktree_roots)'),
    ('repair-malformed-finding', C + 'test_malformed_finding_schema_fails',
     "    for raw in result['findings']:",
     "    for raw in result['findings']:\n        raw = {'title': '[P2] Keep the gate closed', 'body': 'Secret detail for Keep the gate closed', 'priority': 2, 'confidence_score': 0.87, 'code_location': {'absolute_file_path': '/var/lib/codex-review/work/7/tree/src/app.py', 'line_range': {'start': 1, 'end': 1}}}"),
    ('discard-rollout-output', R + 'test_exact_thread_rollout_is_loaded',
     "outputs.append(item.get('review_output'))", 'outputs.append(None)'),
    ('ignore-session-id', R + 'test_missing_wrong_or_ambiguous_rollout_fails',
     "m.get('id') != thread", 'False'),
    ('ignore-event-thread', R + 'test_wrong_event_thread_malformed_or_multiple_outputs_fail',
     "if payload.get('thread_id', thread) != thread:", 'if False:'),
    ('runner-skips-rollout', R + 'test_runner_attaches_structured_output',
     'text = attach_review_output(text, worktree)', 'text = text'),
    ('lose-withheld-audit', A + 'test_withheld_details_saved_privately_before_publication',
     "verdict == 'failed' or audit.get('withheld_count') or len(body) > BODY_LIMIT",
     "verdict == 'failed' or len(body) > BODY_LIMIT"),
    ('reviewer-default-threshold', A + 'test_config_threshold_reaches_reviewer',
     'assess(rc, events, message, roots, self.config.min_confidence, audit)',
     'assess(rc, events, message, roots, 0.8, audit)'),
    ('no-clean-audit', A + 'test_all_withheld_has_clean_marker_and_audit_before_post',
     "verdict == 'failed' or audit.get('withheld_count') or len(body) > BODY_LIMIT",
     "verdict == 'failed' or (verdict == 'findings' and audit.get('withheld_count')) or len(body) > BODY_LIMIT"),
    ('single-rendering-guard', 'test_fail_closed.RealOutputTests.test_real_single_finding_read_only_run_drops_the_block_header',
     'findings.append(finding)', 'pass  # mutated: discard structured finding'),
    ('all-withheld-skips-evidence', C + 'test_all_withheld_broken_recording_stays_failed',
     'if not (rc == 0 and message and posted):', 'if not (rc == 0 and message and findings):'),
    ('all-withheld-health-bypass', C + 'test_all_withheld_requires_each_clean_evidence_check',
     'if not (rc == 0 and message and posted):', 'if False:'),
    ('audit-all-withheld-skips-evidence', A + 'test_broken_withheld_run_publishes_failed_marker',
     'if not (rc == 0 and message and posted):', 'if not (rc == 0 and message and findings):'),
    ('reject-withheld-findings-verdict', C + 'test_empty_posted_accepts_only_single_clean_or_findings_verdict',
     "if declared not in (['CLEAN'], ['FINDINGS']):", "if declared != ['CLEAN']:"),
    ('change-existing-positive-path', C + 'test_published_finding_keeps_existing_nonpassing_failure_path',
     'if not (rc == 0 and message and posted):', 'if True:'),
    ('remove-p1-exemption', C + 'test_p1_bypasses_threshold_and_keeps_score',
     "f.priority == 'P1'", 'False'),
    ('reject-optional-priority', C + 'test_optional_priority_uses_title_and_rejects_ambiguity',
     'if priority is None and prefix:', 'if False:'),
    ('accept-relative-path', C + 'test_relative_location_and_capitalized_correctness_fail_closed',
     "not path.startswith('/')", 'False'),
    ('accept-capitalized-correctness', C + 'test_relative_location_and_capitalized_correctness_fail_closed',
     "result.get('overall_correctness') not in", "str(result.get('overall_correctness')).lower() not in"),
    ('reject-explanation-tag', C + 'test_explanation_priority_tag_is_not_an_extra_finding',
     "' '.join(parsed_summary.split()) != ' '.join(result['overall_explanation'].split())", 'True'),
    ('reject-whitespace-path', C + 'test_whitespace_path_reconciles',
     r'(.+?):(\d+)', r'(\S+?):(\d+)'),
    ('split-body-heading', C + 'test_body_heading_is_not_an_extra_finding',
     'expected_findings=findings)', 'expected_findings=())'),
    ('round-displayed-score', C + 'test_display_preserves_scores_and_threshold_precision',
     'return short if float(short) == value else str(value)', 'return short'),
    ('discard-real-rollout', R + 'test_sanitized_real_rollout_reconciles',
     "outputs.append(item.get('review_output'))", 'outputs.append(None)'),
    ('reject-forwarded-child', R + 'test_forwarded_other_thread_event_is_ignored',
     'continue  # Forwarded sub-agent results cannot replace the parent review.',
     "raise ValueError('unexpected child event')"),
    ('reject-repeated-session', R + 'test_repeated_matching_session_metadata_is_accepted',
     'if not meta or any(', 'if len(meta) != 1 or any('),
    ('ignore-conflicting-session', R + 'test_conflicting_session_metadata_fails_closed',
     'for m in meta):', 'for m in meta[:1]):'),
    ('discard-unlocated-tag', 'test_codex_review.ParseTests.test_priority_tag_without_location_still_counts_as_findings',
     "findings or PRIORITY_RE.search(message)", 'findings'),
]


def run(source, test, path, logs, name):
    (path / 'codex_review.py').write_text(source)
    env = dict(os.environ, PYTHONPATH=str(path / 'tests'), PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-B', '-m', 'unittest', test, '-v'], cwd=path, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30)
    (logs / (name + '.log')).write_text(result.stdout)
    return result.returncode != 0 and 'FAIL:' in result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', action='store_true')
    parser.add_argument('--baseline-ref', default='9690ba7')
    parser.add_argument('--logs', type=Path, required=True)
    args = parser.parse_args()
    args.logs.mkdir(parents=True, exist_ok=True)
    source = (ROOT / 'codex_review.py').read_text()
    with tempfile.TemporaryDirectory(prefix='rcr-confidence-mutation-') as folder:
        path = Path(folder)
        shutil.copytree(ROOT / 'tests', path / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
        if args.baseline:
            source = subprocess.check_output(['git', 'show', args.baseline_ref + ':codex_review.py'], cwd=ROOT, text=True)
            # Missing APIs may raise errors on the original implementation; those are legitimate red tests.
            (path / 'codex_review.py').write_text(source)
            env = dict(os.environ, PYTHONPATH=str(path / 'tests'), PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'test_confidence', '-v'], cwd=path,
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30)
            (args.logs / 'baseline.log').write_text(result.stdout)
            print(result.stdout[result.stdout.rfind('Ran '):].strip())
            return 0 if result.returncode else 1
        failed = []
        for name, test, before, after in MUTATIONS:
            if before not in source:
                raise RuntimeError('mutation target missing: ' + name)
            killed = run(source.replace(before, after, 1), test, path, args.logs, name)
            print(('KILLED ' if killed else 'SURVIVED ') + name + ' -> ' + test, flush=True)
            if not killed:
                failed.append(name)
        print(f'{len(MUTATIONS) - len(failed)}/{len(MUTATIONS)} mutations killed; '
              f'{len(set(m[1] for m in MUTATIONS))} tests covered')
        return bool(failed)


if __name__ == '__main__':
    sys.exit(main())
