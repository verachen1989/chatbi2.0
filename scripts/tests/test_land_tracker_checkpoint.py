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
import land_tracker_enrichment as enrichment


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('land_tracker_checkpoint'),
                             'Validated same-day checkpoint store is missing')
        self.module = importlib.import_module('land_tracker_checkpoint')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime.fromisoformat('2026-09-24T12:00:00+08:00')
        self.store = self.module.CheckpointStore(self.root / 'cache', clock=lambda: self.now)

    def test_only_committed_query_can_be_resumed(self):
        self.store.stage('a' * 64, 'official', self.now)
        self.assertIsNone(self.store.load('a' * 64))
        self.store.finish(True)
        self.assertEqual(self.store.load('a' * 64), ('official', self.now))

    def test_failed_query_and_partial_files_are_not_reused(self):
        self.store.stage('a' * 64, 'page 1', self.now)
        self.store.finish(False)
        self.assertIsNone(self.store.load('a' * 64))
        self.store.directory.mkdir(parents=True, exist_ok=True)
        (self.store.directory / ('b' * 64 + '.json')).write_text('{broken')
        self.assertIsNone(self.store.load('b' * 64))

    def test_stale_future_cross_day_or_tampered_payload_is_rejected(self):
        for observed in (self.now - timedelta(hours=7), self.now + timedelta(minutes=1),
                         self.now.replace(hour=0) - timedelta(seconds=1)):
            self.store.stage('a' * 64, 'official', observed)
            self.store.finish(True)
            self.assertIsNone(self.store.load('a' * 64))
        self.store.stage('a' * 64, 'official', self.now)
        self.store.finish(True)
        path = self.store.directory / ('a' * 64 + '.json')
        payload = json.loads(path.read_text())
        payload['text'] = 'changed'
        path.write_text(json.dumps(payload))
        self.assertIsNone(self.store.load('a' * 64))

    def test_shanghai_midnight_invalidates_even_recent_checkpoint(self):
        self.now = self.now.replace(hour=0, minute=30)
        self.store.stage('a' * 64, 'old day', self.now - timedelta(hours=1))
        self.store.finish(True)
        self.assertIsNone(self.store.load('a' * 64))

    def test_malformed_metadata_is_a_cache_miss(self):
        path = self.store.path('a' * 64)
        path.parent.mkdir(parents=True)
        for payload in (None, [], {}, {'fetchedAt': None}, {'fetchedAt': '2026-09-24T12:00:00'},
                        {'fetchedAt': self.now.isoformat(), 'schemaVersion': 1, 'text': 42}):
            path.write_text(json.dumps(payload))
            self.assertIsNone(self.store.load('a' * 64))

    def test_partial_query_is_not_promoted_by_collector(self):
        client = enrichment.PublicClient(self.root / 'raw', delay=0, checkpoint_dir=self.root / 'cache')
        report = {'queries': [], 'errors': []}
        collector = enrichment.Collector(client, [], report)
        def query():
            client.get(enrichment.ZJW + '/page1')
            raise enrichment.ValidationError('page 2 failed')
        with patch.object(enrichment.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=b'page 1\n200')):
            collector.attempt('construction', 'company', query)
        self.assertEqual(report['queries'], [])
        self.assertEqual(len(report['errors']), 1)
        self.assertEqual(list((self.root / 'cache').glob('*.json')), [])
        self.assertEqual(client.memory, {})

    def test_unverified_resume_cannot_be_relabelled_as_checkpoint(self):
        for mode in ('replay', 'resume'):
            with self.assertRaises(enrichment.ValidationError):
                enrichment.PublicClient(self.root / 'raw', checkpoint_dir=self.root / 'cache', **{mode: self.root})

    def test_client_reuses_validated_response_with_original_time(self):
        url = enrichment.ZJW + '/test'
        client = enrichment.PublicClient(self.root / 'raw1', delay=0,
                                         checkpoint_dir=self.root / 'cache')
        with patch('land_tracker_checkpoint.now', return_value=self.now), patch.object(
                enrichment.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=b'official\n200')):
            client.get(url)
            client.finish_query(True)
            second = enrichment.PublicClient(self.root / 'raw2', checkpoint_dir=self.root / 'cache')
            with patch.object(enrichment.subprocess, 'run', side_effect=AssertionError('Network should not be used')):
                self.assertEqual(second.get(url), 'official')
            second.finish_query(True)
        self.assertEqual(second.reused_requests, 1)
        self.assertEqual(second.oldest_observation, self.now)

    def test_enrichment_payload_preserves_checkpoint_provenance(self):
        def query(collector):
            collector.client.get(enrichment.ZJW + '/test')
            collector.client.finish_query(True)
            return []
        with patch.object(enrichment.Collector, 'run', query), patch.object(
                enrichment.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=b'official\n200')):
            with patch('land_tracker_checkpoint.now', return_value=self.now):
                first = enrichment.enrich([], self.root, self.root / 'first', self.now.date(), checkpoint_dir=self.root / 'cache')[1]
            with patch('land_tracker_checkpoint.now', return_value=self.now + timedelta(hours=1)):
                second = enrichment.enrich([], self.root, self.root / 'second', self.now.date(), checkpoint_dir=self.root / 'cache')[1]
        self.assertEqual(first['collectionMode'], 'live')
        self.assertEqual(second['collectionMode'], 'live_checkpoint')
        self.assertEqual(second['checkedAt'], first['checkedAt'])
        self.assertNotEqual(second['completedAt'], first['completedAt'])
        self.assertEqual(second['reusedRequests'], 1)

    def test_failed_query_does_not_reuse_its_response_on_next_attempt(self):
        client = enrichment.PublicClient(self.root / 'raw', delay=0, checkpoint_dir=self.root / 'cache')
        with patch.object(enrichment.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=b'bad\n200')):
            self.assertEqual(client.get(enrichment.ZJW + '/test'), 'bad')
            client.finish_query(False)
        with patch.object(enrichment.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=b'good\n200')):
            self.assertEqual(client.get(enrichment.ZJW + '/test'), 'good')


if __name__ == '__main__':
    unittest.main()
