import importlib.util
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "replace_transaction_history_from_xlsx.py"


def load_module():
    spec = importlib.util.spec_from_file_location("replace_transaction_history", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class ReplaceTransactionHistoryTests(unittest.TestCase):
    def test_month_label_uses_natural_month(self):
        module = load_module()
        self.assertEqual(module.month_label(datetime(2026, 8, 9)), "26年8月")

    def test_replacement_starts_at_july_2025(self):
        module = load_module()
        self.assertFalse(module.is_replacement_month("25年6月"))
        self.assertTrue(module.is_replacement_month("25年7月"))
        self.assertTrue(module.is_replacement_month("26年8月"))

    def test_detail_summary_is_recomputed_from_rows(self):
        module = load_module()
        rows = [
            {"area": 100, "totalWan": 500},
            {"area": 120, "totalWan": 720},
        ]
        self.assertEqual(
            module.summarize_rows(rows),
            {"suites": 2, "area": 220, "amountWan": 1220, "avgPrice": 55455},
        )

    def test_trade_amount_is_converted_from_yuan_to_wan(self):
        module = load_module()
        self.assertEqual(module.trade_amount_wan(59317429), 5931.7429)

    def test_remaining_suites_closes_against_historical_sales(self):
        module = load_module()
        self.assertEqual(module.remaining_suites(446, 174), 272)
        self.assertIsNone(module.remaining_suites(100, 101))
        self.assertIsNone(module.remaining_suites(None, 10))

    def test_normalized_aliases_match_traditional_and_punctuation(self):
        module = load_module()
        self.assertEqual(module.normalize_name("中海·九树满和"), module.normalize_name("中海玖樹满和"))

    def test_official_new_launch_item_can_be_promoted_for_detail_matching(self):
        module = load_module()
        item = {
            "officialProjectName": "长河花园",
            "district": "昌平区",
            "group": "北部组团",
            "plate": "小汤山",
            "lat": 40.17,
            "lng": 116.39,
            "permits": ["京房售证字(2026)7号"],
            "issueDates": ["2026-01-30"],
            "residentialTotal": 16,
            "detailUrls": ["https://example.test/project"],
        }
        project = module.build_official_project(item, ["25年7月", "26年8月"])
        self.assertEqual(project["project"], "长河花园")
        self.assertEqual(project["officialResidentialTotal"], 16)
        self.assertEqual(project["monthly"]["26年8月"]["suites"], 0)


if __name__ == "__main__":
    unittest.main()
