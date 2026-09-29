import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import import_land_tracker_project_links as importer


class AutomaticMatchingTests(unittest.TestCase):
    def setUp(self):
        self.code = '京土储挂（通）[2025]041号'
        self.rows = [{'landCode': self.code, 'landName': '通州区FZX-0302-6017地块',
                      'projectName': '项目甲', 'district': '通州区', 'dealDate': '25/11/27'}]
        self.proof = {'kind': 'presale', 'landCode': '京土储挂(通)[2025]041号',
                      'name': '甲备案苑', 'permit': '京房售证字(2026)12号',
                      'developer': '北京甲房地产有限公司', 'date': '2026-03-12',
                      'location': '通州区FZX-0302-6017地块',
                      'url': 'http://bjjs.zjw.beijing.gov.cn/eportal/ui?projectID=123',
                      'valid': True, 'residential': True}
        self.evidence = {'records': {'京土储挂(通)[2025]041号': {'fields': {
            'firstPresale': {'status': 'confirmed', 'records': [self.proof]}}}}}
        self.project = {'record_id': 'recA', '去化表项目名': '项目甲', '住建委备案名': '甲备案苑',
                        '开发商': '北京甲房地产有限公司', '预售证': '京房售证字（2026）012号',
                        '地块关联状态': None, '关联地块': None, '地块关联依据': None}
        self.lands = [{'record_id': 'recLand', '地块编号': self.code}]

    def plan(self, projects=None, state=None):
        function = getattr(importer, 'plan_automatic_links', None)
        self.assertIsNotNone(function, 'automatic matching has not been implemented')
        return function(self.rows, self.lands, projects or [self.project], self.evidence, state or {})

    def test_exact_official_presale_and_legal_entity_confirm_without_inventing_code(self):
        result = self.plan()
        patch = result['updates']['recA']
        self.assertEqual(patch['地块关联状态'], ['已确认'])
        self.assertEqual(patch['关联地块'], [{'id': 'recLand'}])
        self.assertIn(self.proof['url'], patch['地块关联依据'])
        self.assertNotIn('projectCode', patch)

    def test_name_only_or_wrong_developer_never_auto_confirms(self):
        for changes in ({'预售证': None}, {'开发商': '北京乙房地产有限公司'}):
            with self.subTest(changes=changes):
                result = self.plan([dict(self.project, **changes)])
                self.assertEqual(result['updates']['recA']['地块关联状态'], ['需人工确认'])

    def test_invalid_or_stale_evidence_never_auto_confirms(self):
        for changes in ({'valid': False}, {'residential': False}, {'date': '2024-01-01'},
                        {'url': 'https://example.com/fake'}, {'landCode': '京土储挂(通)[2025]042号'}):
            with self.subTest(changes=changes):
                original = dict(self.proof)
                self.proof.update(changes)
                result = self.plan()
                self.assertNotEqual(result['updates']['recA']['地块关联状态'], ['已确认'])
                self.proof.clear()
                self.proof.update(original)
        self.evidence['records']['京土储挂(通)[2025]041号']['fields']['firstPresale']['status'] = 'not_reconfirmed'
        self.assertNotEqual(self.plan()['updates']['recA']['地块关联状态'], ['已确认'])

    def test_two_records_claiming_same_official_permit_go_to_review(self):
        second = dict(self.project, record_id='recB', **{'去化表项目名': '项目甲二期'})
        result = self.plan([self.project, second])
        self.assertEqual([p['地块关联状态'] for p in result['updates'].values()],
                         [['需人工确认'], ['需人工确认']])

    def test_distinct_projects_on_same_land_are_all_preserved(self):
        self.evidence['records']['京土储挂(通)[2025]041号']['fields']['firstPresale']['records'].append(
            dict(self.proof, name='乙备案苑', permit='京房售证字(2026)13号'))
        second = dict(self.project, record_id='recB', **{'去化表项目名': '项目乙',
                      '住建委备案名': '乙备案苑', '预售证': '京房售证字(2026)13号'})
        result = self.plan([self.project, second])
        self.assertEqual(len(result['updates']), 2)
        self.assertTrue(all(p['地块关联状态'] == ['已确认'] for p in result['updates'].values()))

    def test_phase_suffixes_are_not_removed_for_confirmation(self):
        project = dict(self.project, **{'预售证': '', '去化表项目名': '项目甲二期', '住建委备案名': '甲备案苑二期'})
        self.assertTrue(all(p['地块关联状态'] != ['已确认'] for p in self.plan([project])['updates'].values()))

    def test_abbreviated_parcel_is_not_treated_as_a_full_identifier(self):
        planning = dict(self.proof, kind='planning', name='通州区FZX-0302-6017住宅项目')
        self.evidence['records']['京土储挂(通)[2025]041号']['fields']['planningPermit'] = {
            'status': 'confirmed', 'records': [planning]}
        project = dict(self.project, **{'预售证': '', '住建委备案名': '0302-6017项目'})
        self.assertTrue(all(p['地块关联状态'] != ['已确认'] for p in self.plan([project])['updates'].values()))

    def test_full_parcel_and_official_legal_entity_can_confirm(self):
        planning = dict(self.proof, kind='planning', name='通州区FZX-0302-6017住宅项目')
        self.evidence['records']['京土储挂(通)[2025]041号']['fields']['planningPermit'] = {
            'status': 'confirmed', 'records': [planning]}
        project = dict(self.project, **{'预售证': '', '住建委备案名': '通州区FZX-0302-6017项目'})
        self.assertEqual(self.plan([project])['updates']['recA']['地块关联状态'], ['已确认'])

    def test_conflicting_permit_is_not_masked_by_another_unambiguous_permit(self):
        self.evidence['records']['京土储挂(通)[2025]041号']['fields']['firstPresale']['records'].append(
            dict(self.proof, permit='京房售证字(2026)13号'))
        second = dict(self.project, record_id='recB')
        first = dict(self.project, **{'预售证': '京房售证字(2026)12号 / 京房售证字(2026)13号'})
        self.assertEqual(self.plan([first, second])['updates']['recA']['地块关联状态'], ['需人工确认'])

    def test_confirmed_and_manually_edited_relations_are_not_overwritten(self):
        self.project.update({'地块关联状态': ['已确认'], '关联地块': [{'id': 'recLand'}], '地块关联依据': '人工确认'})
        self.assertEqual(self.plan()['updates'], {})
        self.project['地块关联状态'] = ['需人工确认']
        self.assertEqual(self.plan()['updates'], {})

    def test_reruns_are_idempotent_and_manual_revocation_stays_revoked(self):
        first = self.plan()
        applied = dict(self.project, **first['updates']['recA'])
        self.assertEqual(self.plan([applied], first['state'])['updates'], {})
        revoked = dict(applied, **{'关联地块': [], '地块关联状态': ['未关联']})
        self.assertEqual(self.plan([revoked], first['state'])['updates'], {})
        self.rows[0]['projectName'] = '项目甲更新名称'
        self.assertEqual(self.plan([revoked], first['state'])['updates'], {})

    def test_input_is_not_mutated(self):
        before = copy.deepcopy([self.rows, self.lands, self.project, self.evidence])
        self.plan()
        self.assertEqual([self.rows, self.lands, self.project, self.evidence], before)


if __name__ == '__main__':
    unittest.main()
