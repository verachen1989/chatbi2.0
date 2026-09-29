import copy
import importlib.util
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ProjectCodeTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('sync_land_tracker_project_codes'), 'Code sync is not implemented')
        import sync_land_tracker_project_codes
        return sync_land_tracker_project_codes

    def project(self, **changes):
        row = {'record_id': 'recA', '去化表项目名': '项目甲', '克而瑞项目名': '甲·花园',
               '住建委备案名': '甲苑', 'projectCode': '', '关联地块': [{'id': 'landA'}],
               '地块关联状态': ['已确认'], '开发商': '企业甲'}
        row.update(changes)
        return row

    def source(self, *pairs):
        return {'datasetCode': '6404', 'datasourceCode': 'sr_ads_tdda',
                'retrievedAt': datetime.now(timezone.utc).isoformat(), 'complete': True,
                'records': [{'projectName': n, 'projectCode': c} for n, c in pairs]}

    def test_unique_alias_repeated_months_updates_only_code(self):
        sync = self.module()
        row = self.project()
        before = copy.deepcopy(row)
        plan = sync.plan_codes([row], self.source(('甲花园', 'UUID_A'), ('甲花园', 'UUID_A')), {})
        self.assertEqual(plan['updates'], {'recA': {'projectCode': 'UUID_A'}})
        self.assertEqual(row, before)

    def test_conflicting_aliases_and_similar_phases_never_pick_first(self):
        sync = self.module()
        source = self.source(('项目甲', 'A'), ('甲花园', 'B'))
        plan = sync.plan_codes([self.project()], source, {})
        self.assertEqual(plan['updates'], {})
        self.assertEqual(plan['items'][0]['decision'], 'ambiguous')
        self.assertEqual(sync.plan_codes([self.project()], self.source(('项目甲二期', 'C')), {})['updates'], {})

    def test_unconfirmed_and_manual_codes_preserved(self):
        sync = self.module()
        rows = [self.project(projectCode='MANUAL'), self.project(record_id='recB', 地块关联状态=['需人工确认'])]
        self.assertEqual(sync.plan_codes(rows, self.source(('项目甲', 'A')), {})['updates'], {})

    def test_shared_code_is_not_assigned_to_two_records(self):
        sync = self.module()
        rows = [self.project(), self.project(record_id='recB')]
        self.assertEqual(sync.plan_codes(rows, self.source(('项目甲', 'A')), {})['updates'], {})
        rows[1]['projectCode'] = 'A'
        self.assertEqual(sync.plan_codes(rows, self.source(('项目甲', 'A')), {})['updates'], {})

    def test_manual_clear_is_not_refilled(self):
        sync = self.module()
        plan = sync.plan_codes([self.project()], self.source(('项目甲', 'A')), {'recA': {'lastWrittenCode': 'A'}})
        self.assertEqual(plan['updates'], {})
        self.assertEqual(plan['items'][0]['decision'], 'preserved_manual_clear')

    def test_bad_stale_partial_or_wrong_source_rejected(self):
        sync = self.module()
        valid = self.source(('项目甲', 'A'))
        for changes in ({'complete': False}, {'datasetCode': '19800'}, {'records': []},
                        {'records': [None]},
                        {'retrievedAt': (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()},
                        {'retrievedAt': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
                        {'records': [{'projectName': '项目甲', 'projectCode': 'bad&code'}]}):
            with self.subTest(changes=changes), self.assertRaises(sync.ValidationError):
                sync.plan_codes([self.project()], dict(valid, **changes), {})

    def test_readback_checks_other_fields_and_missing_records(self):
        sync = self.module()
        before = [self.project()]
        after = [self.project(projectCode='A')]
        sync.verify_result(before, after, {'recA': {'projectCode': 'A'}})
        with self.assertRaises(sync.ValidationError):
            sync.verify_result(before, [self.project(projectCode='A', 开发商='错误企业')], {'recA': {'projectCode': 'A'}})
        with self.assertRaises(sync.ValidationError):
            sync.verify_result(before, [], {'recA': {'projectCode': 'A'}})

    def test_runner_dry_run_apply_noop_and_uncertain_readback(self):
        sync = self.module()
        rows = [{'seq': 1, 'landCode': '京土储挂（通）[2025]041号', 'landName': '甲地块',
                 'projectName': '旧名', 'dealDate': '25/11/27'}]
        html = '<strong id="tableCount">1 条</strong><script>const LAND_ROWS = ' + json.dumps(rows) + ';</script>'
        lands = [{'record_id': 'landA', '地块编号': rows[0]['landCode']}]
        projects = [self.project()]
        writes = []
        uncertain = False

        def api(cli, command, *args):
            if command == '+url-resolve':
                return {'block_type': 'table', 'base_token': 'base', 'table_id': 'projects'}
            if command == '+field-list':
                fields = [{'name': k, 'type': 'text'} for k in projects[0] if k != 'record_id']
                for f in fields:
                    if f['name'] == '关联地块':
                        f.update(type='link', link_table='lands')
                    elif f['name'] == '地块关联状态':
                        f.update(type='select', multiple=False)
                return {'fields': fields}
            self.assertEqual(command, '+record-batch-update')
            payload = json.loads(args[args.index('--json') + 1])['update_records']
            self.assertEqual(payload, {'recA': {'projectCode': 'A'}})
            writes.append(payload)
            projects[0]['projectCode'] = 'A'
            if uncertain:
                raise sync.ValidationError('Response lost after remote write')
            return {}

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            page = root / 'index.html'
            page.write_text(html)
            source = root / 'source.json'
            source.write_text(json.dumps(self.source(('项目甲', 'A'))))
            args = SimpleNamespace(page=page, source=source, report_dir=root / 'reports', state=root / 'state.json',
                                   base_url='url', lark_cli='cli', apply=False, init_state=True)
            with patch.object(sync, 'lark', side_effect=api), patch.object(sync, 'fetch_records',
                    side_effect=lambda cli, base, table, fields: copy.deepcopy(projects if table == 'projects' else lands)):
                sync.run(args)
                self.assertEqual(writes, [])
                self.assertEqual(page.read_text(), html)
                self.assertFalse(args.state.exists())
                args.apply = True
                sync.run(args)
                self.assertEqual(len(writes), 1)
                latest = json.loads((args.report_dir / 'latest.json').read_text())
                self.assertTrue((Path(latest['journal']) / 'after.json').exists())
                self.assertEqual(sync.read_rows(page.read_text())[0][0]['linkedProjects'], [{'projectName': '项目甲', 'projectCode': 'A'}])
                args.init_state = False
                sync.run(args)
                self.assertEqual(len(writes), 1)
                projects[0]['projectCode'] = ''
                sync.run(args)
                self.assertEqual(len(writes), 1, 'Manual clear was overwritten')
                args.state.unlink()
                args.init_state = True
                uncertain = True
                before_page = page.read_text()
                with self.assertRaises(sync.ValidationError):
                    sync.run(args)
                self.assertEqual(page.read_text(), before_page)
                self.assertTrue(json.loads(args.state.read_text())['pending'])
                args.init_state = False
                uncertain = False
                sync.run(args)
                self.assertEqual(len(writes), 2, 'Uncertain write was replayed instead of read back')
                self.assertNotIn('pending', json.loads(args.state.read_text()))

    def test_missing_state_requires_explicit_initialization(self):
        sync = self.module()
        with TemporaryDirectory() as tmp, self.assertRaises(sync.ValidationError):
            sync.load_state(Path(tmp) / 'missing.json', {'base': 'base', 'table': 'table'}, False)

    def test_state_for_another_table_rejected_even_with_init(self):
        sync = self.module()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            path.write_text(json.dumps({'version': 1, 'context': {'table': 'old'}, 'projects': {}}))
            with self.assertRaises(sync.ValidationError):
                sync.load_state(path, {'table': 'new'}, True)

    def test_partial_write_cannot_be_marked_verified(self):
        sync = self.module()
        before = [self.project(), self.project(record_id='recB')]
        partial = [self.project(projectCode='A'), self.project(record_id='recB')]
        with self.assertRaises(sync.ValidationError):
            sync.verify_result(before, partial, {'recA': {'projectCode': 'A'}, 'recB': {'projectCode': 'B'}})


if __name__ == '__main__':
    unittest.main()
