import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import import_land_tracker_project_links as links


class ProjectLinksTests(unittest.TestCase):
    def setUp(self):
        self.code = "京土储挂（通）[2025]041号"
        self.rows = [{"landCode": self.code, "projectName": "历史名", "planningPermit": "26/02/06"}]
        self.lands = [{"record_id": "recLand", "地块编号": self.code}]
        self.project = {"record_id": "recA", "去化表项目名": "项目甲", "克而瑞项目名": "别名",
                        "住建委备案名": "备案名", "projectCode": "P3852",
                        "地块关联状态": ["已确认"], "关联地块": [{"id": "recLand"}]}

    def test_each_project_keeps_its_own_code_and_missing_code_stays_blank(self):
        second = dict(self.project, record_id="recB", projectCode="P9002", **{"去化表项目名": "项目乙"})
        third = dict(self.project, record_id="recC", projectCode=None, **{"去化表项目名": "项目丙"})
        original = copy.deepcopy(self.rows)
        result = links.merge_project_links(self.rows, self.lands, [self.project, second, third])
        self.assertEqual(result[0]["linkedProjects"], [
            {"projectName": "项目甲", "projectCode": "P3852"},
            {"projectName": "项目乙", "projectCode": "P9002"},
            {"projectName": "项目丙", "projectCode": ""}])
        self.assertEqual(result[0]["planningPermit"], "26/02/06")
        self.assertEqual(self.rows, original)

    def test_unconfirmed_name_matches_are_not_used(self):
        for status in (None, [], ["未关联"], ["需人工确认"]):
            project = dict(self.project, **{"地块关联状态": status, "是否确认匹配": "确认匹配"})
            self.assertEqual(links.merge_project_links(self.rows, self.lands, [project])[0]["linkedProjects"], [])

    def test_removed_links_do_not_survive_full_refresh(self):
        self.rows[0].update(linkedProjects=[{"projectName": "旧关联", "projectCode": "P0001"}], projectCode="P0001")
        result = links.merge_project_links(self.rows, self.lands, [])
        self.assertEqual(result[0]["linkedProjects"], [])
        self.assertNotIn("projectCode", result[0])

    def test_unknown_land_and_ambiguous_codes_stop_import(self):
        bad_link = dict(self.project, **{"关联地块": [{"id": "recWrong"}]})
        duplicate = dict(self.project, record_id="recB", **{"去化表项目名": "另一个项目"})
        for projects in ([bad_link], [self.project, duplicate], [dict(self.project, projectCode="x&projectCode=P3852")]):
            with self.subTest(projects=projects), self.assertRaises(links.ValidationError):
                links.merge_project_links(self.rows, self.lands, projects)

    def test_one_project_can_link_multiple_lands_without_alias_guessing(self):
        other = "京土储挂（通）[2025]042号"
        self.rows.append({"landCode": other, "projectName": "同名二期"})
        self.lands.append({"record_id": "recLand2", "地块编号": other})
        self.project["关联地块"].append({"id": "recLand2"})
        result = links.merge_project_links(self.rows, self.lands, [self.project])
        self.assertEqual([row["linkedProjects"][0]["projectCode"] for row in result], ["P3852", "P3852"])

    def test_page_decoder_rejects_incomplete_or_changed_schema(self):
        page = {"fields": ["projectCode"], "record_id_list": ["recA"], "data": [["P3852"]]}
        self.assertEqual(links.decode_records(page, ["projectCode"]), [{"record_id": "recA", "projectCode": "P3852"}])
        for bad in (dict(page, fields=[]), dict(page, data=[]), dict(page, data=[["P3852", "extra"]])):
            with self.subTest(bad=bad), self.assertRaises(links.ValidationError):
                links.decode_records(bad, ["projectCode"])

    def test_pagination_is_complete_and_changed_revision_stops_import(self):
        first = {"fields": ["projectCode"], "record_id_list": ["recA"], "data": [["P3852"]], "rev": 5, "has_more": True}
        last = dict(first, record_id_list=["recB"], data=[["P9002"]], has_more=False)
        with patch.object(links, "lark", side_effect=[first, last]):
            records = links.fetch_records("lark-cli", "base", "table", ["projectCode"])
        self.assertEqual([row["projectCode"] for row in records], ["P3852", "P9002"])
        for invalid in (dict(last, rev=6), first, dict(last, has_more=None)):
            with self.subTest(invalid=invalid), patch.object(links, "lark", side_effect=[first, invalid]), self.assertRaises(links.ValidationError):
                links.fetch_records("lark-cli", "base", "table", ["projectCode"])


if __name__ == "__main__":
    unittest.main()
