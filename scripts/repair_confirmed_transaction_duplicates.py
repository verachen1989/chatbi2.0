#!/usr/bin/env python3
"""Repair confirmed duplicate transaction data without touching unresolved gaps."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from validate_dashboard_month_details import DATA_RE, DETAIL_ASSIGNMENT_RE, load_dashboard


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


def deduplicate_rows(rows: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    result = []
    for row in rows:
        signature = row_signature(row)
        if signature in seen:
            continue
        seen.add(signature)
        result.append(row)
    return result


def rounded(value: float, digits: int = 2) -> int | float:
    result = round(float(value), digits)
    return int(result) if result.is_integer() else result


def summarize_rows(rows: list[dict]) -> dict:
    area = sum(float(row.get("area") or 0) for row in rows)
    amount = sum(float(row.get("totalWan") or 0) for row in rows)
    return {
        "suites": len(rows),
        "area": rounded(area),
        "amountWan": rounded(amount),
        "avgPrice": round(amount * 10000 / area) if area else 0,
    }


def read_assignment(path: Path) -> tuple[str, dict, str, str]:
    text = path.read_text(encoding="utf-8")
    match = DETAIL_ASSIGNMENT_RE.search(text)
    if not match:
        raise ValueError(f"未找到明细变量: {path}")
    data, end = json.JSONDecoder(strict=False).raw_decode(text, match.end())
    return match.group(1), data, text[: match.end()], text[end:]


def tracked_wrapper(path: Path) -> tuple[str, str]:
    text = subprocess.run(
        ["git", "show", f"HEAD:{path.name}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    match = DETAIL_ASSIGNMENT_RE.search(text)
    if not match:
        raise ValueError(f"Git版本未找到明细变量: {path}")
    _, end = json.JSONDecoder(strict=False).raw_decode(text, match.end())
    return text[: match.end()], text[end:]


def write_assignment(path: Path, data: dict, prefix: str, suffix: str) -> None:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    path.write_text(f"{prefix}{payload}{suffix}", encoding="utf-8")


def refresh_multi_month_summary(data: dict) -> None:
    all_projects: set[str] = set()
    all_rows: list[dict] = []
    for month_data in data.get("months", {}).values():
        projects = month_data.get("projects", {})
        month_rows = [row for project in projects.values() for row in project.get("rows", [])]
        month_data["summary"] = {"projects": len(projects), "rows": len(month_rows)}
        all_projects.update(projects)
        all_rows.extend(month_rows)
    summary = data.setdefault("summary", {})
    summary["months"] = len(data.get("months", {}))
    summary["projects"] = len(all_projects)
    summary["rows"] = len(all_rows)
    summary["propertyTypes"] = dict(
        sorted(Counter(row.get("propertyType") or "未填写" for row in all_rows).items())
    )


def refresh_single_month_summary(data: dict) -> None:
    rows = [row for project in data.get("projects", {}).values() for row in project.get("rows", [])]
    summary = data.setdefault("summary", {})
    summary["projects"] = len(data.get("projects", {}))
    summary["rows"] = len(rows)


def repair_guoxianfu_park() -> dict:
    path = ROOT / "new_launch_transaction_details.js"
    _, data, _, _ = read_assignment(path)
    prefix, suffix = tracked_wrapper(path)
    changes = {}
    for month in ("26年4月", "26年5月"):
        project = data["months"][month]["projects"]["国贤府park"]
        before = len(project["rows"])
        project["rows"] = deduplicate_rows(project["rows"])
        project["summary"] = summarize_rows(project["rows"])
        changes[month] = {"before": before, "after": len(project["rows"])}
    refresh_multi_month_summary(data)
    write_assignment(path, data, prefix, suffix)
    return changes


def repair_observation_june_details() -> dict:
    path = ROOT / "june_transaction_details.js"
    _, data, _, _ = read_assignment(path)
    prefix, suffix = tracked_wrapper(path)
    removed = data["projects"].pop("建发金茂观宸", {"rows": []})
    refresh_single_month_summary(data)
    write_assignment(path, data, prefix, suffix)
    return {"removedProjectRows": len(removed.get("rows", []))}


def replace_dashboard_data(path: Path, data: dict) -> None:
    text = path.read_text(encoding="utf-8")
    match = DATA_RE.search(text)
    if not match:
        raise ValueError(f"未找到 DATA: {path}")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text[: match.start(1)] + payload + text[match.end(1) :], encoding="utf-8")


def repair_observation_dashboard() -> dict:
    path = ROOT / "index.html"
    data = load_dashboard(path)
    phase_one = next(project for project in data["projects"] if project["project"] == "建发金茂观宸")
    removed = {month: phase_one["monthly"][month].copy() for month in [f"26年{i}月" for i in range(1, 7)]}
    for month in removed:
        phase_one["monthly"][month] = {"suites": 0, "area": 0, "price": 0, "amount": 0}

    phase_one["suites34"] = sum(phase_one["monthly"][month]["suites"] for month in ("26年6月", "26年7月"))
    phase_one["area34"] = rounded(sum(phase_one["monthly"][month]["area"] for month in ("26年6月", "26年7月")))
    phase_one["amount34"] = rounded(sum(phase_one["monthly"][month]["amount"] for month in ("26年6月", "26年7月")), 4)
    phase_one["price4"] = phase_one["monthly"]["26年7月"]["price"]
    phase_one["suitesAll"] = sum(month["suites"] for month in phase_one["monthly"].values())
    phase_one["amountAll"] = rounded(sum(month["amount"] for month in phase_one["monthly"].values()), 4)

    phase_one["cricProjectName"] = "建发金茂·观宸"
    phase_one["janAprMatchedName"] = "建发金茂·观宸"
    phase_one["junMatchedName"] = "建发金茂·观宸"
    phase_one["junCricProjectName"] = "建发金茂·观宸"
    phase_one["janAprDataSource"] = "克尔瑞未匹配"
    phase_one["junDataSource"] = "克尔瑞未匹配"
    phase_one["mayDataSource"] = "克而瑞未匹配"
    phase_one["maySourceNote"] = "已移除误匹配的建发金茂·观宸二期数据；一期本期克而瑞明细未匹配"

    replace_dashboard_data(path, data)
    return {
        "removedSuites": sum(month["suites"] for month in removed.values()),
        "suitesAllAfter": phase_one["suitesAll"],
    }


def main() -> None:
    result = {
        "guoxianfuPark": repair_guoxianfu_park(),
        "observationJuneDetails": repair_observation_june_details(),
        "observationDashboard": repair_observation_dashboard(),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
