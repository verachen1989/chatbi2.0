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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_URL = "http://bjjs.zjw.beijing.gov.cn"
DATA_RE = re.compile(
    r"const DATA = (.*?);\nconst LAUNCH_OFFICIAL_INVENTORY_OVERRIDES",
    re.S,
)
LAUNCH_OVERRIDES_RE = re.compile(
    r"const LAUNCH_OFFICIAL_INVENTORY_OVERRIDES = \{(.*?)\n\};\nDATA\.launchProjects",
    re.S,
)
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


def fetch_text(url: str, timeout: int = 12) -> str:
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


def primary_url(item: dict[str, Any]) -> str:
    urls = item.get("urls") or item.get("detailUrls")
    if isinstance(urls, list) and urls:
        return str(urls[0])
    return str(item.get("url") or "")


def item_urls(item: dict[str, Any]) -> list[str]:
    urls = item.get("urls") or item.get("detailUrls")
    if isinstance(urls, list) and urls:
        return [str(url) for url in urls if url]
    url = item.get("url")
    return [str(url)] if url else []


def urls_from_value(value: Any) -> list[str]:
    if isinstance(value, list):
        values = [str(item) for item in value]
    else:
        values = re.split(r"\s+", str(value or ""))
    return [value for value in values if re.match(r"^https?://", value)]


def split_permits(value: Any) -> list[str]:
    text = str(value or "")
    permits = re.findall(r"京房售证字[（(]\d{4}[）)](?:开)?\d+号", text)
    return permits or [item.strip() for item in re.split(r"[/、；;\n]+", text) if item.strip()]


def issue_dates_from_project(project: dict[str, Any]) -> list[str]:
    dates = project.get("presaleIssueDates") or []
    if dates:
        return sorted({str(date) for date in dates if date})
    records = project.get("presaleIssueRecords") or []
    return sorted({str(record.get("date")) for record in records if record.get("date")})


def table_cells(row_html: str) -> list[str]:
    cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row_html, flags=re.I | re.S)
    return [strip_tags(cell) for cell in cells]


def parse_project_name(page_html: str) -> str:
    match = re.search(r'id="项目名称"[^>]*>(.*?)</td>', page_html, flags=re.I | re.S)
    return strip_tags(match.group(1)) if match else ""


def issue_dates(item: dict[str, Any]) -> list[str]:
    dates = item.get("issueDates") or item.get("presaleIssueDates") or []
    return sorted({str(date) for date in dates if date})


def first_issue_date(item: dict[str, Any]) -> str:
    dates = issue_dates(item)
    return dates[0] if dates else ""


def latest_issue_date(item: dict[str, Any]) -> str:
    dates = issue_dates(item)
    return dates[-1] if dates else ""


def presale_permit_text(item: dict[str, Any]) -> str:
    permits = item.get("permits") or []
    return " / ".join(str(permit) for permit in permits if permit)


def residential_permits_from_note(item: dict[str, Any]) -> list[str]:
    note = str(item.get("inventoryNote") or "")
    if "计住宅证" not in note:
        return []
    return re.findall(r"京房售证字[（(]\d{4}[）)](?:开)?\d+号", note)


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


def merge_presell_stats(stats_items: list[dict[str, Any]]) -> dict[str, Any]:
    suites = sum(int(item.get("signedStatsSuites") or 0) for item in stats_items)
    area = sum(float(item.get("signedStatsArea") or 0) for item in stats_items)
    amount = sum(
        float(item.get("signedStatsArea") or 0) * float(item.get("signedStatsAvgPrice") or 0)
        for item in stats_items
    )
    result: dict[str, Any] = {}
    if suites:
        result["signedStatsSuites"] = suites
    if area:
        result["signedStatsArea"] = round(area, 2)
    if area and amount:
        result["signedStatsAvgPrice"] = round(amount / area, 2)
    return result


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


def fetch_building_status(row: dict[str, Any], timeout: int) -> dict[str, int]:
    row_html = fetch_text(row["url"], timeout=timeout)
    return count_building_status(row_html)


def scrape_project(item: dict[str, Any], delay: float, timeout: int, max_workers: int) -> dict[str, Any]:
    urls = item_urls(item)
    if not urls:
        raise ValueError(f"{item.get('name') or item.get('dashboardName')} 缺少住建委项目详情页 URL")
    official_names: list[str] = []
    all_building_rows: list[dict[str, Any]] = []
    presell_stats_items: list[dict[str, Any]] = []
    for source_url in urls:
        page_html = fetch_text(source_url)
        residential_permits = residential_permits_from_note(item)
        if residential_permits and not any(permit in page_html for permit in residential_permits):
            continue
        official_name = parse_project_name(page_html)
        if official_name:
            official_names.append(official_name)
        all_building_rows.extend(parse_building_rows(page_html, source_url))
        stats = parse_presell_stats(page_html)
        if stats:
            presell_stats_items.append(stats)
    official_name = official_names[0] if official_names else item.get("name") or item.get("dashboardName") or ""
    building_rows = all_building_rows
    expected_total = int(parse_number(item.get("residentialTotal")) or 0)
    residential_buildings = [row for row in building_rows if "住宅" in row["buildingName"]]
    if not residential_buildings and expected_total:
        all_approved_total = sum(int(row.get("approvedSuites") or 0) for row in building_rows)
        if all_approved_total == expected_total:
            residential_buildings = building_rows
    if not residential_buildings:
        raise ValueError(f"{item.get('name') or primary_url(item)} 未解析到住宅楼栋，已停止写入，避免把总套数误置为0")
    status_items: list[dict[str, int]] = []
    building_status_errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        futures = {}
        for row in residential_buildings:
            future = executor.submit(fetch_building_status, row, timeout)
            futures[future] = row
            time.sleep(delay)
        for future in as_completed(futures):
            row = futures[future]
            try:
                status_items.append(future.result())
            except Exception as exc:
                building_status_errors.append(
                    {
                        "buildingName": str(row.get("buildingName") or ""),
                        "url": str(row.get("url") or ""),
                        "error": str(exc),
                    }
                )
    counts = merge_counts(status_items)
    approved_total = sum(int(row.get("approvedSuites") or 0) for row in residential_buildings)
    status_total = counts.get("total", 0)
    if status_total <= 0:
        raise ValueError(f"{item.get('name') or primary_url(item)} 未解析到楼盘表房源状态，已停止写入")
    target_total = max(expected_total, approved_total, status_total)
    if target_total and status_total < target_total:
        counts["unknown"] = counts.get("unknown", 0) + (target_total - status_total)
        counts["total"] = target_total
        status_total = target_total
    residential_total = target_total
    stats = merge_presell_stats(presell_stats_items)
    sold_from_status = int(counts.get("contractSigned", 0) + counts.get("filed", 0))
    fetched_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    result = {
        "projectId": project_id_from_url(primary_url(item)),
        "officialProjectName": official_name,
        "dashboardName": item.get("dashboardName") or item.get("name") or official_name,
        "dashboardId": item.get("dashboardId"),
        "dashboardCollection": item.get("dashboardCollection") or "",
        "source": item.get("source") or "dashboard",
        "url": primary_url(item),
        "urls": urls,
        "fetchedAt": fetched_at,
        "isNewLaunchResidential": item.get("source") == "officialNewLaunch",
        "firstIssueDate": first_issue_date(item),
        "latestIssueDate": latest_issue_date(item),
        "issueDates": issue_dates(item),
        "presalePermits": item.get("permits") or [],
        "presalePermitText": presale_permit_text(item),
        "summaryRecordName": item.get("summaryRecordName") or "",
        "summaryDeveloper": item.get("summaryDeveloper") or "",
        "developer": item.get("developer") or "",
        "district": item.get("district") or "",
        "plate": item.get("plate") or "",
        "group": item.get("group") or "",
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
        "buildingStatusErrors": building_status_errors,
        "buildingCount": len(residential_buildings),
        "buildings": residential_buildings,
        **stats,
    }
    result["auditNote"] = build_audit_note(len(residential_buildings), int(residential_total), counts, stats)
    result["screenshotInfo"] = {
        "住建委备案名": result["dashboardName"],
        "预售证": result["presalePermitText"],
        "首次取证日期": result["firstIssueDate"],
        "最新取证日期": result["latestIssueDate"],
        "开发商": result["developer"],
        "项目详情页": result["url"],
        "截图字段说明": "对应页面项目详情卡片字段，可按项目详情页重新截图追溯。",
    }
    return result


def load_watchlist(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    projects = payload.get("projects", [])
    if not isinstance(projects, list) or not projects:
        raise ValueError(f"watchlist 为空: {path}")
    return projects


def extract_js_string(body: str, key: str) -> str:
    match = re.search(rf"\b{re.escape(key)}:\s*(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')", body)
    if not match:
        return ""
    raw = match.group(1)
    if raw.startswith('"'):
        try:
            return str(json.loads(raw))
        except json.JSONDecodeError:
            return raw.strip('"')
    return raw.strip("'")


def extract_js_number(body: str, key: str) -> int | None:
    match = re.search(rf"\b{re.escape(key)}:\s*(-?\d+)", body)
    return int(match.group(1)) if match else None


def parse_launch_inventory_overrides(text: str) -> dict[str, dict[str, Any]]:
    match = LAUNCH_OVERRIDES_RE.search(text)
    if not match:
        return {}
    overrides: dict[str, dict[str, Any]] = {}
    body = match.group(1)
    body_start = match.start(1)
    offset = 0
    while True:
        key_match = re.search(r'"([^"]+)":\s*\{', body[offset:])
        if not key_match:
            break
        key = key_match.group(1)
        open_index = body_start + offset + key_match.end() - 1
        close_index = find_matching_brace(text, open_index)
        entry = text[open_index : close_index + 1]
        urls = re.findall(r"https?://[^\"\\\s]+", entry)
        item: dict[str, Any] = {}
        for field in (
            "summaryRecordName",
            "summaryPresalePermit",
            "summaryDeveloper",
            "officialProjectName",
            "officialInventoryFetchedAt",
            "officialInventoryMatchStatus",
            "officialInventoryTotalAuditNote",
        ):
            value = extract_js_string(entry, field)
            if value:
                item[field] = value
        for field in (
            "officialResidentialTotal",
            "officialUnsignedSuites",
            "officialAvailableSuites",
            "officialBookedSuites",
            "officialSignedSuites",
            "officialDetailSignedSuites",
        ):
            value = extract_js_number(entry, field)
            if value is not None:
                item[field] = value
        if urls:
            item["officialInventoryEvidenceUrl"] = "\n".join(urls)
        overrides[key] = item
        offset = close_index - body_start + 1
    return overrides


def load_dashboard_data(path: Path) -> tuple[str, dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    match = DATA_RE.search(text)
    if not match:
        raise ValueError(f"未找到 DATA 项目池: {path}")
    return text, json.loads(match.group(1))


def watchlist_item_from_dashboard_project(project: dict[str, Any], collection_name: str) -> dict[str, Any] | None:
    urls = urls_from_value(project.get("officialInventoryEvidenceUrl"))
    if not urls:
        return None
    display_name = project.get("project") or project.get("officialProjectName") or project.get("summaryRecordName")
    return {
        "source": "dashboard",
        "dashboardId": project.get("id"),
        "dashboardCollection": collection_name,
        "name": project.get("officialProjectName") or project.get("summaryRecordName") or display_name,
        "dashboardName": display_name,
        "summaryRecordName": project.get("summaryRecordName") or "",
        "summaryDeveloper": project.get("summaryDeveloper") or "",
        "urls": urls,
        "url": urls[0],
        "permits": split_permits(project.get("summaryPresalePermit")),
        "issueDates": issue_dates_from_project(project),
        "developer": project.get("summaryDeveloper") or "",
        "district": project.get("district") or "",
        "group": project.get("group") or "",
        "plate": project.get("plate") or "",
        "residentialTotal": parse_number(project.get("officialResidentialTotal")) or parse_number(project.get("approvedTotalSuites")) or 0,
        "approvedTotalSuites": project.get("approvedTotalSuites") or project.get("officialResidentialTotal"),
        "inventoryNote": project.get("officialInventoryTotalAuditNote") or "",
    }


def load_official_projects_from_dashboard(path: Path) -> list[dict[str, Any]]:
    text, data = load_dashboard_data(path)
    launch_overrides = parse_launch_inventory_overrides(text)
    watchlist: list[dict[str, Any]] = []
    seen_sources: set[str] = set()

    for collection_name in ("projects", "launchProjects"):
        for raw_project in data.get(collection_name, []):
            project = dict(raw_project)
            if collection_name == "launchProjects":
                project.update(launch_overrides.get(str(project.get("id")), {}))
            item = watchlist_item_from_dashboard_project(project, collection_name)
            if not item:
                continue
            source_key = "|".join(sorted(project_id_from_url(url) or url for url in item_urls(item)))
            if source_key in seen_sources:
                continue
            seen_sources.add(source_key)
            watchlist.append(item)

    match = OFFICIAL_PROJECTS_RE.search(text)
    if not match:
        raise ValueError(f"未找到页面新开盘项目清单: {path}")
    projects = json.loads(match.group(1))
    for project in projects:
        residential_total = int(parse_number(project.get("residentialTotal")) or 0)
        detail_urls = project.get("detailUrls") or []
        if residential_total <= 0 or not detail_urls:
            continue
        source_key = "|".join(sorted(project_id_from_url(url) or url for url in detail_urls))
        if source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        watchlist.append(
            {
                "source": "officialNewLaunch",
                "name": project.get("officialProjectName"),
                "dashboardName": project.get("officialProjectName"),
                "urls": detail_urls,
                "url": detail_urls[0],
                "permits": project.get("permits") or [],
                "issueDates": project.get("issueDates") or [],
                "developer": project.get("developer") or "",
                "district": project.get("district") or "",
                "group": project.get("group") or "",
                "plate": project.get("plate") or "",
                "residentialTotal": residential_total,
                "approvedTotalSuites": project.get("approvedTotalSuites"),
                "inventoryNote": project.get("inventoryNote") or "",
            }
        )
    if not watchlist:
        raise ValueError(f"页面没有可抓取的住建委库存项目: {path}")
    return watchlist


def write_watchlist(path: Path, projects: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"projects": projects}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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


def project_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "projectName": result["dashboardName"],
        "officialProjectName": result["officialProjectName"],
        "source": result.get("source") or "dashboard",
        "isNewLaunchResidential": bool(result.get("isNewLaunchResidential")),
        "firstIssueDate": result.get("firstIssueDate") or "",
        "latestIssueDate": result.get("latestIssueDate") or "",
        "presalePermitText": result.get("presalePermitText") or "",
        "developer": result.get("developer") or "",
        "district": result.get("district") or "",
        "group": result.get("group") or "",
        "plate": result.get("plate") or "",
        "totalSuites": result["residentialTotal"],
        "remainingSuites": result["unsignedSuites"],
        "cumulativeSoldSuites": result["signedSuites"],
        "contractSignedSuites": result["contractSignedSuites"],
        "filedSuites": result["filedSuites"],
        "bookedSuites": result["bookedSuites"],
        "signedStatsSuites": result.get("signedStatsSuites"),
        "fetchedAt": result["fetchedAt"],
        "evidenceUrl": result["url"],
        "evidenceUrls": result.get("urls") or [result["url"]],
        "screenshotInfo": result["screenshotInfo"],
        "auditNote": result["auditNote"],
    }


def write_snapshot(path: Path, results: list[dict[str, Any]], failures: list[dict[str, Any]]) -> None:
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    snapshot = {
        "generatedAt": generated_at,
        "scope": "页面全部已匹配住建委项目 + 页面未覆盖的新开盘住宅补充项目；取证时间按住建委预售许可证发证日期；总套数/剩余套数/累计已售取自住建委楼盘表房源状态。",
        "fields": {
            "projectName": "项目名称",
            "firstIssueDate": "首次取证时间",
            "latestIssueDate": "最新取证时间",
            "totalSuites": "住宅总套数",
            "remainingSuites": "剩余套数（绿色可售）",
            "cumulativeSoldSuites": "累计已售（已签约+网上联机备案）",
            "screenshotInfo": "项目详情截图字段和证据链接信息",
        },
        "projects": [project_snapshot(result) for result in sorted(results, key=lambda item: item["dashboardName"])],
        "failures": failures,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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


def merge_issue_records(existing: list[dict[str, Any]] | None, result: dict[str, Any]) -> list[dict[str, Any]]:
    records = list(existing or [])
    permits = result.get("presalePermits") or []
    dates = result.get("issueDates") or []
    name = result.get("officialProjectName") or result.get("dashboardName")
    for index, permit in enumerate(permits):
        date = dates[index] if index < len(dates) else (dates[0] if dates else "")
        records.append({"name": name, "permit": permit, "date": date})
    by_key: dict[str, dict[str, Any]] = {}
    for record in records:
        date = str(record.get("date") or "")
        permit = str(record.get("permit") or "")
        if date or permit:
            by_key[f"{date}|{permit}"] = {"name": str(record.get("name") or name or ""), "permit": permit, "date": date}
    return sorted(by_key.values(), key=lambda record: (record.get("date") or "", record.get("permit") or ""))


def patch_dashboard_project(project: dict[str, Any], result: dict[str, Any]) -> None:
    project["officialProjectName"] = result.get("officialProjectName") or project.get("officialProjectName") or result["dashboardName"]
    project["officialResidentialTotal"] = result["residentialTotal"]
    project["officialUnsignedSuites"] = result["unsignedSuites"]
    project["officialAvailableSuites"] = result["availableSuites"]
    project["officialUnsignedBlueSuites"] = 0
    project["officialBookedSuites"] = result["bookedSuites"]
    project["officialContractSignedSuites"] = result["contractSignedSuites"]
    project["officialFilingSuites"] = result["filedSuites"]
    project["officialSignedStatsSuites"] = result.get("signedStatsSuites")
    project["officialSignedStatsArea"] = result.get("signedStatsArea")
    project["officialSignedStatsAvgPrice"] = result.get("signedStatsAvgPrice")
    project["officialSignedSuites"] = result.get("signedStatsSuites") if result.get("signedStatsSuites") is not None else result["signedSuites"]
    project["officialDetailSignedSuites"] = result["signedSuites"]
    project["officialInventoryEvidenceUrl"] = "\n".join(result.get("urls") or [result["url"]])
    project["officialInventoryFetchedAt"] = result["fetchedAt"]
    project["officialInventoryMatchStatus"] = "住建委楼盘表每日抓取"
    project["officialInventoryTotalAuditNote"] = result["auditNote"]
    project["approvedTotalSuites"] = result["residentialTotal"]
    if result.get("summaryRecordName") and not project.get("summaryRecordName"):
        project["summaryRecordName"] = result["summaryRecordName"]
    elif not project.get("summaryRecordName") and result.get("officialProjectName"):
        project["summaryRecordName"] = result["officialProjectName"]
    if result.get("presalePermitText") and not project.get("summaryPresalePermit"):
        project["summaryPresalePermit"] = result["presalePermitText"]
    if result.get("developer") and not project.get("summaryDeveloper"):
        project["summaryDeveloper"] = result["developer"]
    records = merge_issue_records(project.get("presaleIssueRecords"), result)
    if records:
        project["presaleIssueRecords"] = records
        project["presaleIssueDates"] = [record["date"] for record in records if record.get("date")]


def update_data_json(text: str, results: list[dict[str, Any]]) -> str:
    match = DATA_RE.search(text)
    if not match:
        raise ValueError("未找到 DATA")
    data = json.loads(match.group(1))
    by_key = {
        (result.get("dashboardCollection"), str(result.get("dashboardId"))): result
        for result in results
        if result.get("source") == "dashboard" and result.get("dashboardId") is not None
    }
    if not by_key:
        return text
    changed = False
    for collection_name in ("projects", "launchProjects"):
        for project in data.get(collection_name, []):
            key = (collection_name, str(project.get("id")))
            result = by_key.get(key)
            if not result:
                continue
            before = json.dumps(project, ensure_ascii=False, sort_keys=True)
            patch_dashboard_project(project, result)
            after = json.dumps(project, ensure_ascii=False, sort_keys=True)
            changed = changed or before != after
    if not changed:
        return text
    replacement = (
        "const DATA = "
        + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        + ";\nconst LAUNCH_OFFICIAL_INVENTORY_OVERRIDES"
    )
    return text[: match.start()] + replacement + text[match.end() :]


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


def render_launch_override_entry(result: dict[str, Any]) -> str:
    summary_name = result.get("summaryRecordName") or result.get("officialProjectName") or result["dashboardName"]
    developer = result.get("developer") or result.get("summaryDeveloper") or ""
    lines = [
        f'  "{result["dashboardId"]}": {{',
        f"    summaryRecordName: {js_string(summary_name)},",
        f"    summaryPresalePermit: {js_string(result.get('presalePermitText') or '')},",
        f"    summaryDeveloper: {js_string(developer)},",
        f"    officialProjectName: {js_string(result.get('officialProjectName') or summary_name)},",
        f"    officialResidentialTotal: {result['residentialTotal']},",
        f"    officialUnsignedSuites: {result['unsignedSuites']},",
        f"    officialAvailableSuites: {result['availableSuites']},",
        "    officialUnsignedBlueSuites: 0,",
        f"    officialBookedSuites: {result['bookedSuites']},",
        f"    officialSignedSuites: {result['signedSuites']},",
        f"    officialContractSignedSuites: {result['contractSignedSuites']},",
        f"    officialFilingSuites: {result['filedSuites']},",
        f"    officialInventoryEvidenceUrl: {js_string(chr(10).join(result.get('urls') or [result['url']]))},",
        f"    officialInventoryFetchedAt: {js_string(result['fetchedAt'])},",
        '    officialInventoryMatchStatus: "住建委楼盘表每日抓取",',
        f"    officialDetailSignedSuites: {result['signedSuites']},",
        f"    officialInventoryTotalAuditNote: {js_string(result['auditNote'])}",
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


def update_launch_override_object(text: str, result: dict[str, Any]) -> str:
    const_marker = "const LAUNCH_OFFICIAL_INVENTORY_OVERRIDES = {"
    const_start = text.find(const_marker)
    if const_start < 0:
        raise ValueError("未找到 LAUNCH_OFFICIAL_INVENTORY_OVERRIDES")
    object_start = text.find("{", const_start)
    object_end = find_matching_brace(text, object_start)
    key = f'  "{result["dashboardId"]}":'
    key_start = text.find(key, object_start, object_end)
    rendered = render_launch_override_entry(result)
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
    text = update_data_json(original, results)
    for result in results:
        if result.get("source") == "officialNewLaunch":
            text = update_official_project_json(text, result)
            text = update_override_object(text, result)
        elif result.get("dashboardCollection") == "launchProjects":
            text = update_launch_override_object(text, result)
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
    parser.add_argument("--snapshot", type=Path, default=Path("data/zjw_inventory_snapshot.json"))
    parser.add_argument("--dashboard", type=Path, default=Path("index.html"))
    parser.add_argument("--delay", type=float, default=0.4)
    parser.add_argument("--timeout", type=int, default=12)
    parser.add_argument("--max-workers", type=int, default=3)
    parser.add_argument("--max-projects", type=int, default=0)
    parser.add_argument("--apply-dashboard", action="store_true")
    parser.add_argument("--sync-watchlist-from-dashboard", action="store_true")
    args = parser.parse_args()

    watchlist = (
        load_official_projects_from_dashboard(args.dashboard)
        if args.sync_watchlist_from_dashboard
        else load_watchlist(args.watchlist)
    )
    if args.sync_watchlist_from_dashboard:
        write_watchlist(args.watchlist, watchlist)
    if args.max_projects > 0:
        watchlist = watchlist[: args.max_projects]

    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index, item in enumerate(watchlist, 1):
        print(f"[{index}/{len(watchlist)}] 抓取 {item.get('dashboardName') or item.get('name')}", file=sys.stderr, flush=True)
        try:
            results.append(
                scrape_project(item, delay=args.delay, timeout=args.timeout, max_workers=args.max_workers)
            )
        except Exception as exc:
            failures.append(
                {
                    "projectName": item.get("dashboardName") or item.get("name") or "",
                    "url": primary_url(item),
                    "urls": item_urls(item),
                    "error": str(exc),
                    "fetchedAt": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )

    if not results:
        write_snapshot(args.snapshot, [], failures)
        print(json.dumps({"projects": [], "failures": failures, "snapshot": str(args.snapshot)}, ensure_ascii=False, indent=2))
        return 1

    update_history(args.history, results)
    write_snapshot(args.snapshot, results, failures)
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
                "failures": failures,
                "history": str(args.history),
                "snapshot": str(args.snapshot),
                "dashboardChanged": dashboard_changed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
