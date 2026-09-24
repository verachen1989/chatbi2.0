import importlib
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import update_land_tracker as transaction


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('land_tracker_recovery'), 'Recovery runner is missing')
        self.module = importlib.import_module('land_tracker_recovery')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime.fromisoformat('2026-09-24T12:00:00+08:00')

    def test_scheduled_recovery_skips_only_valid_same_day_success(self):
        feed = dict(status='ready', schemaVersion=1, recordCount=1, rows=[{}],
                    checkedAtText=self.now.isoformat(), expiresAt=int((self.now + timedelta(hours=48)).timestamp() * 1000))
        self.assertFalse(self.module.should_collect('schedule', feed, self.now))
        for event in ('push', 'workflow_dispatch'):
            self.assertTrue(self.module.should_collect(event, feed, self.now))
        for changed in (dict(feed, status='failed'), dict(feed, rows=[]),
                        dict(feed, checkedAtText=(self.now - timedelta(days=1)).isoformat()),
                        dict(feed, checkedAtText=(self.now + timedelta(hours=1)).isoformat()),
                        dict(feed, checkedAtText='broken'), {}):
            self.assertTrue(self.module.should_collect('schedule', changed, self.now))

    def run_attempts(self, states):
        seen, sleeps = [], []
        def fake_run(args):
            seen.append(args.report_dir)
            args.report_dir.mkdir(parents=True)
            state = states[len(seen) - 1]
            (args.report_dir / 'report.json').write_text(json.dumps(state))
            if state['status'] == 'passed':
                (args.report_dir / 'proposed.html').write_text('complete snapshot')
            return 0 if state['status'] == 'passed' else 1
        args = SimpleNamespace(root=self.root, report_dir=self.root / 'reports', apply=False)
        with patch.object(self.module, 'run_transaction', side_effect=fake_run), patch.object(self.module.time, 'sleep', side_effect=sleeps.append):
            code = self.module.collect(args)
        return code, seen, sleeps

    def test_network_error_retries_after_delay_then_publishes_candidate(self):
        code, seen, sleeps = self.run_attempts([dict(status='failed', retryable=True), dict(status='passed')])
        self.assertEqual((code, len(seen), sleeps), (0, 2, [60]))
        self.assertEqual((self.root / 'reports/proposed.html').read_text(), 'complete snapshot')
        self.assertTrue((seen[0] / 'report.json').exists())

    def test_validation_conflict_is_not_blindly_retried(self):
        code, seen, sleeps = self.run_attempts([dict(status='failed', retryable=False)])
        self.assertEqual((code, len(seen), sleeps), (1, 1, []))
        self.assertFalse((self.root / 'reports/proposed.html').exists())

    def test_transport_retries_are_bounded(self):
        code, seen, sleeps = self.run_attempts([dict(status='failed', retryable=True)] * 3)
        self.assertEqual((code, len(seen), sleeps), (1, 3, [60, 180]))

    def test_transport_classification_does_not_retry_business_validation(self):
        self.assertFalse(transaction.is_transient(transaction.ValidationError('historical conflict')))
        self.assertTrue(transaction.is_transient(transaction.requests.exceptions.Timeout('slow')))
        self.assertTrue(transaction.is_transient(transaction.SourceUnavailable('curl timeout')))
        self.assertFalse(transaction.is_transient(transaction.requests.exceptions.SSLError('certificate')))

    def test_real_transaction_report_classifies_transport_failure(self):
        import shutil
        page = self.root / transaction.DASHBOARD
        page.parent.mkdir(parents=True)
        shutil.copyfile(transaction.ROOT / transaction.DASHBOARD, page)
        args = SimpleNamespace(root=self.root, report_dir=self.root / 'reports', snapshot_dir=None, apply=False)
        with patch.object(transaction.Fetcher, 'get', side_effect=transaction.SourceUnavailable('curl timeout')):
            self.assertEqual(transaction.run(args), 1)
        report = json.loads((args.report_dir / 'report.json').read_text())
        self.assertTrue(report['retryable'])
        self.assertFalse((args.report_dir / 'proposed.html').exists())

    def test_later_network_failure_does_not_hide_earlier_validation_error(self):
        import shutil
        page = self.root / transaction.DASHBOARD
        page.parent.mkdir(parents=True)
        shutil.copyfile(transaction.ROOT / transaction.DASHBOARD, page)
        def failed(fetcher, today, report):
            report['errors'].append({'error': 'invalid amount', 'retryable': False})
            raise transaction.SourceUnavailable('final list timeout')
        args = SimpleNamespace(root=self.root, report_dir=self.root / 'reports', snapshot_dir=None, apply=False)
        with patch.object(transaction, 'crawl', side_effect=failed):
            self.assertEqual(transaction.run(args), 1)
        self.assertFalse(json.loads((args.report_dir / 'report.json').read_text())['retryable'])


if __name__ == '__main__':
    unittest.main()
