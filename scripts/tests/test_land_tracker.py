import argparse
import copy
import json
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import update_land_tracker as land

FIXTURES = Path(__file__).parent / "fixtures/land_tracker"
TODAY = date(2026, 9, 15)


class LandTrackerTests(unittest.TestCase):
    def setUp(self):
        self.list_html = (FIXTURES / "index.html").read_text(encoding="utf-8-sig")
        self.detail = (FIXTURES / "5702538.html").read_text(encoding="utf-8-sig")
        self.entries = land.parse_list(self.list_html, land.INDEX_URL)[0]
        self.entry = next(e for e in self.entries if e["url"].endswith("5702538.html"))
        self.row = land.parse_detail(self.detail, self.entry, TODAY)[1]
        self.old = {field: "" for field in land.PROTECTED}
        self.old.update(self.row, seq=1, projectName="已核验项目", brand="人工品牌", planningPermit="26/09/10")

    def test_real_list_pagination_and_identifiers(self):
        entries, count, current, pages = land.parse_list(self.list_html, land.INDEX_URL)
        self.assertEqual((len(entries), count, current, pages), (10, 47, 1, 5))
        self.assertEqual(self.entry["projectId"], "S110000C008018182")

    def test_real_detail_amount_floor_price_and_dates(self):
        self.assertEqual(self.row["landCode"], "京土储挂（通）[2026]047号")
        self.assertEqual(self.row["dealDate"], "26/09/08")
        self.assertEqual(self.row["amount"], 9.962)
        self.assertEqual(self.row["floorPrice"], 17251.50)
        self.assertEqual(self.row["bidder"], "北京海港房地产开发有限公司")

    def test_commercial_excluded(self):
        entry = next(e for e in self.entries if e["url"].endswith("5702539.html"))
        page = (FIXTURES / "5702539.html").read_text(encoding="utf-8-sig")
        status, row, _ = land.parse_detail(page, entry, TODAY)
        self.assertEqual(status, "excluded")
        self.assertIsNone(row)

    def test_mixed_residential_and_ambiguous_use(self):
        self.assertEqual(land.classify_use("R2 二类居住用地、A334托幼用地"), "residential")
        self.assertEqual(land.classify_use("F2公建混合住宅用地"), "residential")
        self.assertEqual(land.classify_use("F3其他类多功能用地"), "review")
        self.assertEqual(land.classify_use("未知类别"), "review")

    def test_district_and_project_mismatch_fail(self):
        for field, value in (("district", "海淀区"), ("projectId", "S0000"), ("name", "其他地块")):
            with self.subTest(field=field), self.assertRaises(land.ValidationError):
                land.parse_detail(self.detail, dict(self.entry, **{field: value}), TODAY)

    def test_missing_transaction_never_uses_listing_date(self):
        with self.assertRaises(land.ValidationError):
            land.parse_detail(self.detail.replace("成交时间：", "暂未成交："), self.entry, TODAY)

    def test_future_dates_rejected(self):
        with self.assertRaises(land.ValidationError):
            land.parse_detail(self.detail, self.entry, date(2026, 9, 1))

    def test_units_ambiguous_areas_and_zero(self):
        self.assertEqual(land.number("建筑控制规模 ≤57,745.72平方米", "平方米"), Decimal("57745.72"))
        for value in ("0平方米", "-2平方米", "2至3平方米", "10000平方米，其中住宅8000平方米", "10公顷"):
            with self.subTest(value=value), self.assertRaises(land.ValidationError):
                land.number(value, "平方米")
        with self.assertRaises(land.ValidationError):
            land.number("10亿元", "万元")

    def test_normalized_code_merges_and_preserves_enrichment(self):
        incoming = dict(self.row, landCode="京土储挂 (通) [2026]047号", bidder="官方更正竞得人")
        rows, added, updated = land.merge_rows([self.old], [incoming], TODAY)
        self.assertEqual(len(rows), 1)
        self.assertFalse(added)
        self.assertEqual(updated[0]["fields"]["bidder"]["after"], "官方更正竞得人")
        for field in land.PROTECTED:
            self.assertEqual(rows[0][field], self.old[field])

    def test_duplicate_codes_fail(self):
        with self.assertRaises(land.ValidationError):
            land.merge_rows([self.old, self.old], [self.row], TODAY)
        with self.assertRaises(land.ValidationError):
            land.merge_rows([self.old], [self.row, self.row], TODAY)

    def test_shortened_official_title_retains_complete_title(self):
        warnings = []
        rows, _, updates = land.merge_rows([self.old], [dict(self.row, landName=self.row["landName"][:-1])], TODAY, warnings)
        self.assertEqual(rows[0]["landName"], self.old["landName"])
        self.assertFalse(updates)
        self.assertTrue(warnings)

    def test_system_tls_fallback_verifies_certificates(self):
        import requests
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            fetcher = land.Fetcher(Path(temp), interval=0)
            with patch.object(fetcher.session, "get", side_effect=requests.exceptions.SSLError("EC handshake")), patch.object(land.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"official", b"")) as curl:
                self.assertEqual(fetcher.get(land.INDEX_URL), "official")
                self.assertEqual(fetcher.get(land.INDEX_URL), "official")
                self.assertTrue(fetcher.use_curl)
                self.assertNotIn("--insecure", curl.call_args[0][0])
                self.assertNotIn("-k", curl.call_args[0][0])

    def test_historical_conflict_fails(self):
        for changed in (dict(self.row, amount=100), dict(self.row, floorPrice=100), dict(self.row, dealDate="26/09/09")):
            with self.assertRaises(land.ValidationError):
                land.merge_rows([self.old], [changed], TODAY)

    def test_old_rows_retained_and_sort_descending(self):
        historic = dict(self.old, landCode="京土储挂（通）[2025]001号", dealDate="25/02/01")
        rows, added, _ = land.merge_rows([historic], [self.row], TODAY)
        self.assertEqual([r["dealDate"] for r in rows], ["26/09/08", "25/02/01"])
        self.assertEqual(len(added), 1)
        self.assertEqual([r["seq"] for r in rows], [1, 2])

    def test_repeat_run_is_idempotent(self):
        rows = land.merge_rows([self.old], [self.row], TODAY)[0]
        again, added, updated = land.merge_rows(rows, [self.row], TODAY)
        self.assertEqual(rows, again)
        self.assertEqual((added, updated), ([], []))

    def test_login_empty_page_and_offsite_link_fail(self):
        for page in ("<h1>登录</h1>", self.list_html.replace("/zpgcjzd/20260908/5702538.html", "https://example.com/")):
            with self.assertRaises(land.ValidationError):
                land.parse_list(page, land.INDEX_URL)

    def test_pagination_coverage_failure(self):
        class IncompleteFetcher:
            def get(inner, url, suffix=""):
                return self.list_html
        report = {"evidence": [], "errors": []}
        with self.assertRaises(land.ValidationError):
            land.crawl(IncompleteFetcher(), TODAY, report)

    def test_safe_html_embedding_roundtrip(self):
        source = '<strong id="tableCount">1 条</strong><script>const LAND_ROWS = ' + json.dumps([self.old]) + ';</script>'
        row = dict(self.old, bidder='</script><img onerror="bad()">')
        output = land.embed_rows(source, [row])
        self.assertEqual(land.read_rows(output)[0], [row])
        self.assertEqual(output.count("</script>"), 1)

    def test_failure_leaves_production_files_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            page = root / land.DASHBOARD
            page.parent.mkdir()
            source = '<strong id="tableCount">1 条</strong><script>const LAND_ROWS = ' + json.dumps([self.old]) + ';</script>'
            page.write_text(source)
            args = argparse.Namespace(root=root, report_dir=root / "report", snapshot_dir=None, apply=True)
            with patch.object(land.Fetcher, "get", side_effect=land.ValidationError("network failed")):
                self.assertEqual(land.run(args), 1)
            self.assertEqual(page.read_text(), source)
            self.assertFalse((root / land.SOURCES).exists())
            self.assertEqual(json.loads((root / "report/report.json").read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
