#!/usr/bin/env python3
"""Merge selected months from one transaction-details JS file into another."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


DETAIL_PATTERN = re.compile(
    r"window\.TRANSACTION_DETAILS = (.*?);\nwindow\.MAY_TRANSACTION_DETAILS",
    re.S,
)


def load_details(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = DETAIL_PATTERN.search(text)
    if not match:
        raise ValueError(f"未找到 TRANSACTION_DETAILS: {path}")
    return json.loads(match.group(1))


def recompute_summary(details: dict) -> None:
    months = details.get("months", {})
    project_count = 0
    row_count = 0
    property_types: dict[str, int] = {}
    excluded_parking_rows = 0

    for month_data in months.values():
        projects = month_data.get("projects", {})
        project_count += len(projects)
        for project in projects.values():
            rows = project.get("rows", [])
            row_count += len(rows)
            for row in rows:
                property_type = row.get("propertyType") or "未知"
                property_types[property_type] = property_types.get(property_type, 0) + 1
                if any(keyword in str(property_type) for keyword in ("车库", "车位")):
                    excluded_parking_rows += 1

    details["summary"] = {
        "months": len(months),
        "projects": project_count,
        "rows": row_count,
        "skippedRows": 0,
        "excludedParkingRows": excluded_parking_rows,
        "propertyTypes": dict(sorted(property_types.items())),
    }


def write_details(path: Path, details: dict) -> None:
    payload = json.dumps(details, ensure_ascii=False, separators=(",", ":"))
    text = (
        f"window.TRANSACTION_DETAILS = {payload};\n"
        "window.MAY_TRANSACTION_DETAILS = window.TRANSACTION_DETAILS.months['26年5月'] || "
        "{projects:{},aliases:{},summary:{projects:0,rows:0}};\n"
    )
    path.write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--incoming", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--months", nargs="+", required=True)
    parser.add_argument("--source", required=True)
    args = parser.parse_args()

    base = load_details(args.base)
    incoming = load_details(args.incoming)

    base.setdefault("months", {})
    for month in args.months:
        if month not in incoming.get("months", {}):
            raise ValueError(f"新文件缺少月份: {month}")
        base["months"][month] = incoming["months"][month]

    base["source"] = args.source
    recompute_summary(base)
    write_details(args.out, base)

    print(json.dumps(base["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
