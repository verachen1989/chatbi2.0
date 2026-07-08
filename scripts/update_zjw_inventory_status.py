#!/usr/bin/env python3
"""Fetch Beijing ZJW project inventory and patch dashboard overrides."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_URL = "http://bjjs.zjw.beijing.gov.cn"
OFFICIAL_PROJECTS_RE = re.compile(
    r"const ZJW_OFFICIAL_NEW_LAUNCH_PROJECTS = (.*?);\nconst ZJW_NEW_LAUNCH_INVENTORY_STATUS_OVERRIDES",
    re.S,
)

STATUS_COLORS = {
    "#cccccc": "nonSale",
    "#33cc00": "available",
    "#ffcc99": "booked",
    "#ff0000": "contractSigned",
    "#ffff00": "mortgage",
    "#d2691e": "filed",
    "#00ffff": "qualification",
}

STATUS_LABELS = {
    "available": "绿色可售",
    "booked": "已预订",
    "contractSigned": "已签约",
    "filed": "网上联机备案",
    "qualification": "资格核验中",
    "mortgage": "已办理预售项目抵押",
    "nonSale": "不可售",
    "unknown": "未知状态",
}


def fetch_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read()
    return raw.decode("utf-8", errors="replace")


def strip_tags(value: str) -> str:
    value = re.sub(r"<script\b.*?</script>", "", value, flags=re.I | re.S)
    value = re.sub(r"<style\b.*?</style>", "", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def parse_number(value: Any) -> float | int | None:
    text = str(value or "").replace(",", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group(0))
    return int(number) if number.is_integer() else number


def project_id_from_url(url: str) -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return (query.get("projectID") or query.get("projectId") or [""])[0]


def table_cells(row_html: str) -> list[str]:
    cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row_html, flags=re.I | re.S)
    return [strip_tags(cell) for cell in cells]


def parse_project_name(page_html: str) -> str:
    match = re.search(r'id="项目名称"[^>]*>(.*?)</td>', page_html, flags=re.I | re.S)
    return strip_tags(match.group(1)) if match else ""


def parse_building_rows(page_html: str, source_url: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for match in re.finditer(
        r"(<tr\b[^>]*>(?:(?!</tr>).)*?href=\"(?P<href>[^\"]*pageId=320833[^\"]*buildingId=\d+[^\"]*)\"(?:(?!</tr>).)*?</tr>)",
        page_html,
        flags=re.I | re.S,
    ):
        row_html = match.group(1)
        cells = table_cells(row_html)
        if len(cells) < 3:
            continue
        building_name = cells[0]
        approved_suites = parse_number(cells[1])
        approved_area = parse_number(cells[2])
        rows.append(
            {
                "buildingName": building_name,
                "approvedSuites": int(approved_suites or 0),
                "approvedArea": approved_area,
                "saleStatus": cells[3] if len(cells) > 3 else "",
                "listPrice": parse_number(cells[4] if len(cells) > 4 else ""),
                "url": urllib.parse.urljoin(source_url, html.unescape(match.group("href"))),
            }
        )
    return rows


def parse_presell_stats(page_html: str) -> dict[str, Any]:
    start = page_html.find("期房签约统计")
    if start < 0:
        return {}
    block = page_html[start : start + 5000]
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", block, flags=re.I | re.S):
        cells = table_cells(row)
        if len(cells) >= 4 and "住宅" in cells[0]:
            return {
                "signedStatsSuites": int(parse_number(cells[1]) or 0),
                "signedStatsArea": parse_number(cells[2]),
                "signedStatsAvgPrice": parse_number(cells[3]),
            }
    return {}


def count_building_status(page_html: str) -> dict[str, int]:
    counts = {status: 0 for status in STATUS_LABELS}
    for match in re.finditer(r"<div\b(?P<attrs>[^>]*)>(?P<body>.*?)</div>", page_html, flags=re.I | re.S):
        attrs = match.group("attrs")
        if not re.search(r"width\s*:\s*130px", attrs, flags=re.I):
            continue
        color_match = re.search(r"background\s*:\s*(#[0-9a-fA-F]{6})", attrs)
        status = STATUS_COLORS.get(color_match.group(1).lower() if color_match else "", "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def merge_counts(items: list[dict[str, int]]) -> dict[str, int]:
    merged = {status: 0 for status in STATUS_LABELS}
    for item in items:
        for key, value in item.items():
            merged[key] = merged.get(key, 0) + int(value or 0)
    merged["total"] = sum(value for key, value in merged.items() if key != "total")
    return merged


def build_audit_note(building_count: int, total: int, counts: dict[str, int], stats: dict[str, Any]) -> str:
    parts = [
        f"住建委楼盘表{building_count}栋住宅楼房源状态复核：共{total}套",
        f"{STATUS_LABELS['available']}{counts.get('available', 0)}套",
        f"{STATUS_LABELS['booked']}{counts.get('booked', 0)}套",
        f"{STATUS_LABELS['contractSigned']}{counts.get('contractSigned', 0)}套",
        f"{STATUS_LABELS['filed']}{counts.get('filed', 0)}套",
    ]
    if counts.get("qualification", 0):
        parts.append(f"{STATUS_LABELS['qualification']}{counts['qualification']}套")
    if counts.get("unknown", 0):
        parts.append(f"{STATUS_LABELS['unknown']}{counts['unknown']}套")
    note = "，".join(parts) + "；剩余套数按绿色可售住宅房源计。"
    signed_stats = stats.get("signedStatsSuites")
    if signed_stats is not None:
        stat_parts = [f"期房签约统计住宅已签约{signed_stats}套"]
        if stats.get("signedStatsArea") is not None:
            stat_parts.append(f"已签约面积{stats['signedStatsArea']}㎡")
        if stats.get("signedStatsAvgPrice") is not None:
            stat_parts.append(f"成交均价{stats['signedStatsAvgPrice']}元/㎡")
        note += " " + "，".join(stat_parts) + "。"
    return note


def scrape_project(item: dict[str, Any], delay: float) -> dict[str, Any]:
    source_url = item["url"]
    page_html = fetch_text(source_url)
    official_name = parse_project_name(page_html) or item.get("name") or item.get("dashboardName") or ""
    building_rows = parse_building_rows(page_html, source_url)
    residential_buildings = [row for row in building_rows if "住宅" in row["buildingName"]]
    if not residential_buildings:
        raise ValueError(f"{item.get('name') or source_url} 未解析到住宅楼栋，已停止写入，避免把总套数误置为0")
    status_items: list[dict[str, int]] = []
    for row in residential_buildings:
        time.sleep(delay)
        row_html = fetch_text(row["url"])
        status_items.append(count_building_status(row_html))
    counts = merge_counts(status_items)
    approved_total = sum(int(row.get("approvedSuites") or 0) for row in residential_buildings)
    status_total = counts.get("total", 0)
    if status_total <= 0:
        raise ValueError(f"{item.get('name') or source_url} 未解析到楼盘表房源状态，已停止写入")
    if approved_total and status_total < approved_total:
        counts["unknown"] = counts.get("unknown", 0) + (approved_total - status_total)
        counts["total"] = approved_total
        status_total = approved_total
    residential_total = approved_total or status_total
    stats = parse_presell_stats(page_html)
    sold_from_status = int(counts.get("contractSigned", 0) + counts.get("filed", 0))
    fetched_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    result = {
        "projectId": project_id_from_url(source_url),
        "officialProjectName": official_name,
        "dashboardName": item.get("dashboardName") or item.get("name") or official_name,
        "url": source_url,
        "fetchedAt": fetched_at,
        "residentialTotal": int(residential_total),
        "approvedResidentialTotal": int(approved_total),
        "roomStatusTotal": int(status_total),
        "unsignedSuites": int(counts.get("available", 0)),
        "availableSuites": int(counts.get("available", 0)),
        "bookedSuites": int(counts.get("booked", 0)),
        "contractSignedSuites": int(counts.get("contractSigned", 0)),
        "filedSuites": int(counts.get("filed", 0)),
        "qualificationSuites": int(counts.get("qualification", 0)),
        "signedSuites": sold_from_status,
        "statusCounts": counts,
        "buildingCount": len(residential_buildings),
        "buildings": residential_buildings,
        **stats,
    }
    result["auditNote"] = build_audit_note(len(residential_buildings), int(residential_total), counts, stats)
    return result


def load_watchlist(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    projects = payload.get("projects", [])
    if not isinstance(projects, list) or not projects:
        raise ValueError(f"watchlist 为空: {path}")
    return projects


def update_history(path: Path, results: list[dict[str, Any]]) -> None:
    payload: dict[str, Any] = {"projects": {}}
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.setdefault("projects", {})
    for result in results:
        key = result["dashboardName"]
        bucket = payload["projects"].setdefault(key, {"history": []})
        bucket["latest"] = result
        bucket["history"] = [
            item
            for item in bucket.setdefault("history", [])
            if int(item.get("approvedResidentialTotal") or item.get("roomStatusTotal") or 0) > 0
        ]
        fetch_date = str(result.get("fetchedAt", "")).split(" ")[0]
        bucket["history"] = [
            item for item in bucket["history"] if str(item.get("fetchedAt", "")).split(" ")[0] != fetch_date
        ]
        bucket["history"].append(result)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def find_matching_brace(text: str, open_index: int) -> int:
    depth = 0
    in_string: str | None = None
    escape = False
    for index in range(open_index, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == in_string:
                in_string = None
            continue
        if char in {'"', "'", "`"}:
            in_string = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("未找到匹配的右花括号")


def update_official_project_json(text: str, result: dict[str, Any]) -> str:
    match = OFFICIAL_PROJECTS_RE.search(text)
    if not match:
        raise ValueError("未找到 ZJW_OFFICIAL_NEW_LAUNCH_PROJECTS")
    projects = json.loads(match.group(1))
    changed = False
    for project in projects:
        if project.get("officialProjectName") == result["dashboardName"]:
            project["residentialTotal"] = result["residentialTotal"]
            project["approvedTotalSuites"] = result["residentialTotal"]
            changed = True
            break
    if not changed:
        raise ValueError(f"未在 ZJW_OFFICIAL_NEW_LAUNCH_PROJECTS 找到 {result['dashboardName']}")
    replacement = (
        "const ZJW_OFFICIAL_NEW_LAUNCH_PROJECTS = "
        + json.dumps(projects, ensure_ascii=False, separators=(",", ":"))
        + ";\nconst ZJW_NEW_LAUNCH_INVENTORY_STATUS_OVERRIDES"
    )
    return text[: match.start()] + replacement + text[match.end() :]


def js_string(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_override_entry(result: dict[str, Any]) -> str:
    lines = [
        f'  "{result["dashboardName"]}": {{',
        f"    unsignedSuites: {result['unsignedSuites']},",
        f"    availableSuites: {result['availableSuites']},",
        f"    bookedSuites: {result['bookedSuites']},",
        f"    contractSignedSuites: {result['contractSignedSuites']},",
        f"    filedSuites: {result['filedSuites']},",
        f"    signedSuites: {result['signedSuites']},",
        f"    signedStatsSuites: {result.get('signedStatsSuites', 0)},",
        f"    signedStatsArea: {result.get('signedStatsArea', 0)},",
        f"    signedStatsAvgPrice: {result.get('signedStatsAvgPrice', 0)},",
        f"    fetchedAt: {js_string(result['fetchedAt'])},",
        f"    auditNote: {js_string(result['auditNote'])}",
        "  }",
    ]
    return "\n".join(lines)


def update_override_object(text: str, result: dict[str, Any]) -> str:
    const_marker = "const ZJW_NEW_LAUNCH_INVENTORY_STATUS_OVERRIDES = {"
    const_start = text.find(const_marker)
    if const_start < 0:
        raise ValueError("未找到 ZJW_NEW_LAUNCH_INVENTORY_STATUS_OVERRIDES")
    object_start = text.find("{", const_start)
    object_end = find_matching_brace(text, object_start)
    key = f'  "{result["dashboardName"]}":'
    key_start = text.find(key, object_start, object_end)
    rendered = render_override_entry(result)
    if key_start >= 0:
        value_start = text.find("{", key_start)
        value_end = find_matching_brace(text, value_start) + 1
        has_comma = text[value_end:object_end].lstrip().startswith(",")
        if has_comma:
            rendered += ","
            comma_start = value_end + len(text[value_end:object_end]) - len(text[value_end:object_end].lstrip())
            comma_end = comma_start + 1
            return text[:key_start] + rendered + text[comma_end:]
        return text[:key_start] + rendered + text[value_end:]
    insert = "\n" + rendered
    if text[object_start + 1 : object_end].strip():
        insert = ",\n" + rendered
    return text[:object_end] + insert + text[object_end:]


def apply_dashboard(path: Path, results: list[dict[str, Any]]) -> bool:
    original = path.read_text(encoding="utf-8")
    text = original
    for result in results:
        text = update_official_project_json(text, result)
        text = update_override_object(text, result)
    text = text.replace(
        'const inventoryFetchedAt = hasInventoryStatus\n    ? "2026-07-06 住建委楼盘表房源状态复核"\n    : (hasSignedStats ? "2026-07-06 住建委期房签约统计复核" : "2026-07-06 住建委预售证详情页抓取");',
        'const inventoryFetchedAt = inventoryStatus.fetchedAt || (hasInventoryStatus\n    ? "2026-07-06 住建委楼盘表房源状态复核"\n    : (hasSignedStats ? "2026-07-06 住建委期房签约统计复核" : "2026-07-06 住建委预售证详情页抓取"));',
    )
    if text != original:
        path.write_text(text, encoding="utf-8")
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watchlist", type=Path, default=Path("data/zjw_inventory_watchlist.json"))
    parser.add_argument("--history", type=Path, default=Path("data/zjw_inventory_history.json"))
    parser.add_argument("--dashboard", type=Path, default=Path("index.html"))
    parser.add_argument("--delay", type=float, default=0.4)
    parser.add_argument("--apply-dashboard", action="store_true")
    args = parser.parse_args()

    results = [scrape_project(item, delay=args.delay) for item in load_watchlist(args.watchlist)]
    update_history(args.history, results)
    dashboard_changed = apply_dashboard(args.dashboard, results) if args.apply_dashboard else False
    print(
        json.dumps(
            {
                "projects": [
                    {
                        "name": item["dashboardName"],
                        "total": item["residentialTotal"],
                        "available": item["availableSuites"],
                        "sold": item["signedSuites"],
                        "signedStats": item.get("signedStatsSuites"),
                        "fetchedAt": item["fetchedAt"],
                    }
                    for item in results
                ],
                "history": str(args.history),
                "dashboardChanged": dashboard_changed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
