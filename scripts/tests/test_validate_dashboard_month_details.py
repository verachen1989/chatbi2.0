#!/usr/bin/env python3
"""Regression tests for dashboard DATA extraction."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from validate_dashboard_month_details import load_dashboard, load_details, main, norm


def write_assignment(path: Path, variable: str, data: dict, suffix: str = "") -> None:
    payload = json.dumps(data, ensure_ascii=False)
    path.write_text(f"window.{variable} = {payload};\n{suffix}", encoding="utf-8")


class NormTests(unittest.TestCase):
    def test_ascii_case_is_ignored(self) -> None:
        self.assertEqual(norm("国贤府PARK"), norm("国贤府park"))

    def test_normalizes_yun_variant(self) -> None:
        self.assertEqual(norm("橒"), norm("云"))

    def test_normalizes_tai_variant(self) -> None:
        self.assertEqual(norm("臺"), norm("台"))


class LoadDashboardTests(unittest.TestCase):
    def assert_loads_before(self, following_constants: str) -> None:
        expected = {"projects": [], "launchProjects": []}
        html = f"const DATA = {expected!r};\n{following_constants}\n".replace("'", '"')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.html"
            path.write_text(html, encoding="utf-8")
            self.assertEqual(load_dashboard(path), expected)

    def test_data_before_project_metadata_overrides(self) -> None:
        self.assert_loads_before(
            "const PROJECT_METADATA_OVERRIDES = {};\n"
            "const LAUNCH_OFFICIAL_INVENTORY_OVERRIDES = {};"
        )

    def test_data_before_launch_official_inventory_overrides(self) -> None:
        self.assert_loads_before("const LAUNCH_OFFICIAL_INVENTORY_OVERRIDES = {};")

    def test_data_before_default_periods(self) -> None:
        self.assert_loads_before("const DEFAULT_PERIODS = [];")


class LoadDetailsTests(unittest.TestCase):
    def test_merges_sources_with_page_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_path = root / "base.js"
            launch_path = root / "launch.js"
            june_path = root / "june.js"
            july_path = root / "july.js"
            write_assignment(
                base_path,
                "TRANSACTION_DETAILS",
                {
                    "months": {
                        "26年1月": {
                            "projects": {
                                "base": {"rows": [{"source": "base"}]},
                                "shared": {"rows": [{"source": "base"}]},
                            },
                            "aliases": {"base-alias": "base", "shared-alias": "shared"},
                        }
                    }
                },
                "window.MAY_TRANSACTION_DETAILS = {};",
            )
            write_assignment(
                launch_path,
                "NEW_LAUNCH_TRANSACTION_DETAILS",
                {
                    "months": {
                        "26年1月": {
                            "projects": {
                                "launch": {"rows": [{"source": "launch"}]},
                                "shared": {"rows": [{"source": "launch"}, {}]},
                            },
                            "aliases": {
                                "launch-alias": "launch",
                                "shared-alias": "launch",
                            },
                        },
                        "26年6月": {
                            "projects": {
                                "june-launch": {"rows": [{}]},
                                "june-shared": {"rows": [{}, {}]},
                            },
                            "aliases": {"june-launch-alias": "june-launch"},
                        },
                    }
                },
            )
            write_assignment(
                june_path,
                "JUNE_TRANSACTION_DETAILS",
                {
                    "month": "2026年6月",
                    "projects": {
                        "june-base": {"rows": [{}]},
                        "june-shared": {"rows": [{"source": "june"}]},
                    },
                    "aliases": {"june-base-alias": "june-base"},
                },
            )
            write_assignment(
                july_path,
                "JULY_TRANSACTION_DETAILS",
                {
                    "month": "2026年7月",
                    "projects": {"july": {"rows": [{}]}},
                    "aliases": {"july-alias": "july"},
                },
            )

            details = load_details([base_path, launch_path, june_path, july_path])

        january = details["months"]["26年1月"]
        self.assertEqual(set(january["projects"]), {"base", "shared", "launch"})
        self.assertEqual(january["projects"]["shared"]["rows"], [{"source": "base"}])
        self.assertEqual(january["aliases"]["launch-alias"], "launch")
        self.assertEqual(january["aliases"]["shared-alias"], "shared")
        june = details["months"]["26年6月"]
        self.assertEqual(set(june["projects"]), {"june-base", "june-shared", "june-launch"})
        self.assertEqual(june["projects"]["june-shared"]["rows"], [{"source": "june"}])
        self.assertEqual(june["aliases"]["june-launch-alias"], "june-launch")
        self.assertEqual(set(details["months"]["26年7月"]["projects"]), {"july"})

    def test_maps_current_jul_variable_to_july(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "july.js"
            write_assignment(
                path,
                "JUL_TRANSACTION_DETAILS",
                {"month": "2026年7月", "projects": {"july": {"rows": [{}]}}},
            )

            details = load_details([path])

        self.assertEqual(set(details["months"]["26年7月"]["projects"]), {"july"})

    def test_cli_accepts_multiple_detail_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            html_path = root / "index.html"
            base_path = root / "base.js"
            launch_path = root / "launch.js"
            dashboard = {
                "projects": [
                    {
                        "project": "launch",
                        "monthly": {"26年1月": {"suites": 1}},
                    }
                ],
                "launchProjects": [],
            }
            html_path.write_text(
                f"const DATA = {json.dumps(dashboard)};\nconst DEFAULT_PERIODS = [];",
                encoding="utf-8",
            )
            write_assignment(
                base_path,
                "TRANSACTION_DETAILS",
                {"months": {"26年1月": {"projects": {}, "aliases": {}}}},
                "window.MAY_TRANSACTION_DETAILS = {};",
            )
            write_assignment(
                launch_path,
                "NEW_LAUNCH_TRANSACTION_DETAILS",
                {
                    "months": {
                        "26年1月": {
                            "projects": {"launch": {"rows": [{}]}},
                            "aliases": {},
                        }
                    }
                },
            )
            argv = [
                "validate_dashboard_month_details.py",
                "--html",
                str(html_path),
                "--details",
                str(base_path),
                str(launch_path),
                "--months",
                "26年1月",
            ]
            with patch.object(sys, "argv", argv):
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    main()

    def test_cli_reports_base_launch_and_total_project_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            html_path = root / "index.html"
            details_path = root / "details.js"
            dashboard = {
                "projects": [{"project": "base-1"}, {"project": "base-2"}],
                "launchProjects": [{"project": "launch-1"}],
            }
            html_path.write_text(
                f"const DATA = {json.dumps(dashboard)};\nconst DEFAULT_PERIODS = [];",
                encoding="utf-8",
            )
            write_assignment(
                details_path,
                "TRANSACTION_DETAILS",
                {"months": {}},
            )
            argv = [
                "validate_dashboard_month_details.py",
                "--html",
                str(html_path),
                "--details",
                str(details_path),
                "--months",
                "26年1月",
            ]
            output = StringIO()
            with patch.object(sys, "argv", argv):
                with redirect_stdout(output), redirect_stderr(StringIO()):
                    main()

        result = json.loads(output.getvalue())
        self.assertEqual(result["projects"], 2)
        self.assertEqual(result["baseProjects"], 2)
        self.assertEqual(result["launchProjects"], 1)
        self.assertEqual(result["totalProjects"], 3)
        self.assertEqual(result["checkedNonZeroProjectMonths"], 0)


if __name__ == "__main__":
    unittest.main()
