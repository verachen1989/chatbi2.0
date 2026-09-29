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


class LinkSyncTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('sync_land_tracker_project_links'), 'sync runner is not implemented')
        import sync_land_tracker_project_links
        return sync_land_tracker_project_links

    def test_directory_adds_only_missing_keys_and_keeps_existing_relations(self):
        sync = self.module()
        rows = [{'landCode': '京土储挂（通）[2025]041号', 'landName': '地块甲', 'district': '通州区', 'dealDate': '25/11/27'},
                {'landCode': '京土储挂（通）[2025]042号', 'landName': '地块乙', 'district': '通州区', 'dealDate': '25/12/01'}]
        existing = [{'record_id': 'rec1', '地块编号': rows[0]['landCode'], '地块名称': '地块甲',
                     '行政区': '通州区', '成交时间': '2025-11-27T00:00:00.000+08:00', '官方来源': '',
                     '关联项目': [{'id': 'project'}]}]
        create, updates = sync.directory_changes(rows, existing, {'records': {}})
        self.assertEqual(len(create), 1)
        self.assertEqual(create[0]['地块编号'], rows[1]['landCode'])
        self.assertEqual(updates, {})
        existing[0]['地块名称'] = '旧名称'
        self.assertEqual(sync.directory_changes(rows, existing, {'records': {}})[1], {'rec1': {'地块名称': '地块甲'}})

    def test_stale_future_or_partial_source_cannot_write(self):
        sync = self.module()
        now = datetime(2026, 9, 28, 8, tzinfo=timezone.utc)
        for data in ({'checkedAt': (now - timedelta(days=3)).isoformat(), 'collectionMode': 'live'},
                     {'checkedAt': (now + timedelta(days=1)).isoformat(), 'collectionMode': 'live'},
                     {'checkedAt': now.isoformat(), 'collectionMode': 'snapshot'}):
            with self.subTest(data=data), self.assertRaises(sync.ValidationError):
                sync.validate_freshness(data, now)
        sync.validate_freshness({'checkedAt': now.isoformat(), 'collectionMode': 'live'}, now)

    def test_preflight_stops_when_user_edits_record(self):
        sync = self.module()
        records = [{'record_id': 'recA', '开发商': '甲'}]
        with self.assertRaises(sync.ValidationError):
            sync.assert_unchanged(records, [{'record_id': 'recA', '开发商': '乙'}])
        sync.assert_unchanged(records, records)

    def test_failed_or_ignored_write_does_not_count_as_success(self):
        sync = self.module()
        with patch.object(sync, 'lark', return_value={'ignored_fields': ['关联地块']}), self.assertRaises(sync.ValidationError):
            sync.write_batch('cli', 'base', 'table', {'recA': {'关联地块': []}})

    def test_readback_rejects_silent_write_failure(self):
        sync = self.module()
        with self.assertRaises(sync.ValidationError):
            sync.verify_updates([{'record_id': 'recA', '地块关联状态': ['未关联']}],
                                {'recA': {'地块关联状态': ['已确认']}})
        sync.verify_updates([{'record_id': 'recA', '关联地块': [{'id': 'land', 'name': '地块'}]}],
                            {'recA': {'关联地块': [{'id': 'land'}]}})

    def test_runner_dry_run_then_apply_then_noop_and_failed_readback(self):
        sync = self.module()
        from test_land_tracker_matching import AutomaticMatchingTests
        fixture = AutomaticMatchingTests()
        fixture.setUp()
        fixture.rows[0].update(bidder='北京甲房地产有限公司', amount=1, floorPrice=10000)
        fixture.evidence.update(checkedAt=datetime.now(timezone.utc).isoformat(), collectionMode='live')
        source = '<strong id="tableCount">1 条</strong><script>const LAND_ROWS = ' + json.dumps(fixture.rows) + ';</script>'
        lands = [dict(fixture.lands[0], **{'地块名称': fixture.rows[0]['landName'], '行政区': '通州区',
                 '成交时间': '2025-11-27T00:00:00.000+08:00', '官方来源': ''})]
        projects = [copy.deepcopy(fixture.project)]
        writes = []

        def api(cli, command, *args):
            if command == '+url-resolve':
                return {'block_type': 'table', 'base_token': 'base', 'table_id': 'projects'}
            if command == '+field-get':
                return {'field': {'link_table': 'lands'}}
            if command == '+field-list':
                table = args[args.index('--table-id') + 1]
                names = sync.MATCH_FIELDS if table == 'projects' else sync.LAND_FIELDS
                fields = [{'name': name, 'type': 'datetime' if name == '成交时间' else 'text'} for name in names]
                for field in fields:
                    if field['name'] == '关联地块':
                        field.update(type='link', link_table='lands')
                    if field['name'] == '地块关联状态':
                        field.update(type='select', multiple=False, options=[{'name': n} for n in ['未关联', '已确认', '需人工确认']])
                return {'fields': fields}
            self.assertEqual(command, '+record-batch-update')
            payload = json.loads(args[args.index('--json') + 1])['update_records']
            writes.append(payload)
            projects[0].update(payload['recA'])
            return {}

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            page = root / 'index.html'
            page.write_text(source)
            args = SimpleNamespace(page=page, report_dir=root / 'reports', state=root / 'state.json',
                base_url='url', lark_cli='cli', refresh_source=True, apply=False)
            with (patch.object(sync, 'load_source', return_value=(source, fixture.evidence, {'records': {}}, 'revision')),
                    patch.object(sync, 'lark', side_effect=api),
                    patch.object(sync, 'fetch_records', side_effect=lambda cli, base, table, fields: copy.deepcopy(projects if table == 'projects' else lands))):
                sync.run(args)
                self.assertEqual(writes, [])
                self.assertEqual(page.read_text(), source)
                self.assertFalse(args.state.exists())
                args.apply = True
                sync.run(args)
                self.assertEqual(len(writes), 1)
                self.assertEqual(sync.read_rows(page.read_text())[0][0]['linkedProjects'], [{'projectName': '项目甲', 'projectCode': ''}])
                sync.run(args)
                self.assertEqual(len(writes), 1)
                before = page.read_text()
                args.state.unlink()
                projects[0] = copy.deepcopy(fixture.project)
                with patch.object(sync, 'write_batch', return_value=None), self.assertRaises(sync.ValidationError):
                    sync.run(args)
                self.assertFalse(args.state.exists())
                self.assertEqual(page.read_text(), before)


if __name__ == '__main__':
    unittest.main()
