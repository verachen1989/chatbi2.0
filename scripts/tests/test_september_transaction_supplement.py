"""Check the September 9 import of July/August residential transactions."""

from decimal import Decimal
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import update_dashboard_august_from_db_export as dashboard


class SeptemberTransactionSupplementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html, cls.data, _ = dashboard.load_dashboard(ROOT / "index.html")
        cls.details = dashboard.load_transaction_details(ROOT / "transaction_details.js")
        cls.projects = {
            p["project"]: p for p in dashboard.base.all_projects(cls.data)
        }

    def test_supplement_months_close_to_source(self):
        expected = [
            ("半壁店地块 保利熙瑞", "26年7月", 43, "7240.28", "82592.0128"),
            ("半壁店地块 保利熙瑞", "26年8月", 33, "5105.41", "57248.8543"),
            ("北清润府", "26年8月", 1, "93.35", "408.8534"),
        ]
        for name, month, suites, area, amount in expected:
            with self.subTest(project=name, month=month):
                project = self.projects[name]
                key = dashboard.base.normalize_name(name)
                detail = self.details["months"][month]["projects"][key]
                rows = detail["rows"]
                self.assertEqual(len(rows), suites)
                self.assertEqual(len({dashboard.transaction_detail_house_key(r) for r in rows}), suites)
                self.assertEqual(sum(Decimal(str(r["area"])) for r in rows), Decimal(area))
                self.assertEqual(sum(Decimal(str(r["totalWan"])) for r in rows), Decimal(amount))
                self.assertEqual(detail["summary"], dashboard.summarize_rows(rows))
                self.assertEqual(project["monthly"][month], {
                    "suites": suites,
                    "area": float(area),
                    "amount": float(amount),
                    "price": round(Decimal(amount) * 10000 / Decimal(area)),
                })

    def test_existing_project_identity_and_official_match_are_preserved(self):
        poly = self.projects["半壁店地块 保利熙瑞"]
        north = self.projects["北清润府"]
        self.assertEqual(poly["id"], 53)
        self.assertEqual(north["id"], "zjw-launch-2026)67")
        self.assertEqual(north["officialProjectName"], "润樾雅苑")
        self.assertIn("京房售证字(2026)67号", north["summaryPresalePermit"])
        self.assertEqual(north["group"], "北部组团")
        self.assertEqual(north["plate"], "北七家")
        self.assertEqual(north["officialResidentialTotal"], 598)
        self.assertNotIn("earliestLaunchDate", north)
        self.assertNotIn("plannedHouseholds", north)
        self.assertEqual(poly["historicalTransactionSoldSuites"], 76)
        self.assertEqual(north["historicalTransactionSoldSuites"], 1)
        for project in [poly, north]:
            self.assertEqual(project["status"], "在售")
            self.assertEqual(project["historicalTransactionDuplicateRows"], 0)
            self.assertIn("北清润府&保利熙瑞.xlsx", project["transactionDetailSource"])


if __name__ == "__main__":
    unittest.main()
