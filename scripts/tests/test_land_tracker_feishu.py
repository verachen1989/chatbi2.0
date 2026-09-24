import copy
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import land_tracker_feishu as sync
from update_land_tracker import ValidationError


class FeishuFeedTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.fromisoformat('2026-09-20T12:00:00+08:00')
        self.row = dict(seq=1, landCode='京土储挂（石）[2026]020号',
                        landName='石景山区住宅地块', projectName='璟序家园',
                        district='石景山区', plate='', dealDate='26/05/28',
                        bidder='建设单位', brand='', amount=22.0, floorPrice=30437,
                        planningPermit='', constructionPlan='26/06/10',
                        constructionPermit='', firstPresale='26/09/19', firstCompletion='',
                        fieldEvidence={'firstPresale': {'status': 'confirmed',
                            'url': 'https://zjw.beijing.gov.cn/test', 'permit': '85号'}})
        self.proof = {'collectionMode': 'live', 'checkedAt': '2026-09-19T13:29:02+08:00'}

    def feed(self, rows=None, proof=None):
        return sync.build_feed(rows or [self.row], proof or self.proof, {'records': {}}, self.now)

    def test_dates_are_shanghai_milliseconds_and_numbers_stay_numeric(self):
        feed = self.feed()
        row = feed['rows'][0]
        self.assertEqual(row['dealDate'], 1779897600000)
        self.assertIsInstance(row['amount'], float)
        presale = next(p for p in row['patches'] if p['field'] == 'firstPresale')
        self.assertEqual(presale['dateValue'], 1789747200000)
        self.assertEqual(presale['dateText'], '2026/09/19 00:00:00')
        self.assertEqual(feed['expiresAt'] - feed['checkedAt'], 48 * 3600 * 1000)
        self.assertEqual(feed['updates'][0]['landCode'], row['landCode'])
        self.assertEqual(len(feed['updates']), len(row['patches']))

    def test_allowlist_and_empty_values_cannot_clear_manual_or_optional_fields(self):
        self.row['人工备注'] = 'do not publish'
        self.row['secret'] = 'do not publish'
        row = self.feed()['rows'][0]
        self.assertNotIn('do not publish', json.dumps(row))
        self.assertNotIn('planningPermit', [p['field'] for p in row['patches']])
        self.assertNotIn('brand', [p['field'] for p in row['patches']])
        self.assertIn('firstPresale', [p['field'] for p in row['patches']])

    def test_normalized_duplicate_and_empty_snapshot_fail_closed(self):
        duplicate = dict(self.row, landCode='京土储挂 (石) [2026]020号')
        with self.assertRaises(ValidationError):
            self.feed([self.row, duplicate])
        with self.assertRaises(ValidationError):
            sync.build_feed([], self.proof, {}, self.now)

    def test_replay_future_or_stale_sources_are_rejected(self):
        for proof in [dict(self.proof, collectionMode='replay'),
                      dict(self.proof, checkedAt=(self.now + timedelta(days=1)).isoformat()),
                      dict(self.proof, checkedAt=(self.now - timedelta(days=3)).isoformat())]:
            with self.subTest(proof=proof), self.assertRaises(ValidationError):
                self.feed(proof=proof)

    def test_native_loop_limit_cannot_silently_truncate_optional_updates(self):
        rows = [dict(self.row, landCode=f'京土储挂（石）[2026]{n:04d}号') for n in range(400)]
        with self.assertRaises(ValidationError):
            self.feed(rows)

    def test_same_day_checkpoint_keeps_oldest_observation_not_finish_time(self):
        proof = dict(self.proof, collectionMode='live_checkpoint', checkpointVersion=1,
                     reusedRequests=10, checkedAt='2026-09-20T09:00:00+08:00',
                     completedAt=self.now.isoformat())
        feed = self.feed(proof=proof)
        self.assertEqual(feed['checkedAtText'], proof['checkedAt'])
        for changed in (dict(proof, checkedAt=self.proof['checkedAt']),
                        dict(proof, checkpointVersion=0), dict(proof, reusedRequests=0)):
            with self.assertRaises(ValidationError):
                self.feed(proof=changed)

    def test_sorted_snapshot_is_deterministic_and_preserves_matching_code(self):
        old = dict(self.row, landCode='京土储挂（丰） [2026]021号', dealDate='26/04/01')
        one = self.feed([old, self.row])
        two = self.feed([self.row, old])
        self.assertEqual(one, two)
        self.assertEqual(one['rows'][1]['landCode'], old['landCode'])
        self.assertEqual(one['rows'][1]['seq'], 2)

    def test_official_links_are_readable_and_legacy_dates_not_claimed_confirmed(self):
        self.row['planningPermit'] = '26/06/09'
        self.row['fieldEvidence']['planningPermit'] = {
            'status': 'confirmed', 'url': 'https://yewu.ghzrzyw.beijing.gov.cn/esSearchDetail/x'}
        row = self.feed()['rows'][0]
        source = next(p['textValue'] for p in row['patches'] if p['field'] == 'sources')
        self.assertNotIn('esSearchDetail', source)
        self.assertIn('jsgcgh.html', source)
        self.assertIn('历史保留', row['notes'])

    def test_workflow_has_daily_schedule_key_lookup_and_no_manual_writes(self):
        body = sync.build_workflow(self.feed(), '地块跟踪明细', 'ou_owner', '2026-09-20 15:00',
                                   'fldExpiry', 'fldDate', 'fldChecked')
        steps = {s['id']: s for s in body['steps']}
        self.assertEqual(steps['timer']['data']['rule'], 'DAILY')
        self.assertEqual(steps['timer']['data']['start_time'], '2026-09-20 15:00')
        self.assertEqual(steps['find']['data']['filter_info']['conditions'][0]['field_name'], '地块编号')
        self.assertEqual(steps['rows']['data']['loop_mode'], 'end')
        self.assertEqual(steps['patches']['data']['loop_mode'], 'end')
        self.assertEqual(steps['rows']['next'], 'patches')
        self.assertEqual(steps['patches']['data']['data'][0]['value'], '$.http.body.updates')
        self.assertEqual(steps['patches']['next'], 'state_end')
        self.assertEqual(steps['upsert']['type'], 'SwitchBranch')
        self.assertEqual(steps['upsert']['children']['links'][2]['to'], 'duplicate')
        self.assertEqual(len(steps['patch_field']['data']['child_branch_list']), len(sync.OPTIONAL))
        schema = steps['valid']['data']['condition']['conditions'][0]['conditions'][1]
        self.assertEqual(schema['right_value'], [{'value_type': 'number', 'value': -1}])
        self.assertEqual(steps['deal_date']['type'], 'SetRecordAction')
        self.assertEqual(steps['add']['data']['field_values'][4]['value'][0]['value'], '$.deal_date.fldDate')
        expiry = steps['fresh']['data']['condition']['conditions'][0]['conditions'][0]
        self.assertEqual(expiry['left_value']['value'], '$.state_loaded.fldExpiry')
        self.assertEqual(expiry['right_value'], [{'value_type': 'ref', 'value': '$.timer.scheduleTime'}])
        for step in steps.values():
            if step.get('next'):
                self.assertIn(step['next'], steps)
            for edge in step.get('children', {}).get('links', []):
                self.assertIn(edge['to'], steps)
            for field in step['data'].get('field_values', []):
                self.assertNotEqual(field['field_name'], '人工备注')
        self.assertEqual(steps['update']['data']['field_values'][-1]['value'],
                         [{'value_type': 'date', 'value': 'now'}])
        incoming = {}
        for step in steps.values():
            targets = [step.get('next')] + [edge['to'] for edge in step.get('children', {}).get('links', [])]
            for target in filter(None, targets):
                incoming[target] = incoming.get(target, 0) + 1
        self.assertTrue(all(count == 1 for count in incoming.values()), incoming)


if __name__ == '__main__':
    unittest.main()
