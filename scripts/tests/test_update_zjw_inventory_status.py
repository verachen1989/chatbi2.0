#!/usr/bin/env python3
"""Regression tests for permit-batch inventory coverage."""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from update_zjw_inventory_status import (  # noqa: E402
    aggregate_permit_coverage,
    build_permit_batch_specs,
    classify_failure_texts,
    match_permit_for_page,
    scrape_project,
)
import update_zjw_inventory_status as scraper  # noqa: E402


class PermitBatchModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.item = {
            "dashboardName": "嘉棠璟樾",
            "permits": ["京房售证字(2026)5号", "京房售证字(2026)53号"],
            "issueDates": ["2026-01-28", "2026-06-30"],
            "urls": [
                "http://example.test/detail?projectID=8228442",
                "http://example.test/detail?projectID=8157580",
            ],
        }

    def test_builds_permit_specs_without_using_url_order(self) -> None:
        specs = build_permit_batch_specs(self.item)

        self.assertEqual(
            [(spec["permit"], spec["issueDate"]) for spec in specs],
            [
                ("京房售证字(2026)5号", "2026-01-28"),
                ("京房售证字(2026)53号", "2026-06-30"),
            ],
        )
        self.assertEqual(
            [spec["detailUrls"] for spec in specs],
            [[], []],
        )

    def test_matches_detail_page_by_permit_text_not_url_order(self) -> None:
        page = "<td>预售许可证</td><td>京房售证字(2026)53号</td>"

        permit, reason = match_permit_for_page(
            page,
            ["京房售证字(2026)5号", "京房售证字(2026)53号"],
        )

        self.assertEqual(permit, "京房售证字(2026)53号")
        self.assertEqual(reason, "")

    def test_one_missing_batch_blocks_project_completion(self) -> None:
        summary = aggregate_permit_coverage(
            [
                {
                    "permit": "京房售证字(2026)53号",
                    "detailStatus": "complete",
                    "approvedSuites": 3,
                    "roomStatusTotal": 3,
                    "unknown": 0,
                    "buildingKeys": ["8228442|1"],
                },
                {
                    "permit": "京房售证字(2026)5号",
                    "detailStatus": "unavailable",
                    "approvedSuites": 0,
                    "roomStatusTotal": 0,
                    "unknown": 0,
                    "buildingKeys": [],
                    "error": "详情页返回空壳",
                },
            ],
            expected_total=440,
        )

        self.assertEqual(summary["coverageStatus"], "unavailable")
        self.assertEqual(summary["approvedSuites"], 3)
        self.assertEqual(summary["missingPermits"], ["京房售证字(2026)5号"])

    def test_all_batches_close_and_sum_to_project_total(self) -> None:
        summary = aggregate_permit_coverage(
            [
                {
                    "permit": "京房售证字(2026)53号",
                    "detailStatus": "complete",
                    "approvedSuites": 3,
                    "roomStatusTotal": 3,
                    "unknown": 0,
                    "buildingKeys": ["8228442|1"],
                },
                {
                    "permit": "京房售证字(2026)5号",
                    "detailStatus": "complete",
                    "approvedSuites": 437,
                    "roomStatusTotal": 437,
                    "unknown": 0,
                    "buildingKeys": ["8157580|1", "8157580|2"],
                },
            ],
            expected_total=440,
        )

        self.assertEqual(summary["coverageStatus"], "complete")
        self.assertEqual(summary["approvedSuites"], 440)
        self.assertEqual(summary["roomStatusTotal"], 440)
        self.assertEqual(summary["missingPermits"], [])

    def test_duplicate_building_key_blocks_completion(self) -> None:
        summary = aggregate_permit_coverage(
            [
                {
                    "permit": "京房售证字(2026)5号",
                    "detailStatus": "complete",
                    "approvedSuites": 10,
                    "roomStatusTotal": 10,
                    "unknown": 0,
                    "buildingKeys": ["same|1"],
                },
                {
                    "permit": "京房售证字(2026)53号",
                    "detailStatus": "complete",
                    "approvedSuites": 10,
                    "roomStatusTotal": 10,
                    "unknown": 0,
                    "buildingKeys": ["same|1"],
                },
            ],
            expected_total=20,
        )

        self.assertEqual(summary["coverageStatus"], "mismatch")
        self.assertIn("重复楼栋证据", summary["coverageNote"])

    def test_expected_total_is_required_for_complete(self) -> None:
        summary = aggregate_permit_coverage(
            [
                {
                    "permit": "京房售证字(2026)5号",
                    "detailStatus": "complete",
                    "approvedSuites": 10,
                    "roomStatusTotal": 10,
                    "unknown": 0,
                    "buildingKeys": ["8157580|1"],
                }
            ],
            expected_total=0,
        )

        self.assertEqual(summary["coverageStatus"], "partial")
        self.assertIn("expectedTotal", summary["coverageNote"])

    def test_retry_policy_separates_shell_network_and_metric_failures(self) -> None:
        self.assertEqual(classify_failure_texts(["页面过小(11912字节)，疑似空壳"]), ("shell", True))
        self.assertEqual(classify_failure_texts(["timed out"]), ("network", True))
        self.assertEqual(classify_failure_texts(["批准440套，解析0套，差额440套"]), ("metric", False))

    def test嘉棠双证_missing_main_batch_is_unavailable(self) -> None:
        valid_small_batch = (
            '<td id="项目名称">嘉棠家苑</td>'
            '<table><tr><td>1#住宅楼</td><td>3</td><td>100</td><td>在售</td><td>100</td>'
            '<td><a href="/eportal/ui?pageId=320833&salePermitId=8228442&buildingId=1">楼盘表</a></td></tr></table>'
            '<div>京房售证字(2026)53号</div>'
            # The production validity gate rejects short HTML as a shell page.
            # Keep this fixture large enough to exercise the permit matching and
            # batch aggregation logic rather than the size guard.
            + ("x" * 50000)
        )
        shell = "<html><body>此频道不存在</body></html>"
        status_counts = {status: 0 for status in scraper.STATUS_LABELS}
        status_counts.update({"available": 3, "total": 3})
        item = {
            "dashboardName": "嘉棠璟樾",
            "name": "嘉棠家苑",
            "permits": ["京房售证字(2026)5号", "京房售证字(2026)53号"],
            "issueDates": ["2026-01-28", "2026-06-30"],
            "urls": [
                "http://example.test/detail?projectID=8157580",
                "http://example.test/detail?projectID=8228442",
            ],
            "residentialTotal": 440,
        }

        def fake_fetch(url: str, timeout: int = 12) -> str:
            return valid_small_batch if "8228442" in url else shell

        with patch.object(scraper, "fetch_text", side_effect=fake_fetch), \
            patch.object(scraper, "fetch_building_status_checked", return_value=(status_counts, [])), \
            patch.object(scraper.time, "sleep", return_value=None):
            result = scrape_project(
                item,
                delay=0,
                timeout=1,
                max_workers=1,
                retry_attempts=0,
                retry_delay=0,
                retry_timeout_step=0,
            )

        self.assertEqual(result["coverageStatus"], "unavailable")
        self.assertIn("京房售证字(2026)5号", result["missingPermitBatches"])
        self.assertEqual(result["permitCoverage"][0]["approvedSuites"], 0)
        self.assertEqual(result["permitCoverage"][1]["approvedSuites"], 3)

    def test_hidden_building_discovery_is_audit_only(self) -> None:
        detail_url = "http://example.test/detail?projectID=8157580"
        detail_html = (
            '<td id="项目名称">测试项目</td>'
            '<table><tr><td>1#住宅楼</td><td>10</td><td>100</td><td>在售</td><td>100</td>'
            '<td><a href="/eportal/ui?pageId=320833&salePermitId=8157580&buildingId=1">楼盘表</a></td></tr></table>'
            '<div>京房售证字(2026)5号</div>'
            + ("x" * 50000)
        )
        status_counts = {status: 0 for status in scraper.STATUS_LABELS}
        status_counts.update({"available": 10, "total": 10})
        item = {
            "dashboardName": "测试项目",
            "name": "测试项目",
            "permits": ["京房售证字(2026)5号"],
            "urls": [detail_url],
            "residentialTotal": 20,
        }

        def fake_discovery(buildings, *_args):
            buildings["8157580|2"] = {
                "buildingName": "2#住宅楼",
                "salePermitId": "8157580",
                "buildingId": "2",
                "buildingKey": "8157580|2",
                "approvedSuites": 10,
                "url": "http://example.test/building/2",
                "sourceUrl": detail_url,
                "permitBatchKey": "京房售证字(2026)5号",
            }
            return [{"buildingId": "2", "buildingName": "2#住宅楼"}]

        with patch.object(scraper, "fetch_text", return_value=detail_html), \
            patch.object(scraper, "fetch_building_status_checked", return_value=(status_counts, [])), \
            patch.object(scraper, "discover_hidden_residential_buildings", side_effect=fake_discovery), \
            patch.object(scraper.time, "sleep", return_value=None):
            result = scrape_project(
                item,
                delay=0,
                timeout=1,
                max_workers=1,
                retry_attempts=0,
                retry_delay=0,
                retry_timeout_step=0,
            )

        self.assertEqual(result["coverageStatus"], "mismatch")
        self.assertEqual(result["approvedResidentialTotal"], 10)
        self.assertEqual(result["buildingCount"], 1)

    def test_successful_permit_batch_is_reused_on_next_project_retry(self) -> None:
        detail_url = "http://example.test/detail?projectID=8157580"
        detail_html = (
            '<td id="项目名称">缓存测试项目</td>'
            '<table><tr><td>1#住宅楼</td><td>10</td><td>100</td><td>在售</td><td>100</td>'
            '<td><a href="/eportal/ui?pageId=320833&salePermitId=8157580&buildingId=1">楼盘表</a></td></tr></table>'
            '<div>京房售证字(2026)5号</div>'
            + ("x" * 50000)
        )
        status_counts = {status: 0 for status in scraper.STATUS_LABELS}
        status_counts.update({"available": 10, "total": 10})
        item = {
            "dashboardName": "缓存测试项目",
            "name": "缓存测试项目",
            "permits": ["京房售证字(2026)5号"],
            "urls": [detail_url],
            "residentialTotal": 10,
        }

        with patch.object(scraper, "fetch_text", return_value=detail_html), \
            patch.object(scraper, "fetch_building_status_checked", return_value=(status_counts, [])), \
            patch.object(scraper, "discover_hidden_residential_buildings", return_value=[]), \
            patch.object(scraper.time, "sleep", return_value=None):
            first = scrape_project(
                item, delay=0, timeout=1, max_workers=1, retry_attempts=0,
                retry_delay=0, retry_timeout_step=0,
            )
        self.assertEqual(first["coverageStatus"], "complete")

        second_fetch = Mock(side_effect=AssertionError("成功批次不应在下一轮重新抓详情页"))
        second_status = Mock(return_value=(status_counts, []))
        with patch.object(scraper, "fetch_text", second_fetch), \
            patch.object(scraper, "fetch_building_status_checked", second_status), \
            patch.object(scraper, "discover_hidden_residential_buildings", return_value=[]), \
            patch.object(scraper.time, "sleep", return_value=None):
            second = scrape_project(
                item, delay=0, timeout=1, max_workers=1, retry_attempts=0,
                retry_delay=0, retry_timeout_step=0,
            )
        self.assertEqual(second["coverageStatus"], "complete")
        second_fetch.assert_not_called()
        second_status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
