#!/usr/bin/env python3
"""Regression tests for confirmed transaction-detail duplicate repairs."""

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from validate_dashboard_month_details import load_dashboard, load_detail_source


ROW_FIELDS = (
    "date",
    "permit",
    "building",
    "unit",
    "room",
    "propertyType",
    "layout",
    "area",
    "unitPrice",
    "totalWan",
    "sourceProject",
)


def row_signature(row: dict) -> tuple:
    return tuple(str(row.get(field, "")).strip() for field in ROW_FIELDS)


class ConfirmedTransactionRepairTests(unittest.TestCase):
    def test_authoritative_details_have_no_exact_duplicates(self) -> None:
        _, details = load_detail_source(ROOT / "transaction_details.js")
        checked_rows = 0
        for month, month_data in details["months"].items():
            for project_name, project in month_data["projects"].items():
                rows = project["rows"]
                checked_rows += len(rows)
                with self.subTest(month=month, project=project_name):
                    self.assertEqual(len({row_signature(row) for row in rows}), len(rows))
                    self.assertEqual(project["summary"]["suites"], len(rows))
        self.assertEqual(checked_rows, 18936)

    def test_observation_phases_keep_separate_authoritative_rows(self) -> None:
        dashboard = load_dashboard(ROOT / "index.html")
        _, details = load_detail_source(ROOT / "transaction_details.js")
        projects = {
            project["project"]: project
            for project in dashboard.get("projects", []) + dashboard.get("launchProjects", [])
        }
        phase_one = projects["建发金茂观宸"]
        phase_two = projects["槐新02地块 建发金茂观宸二期"]

        for month in [f"26年{number}月" for number in range(1, 7)]:
            with self.subTest(month=month):
                month_projects = details["months"][month]["projects"]
                expected_one = month_projects.get("建发金茂观宸", {}).get("summary", {}).get("suites", 0)
                expected_two = month_projects.get("槐新02地块建发金茂观宸二期", {}).get("summary", {}).get("suites", 0)
                self.assertEqual(phase_one["monthly"][month]["suites"], expected_one)
                self.assertEqual(phase_two["monthly"][month]["suites"], expected_two)


if __name__ == "__main__":
    unittest.main()
