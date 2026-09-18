import copy
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import land_tracker_enrichment as e
import update_land_tracker_enrichment as cli

FIXTURES = Path(__file__).parent / "fixtures/land_tracker_enrichment"


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.row = {"landCode": "京土储挂（通）[2025]041号", "landName": "北京市通州区FZX-0302-6017地块R2二类居住用地", "district": "通州区", "bidder": "北京海铧房地产开发有限公司", "dealDate": "25/11/27"}
        self.record = {"kind": "planning", "name": "通州区FZX-0302-6017地块（1#住宅楼等16项）", "developer": self.row["bidder"], "district": "通州区", "location": "九棵树街道", "date": "2026-02-06", "permit": "2026规自（通）建字0004号", "url": "https://yewu.ghzrzyw.beijing.gov.cn/test", "valid": True, "residential": True, "landCode": e.code_key(self.row["landCode"]), "matchBasis": ["parcel", "district"]}

    def fixture(self, filename):
        return (FIXTURES / filename).read_text(encoding="utf-8")

    def test_parcel_siblings_and_boundaries(self):
        self.assertEqual(e.parcel_codes("HD00-1408-0034、0049、0040地块"), {"HD00-1408-0034", "HD00-1408-0049", "HD00-1408-0040"})
        self.assertNotEqual(e.parcel_codes("FZX-0302-6017地块"), e.parcel_codes("FZX-0302-6018地块"))
        self.assertEqual(e.parcel_codes("01-008、01-016、01-026-1、01-020地块"), {"01-008", "01-016", "01-026-1", "01-020"})
        self.assertEqual(e.parcel_codes("1606-640、1606-648地块"), {"1606-640", "1606-648"})
        self.assertNotIn("FZX-0302-6017", e.parcel_codes("FZX-0302-60170地块"))

    def test_exact_parcel_and_district_required(self):
        self.assertEqual(e.direct_match(self.record, [self.row])[0], e.code_key(self.row["landCode"]))
        record = dict(self.record, name="1#住宅楼", developer=self.row["bidder"])
        self.assertIsNone(e.direct_match(record, [self.row])[0])
        record = dict(self.record, district="", location="", name="FZX-0302-6017地块1#住宅楼")
        self.assertIsNone(e.direct_match(record, [self.row])[0])

    def test_multiple_land_matches_need_review(self):
        other = dict(self.row, landCode="京土储挂（通）[2026]027号", landName="通州区FZX-0302-6016地块")
        record = dict(self.record, name="通州区FZX-0302-6017、6016地块1#住宅楼")
        self.assertIsNone(e.direct_match(record, [self.row, other])[0])

    def test_district_official_short_names(self):
        for district, title in (("通州区", "北京城市副中心"), ("昌平区", "昌平新城"), ("大兴区", "大兴经济开发区")):
            row = dict(self.row, district=district)
            record = dict(self.record, name=title + "FZX-0302-6017地块建设方案", district="", location="")
            self.assertEqual(e.direct_match(record, [row])[0], self.record["landCode"])

    def test_residential_scope_not_land_use(self):
        for title in ("FZX-0302-6017地块R2二类居住用地（幼儿园）", "FZX-0302-6017地块R2二类居住用地（地下车库）", "1#住宅楼基坑土护降工程", "1#住宅楼售楼处装修工程"):
            self.assertFalse(e.residential_scope(title), title)
        self.assertTrue(e.residential_scope(self.record["name"]))

    def test_earliest_valid_date_and_provenance(self):
        late = dict(self.record, date="2026-03-02")
        invalid = dict(self.record, date="2026-01-01", valid=False)
        result, changes, review, proof = e.reconcile([self.row], [late, invalid, self.record], date(2026, 9, 18))
        self.assertEqual(result[0]["planningPermit"], "26/02/06")
        self.assertEqual(len(changes), 1)
        self.assertTrue(proof[e.code_key(self.row["landCode"])]["fields"]["planningPermit"]["records"])
        self.assertEqual(len(review), 1)

    def test_prior_to_deal_and_future_rejected(self):
        records = [dict(self.record, date="2024-01-01"), dict(self.record, date="2027-01-01")]
        result, changes, review, _ = e.reconcile([self.row], records, date(2026, 9, 18))
        self.assertEqual(changes, [])
        self.assertEqual(len(review), 2)

    def test_planning_preview_keeps_official_record_not_old_display(self):
        row = dict(self.row, planningPermit="26/01/05")
        record = dict(self.record, raw={"fzjg": "通州分局", "jianZhuGuiMo": "83354.849平方米"})
        result, _, _, _ = e.reconcile([row], [record], date(2026, 9, 18))
        preview = result[0]["fieldEvidence"]["planningPermit"]
        self.assertEqual(preview["status"], "conflict")
        self.assertEqual(preview["record"]["date"], "26/02/06")
        self.assertEqual(preview["record"]["name"], record["name"])
        self.assertEqual(preview["record"]["developer"], record["developer"])
        self.assertEqual(preview["record"]["issuer"], "通州分局")
        self.assertEqual(preview["url"], record["url"])
        self.assertEqual(result[0]["planningPermit"], "26/01/05")

    def test_empty_planning_evidence_has_no_invented_preview(self):
        preview = e.page_evidence("planningPermit", {"status": "legacy_unverified", "records": [], "value": "26/02/06"})
        self.assertNotIn("record", preview)
        self.assertEqual(preview["url"], "")

    def test_preserve_old_conflict_and_mark_it(self):
        row = dict(self.row, planningPermit="26/01/05")
        result, changes, review, proof = e.reconcile([row], [self.record], date(2026, 9, 18))
        self.assertEqual(result[0]["planningPermit"], "26/01/05")
        self.assertEqual(changes, [])
        self.assertEqual(proof[e.code_key(row["landCode"])]["fields"]["planningPermit"]["status"], "conflict")

    def test_official_alias_does_not_erase_marketing_name(self):
        row = dict(self.row, projectName="中海九树满和")
        rec = dict(self.record, kind="presale", name="满和苑")
        result, _, _, proof = e.reconcile([row], [rec], date(2026, 9, 18))
        self.assertEqual(result[0]["projectName"], row["projectName"])
        self.assertEqual(proof[e.code_key(row["landCode"])]["fields"]["projectName"]["value"], "满和苑")

    def test_brand_does_not_require_a_marketing_name(self):
        identity = dict(self.record, kind="identity", name="", brand="中海")
        presale = dict(self.record, kind="presale", name="满和苑")
        result, changes, _, proof = e.reconcile([self.row], [identity, presale], date(2026, 9, 18))
        self.assertEqual(result[0]["brand"], "中海")
        self.assertEqual(result[0]["projectName"], "满和苑")
        self.assertEqual(proof[identity["landCode"]]["fields"]["projectName"]["nameType"], "official")
        result, _, _, _ = e.reconcile([self.row], [identity], date(2026, 9, 18))
        self.assertNotIn("projectName", result[0])

    def test_manifest_can_confirm_brand_only(self):
        source = {"landCode": self.row["landCode"], "brand": "中海", "published": "2026-03-01", "requiredText": ["FZX-0302-6017", "中海"], "url": "https://www.shougang.com.cn/test"}
        client = SimpleNamespace(get=lambda url: "<article>中海 FZX-0302-6017</article>")
        report = {"errors": []}
        result = e.collect_identities(client, {"identities": [source]}, [self.row], report)
        self.assertEqual(report["errors"], [])
        self.assertEqual(result[0]["brand"], "中海")
        self.assertEqual(result[0]["name"], "")

    def test_repeat_run_is_idempotent(self):
        first = e.reconcile([self.row], [self.record], date(2026, 9, 18))
        second = e.reconcile(first[0], [self.record], date(2026, 9, 18), first[3])
        self.assertEqual(second[1], [])
        self.assertEqual(first[0], second[0])

    def test_unverified_legacy_values_stay_unverified_on_replay(self):
        row = dict(self.row, brand="历史品牌", projectName="历史案名")
        first = e.reconcile([row], [self.record], date(2026, 9, 18))
        second = e.reconcile(first[0], [self.record], date(2026, 9, 18), first[3])
        self.assertEqual(first[0], second[0])
        self.assertEqual(second[1], [])
        self.assertEqual(second[3][self.record["landCode"]]["fields"]["brand"]["status"], "legacy_unverified")

    def test_real_planning_uses_issue_date_not_publication(self):
        record = e.parse_planning(json.loads(self.fixture("planning.json")), "")[0]
        self.assertEqual(record["date"], "2026-02-06")
        self.assertTrue(record["residential"])

    def test_real_portal_lists_and_zero_results(self):
        self.assertEqual(e.parse_portal_list(self.fixture("construction-list.html"), "construction", e.ZJW)[1], 1)
        self.assertEqual(e.parse_portal_list(self.fixture("completion-empty.html"), "completion", e.ZJW)[1], 0)
        self.assertEqual(e.parse_portal_list(self.fixture("presale-list.html"), "presale", e.ZJW)[1], 3)
        with self.assertRaises(e.ValidationError):
            e.parse_portal_list("<html>登录/访问异常</html>", "presale", e.ZJW)

    def test_real_completion_record(self):
        records, total, _, _ = e.parse_portal_list(self.fixture("completion-list.html"), "completion", e.ZJW)
        record = next(r for r in records if "1606-640" in r["name"])
        self.assertEqual(record["date"], "2026-05-26")
        self.assertEqual(record["permit"], "0074石竣2026(建)0005号")
        self.assertTrue(record["residential"])
        self.assertGreaterEqual(total, 1)

    def test_real_plan_not_planned_start_or_stale_meta(self):
        entries, count = e.parse_plan_list(self.fixture("plan-list.html"), e.ZJW)
        self.assertEqual(count, 1)
        record = e.parse_plan(self.fixture("plan-detail.html"), entries[0])
        self.assertEqual(record["date"], "2026-02-14")
        self.assertEqual(record["developer"], self.row["bidder"])
        self.assertNotIn("6018", e.parcel_codes(record["name"]))

    def test_real_presale_residential_and_exact_permit_reference(self):
        entry = {"kind": "presale", "name": "樾序海苑", "permit": "京房售证字(2026)84号", "date": "2026-09-18", "url": e.ZJW}
        result = e.parse_presale(self.fixture("presale-detail.html"), entry)
        self.assertTrue(result["residential"])
        self.assertEqual(result["planningRefs"], ["2026规自(昌)建字0022号"])
        text = self.fixture("presale-detail.html").replace("13#住宅楼", "13#地下车库")
        self.assertFalse(e.parse_presale(text, entry)["residential"])

    def test_network_not_replaced_by_empty(self):
        with self.assertRaises(e.ValidationError):
            e.parse_planning({"code": "403", "data": []}, "")

    def test_presale_requires_all_references_and_same_developer(self):
        ref = e.normalized(self.record["permit"])
        presale = dict(self.record, planningRefs=[ref])
        links = {ref: [self.record]}
        self.assertEqual(e.presale_match(presale, links)[0], self.record["landCode"])
        self.assertIsNone(e.presale_match(dict(presale, developer="另一项目公司"), links)[0])
        self.assertIsNone(e.presale_match(dict(presale, planningRefs=[ref, "2026规自(通)建字0099号"]), links)[0])
        self.assertIsNone(e.presale_match(dict(presale, planningRefs=[]), links)[0])
        other = dict(self.record, landCode="京土储挂(通)[2026]027号")
        self.assertIsNone(e.presale_match(presale, {ref: [self.record, other]})[0])
        self.assertIsNone(e.presale_match(dict(presale, planningRefsComplete=False), links)[0])

    def test_unparsed_permit_references_are_not_ignored(self):
        self.assertTrue(e.complete_permit_refs("2026规自（通）建字0001号、2026规自（通）建字0002号"))
        self.assertFalse(e.complete_permit_refs("2026规自（通）建字0001号、0002号"))
        self.assertFalse(e.complete_permit_refs("2026规自（通）建字0001号，另有京规建字证号"))
        self.assertFalse(e.complete_permit_refs(""))
        self.assertTrue(e.complete_permit_refs("2025规自(丰)简建字6001号,2025规自(丰)建字0002号"))
        self.assertEqual(e.permit_keys("2025规字(海)建字0061号"), {"2025规字(海)建字0061号"})

    def test_ancillary_permit_can_link_but_not_set_residential_milestone(self):
        collector = e.Collector(None, [self.row], {})
        ancillary = dict(self.record, residential=False)
        collector.keep(ancillary)
        self.assertIn(e.normalized(self.record["permit"]), collector.planning_links)
        self.assertEqual(collector.developers, {})
        result, changes, _, _ = e.reconcile([self.row], [ancillary], date(2026, 9, 18))
        self.assertNotIn("planningPermit", result[0])
        self.assertEqual(changes, [])

    def test_invalid_legacy_date_not_labeled_official_candidate(self):
        result, _, _, _ = e.reconcile([dict(self.row, firstPresale="24/03/16")], [], date(2026, 9, 18))
        self.assertEqual(result[0]["fieldEvidence"]["firstPresale"]["status"], "conflict")
        self.assertEqual(result[0]["fieldEvidence"]["firstPresale"]["candidate"], "")

    def test_failed_query_rolls_back_partial_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = {"queries": [], "errors": []}
            collector = e.Collector(e.PublicClient(Path(tmp) / "raw"), [self.row], report)
            def failed():
                collector.keep(self.record)
                raise e.ValidationError("page 2 failed")
            collector.attempt("planning", "6017", failed)
            self.assertEqual(collector.records, {})
            self.assertEqual(collector.developers, {})
            self.assertEqual(collector.planning_links, {})
            self.assertEqual(len(report["errors"]), 1)
            self.assertEqual(report["queries"], [])

    def test_old_and_unrelated_plan_details_are_not_required(self):
        entries = [dict(self.record, kind="plan", url=e.ZJW + "/old", date="2015-07-01"),
                   dict(self.record, kind="plan", url=e.ZJW + "/unrelated", name="其他区FZX-0302-6018地块", district="", location="")]
        client = SimpleNamespace(get=lambda *args: "list")
        collector = e.Collector(client, [self.row], {})
        with patch.object(e, "parse_plan_list", return_value=(entries, 1)), patch.object(e, "parse_plan", side_effect=AssertionError("unrelated detail fetched")):
            collector.plans(self.row)
        self.assertEqual(len(collector.records), 1)
        self.assertIsNone(next(iter(collector.records.values()))["landCode"])

    def test_disappeared_source_is_not_still_confirmed(self):
        first = e.reconcile([self.row], [self.record], date(2026, 9, 18))
        result, changes, _, proof = e.reconcile(first[0], [], date(2026, 9, 18), first[3])
        self.assertEqual(result[0]["planningPermit"], "26/02/06")
        self.assertEqual(changes, [])
        self.assertEqual(proof[self.record["landCode"]]["fields"]["planningPermit"]["status"], "not_reconfirmed")

    def test_replay_does_not_silently_fall_back_to_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = e.PublicClient(Path(tmp) / "output", replay=Path(tmp) / "missing")
            with self.assertRaises(FileNotFoundError):
                client.get(e.ZJW + "/eportal/ui?pageId=307670")

    def test_unapproved_hosts_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = e.PublicClient(tmp)
            for url in ("https://example.com/", "file:///tmp/test", "https://user@bjjs.zjw.beijing.gov.cn/"):
                with self.assertRaises(e.ValidationError):
                    client.get(url)

    def test_failed_enrichment_leaves_production_unchanged(self):
        original = (cli.ROOT / cli.DASHBOARD).read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            page = root / cli.DASHBOARD
            page.parent.mkdir(parents=True)
            page.write_text(original, encoding="utf-8")
            proof = root / "data/land_tracker_enrichment.json"
            proof.parent.mkdir()
            proof.write_text('{"original":true}', encoding="utf-8")
            args = SimpleNamespace(root=root, page=None, report_dir=root / "reports", snapshot_dir=None, resume_dir=None, apply=True)
            with patch.object(cli, "enrich", side_effect=e.ValidationError("source unavailable")):
                with self.assertRaises(e.ValidationError):
                    cli.run(args)
            self.assertEqual(page.read_text(encoding="utf-8"), original)
            self.assertEqual(proof.read_text(), '{"original":true}')
            self.assertFalse((root / "reports/proposed.html").exists())

    def test_concurrent_page_edit_is_not_overwritten(self):
        original = (cli.ROOT / cli.DASHBOARD).read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            page = root / cli.DASHBOARD
            page.parent.mkdir(parents=True)
            page.write_text(original, encoding="utf-8")
            args = SimpleNamespace(root=root, page=None, report_dir=root / "reports", snapshot_dir=None, resume_dir=None, apply=True)
            def collected(rows, *unused):
                page.write_text(original + "\n<!-- concurrent edit -->", encoding="utf-8")
                return rows, {}, {"changes": [], "review": []}
            with patch.object(cli, "enrich", side_effect=collected):
                with self.assertRaisesRegex(e.ValidationError, "Page changed"):
                    cli.run(args)
            self.assertTrue(page.read_text(encoding="utf-8").endswith("<!-- concurrent edit -->"))
            self.assertFalse((root / "data/land_tracker_enrichment.json").exists())


if __name__ == "__main__":
    unittest.main()
