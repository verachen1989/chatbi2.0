#!/usr/bin/env python3
"""Validate dashboard monthly suites against transaction-detail row counts."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


DATA_RE = re.compile(
    r"const DATA = (.*?);\n(?:const PROJECT_METADATA_OVERRIDES|const LAUNCH_OFFICIAL_INVENTORY_OVERRIDES|const DEFAULT_PERIODS)",
    re.S,
)
DETAIL_ASSIGNMENT_RE = re.compile(
    r"window\.(TRANSACTION_DETAILS|NEW_LAUNCH_TRANSACTION_DETAILS|"
    r"JUNE_TRANSACTION_DETAILS|JULY_TRANSACTION_DETAILS|JUL_TRANSACTION_DETAILS)\s*=\s*"
)
SINGLE_MONTH_VARIABLES = {
    "JUNE_TRANSACTION_DETAILS": "26年6月",
    "JULY_TRANSACTION_DETAILS": "26年7月",
    "JUL_TRANSACTION_DETAILS": "26年7月",
}

CHAR_MAP = str.maketrans(
    {
        "·": "",
        "•": "",
        "﹒": "",
        ".": "",
        " ": "",
        "　": "",
        "樹": "树",
        "玖": "九",
        "澐": "云",
        "橒": "云",
        "雲": "云",
        "臺": "台",
        "灣": "湾",
        "鳴": "鸣",
        "號": "号",
        "鄕": "乡",
        "萬": "万",
        "壹": "一",
        "叁": "三",
    }
)


def norm(value: object) -> str:
    return str(value or "").translate(CHAR_MAP).casefold()


def load_dashboard(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = DATA_RE.search(text)
    if not match:
        raise ValueError(f"未找到 DATA: {path}")
    return json.loads(match.group(1), strict=False)


def load_detail_source(path: Path) -> tuple[str, dict]:
    text = path.read_text(encoding="utf-8")
    match = DETAIL_ASSIGNMENT_RE.search(text)
    if not match:
        raise ValueError(f"未找到明细数据变量: {path}")
    data, _ = json.JSONDecoder(strict=False).raw_decode(text, match.end())
    return match.group(1), data


def merge_new_launch_details(details: dict, incoming: dict) -> None:
    months = details.setdefault("months", {})
    for month, month_data in incoming.get("months", {}).items():
        target = months.setdefault(month, {"projects": {}, "aliases": {}})
        target_projects = target.setdefault("projects", {})
        for project_key, project_value in month_data.get("projects", {}).items():
            target_projects.setdefault(project_key, project_value)
        target["aliases"] = {
            **month_data.get("aliases", {}),
            **target.get("aliases", {}),
        }


def load_details(paths: Path | list[Path]) -> dict:
    if isinstance(paths, Path):
        paths = [paths]
    sources = [load_detail_source(path) for path in paths]
    details = {"months": {}}

    for variable, data in sources:
        if variable == "TRANSACTION_DETAILS":
            details = data
    months = details.setdefault("months", {})
    for variable, data in sources:
        if variable in SINGLE_MONTH_VARIABLES:
            months[SINGLE_MONTH_VARIABLES[variable]] = data
    for variable, data in sources:
        if variable == "NEW_LAUNCH_TRANSACTION_DETAILS":
            merge_new_launch_details(details, data)

    return details


def project_candidates(project: dict) -> list[str]:
    keys = [
        "project",
        "cricProjectName",
        "janAprMatchedName",
        "junMatchedName",
        "junCricProjectName",
        "julMatchedName",
        "julCricProjectName",
        "augMatchedName",
        "augCricProjectName",
        "matchedName",
        "summaryRecordName",
        "officialProjectName",
    ]
    seen: set[str] = set()
    candidates: list[str] = []
    for key in keys:
        value = project.get(key)
        if value and value not in seen:
            seen.add(value)
            candidates.append(value)
    return candidates


def find_detail_key(month_data: dict, candidates: list[str]) -> str | None:
    projects = month_data.get("projects", {})
    aliases = month_data.get("aliases", {})
    normalized_index = {norm(key): key for key in projects}

    for candidate in candidates:
        if candidate in projects:
            return candidate
        if candidate in aliases and aliases[candidate] in projects:
            return aliases[candidate]
        normalized = norm(candidate)
        if normalized in aliases and aliases[normalized] in projects:
            return aliases[normalized]
        if normalized in normalized_index:
            return normalized_index[normalized]
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", type=Path, default=Path("index.html"))
    parser.add_argument(
        "--details",
        type=Path,
        nargs="+",
        default=[Path("transaction_details.js")],
    )
    parser.add_argument("--months", nargs="+", required=True)
    parser.add_argument(
        "--allow-missing",
        nargs="*",
        default=[],
        help="已知允许缺少明细的项目月份，格式：项目名@月份",
    )
    args = parser.parse_args()

    dashboard = load_dashboard(args.html)
    details = load_details(args.details)

    checked = 0
    missing = []
    mismatches = []

    for project in dashboard.get("projects", []) + dashboard.get("launchProjects", []):
        candidates = project_candidates(project)
        for month in args.months:
            suites = project.get("monthly", {}).get(month, {}).get("suites", 0)
            if not suites:
                continue
            month_data = details.get("months", {}).get(month, {})
            detail_key = find_detail_key(month_data, candidates)
            allow_key = f"{project.get('project')}@{month}"
            if not detail_key and allow_key in set(args.allow_missing):
                checked += 1
                continue
            if not detail_key:
                missing.append(
                    {
                        "month": month,
                        "project": project.get("project"),
                        "suites": suites,
                        "candidates": candidates,
                    }
                )
                continue
            rows = len(month_data["projects"][detail_key].get("rows", []))
            checked += 1
            if rows != suites:
                mismatches.append(
                    {
                        "month": month,
                        "project": project.get("project"),
                        "dashboardSuites": suites,
                        "detailProject": detail_key,
                        "detailRows": rows,
                    }
                )

    base_project_count = len(dashboard.get("projects", []))
    launch_project_count = len(dashboard.get("launchProjects", []))
    result = {
        "projects": base_project_count,
        "baseProjects": base_project_count,
        "launchProjects": launch_project_count,
        "totalProjects": base_project_count + launch_project_count,
        "months": args.months,
        "checkedNonZeroProjectMonths": checked,
        "missing": missing,
        "mismatches": mismatches,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if missing or mismatches:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
