#!/usr/bin/env python3
"""Tests for worker survival, the rollback path, the free-disk gate, the supervisor and sd_notify."""
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import codex_review as cr  # noqa: E402
from test_codex_review import HEAD, Harness, config, pull_payload  # noqa: E402

FULL = 'database or disk is full'
ROOT = Path(__file__).resolve().parent.parent
MIB = 1024 * 1024


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FullDiskHarness(Harness):
    """Make the next UPDATE on jobs fail with a real SQLITE_FULL from the engine."""

    def fill(self):
        db = self.store.db
        with self.store.lock:
            db.execute('CREATE TRIGGER fill AFTER UPDATE ON jobs BEGIN '
                       'INSERT INTO deliveries VALUES (hex(randomblob(8192)), 0); END')
            db.execute(f"PRAGMA max_page_count = {db.execute('PRAGMA page_count').fetchone()[0]}")

    def free(self):
        with self.store.lock:
            self.store.db.execute('DROP TRIGGER fill')
            self.store.db.execute('PRAGMA max_page_count = 1073741823')


class RollbackTests(Harness):
    def test_original_error_survives_an_auto_rolled_back_transaction(self):
        def work():
            self.store.db.execute('ROLLBACK')
            raise sqlite3.OperationalError(FULL)
        with self.assertRaises(sqlite3.OperationalError) as caught:
            self.store._transaction(work)
        self.assertEqual(str(caught.exception), FULL)
        self.assertFalse(self.store.db.in_transaction)

    def test_rollback_still_happens_when_the_transaction_is_open(self):
        def work():
            self.store.db.execute("INSERT INTO deliveries VALUES ('x', 0)")
            raise RuntimeError('stop')
        with self.assertRaises(RuntimeError):
            self.store._transaction(work)
        self.assertFalse(self.store.db.in_transaction)
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM deliveries WHERE guid = 'x'").fetchone())


class SurvivalTests(FullDiskHarness):
    def setUp(self):
        super().setUp()
        self.reviewer.backoff_first = self.reviewer.backoff_max = 0.02
        self.logs = []
        self.worker = threading.Thread(target=self.reviewer.work_forever, daemon=True)

    def tearDown(self):
        self.reviewer.stop.set()
        self.reviewer.wake.set()
        self.worker.join(5)
        super().tearDown()

    def test_full_database_during_claim_keeps_the_job_and_the_worker(self):
        self.deliver(pull_payload())
        self.fill()
        with mock.patch.object(cr, 'log', self.logs.append):
            self.worker.start()
            self.assertTrue(wait_for(lambda: any(FULL in line for line in self.logs)))
            self.assertTrue(wait_for(lambda: sum(FULL in line for line in self.logs) >= 3), 'claims must be retried')
            self.assertTrue(self.worker.is_alive(), 'a failed claim must not end the worker thread')
            self.assertEqual([(job.head, job.status) for job in self.store.queued()], [(HEAD, 'queued')])
            self.free()
            self.assertTrue(wait_for(lambda: bool(self.github.posted)), 'the review must run once space is back')
        self.assertTrue(self.worker.is_alive())
        self.assertEqual(self.github.posted[0]['head'], HEAD)

    def test_failing_requeue_after_a_broken_review_keeps_the_worker(self):
        self.deliver(pull_payload())
        calls = []

        def broken_run(job):
            calls.append(job.id)
            raise RuntimeError('review exploded')

        def broken_retry(*_args, **_kwargs):
            raise sqlite3.OperationalError(FULL)
        self.reviewer.run = broken_run
        self.store.retry = broken_retry
        with mock.patch.object(cr, 'log', self.logs.append):
            self.worker.start()
            self.assertTrue(wait_for(lambda: any('review exploded' in line for line in self.logs)))
            self.assertTrue(wait_for(lambda: any(FULL in line for line in self.logs)))
        self.assertTrue(self.worker.is_alive())
        self.assertEqual(calls, [1])


class DiskGateTests(Harness):
    def setUp(self):
        super().setUp()
        self.free = [10 * 1024 * MIB]
        self.reviewer.disk_free = lambda: self.free[0]
        self.logs = []

    def test_low_free_space_skips_claims_logs_once_and_resumes(self):
        self.deliver(pull_payload())
        self.free[0] = 100 * MIB
        with mock.patch.object(cr, 'log', self.logs.append):
            self.assertFalse(self.reviewer.work_once())
            self.assertFalse(self.reviewer.work_once())
        self.assertEqual([job.status for job in self.store.queued()], ['queued'])
        low = [line for line in self.logs if 'CODEX_REVIEW_MIN_FREE_MB' in line]
        self.assertEqual(len(low), 1, self.logs)
        self.assertIn('100 MB free', low[0])
        self.assertIn('1024', low[0])
        self.assertIn(str(self.config.state_dir), low[0])
        self.assertIn('disk space low', self.reviewer.health())
        self.free[0] = 10 * 1024 * MIB
        with mock.patch.object(cr, 'log', self.logs.append):
            self.assertTrue(self.reviewer.work_once())
        self.assertTrue(any('restored' in line for line in self.logs))
        self.assertEqual(len(self.github.posted), 1)
        self.assertNotIn('disk space low', self.reviewer.health())

    def test_threshold_comes_from_config_and_zero_disables_the_gate(self):
        self.assertEqual(config().min_free_bytes, 1024 * MIB)
        self.assertEqual(config(CODEX_REVIEW_MIN_FREE_MB='5').min_free_bytes, 5 * MIB)
        with self.assertRaises(cr.ConfigError):
            config(CODEX_REVIEW_MIN_FREE_MB='-1')
        self.reviewer.config = config(CODEX_REVIEW_MIN_FREE_MB='0', CODEX_REVIEW_STATE_DIR=str(self.root))
        self.free[0] = 0
        self.deliver(pull_payload())
        self.assertTrue(self.reviewer.work_once())

    def test_unmeasurable_state_directory_does_not_block_claims(self):
        def missing():
            raise FileNotFoundError('/nonexistent')
        self.reviewer.disk_free = missing
        self.deliver(pull_payload())
        with mock.patch.object(cr, 'log', self.logs.append):
            self.assertTrue(self.reviewer.work_once())
        self.assertTrue(any('free-space check' in line for line in self.logs))
        self.assertEqual(len(self.github.posted), 1)

    def test_default_measures_the_state_directory(self):
        reviewer = cr.Reviewer(self.config, self.github, self.store, self.git, self.codex)
        with mock.patch.object(cr.shutil, 'disk_usage', return_value=mock.Mock(free=7)) as usage:
            self.assertEqual(reviewer.disk_free(), 7)
        usage.assert_called_once_with(self.config.state_dir)


class WebhookFullDiskTests(Harness):
    def test_full_disk_on_accept_answers_503_without_a_traceback(self):
        def full(*_args):
            raise sqlite3.OperationalError(FULL)
        self.store.accept = full
        logs = []
        with mock.patch.object(cr, 'log', logs.append):
            self.assertEqual(self.deliver(pull_payload()), 503)
        self.assertTrue(any(FULL in line for line in logs))


class FakeReviewer:
    def __init__(self):
        self.stop = threading.Event()
        self.wake = threading.Event()

    def health(self):
        return 'ok'


def finished_thread():
    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join()
    return thread


class SupervisorTests(unittest.TestCase):
    def test_all_workers_dead_returns_a_restartable_failure_status(self):
        reviewer = FakeReviewer()
        notes, logs = [], []
        with mock.patch.object(cr, 'log', logs.append):
            status = cr.supervise(reviewer, [finished_thread(), finished_thread()], 0.01, notes.append)
        self.assertEqual(status, cr.WORKERS_DEAD)
        self.assertNotIn(cr.WORKERS_DEAD, (0, cr.SELF_CHECK_FAILED))
        self.assertTrue(reviewer.stop.is_set())
        self.assertTrue(any('all 2 review workers have exited' in line for line in logs))
        self.assertTrue(any(note.startswith('STATUS=') and 'exited' in note for note in notes))
        self.assertFalse(any('WATCHDOG=1' in note for note in notes))

    def test_live_workers_pet_the_watchdog_until_stop(self):
        reviewer = FakeReviewer()
        worker = threading.Thread(target=reviewer.stop.wait, daemon=True)
        worker.start()
        notes = []
        result = []
        thread = threading.Thread(target=lambda: result.append(cr.supervise(reviewer, [worker, finished_thread()],
                                                                               0.01, notes.append)))
        thread.start()
        self.assertTrue(wait_for(lambda: len([n for n in notes if 'WATCHDOG=1' in n]) >= 2))
        reviewer.stop.set()
        thread.join(5)
        self.assertEqual(result, [0])
        self.assertIn('STATUS=1/2 workers alive; ok', notes[0])

    def test_unit_restarts_on_failure_and_accepts_notifications(self):
        unit = (ROOT / 'deploy' / 'codex-review.service').read_text(encoding='utf-8')
        self.assertIn('Restart=on-failure\n', unit)
        self.assertIn(f'RestartPreventExitStatus={cr.SELF_CHECK_FAILED}\n', unit)
        self.assertIn('NotifyAccess=main\n', unit)
        self.assertIn('WatchdogSec=', unit)


class StartupTests(unittest.TestCase):
    def test_watchdog_is_petted_throughout_a_slow_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / 'secret'
            secret.write_text('s')
            secret.chmod(0o600)
            env = root / 'env'
            env.write_text(f'CODEX_REVIEW_APP_ID=42\nCODEX_REVIEW_PRIVATE_KEY={root}/key\n'
                           f'CODEX_REVIEW_WEBHOOK_SECRET_FILE={secret}\nCODEX_REVIEW_ACCOUNTS=o\n'
                           f'CODEX_REVIEW_AUTHORS=o\nCODEX_REVIEW_STATE_DIR={root}\n')
            order = []

            def slow_sweep():
                order.append(('sweep',))
                time.sleep(0.2)

            def check(*_a, **_k):
                order.append(('self_check',))
                time.sleep(0.2)
                return 'stub'
            with mock.patch.object(cr, 'sd_notify', lambda state: order.append(('notify', state))), \
                    mock.patch.object(cr, 'SUPERVISE_INTERVAL', 0.02), \
                    mock.patch.object(cr, 'Git') as git, \
                    mock.patch.object(cr, 'self_check', check), \
                    mock.patch.object(cr, 'serve') as serve:
                git.return_value.sweep.side_effect = slow_sweep
                self.assertEqual(cr.main(['--config', str(env)]), cr.SELF_CHECK_FAILED)
            serve.assert_not_called()
        steps = [step[0] for step in order]
        self.assertEqual(steps[0], 'notify')
        self.assertLess(steps.index('sweep'), steps.index('self_check'))
        during_sweep = steps[steps.index('sweep'):steps.index('self_check')].count('notify')
        after_check = steps[steps.index('self_check'):].count('notify')
        self.assertGreaterEqual(during_sweep, 3, steps)
        self.assertGreaterEqual(after_check, 3, steps)
        self.assertTrue(all(step[1].startswith('WATCHDOG=1\n') for step in order if step[0] == 'notify'))

    def test_heartbeat_stops_when_done(self):
        done = threading.Event()
        notes = []
        thread = threading.Thread(target=cr.heartbeat, args=(done, 'starting', 0.01, notes.append))
        thread.start()
        self.assertTrue(wait_for(lambda: len(notes) >= 3))
        done.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(notes[0], 'WATCHDOG=1\nSTATUS=starting')


class NotifyTests(unittest.TestCase):
    def test_without_notify_socket_is_a_no_op(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cr.sd_notify('STATUS=x')

    def test_sends_a_datagram_to_notify_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'n')
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.bind(path)
                sock.settimeout(2)
                with mock.patch.dict(os.environ, {'NOTIFY_SOCKET': path}):
                    cr.sd_notify('WATCHDOG=1\nSTATUS=2/2 workers alive')
                self.assertEqual(sock.recv(4096), b'WATCHDOG=1\nSTATUS=2/2 workers alive')

    def test_unreachable_socket_does_not_raise(self):
        with mock.patch.dict(os.environ, {'NOTIFY_SOCKET': '/nonexistent/notify'}):
            cr.sd_notify('STATUS=x')


if __name__ == '__main__':
    unittest.main()
