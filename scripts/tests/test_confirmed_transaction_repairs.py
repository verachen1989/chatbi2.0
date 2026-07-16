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
    def test_guoxianfu_park_april_and_may_have_no_exact_duplicates(self) -> None:
        _, details = load_detail_source(ROOT / "new_launch_transaction_details.js")
        expected = {"26年4月": 360, "26年5月": 71}

        for month, expected_rows in expected.items():
            project = details["months"][month]["projects"]["国贤府park"]
            rows = project["rows"]
            with self.subTest(month=month):
                self.assertEqual(len(rows), expected_rows)
                self.assertEqual(len({row_signature(row) for row in rows}), expected_rows)
                self.assertEqual(project["summary"]["suites"], expected_rows)

    def test_observation_phase_two_june_rows_have_single_owner(self) -> None:
        _, details = load_detail_source(ROOT / "june_transaction_details.js")

        self.assertNotIn("建发金茂观宸", details["projects"])
        phase_two = details["projects"]["槐新02地块建发金茂观宸二期"]
        self.assertEqual(len(phase_two["rows"]), 29)
        self.assertEqual(phase_two["summary"]["suites"], 29)

    def test_observation_phase_one_does_not_reuse_phase_two_2026_sales(self) -> None:
        dashboard = load_dashboard(ROOT / "index.html")
        projects = {
            project["project"]: project
            for project in dashboard.get("projects", []) + dashboard.get("launchProjects", [])
        }
        phase_one = projects["建发金茂观宸"]
        phase_two = projects["槐新02地块 建发金茂观宸二期"]

        for month in [f"26年{number}月" for number in range(1, 7)]:
            with self.subTest(month=month):
                self.assertEqual(phase_one["monthly"][month]["suites"], 0)
                self.assertGreater(phase_two["monthly"][month]["suites"], 0)
        self.assertEqual(phase_one["janAprMatchedName"], "建发金茂·观宸")
        self.assertEqual(phase_one["junMatchedName"], "建发金茂·观宸")


if __name__ == "__main__":
    unittest.main()
