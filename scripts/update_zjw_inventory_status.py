#!/usr/bin/env python3
"""Fetch Beijing ZJW project inventory and patch dashboard overrides."""

from __future__ import annotations

import argparse
from collections import Counter
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
    r"const DATA = (.*?);\nconst PROJECT_METADATA_OVERRIDES",
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
    "soldOut": "整栋已售完（无楼盘表链接）",
    "qualification": "资格核验中",
    "mortgage": "已办理预售项目抵押",
    "nonSale": "不可售",
    "unknown": "未知状态",
}

TRANSIENT_ERROR_PATTERNS = (
    "timed out",
    "timeout",
    "nodename nor servname",
    "name or service not known",
    "temporary failure",
    "network is unreachable",
    "connection reset",
    "connection refused",
    "remote end closed",
    "urlopen error",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
)

NON_RETRYABLE_ERROR_PATTERNS = (
    "缺少住建委项目详情页",
    "未找到",
)

PROJECT_BUILDING_CACHE: dict[str, dict[str, dict[str, Any]]] = {}
PROJECT_PERMIT_DETAIL_CACHE: dict[str, dict[str, dict[str, Any]]] = {}
PROJECT_BUILDING_STATUS_CACHE: dict[str, dict[str, dict[str, Any]]] = {}
# Do not probe guessed historical building IDs during the daily crawl.  Such
# probes are useful only as an explicit audit job; they are not independent
# coverage evidence and can consume dozens of requests per project.
HISTORICAL_DISCOVERY_ENABLED = False

# Coverage status categories — only "complete" updates official inventory
COVERAGE_COMPLETE = "complete"       # 完整闭合，可更新正式看板
COVERAGE_PARTIAL = "partial"         # 部分楼栋，不能更新正式库存
COVERAGE_MISMATCH = "mismatch"       # 套数口径冲突，不能更新正式库存
COVERAGE_UNAVAILABLE = "unavailable" # 来源页面不可用

INVALID_PAGE_MARKERS = (
    "此频道不存在",
    "该频道不存在",
    "没有找到相关内容",
    "页面为空",
)

EMPTY_SHELL_SIZE_THRESHOLD = 15000  # <15KB 大概率是空壳（初筛）


def is_valid_project_page(page_html: str) -> tuple[bool, str]:
    """多维度判定项目详情页是否有效。

    Returns (is_valid, reason):
      - (True, "") 页面有效
      - (False, reason) 页面无效及原因
    """
    # 1. 检查明确无效标记
    plain_text = strip_tags(page_html)
    for marker in INVALID_PAGE_MARKERS:
        if marker in plain_text:
            return False, f"页面包含无效标记: {marker}"

    # 2. 页面大小初筛
    if len(page_html) < EMPTY_SHELL_SIZE_THRESHOLD:
        return False, f"页面过小({len(page_html)}字节)，疑似空壳"

    # 3. 项目名称必须存在
    name = parse_project_name(page_html)
    if not name:
        return False, "项目详情页未找到项目名称"

    # 4. 至少有一栋楼的楼栋表
    rows_match = re.finditer(r"<tr\b[^>]*>(?P<body>.*?)</tr>", page_html, flags=re.I | re.S)
    has_building_rows = False
    for match in rows_match:
        cells = table_cells(match.group(0))
        if len(cells) >= 3 and cells[0]:
            has_building_rows = True
            break
    if not has_building_rows:
        return False, "项目详情页未找到楼栋表行"

    return True, ""


def is_valid_building_page(page_html: str, expected_total: int = 0) -> tuple[bool, str]:
    """多维度判定楼栋房源状态页是否有效。

    Returns (is_valid, reason):
      - (True, "") 页面有效
      - (False, reason) 页面无效及原因
    """
    # 1. 检查明确无效标记
    plain_text = strip_tags(page_html)
    for marker in INVALID_PAGE_MARKERS:
        if marker in plain_text:
            return False, f"页面包含无效标记: {marker}"

    # 2. 页面大小初筛
    if len(page_html) < EMPTY_SHELL_SIZE_THRESHOLD:
        return False, f"页面过小({len(page_html)}字节)，疑似空壳"

    # 3. 楼栋名称必须存在
    building_name = parse_building_page_name(page_html)
    if not building_name:
        return False, "楼栋房源页未找到楼栋名称"

    # 4. 必须有房源状态色块
    counts = count_building_status(page_html)
    total = int(counts.get("total") or 0)
    if total <= 0:
        return False, f"楼栋房源页未解析到房源状态色块(total={total})"

    # 5. 不能有无法识别的颜色
    if int(counts.get("unknown") or 0) > 0:
        return False, f"楼栋房源页存在{counts['unknown']}套无法识别的房源颜色"

    # 6. 如果有预期套数，闭合校验
    if expected_total > 0 and total != expected_total:
        return False, f"房源数不闭合: 预期{expected_total}套，解析{total}套"

    return True, ""


def classify_coverage(
    approved_total: int,
    expected_total: int,
    unknown_count: int,
    building_status_errors: list[dict[str, str]],
    detail_url_failures: list[str],
) -> tuple[str, str]:
    """分类覆盖状态，返回 (status, note)。

    只有 COVERAGE_COMPLETE 才会写入正式库存。
    """
    # 来源不可用：详情页抓取失败
    if detail_url_failures:
        return COVERAGE_UNAVAILABLE, (
            f"项目详情页{len(detail_url_failures)}个URL不可用: "
            + "、".join(detail_url_failures[:3])
        )

    # 来源不可用：楼栋抓取失败
    if building_status_errors:
        error_names = "、".join(
            e.get("buildingName", "") for e in building_status_errors[:5]
        )
        return COVERAGE_UNAVAILABLE, f"楼栋抓取失败({len(building_status_errors)}栋): {error_names}"

    # 有无法识别的颜色
    if unknown_count > 0:
        return COVERAGE_PARTIAL, f"存在{unknown_count}套无法识别的房源颜色"

    # 口径冲突：实际套数和预期不一致
    if expected_total > 0 and approved_total != expected_total:
        diff = approved_total - expected_total
        if diff > 0:
            return COVERAGE_MISMATCH, (
                f"实际超出预期{diff}套(批准{approved_total} vs 预期{expected_total})，"
                "可能是分期口径或项目范围不同"
            )
        return COVERAGE_MISMATCH, (
            f"覆盖不足{abs(diff)}套(批准{approved_total} vs 预期{expected_total})"
        )

    # 完整闭合
    return COVERAGE_COMPLETE, "逐栋批准套数与房源状态数已完整闭合"


def is_retryable_error(exc: Exception) -> bool:
    text = str(exc).lower()
    if any(pattern.lower() in text for pattern in NON_RETRYABLE_ERROR_PATTERNS):
        return False
    return any(pattern in text for pattern in TRANSIENT_ERROR_PATTERNS)


def classify_failure_texts(texts: list[str]) -> tuple[str, bool]:
    """Map audit text to the retry loop's failure class and retry policy."""
    values = [str(text or "") for text in texts if str(text or "")]
    lowered = [value.lower() for value in values]
    if any(
        any(pattern.lower() in value for pattern in TRANSIENT_ERROR_PATTERNS)
        for value in lowered
    ):
        return "network", True
    if any(
        marker in value
        for value in values
        for marker in ("空壳", "页面过小", "频道不存在")
    ):
        return "shell", True
    if any("无法识别" in value or "未知状态" in value for value in values):
        return "color", False
    if any(
        marker in value
        for value in values
        for marker in ("不闭合", "差额", "覆盖不足", "超出预期", "重复楼栋")
    ):
        return "metric", False
    if any(
        marker in value
        for value in values
        for marker in ("未找到", "未解析", "未获得", "无法确定", "缺少")
    ):
        return "structure", False
    # An unavailable result without a more specific signature is not safe to
    # retry indefinitely; keep the old value and require audit review.
    return "structure", False


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


def building_id_from_url(url: str) -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return (query.get("buildingId") or query.get("buildingID") or [""])[0]


def sale_permit_id_from_url(url: str) -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return (query.get("salePermitId") or query.get("salePermitID") or [""])[0]


def building_key_from_url(url: str) -> str:
    sale_permit_id = sale_permit_id_from_url(url)
    building_id = building_id_from_url(url)
    return f"{sale_permit_id}|{building_id}" if sale_permit_id and building_id else ""


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
    permits: list[str] = []
    active_year = ""
    for part in re.split(r"[/、；;\n]+", text):
        token = part.strip().replace("（", "(").replace("）", ")")
        full = re.search(r"京房售证字\((\d{4})\)(开?)(\d+)号", token)
        if full:
            active_year = full.group(1)
            permits.append(
                f"京房售证字({active_year}){full.group(2)}{full.group(3)}号"
            )
            continue
        short = re.fullmatch(r"(开?)(\d+)号", token)
        if short and active_year:
            permits.append(
                f"京房售证字({active_year}){short.group(1)}{short.group(2)}号"
            )
    return list(dict.fromkeys(permits))


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


def normalize_permit_text(value: Any) -> str:
    """Normalize a Beijing presale permit for identity matching."""
    return re.sub(r"\s+", "", str(value or "")).replace("（", "(").replace("）", ")")


def build_permit_batch_specs(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Build independent permit batches without assuming URL order is permit order."""
    permits = [
        permit
        for raw_permit in (item.get("permits") or [])
        for permit in split_permits(raw_permit)
    ]
    if not permits:
        permits = split_permits(item.get("presalePermitText"))
    known_permit_keys = {normalize_permit_text(permit) for permit in permits if permit}
    for record in item.get("presaleIssueRecords") or []:
        for permit in split_permits(record.get("permit")):
            permit_key = normalize_permit_text(permit)
            if permit_key and permit_key not in known_permit_keys:
                permits.append(permit)
                known_permit_keys.add(permit_key)
    records = {
        normalize_permit_text(record.get("permit")): str(record.get("date") or "")
        for record in (item.get("presaleIssueRecords") or [])
        if record.get("permit")
    }
    dates = list(item.get("issueDates") or item.get("presaleIssueDates") or [])
    specs: list[dict[str, Any]] = []
    for index, permit in enumerate(permits):
        permit_text = str(permit)
        issue_date = records.get(normalize_permit_text(permit_text), "")
        if not issue_date and index < len(dates):
            issue_date = str(dates[index] or "")
        specs.append(
            {
                "permit": permit_text,
                "permitKey": normalize_permit_text(permit_text),
                "issueDate": issue_date,
                "detailUrls": [],
            }
        )
    return specs


def match_permit_for_page(page_html: str, permits: list[str]) -> tuple[str | None, str]:
    """Match a detail page to a configured permit using page evidence, not URL order."""
    normalized_page = normalize_permit_text(page_html)
    matches = [
        permit
        for permit in permits
        if normalize_permit_text(permit) and normalize_permit_text(permit) in normalized_page
    ]
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return None, "详情页同时包含多个预售证号，无法确定批次"
    if len(permits) == 1:
        return permits[0], ""
    return None, "详情页未找到可匹配的预售证号"


def classify_permit_batch(batch: dict[str, Any]) -> tuple[str, str]:
    """Apply the G1/G2 gates to one permit batch."""
    if batch.get("detailStatus") != "complete":
        error = str(batch.get("error") or "、".join(batch.get("errors") or []) or "详情页未闭合")
        return COVERAGE_UNAVAILABLE, error
    unknown = int(batch.get("unknown") or 0)
    if unknown:
        return COVERAGE_PARTIAL, f"存在{unknown}套无法识别的房源颜色"
    approved = int(batch.get("approvedSuites") or 0)
    room_status = int(batch.get("roomStatusTotal") or 0)
    if approved != room_status:
        return COVERAGE_MISMATCH, f"批准{approved}套，房源状态{room_status}套，不闭合"
    return COVERAGE_COMPLETE, "预售证批次已完成逐栋闭合"


def aggregate_permit_coverage(
    batches: list[dict[str, Any]], expected_total: int
) -> dict[str, Any]:
    """Aggregate independently closed permit batches into a project gate."""
    approved_total = sum(int(batch.get("approvedSuites") or 0) for batch in batches)
    room_status_total = sum(int(batch.get("roomStatusTotal") or 0) for batch in batches)
    unknown_total = sum(int(batch.get("unknown") or 0) for batch in batches)
    batch_states = [classify_permit_batch(batch) for batch in batches]
    missing_permits = [
        str(batch.get("permit") or "")
        for batch, (status, _note) in zip(batches, batch_states)
        if status == COVERAGE_UNAVAILABLE
    ]
    building_keys: list[str] = []
    for batch in batches:
        building_keys.extend(str(key) for key in batch.get("buildingKeys") or [] if key)
    duplicate_keys = sorted(
        key for key, count in Counter(building_keys).items() if count > 1
    )
    if missing_permits:
        status = COVERAGE_UNAVAILABLE
        note = "预售证批次未闭合：" + "、".join(missing_permits)
    elif any(status == COVERAGE_MISMATCH for status, _note in batch_states):
        status = COVERAGE_MISMATCH
        note = "至少一个预售证批次批准套数与房源状态数不闭合"
    elif any(status == COVERAGE_PARTIAL for status, _note in batch_states):
        status = COVERAGE_PARTIAL
        note = "至少一个预售证批次存在未知房源状态"
    elif duplicate_keys:
        status = COVERAGE_MISMATCH
        note = "重复楼栋证据：" + "、".join(duplicate_keys[:5])
    elif expected_total <= 0:
        status = COVERAGE_PARTIAL
        note = "缺少独立的住宅总套数证据（expectedTotal<=0）"
    elif expected_total > 0 and approved_total != expected_total:
        status = COVERAGE_MISMATCH
        diff = approved_total - expected_total
        note = (
            f"实际超出预期{diff}套(批准{approved_total} vs 预期{expected_total})"
            if diff > 0
            else f"覆盖不足{abs(diff)}套(批准{approved_total} vs 预期{expected_total})"
        )
    else:
        status = COVERAGE_COMPLETE
        note = "所有预售证批次逐栋批准套数与房源状态数已完整闭合"
    return {
        "coverageStatus": status,
        "coverageNote": note,
        "approvedSuites": approved_total,
        "roomStatusTotal": room_status_total,
        "unknown": unknown_total,
        "missingPermits": missing_permits,
        "duplicateBuildingKeys": duplicate_keys,
    }


def inventory_closure_gate(
    expected_permits: list[str],
    permit_coverage: list[dict[str, Any]],
    coverage_complete: bool,
) -> dict[str, Any]:
    """Require every known presale permit batch before project-level closure."""
    covered = {
        normalize_permit_text(batch.get("permit"))
        for batch in permit_coverage
        if batch.get("permit") and batch.get("coverageStatus") == COVERAGE_COMPLETE
    }
    missing = [
        permit
        for permit in expected_permits
        if normalize_permit_text(permit) not in covered
    ]
    can_close = bool(expected_permits) and coverage_complete and not missing
    return {
        "canClose": can_close,
        "expectedPermitCount": len(expected_permits),
        "closedPermitCount": len(covered),
        "missingPermits": missing,
        "note": (
            f"已知{len(expected_permits)}张住宅预售证均完成逐证、逐楼栋闭合"
            if can_close
            else "已知预售证未全部完成独立闭合"
        ),
    }


def residential_permits_from_note(item: dict[str, Any]) -> list[str]:
    note = str(item.get("inventoryNote") or "")
    if "计住宅证" not in note:
        return []
    return re.findall(r"京房售证字[（(]\d{4}[）)](?:开)?\d+号", note)


def parse_building_rows(page_html: str, source_url: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    source_permit_id = project_id_from_url(source_url)
    for match in re.finditer(r"<tr\b[^>]*>(?P<body>.*?)</tr>", page_html, flags=re.I | re.S):
        row_html = match.group(0)
        cells = table_cells(row_html)
        if len(cells) < 3:
            continue
        building_name = cells[0]
        approved_suites = parse_number(cells[1])
        approved_area = parse_number(cells[2])
        if not building_name or approved_suites is None:
            continue
        href_match = re.search(
            r'href="(?P<href>[^"]*pageId=320833[^"]*buildingId=\d+[^"]*)"',
            row_html,
            flags=re.I,
        )
        sale_status = cells[3] if len(cells) > 3 else ""
        if not href_match and "已售完" not in sale_status:
            continue
        building_url = (
            urllib.parse.urljoin(source_url, html.unescape(href_match.group("href")))
            if href_match
            else ""
        )
        sale_permit_id = sale_permit_id_from_url(building_url) or source_permit_id
        building_id = building_id_from_url(building_url)
        building_key = (
            building_key_from_url(building_url)
            or f"{sale_permit_id}|no-detail|{building_name}"
        )
        rows.append(
            {
                "buildingName": building_name,
                "salePermitId": sale_permit_id,
                "buildingId": building_id,
                "buildingKey": building_key,
                "approvedSuites": int(approved_suites or 0),
                "approvedArea": approved_area,
                "saleStatus": sale_status,
                "listPrice": parse_number(cells[4] if len(cells) > 4 else ""),
                "url": building_url,
                "sourceUrl": source_url,
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
    for match in re.finditer(r"<div\b(?P<attrs>[^>]*)>", page_html, flags=re.I | re.S):
        attrs = match.group("attrs")
        if not re.search(r"width\s*:\s*130px", attrs, flags=re.I):
            continue
        color_match = re.search(r"background\s*:\s*(#[0-9a-fA-F]{6})", attrs)
        status = STATUS_COLORS.get(color_match.group(1).lower() if color_match else "", "unknown")
        counts[status] = counts.get(status, 0) + 1
    counts["total"] = sum(value for key, value in counts.items() if key != "total")
    return counts


def parse_building_page_name(page_html: str) -> str:
    match = re.search(r"<span[^>]*>([^<]*?)&nbsp;\s*楼盘表\s*</span>", page_html, flags=re.I | re.S)
    return strip_tags(match.group(1)) if match else ""


def building_page_permit_ids(page_html: str) -> set[str]:
    return set(re.findall(r"[?&]salePermitId=(\d+)", page_html, flags=re.I))


def fetch_discovered_building(
    url: str,
    timeout: int,
    retry_attempts: int,
    retry_delay: float,
    retry_timeout_step: int,
    permit_evidence_optional: bool = False,
) -> tuple[str, dict[str, int], list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    total_attempts = max(5, retry_attempts + 3)
    expected_permit_id = sale_permit_id_from_url(url)
    best_name = ""
    best_counts: dict[str, int] = {}
    signature_counts: dict[tuple[Any, ...], int] = {}
    empty_shell_streak = 0
    attempt_index = 0
    while attempt_index < total_attempts:
        current_timeout = timeout + attempt_index * max(0, retry_timeout_step)
        try:
            page_html = fetch_text(url, timeout=current_timeout)
            building_name = parse_building_page_name(page_html)
            counts = count_building_status(page_html)
            if not building_name:
                empty_shell_streak += 1
                if empty_shell_streak >= 2:
                    attempts.append(
                        {
                            "attempt": attempt_index + 1,
                            "timeout": current_timeout,
                            "error": "历史楼栋直连页连续2次返回空壳，本轮跳过该候选ID",
                        }
                    )
                    return best_name, best_counts, attempts
                raise RuntimeError("历史楼栋直连页返回空壳页面")
            empty_shell_streak = 0
            page_permit_ids = building_page_permit_ids(page_html)
            if (
                expected_permit_id
                and expected_permit_id not in page_permit_ids
                and not (permit_evidence_optional and not page_permit_ids)
            ):
                return "", {}, attempts
            if int(counts.get("total") or 0) <= 0:
                raise RuntimeError("历史楼栋直连页未解析到完整房源状态")
            if int(counts.get("unknown") or 0) > 0:
                raise RuntimeError(f"历史楼栋直连页存在{counts['unknown']}套无法识别的房源颜色")
            attempts.append(
                {
                    "attempt": attempt_index + 1,
                    "timeout": current_timeout,
                    "error": "历史楼栋多次采样，保留状态数最大的完整响应",
                    "buildingName": building_name,
                    "statusCounts": counts,
                }
            )
            signature = (
                building_name,
                tuple((status, int(counts.get(status) or 0)) for status in sorted(STATUS_LABELS)),
            )
            signature_counts[signature] = signature_counts.get(signature, 0) + 1
            if int(counts.get("total") or 0) > int(best_counts.get("total") or 0):
                best_name = building_name
                best_counts = counts
            if signature_counts[signature] >= 2:
                return building_name, counts, attempts
        except Exception as exc:
            attempts.append(
                {
                    "attempt": attempt_index + 1,
                    "timeout": current_timeout,
                    "error": str(exc),
                }
            )
        if attempt_index >= total_attempts - 1:
            return best_name, best_counts, attempts
        time.sleep(max(0.5, retry_delay))
        attempt_index += 1
    return best_name, best_counts, attempts


def discover_hidden_residential_buildings(
    buildings_by_key: dict[str, dict[str, Any]],
    expected_total: int,
    timeout: int,
    retry_attempts: int,
    retry_delay: float,
    retry_timeout_step: int,
    delay: float,
) -> list[dict[str, Any]]:
    discovery_attempts: list[dict[str, Any]] = []
    residential_rows = [row for row in buildings_by_key.values() if "住宅" in str(row.get("buildingName") or "")]
    covered_total = sum(int(row.get("approvedSuites") or 0) for row in residential_rows)
    if expected_total <= 0 or covered_total >= expected_total:
        return discovery_attempts
    if not HISTORICAL_DISCOVERY_ENABLED:
        discovery_attempts.append(
            {
                "error": (
                    "历史楼栋ID探测未执行：未锚定的ID只能作为人工审计线索，"
                    "不能用于补齐正式库存"
                )
            }
        )
        return discovery_attempts
    rows_by_permit: dict[str, list[dict[str, Any]]] = {}
    for row in residential_rows:
        permit_id = str(row.get("salePermitId") or "")
        if permit_id and row.get("buildingId"):
            rows_by_permit.setdefault(permit_id, []).append(row)
    for permit_id, permit_rows in rows_by_permit.items():
        if covered_total >= expected_total:
            break
        all_permit_rows = [
            row
            for row in buildings_by_key.values()
            if str(row.get("salePermitId") or "") == permit_id and row.get("buildingId")
        ]
        all_known_ids = {
            int(str(row.get("buildingId")))
            for row in all_permit_rows
            if str(row.get("buildingId") or "").isdigit()
        }
        residential_known_ids = {
            int(str(row.get("buildingId")))
            for row in permit_rows
            if str(row.get("buildingId") or "").isdigit()
        }
        if not residential_known_ids:
            continue
        min_residential_id = min(residential_known_ids)
        max_residential_id = max(residential_known_ids)
        source_url = str(permit_rows[0].get("sourceUrl") or "")
        permit_batch_key = str(permit_rows[0].get("permitBatchKey") or "")
        internal_ids = [
            building_id
            for building_id in range(min_residential_id, max_residential_id + 1)
            if building_id not in all_known_ids
        ]
        print(
            f"  开始历史楼栋发现 预售证ID={permit_id}: 内部候选{len(internal_ids)}个，"
            f"当前住宅批准{covered_total}/{expected_total}套",
            file=sys.stderr,
            flush=True,
        )
        if len(internal_ids) > 30:
            discovery_attempts.append(
                {
                    "salePermitId": permit_id,
                    "error": (
                        f"住宅楼栋 ID 跨度产生 {len(internal_ids)} 个未确认候选，超过安全上限30；"
                        "本轮不猜测跨区间楼栋"
                    ),
                }
            )
            continue

        def inspect_candidate(
            building_id: int,
            permit_evidence_optional: bool,
        ) -> tuple[dict[str, Any], dict[str, Any] | None]:
            candidate_key = f"{permit_id}|{building_id}"
            candidate_url = urllib.parse.urljoin(
                source_url,
                "/eportal/ui?"
                + urllib.parse.urlencode(
                    {
                        "pageId": "320833",
                        "systemId": "2",
                        "categoryId": "1",
                        "salePermitId": permit_id,
                        "buildingId": str(building_id),
                    }
                ),
            )
            building_name, counts, attempts = fetch_discovered_building(
                candidate_url,
                min(timeout, 8),
                retry_attempts,
                retry_delay,
                0,
                permit_evidence_optional=permit_evidence_optional,
            )
            audit_entry = {
                "salePermitId": permit_id,
                "buildingId": str(building_id),
                "url": candidate_url,
                "buildingName": building_name,
                "statusTotal": int(counts.get("total") or 0),
                "attempts": attempts,
            }
            if "住宅" not in building_name or int(counts.get("total") or 0) <= 0:
                return audit_entry, None
            approved_suites = int(counts["total"])
            return audit_entry, {
                "buildingName": building_name,
                "salePermitId": permit_id,
                "permitBatchKey": permit_batch_key,
                "buildingId": str(building_id),
                "buildingKey": candidate_key,
                "approvedSuites": approved_suites,
                "approvedArea": None,
                "saleStatus": "历史楼栋楼盘表直连取证",
                "listPrice": None,
                "url": candidate_url,
                "sourceUrl": source_url,
                "discoveryCounts": counts,
                "discoveryAttempts": attempts,
            }

        internal_results: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(inspect_candidate, building_id, True): building_id
                for building_id in internal_ids
            }
            for future in as_completed(futures):
                internal_results.append(future.result())
        for audit_entry, discovered_row in sorted(
            internal_results,
            key=lambda item: int(item[0]["buildingId"]),
        ):
            discovery_attempts.append(audit_entry)
            if not discovered_row:
                continue
            candidate_key = str(discovered_row["buildingKey"])
            if candidate_key in buildings_by_key:
                continue
            buildings_by_key[candidate_key] = discovered_row
            covered_total += int(discovered_row["approvedSuites"])
        print(
            f"  历史楼栋发现 预售证ID={permit_id}: 内部候选{len(internal_ids)}个，"
            f"累计住宅批准{covered_total}/{expected_total}套",
            file=sys.stderr,
            flush=True,
        )
        if covered_total >= expected_total:
            continue

        outward_ids: list[tuple[int, str]] = []
        for offset in range(1, 31):
            outward_ids.extend(((min_residential_id - offset, "lower"), (max_residential_id + offset, "upper")))
        outward_empty = {"lower": 0, "upper": 0}
        for building_id, outward_side in outward_ids:
            if covered_total >= expected_total:
                break
            if outward_side and outward_empty[outward_side] >= 2:
                continue
            candidate_key = f"{permit_id}|{building_id}"
            if candidate_key in buildings_by_key:
                continue
            audit_entry, discovered_row = inspect_candidate(building_id, False)
            discovery_attempts.append(audit_entry)
            time.sleep(max(0.5, delay))
            building_name = str(audit_entry.get("buildingName") or "")
            if outward_side:
                outward_empty[outward_side] = 0 if building_name else outward_empty[outward_side] + 1
                if outward_empty["lower"] >= 2 and outward_empty["upper"] >= 2:
                    break
            if not discovered_row:
                continue
            buildings_by_key[candidate_key] = discovered_row
            covered_total += int(discovered_row["approvedSuites"])
    return discovery_attempts


def merge_counts(items: list[dict[str, int]]) -> dict[str, int]:
    merged = {status: 0 for status in STATUS_LABELS}
    for item in items:
        for key, value in item.items():
            merged[key] = merged.get(key, 0) + int(value or 0)
    merged["total"] = sum(value for key, value in merged.items() if key != "total")
    return merged


def closed_inventory_metrics(total: Any, counts: dict[str, Any]) -> dict[str, Any]:
    """Return same-scope residential inventory metrics that can be reconciled."""
    residential_total = int(total or 0)
    sold_keys = ("contractSigned", "filed", "soldOut")
    has_sold_evidence = any(key in counts and counts.get(key) is not None for key in sold_keys)
    cumulative_sold = (
        sum(int(counts.get(key) or 0) for key in sold_keys)
        if has_sold_evidence
        else None
    )
    remaining = None
    if (
        residential_total > 0
        and cumulative_sold is not None
        and 0 <= cumulative_sold <= residential_total
    ):
        remaining = residential_total - cumulative_sold
    available = counts.get("available")
    return {
        "totalSuites": residential_total if residential_total > 0 else None,
        "cumulativeSoldSuites": cumulative_sold,
        "remainingSuites": remaining,
        "availableSuites": int(available) if available is not None else None,
        "closed": remaining is not None,
    }


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
    if counts.get("mortgage", 0):
        parts.append(f"{STATUS_LABELS['mortgage']}{counts['mortgage']}套")
    if counts.get("nonSale", 0):
        parts.append(f"{STATUS_LABELS['nonSale']}{counts['nonSale']}套")
    if counts.get("soldOut", 0):
        parts.append(f"{STATUS_LABELS['soldOut']}{counts['soldOut']}套")
    sold = int(
        counts.get("contractSigned", 0)
        + counts.get("filed", 0)
        + counts.get("soldOut", 0)
    )
    remaining = max(total - sold, 0)
    note = (
        "，".join(parts)
        + f"；逐栋批准套数与房源状态数已完整闭合，累计已售{sold}套，"
        + f"剩余{remaining}套=住宅总套数{total}套-累计已售{sold}套；绿色可售仅作为房源状态明细。"
    )
    signed_stats = stats.get("signedStatsSuites")
    if signed_stats is not None:
        stat_parts = [f"期房签约统计住宅已签约{signed_stats}套"]
        if stats.get("signedStatsArea") is not None:
            stat_parts.append(f"已签约面积{stats['signedStatsArea']}㎡")
        if stats.get("signedStatsAvgPrice") is not None:
            stat_parts.append(f"成交均价{stats['signedStatsAvgPrice']}元/㎡")
        note += " " + "，".join(stat_parts) + "。"
    return note


def fetch_building_status_checked(
    row: dict[str, Any],
    timeout: int,
    retry_attempts: int,
    retry_delay: float,
    retry_timeout_step: int,
    max_samples: int = 3,
    sample_delay: float = 1.0,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """抓取楼栋房源状态页，带抗空壳多次采样。

    多维度判定页面有效性：不只看大小，还检查楼栋名、色块数、闭合数。
    采样机制：取出现 >=2 次的签名（和 fetch_discovered_building 一致）。
    """
    attempts: list[dict[str, Any]] = []
    total_attempts = max(0, retry_attempts) + 1
    approved_suites = int(row.get("approvedSuites") or 0)
    best_counts: dict[str, int] = {}
    signature_counts: dict[tuple[Any, ...], int] = {}
    empty_shell_streak = 0
    attempt_index = 0
    while attempt_index < total_attempts:
        current_timeout = timeout + attempt_index * max(0, retry_timeout_step)
        # 对每次主重试，内部做多次采样抗空壳
        for sample_index in range(max_samples):
            try:
                row_html = fetch_text(row["url"], timeout=current_timeout)
                # 多维度判定页面有效性
                is_valid, reason = is_valid_building_page(row_html, expected_total=approved_suites)
                if not is_valid:
                    empty_shell_streak += 1
                    if empty_shell_streak >= 2:
                        attempts.append(
                            {
                                "attempt": attempt_index + 1,
                                "sample": sample_index + 1,
                                "timeout": current_timeout,
                                "error": f"连续2次空壳: {reason}",
                            }
                        )
                        # 连续空壳，跳过采样，直接进入下一次主重试
                        break
                    attempts.append(
                        {
                            "attempt": attempt_index + 1,
                            "sample": sample_index + 1,
                            "timeout": current_timeout,
                            "error": reason,
                        }
                    )
                    if sample_index < max_samples - 1:
                        time.sleep(max(0.3, sample_delay))
                    continue

                empty_shell_streak = 0
                counts = count_building_status(row_html)
                parsed_total = int(counts.get("total") or 0)
                if parsed_total != approved_suites:
                    attempts.append(
                        {
                            "attempt": attempt_index + 1,
                            "sample": sample_index + 1,
                            "timeout": current_timeout,
                            "error": (
                                f"页面状态不完整：批准{approved_suites}套，"
                                f"解析{parsed_total}套，差额{approved_suites - parsed_total}套"
                            ),
                        }
                    )
                    if sample_index < max_samples - 1:
                        time.sleep(max(0.3, sample_delay))
                    continue

                # 有效且闭合：记录签名，取出现 >=2 次的签名确认
                signature = (
                    str(parse_building_page_name(row_html)),
                    tuple((status, int(counts.get(status) or 0)) for status in sorted(STATUS_LABELS)),
                )
                signature_counts[signature] = signature_counts.get(signature, 0) + 1
                if int(counts.get("total") or 0) > int(best_counts.get("total") or 0):
                    best_counts = counts
                if signature_counts[signature] >= 2:
                    return counts, attempts
                if sample_index < max_samples - 1:
                    time.sleep(max(0.3, sample_delay))
            except Exception as exc:
                attempts.append(
                    {
                        "attempt": attempt_index + 1,
                        "sample": sample_index + 1,
                        "timeout": current_timeout,
                        "error": str(exc),
                    }
                )
                if sample_index < max_samples - 1:
                    time.sleep(max(0.3, sample_delay))
                continue
        # 如果采样中有确认结果（>=2 次签名），直接返回
        if best_counts and int(best_counts.get("total") or 0) == approved_suites:
            # 最后一次采样还没确认，但best_counts已经闭合，返回
            for sig, cnt in signature_counts.items():
                if cnt >= 2 and sig[1]:  # 已有>=2次确认
                    return best_counts, attempts
        if attempt_index >= total_attempts - 1:
            # 最后一次重试，如果有闭合的best_counts就返回
            if best_counts and int(best_counts.get("total") or 0) == approved_suites:
                return best_counts, attempts
            error_msg = "楼栋抓取失败"
            if best_counts:
                error_msg = (
                    f"最佳采样不闭合: 批准{approved_suites}套，"
                    f"最大采样{int(best_counts.get('total') or 0)}套"
                )
            elif attempts:
                last_error = attempts[-1].get("error", "未知错误")
                error_msg = last_error
            raise RuntimeError(error_msg)
        time.sleep(max(0.5, retry_delay))
        attempt_index += 1
    raise RuntimeError("楼栋抓取失败")


def scrape_project(
    item: dict[str, Any],
    delay: float,
    timeout: int,
    max_workers: int,
    retry_attempts: int,
    retry_delay: float,
    retry_timeout_step: int,
) -> dict[str, Any]:
    urls = item_urls(item)
    if not urls:
        raise ValueError(f"{item.get('name') or item.get('dashboardName')} 缺少住建委项目详情页 URL")
    expected_total = int(parse_number(item.get("residentialTotal")) or 0)
    residential_permits = residential_permits_from_note(item)
    permit_specs = build_permit_batch_specs(item)
    if not permit_specs:
        permit_specs = [
            {
                "permit": "",
                "permitKey": "__single_project_batch__",
                "issueDate": "",
                "detailUrls": [],
            }
        ]
    permit_names = [str(spec.get("permit") or "") for spec in permit_specs]
    permit_state = {
        str(spec["permitKey"]): {
            **spec,
            "detailStatus": "pending",
            "completedUrls": set(),
            "rows": {},
            "errors": [],
        }
        for spec in permit_specs
    }
    cache_key = str(item.get("dashboardName") or item.get("name") or primary_url(item))
    official_names: list[str] = []
    buildings_by_key: dict[str, dict[str, Any]] = {
        key: dict(value)
        for key, value in PROJECT_BUILDING_CACHE.get(cache_key, {}).items()
    }
    stats_by_url: dict[str, dict[str, Any]] = {}
    completed_detail_urls: set[str] = set()
    detail_retry_blocked: set[str] = set()
    cached_batches = PROJECT_PERMIT_DETAIL_CACHE.get(cache_key, {})
    for permit_key, cached in cached_batches.items():
        if permit_key not in permit_state:
            continue
        cached_urls = [str(url) for url in cached.get("detailUrls") or []]
        if not cached_urls or not all(url in urls for url in cached_urls):
            continue
        batch = permit_state[permit_key]
        batch["detailStatus"] = "complete"
        batch["completedUrls"].update(cached_urls)
        batch["detailUrls"].extend(cached_urls)
        batch["rows"].update({key: dict(value) for key, value in (cached.get("rows") or {}).items()})
        if cached.get("stats"):
            batch["stats"] = dict(cached["stats"])
        completed_detail_urls.update(cached_urls)
        for key, row in batch["rows"].items():
            buildings_by_key.setdefault(key, dict(row))
        for source_url, stats in (cached.get("statsByUrl") or {}).items():
            stats_by_url[str(source_url)] = dict(stats)
    detail_attempts: list[dict[str, Any]] = []
    # The CLI retry budget controls detail-page sampling too.  Do not hide a
    # persistent structural failure behind ten unconditional requests; the
    # outer validation loop will revisit only a retryable batch.
    total_detail_attempts = max(1, retry_attempts + 1)
    residential_buildings: list[dict[str, Any]] = []
    unavailable_detail_urls: list[str] = []
    detail_attempt_index = 0
    while detail_attempt_index < total_detail_attempts:
        current_timeout = min(timeout + detail_attempt_index * max(0, retry_timeout_step), 20)
        attempt_errors: list[dict[str, str]] = []
        for source_url in urls:
            if source_url in completed_detail_urls or source_url in detail_retry_blocked:
                continue
            try:
                page_html = fetch_text(source_url, timeout=current_timeout)
                # 多维度判定页面有效性
                is_valid, validity_reason = is_valid_project_page(page_html)
                if not is_valid:
                    # 页面无效 → 记为 sourceUnavailable，不标记为完成
                    unavailable_detail_urls.append(source_url)
                    error_text = f"页面无效: {validity_reason}"
                    attempt_errors.append({"url": source_url, "error": error_text})
                    if not classify_failure_texts([error_text])[1]:
                        detail_retry_blocked.add(source_url)
                    continue
                matched_permit, permit_reason = match_permit_for_page(page_html, permit_names)
                if matched_permit is None:
                    unavailable_detail_urls.append(source_url)
                    attempt_errors.append({"url": source_url, "error": permit_reason})
                    detail_retry_blocked.add(source_url)
                    continue
                permit_key = normalize_permit_text(matched_permit) or "__single_project_batch__"
                batch = permit_state[permit_key]
                if residential_permits and normalize_permit_text(matched_permit) not in {
                    normalize_permit_text(permit) for permit in residential_permits
                }:
                    # 预售证不匹配 → 不算完成，但也不是失败，跳过本轮
                    continue
                official_name = parse_project_name(page_html)
                if official_name:
                    official_names.append(official_name)
                parsed_rows = parse_building_rows(page_html, source_url)
                if not parsed_rows:
                    # 楼栋表空 ≠ 整个页面无效（可能有楼栋但无链接）
                    unavailable_detail_urls.append(source_url)
                    attempt_errors.append({"url": source_url, "error": "项目详情页未解析到楼栋表行"})
                    batch["errors"].append("项目详情页未解析到楼栋表行")
                    detail_retry_blocked.add(source_url)
                    continue
                for row in parsed_rows:
                    row["permitBatchKey"] = permit_key
                    key = str(
                        row.get("buildingKey")
                        or building_key_from_url(str(row.get("url") or ""))
                        or row.get("url")
                        or ""
                    )
                    if not key:
                        continue
                    existing = buildings_by_key.get(key)
                    if existing and int(existing.get("approvedSuites") or 0) != int(row.get("approvedSuites") or 0):
                        raise ValueError(
                            f"同一楼栋 {key} 批准套数冲突："
                            f"{existing.get('approvedSuites')} / {row.get('approvedSuites')}"
                        )
                    if existing:
                        existing.setdefault("permitBatchKey", permit_key)
                    buildings_by_key.setdefault(key, row)
                    batch["rows"][key] = dict(row)
                batch["completedUrls"].add(source_url)
                batch["detailStatus"] = "complete"
                batch["detailUrls"].append(source_url)
                completed_detail_urls.add(source_url)
                stats = parse_presell_stats(page_html)
                if stats:
                    stats_by_url[source_url] = stats
                    batch["stats"] = stats
            except Exception as exc:
                error_text = str(exc)
                attempt_errors.append({"url": source_url, "error": error_text})
                if not classify_failure_texts([error_text])[1]:
                    detail_retry_blocked.add(source_url)
            time.sleep(max(0.5, delay))
        building_rows = list(buildings_by_key.values())
        residential_buildings = [row for row in building_rows if "住宅" in row["buildingName"]]
        if not residential_buildings and expected_total:
            all_approved_total = sum(int(row.get("approvedSuites") or 0) for row in building_rows)
            if all_approved_total == expected_total:
                residential_buildings = building_rows
        covered_total = sum(int(row.get("approvedSuites") or 0) for row in residential_buildings)
        detail_attempts.append(
            {
                "attempt": detail_attempt_index + 1,
                "timeout": current_timeout,
                "buildingCount": len(residential_buildings),
                "approvedSuites": covered_total,
                "completedDetailUrls": len(completed_detail_urls),
                "expectedDetailUrls": len(urls),
                "errors": attempt_errors,
            }
        )
        if residential_buildings and len(completed_detail_urls) == len(urls):
            break
        if all(
            url in completed_detail_urls or url in detail_retry_blocked
            for url in urls
        ):
            break
        if detail_attempt_index < total_detail_attempts - 1:
            time.sleep(max(0.5, retry_delay))
        detail_attempt_index += 1
    missing_detail_urls = [url for url in urls if url not in completed_detail_urls]
    if permit_state:
        cached_project_batches = PROJECT_PERMIT_DETAIL_CACHE.setdefault(cache_key, {})
        for permit_key, batch in permit_state.items():
            if batch.get("detailStatus") != "complete":
                continue
            cached_project_batches[permit_key] = {
                "detailUrls": sorted(set(batch.get("detailUrls") or [])),
                "rows": {key: dict(value) for key, value in batch.get("rows", {}).items()},
                "stats": dict(batch.get("stats") or {}),
                "statsByUrl": {url: dict(value) for url, value in stats_by_url.items() if url in batch.get("completedUrls", set())},
            }
    for batch in permit_state.values():
        batch["detailUrls"] = sorted(set(batch.get("detailUrls") or []))
        if batch.get("completedUrls"):
            batch["detailStatus"] = "complete"
        elif batch.get("detailStatus") == "pending":
            batch["detailStatus"] = "unavailable"
            batch["errors"].append("未获得可验证的详情页响应")
    if missing_detail_urls:
        PROJECT_BUILDING_CACHE[cache_key] = {
            key: dict(value) for key, value in buildings_by_key.items()
        }
    discovery_attempts = []
    if not missing_detail_urls:
        # Historical ID-range probing is audit-only.  Run it on a copy so a
        # guessed/legacy candidate can never satisfy the formal G1 coverage
        # gate or be written into the project cache as official evidence.
        discovery_buildings = {
            key: dict(value) for key, value in buildings_by_key.items()
        }
        discovery_attempts = discover_hidden_residential_buildings(
            discovery_buildings,
            expected_total,
            timeout,
            retry_attempts,
            retry_delay,
            retry_timeout_step,
            delay,
        )
    if discovery_attempts:
        detail_attempts.append(
            {
                "attempt": "历史楼栋直连发现",
                "candidateCount": len(discovery_attempts),
                "candidates": discovery_attempts,
            }
        )
    PROJECT_BUILDING_CACHE[cache_key] = {
        key: dict(value) for key, value in buildings_by_key.items()
    }
    official_name = official_names[0] if official_names else item.get("name") or item.get("dashboardName") or ""
    presell_stats_items = list(stats_by_url.values())
    building_rows = list(buildings_by_key.values())
    residential_buildings = [row for row in building_rows if "住宅" in row["buildingName"]]
    if not residential_buildings and expected_total:
        all_approved_total = sum(int(row.get("approvedSuites") or 0) for row in building_rows)
        if all_approved_total == expected_total:
            residential_buildings = building_rows
    if not residential_buildings and not missing_detail_urls:
        raise ValueError(f"{item.get('name') or primary_url(item)} 未解析到住宅楼栋，已停止写入，避免把总套数误置为0")
    invalid_buildings = [row for row in residential_buildings if int(row.get("approvedSuites") or 0) <= 0]
    if invalid_buildings:
        names = "、".join(str(row.get("buildingName") or row.get("buildingId")) for row in invalid_buildings)
        raise ValueError(f"{item.get('name') or primary_url(item)} 住宅楼栋批准套数无效：{names}")
    approved_total = sum(int(row.get("approvedSuites") or 0) for row in residential_buildings)
    status_items: list[dict[str, int]] = []
    building_status_errors: list[dict[str, str]] = []
    building_coverage: list[dict[str, Any]] = []
    status_cache = PROJECT_BUILDING_STATUS_CACHE.setdefault(cache_key, {})
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        futures = {}
        for row in residential_buildings:
            building_cache_key = str(
                row.get("buildingKey")
                or building_key_from_url(str(row.get("url") or ""))
                or row.get("buildingName")
            )
            cached_status = status_cache.get(building_cache_key)
            if cached_status:
                cached_counts = cached_status.get("statusCounts") or {}
                if (
                    int(cached_counts.get("total") or 0) == int(row.get("approvedSuites") or 0)
                    and int(cached_counts.get("unknown") or 0) == 0
                ):
                    status_items.append(dict(cached_counts))
                    building_coverage.append(dict(cached_status["coverage"]))
                    continue
            if not row.get("url"):
                approved_suites = int(row.get("approvedSuites") or 0)
                if "已售完" not in str(row.get("saleStatus") or ""):
                    building_status_errors.append(
                        {
                            "buildingName": str(row.get("buildingName") or ""),
                            "permitBatchKey": row.get("permitBatchKey") or "",
                            "url": "",
                            "error": f"无楼盘表链接且销售状态不是已售完：{row.get('saleStatus') or '空'}",
                        }
                    )
                    continue
                building_counts = {status: 0 for status in STATUS_LABELS}
                building_counts["soldOut"] = approved_suites
                building_counts["total"] = approved_suites
                status_items.append(building_counts)
                coverage_entry = {
                    "buildingId": "",
                    "salePermitId": row.get("salePermitId") or "",
                    "permitBatchKey": row.get("permitBatchKey") or "",
                    "buildingKey": row.get("buildingKey") or "",
                    "buildingName": row.get("buildingName") or "",
                    "approvedSuites": approved_suites,
                    "parsedSuites": approved_suites,
                    "retryAttempts": 0,
                    "attempts": [],
                    "statusSource": "项目详情页销售状态=已售完；住建委未提供楼盘表链接",
                    "statusCounts": building_counts,
                    "url": row.get("sourceUrl") or "",
                }
                building_coverage.append(coverage_entry)
                status_cache[building_cache_key] = {
                    "statusCounts": dict(building_counts),
                    "coverage": dict(coverage_entry),
                }
                continue
            future = executor.submit(
                fetch_building_status_checked,
                row,
                timeout,
                retry_attempts + (3 if row.get("discoveryCounts") else 0),
                retry_delay,
                retry_timeout_step,
            )
            futures[future] = row
            time.sleep(max(0.5, delay))
        for future in as_completed(futures):
            row = futures[future]
            try:
                building_counts, building_attempts = future.result()
                parsed_total = int(building_counts.get("total") or 0)
                approved_suites = int(row.get("approvedSuites") or 0)
                # 闭合校验仍然执行，但不直接 raise，而是标记到 building_status_errors
                # 让 classify_coverage 统一判定
                if parsed_total != approved_suites:
                    building_status_errors.append(
                        {
                            "buildingName": str(row.get("buildingName") or ""),
                            "permitBatchKey": row.get("permitBatchKey") or "",
                            "url": str(row.get("url") or ""),
                            "error": f"页面状态不完整：批准{approved_suites}套，解析{parsed_total}套，差额{approved_suites - parsed_total}套",
                        }
                    )
                    continue
                if int(building_counts.get("unknown") or 0) > 0:
                    building_status_errors.append(
                        {
                            "buildingName": str(row.get("buildingName") or ""),
                            "url": str(row.get("url") or ""),
                            "error": f"页面存在{building_counts['unknown']}套无法识别的房源颜色",
                        }
                    )
                    continue
                status_items.append(building_counts)
                coverage_entry = {
                    "buildingId": row.get("buildingId") or building_id_from_url(str(row.get("url") or "")),
                    "salePermitId": row.get("salePermitId") or sale_permit_id_from_url(str(row.get("url") or "")),
                    "permitBatchKey": row.get("permitBatchKey") or "",
                    "buildingKey": row.get("buildingKey") or building_key_from_url(str(row.get("url") or "")),
                    "buildingName": row.get("buildingName") or "",
                    "approvedSuites": approved_suites,
                    "parsedSuites": parsed_total,
                    "retryAttempts": len(building_attempts),
                    "attempts": building_attempts,
                    "statusCounts": building_counts,
                    "url": row.get("url") or "",
                }
                building_coverage.append(coverage_entry)
                status_cache[building_cache_key] = {
                    "statusCounts": dict(building_counts),
                    "coverage": dict(coverage_entry),
                }
            except Exception as exc:
                building_status_errors.append(
                    {
                        "buildingName": str(row.get("buildingName") or ""),
                        "permitBatchKey": row.get("permitBatchKey") or "",
                        "url": str(row.get("url") or ""),
                        "error": str(exc),
                    }
                )
    counts = merge_counts(status_items)
    status_total = counts.get("total", 0)
    unknown_count = int(counts.get("unknown") or 0)
    permit_batches: list[dict[str, Any]] = []
    for spec in permit_specs:
        permit_key = str(spec.get("permitKey") or "")
        batch = permit_state[permit_key]
        batch_rows = [
            row for row in residential_buildings
            if str(row.get("permitBatchKey") or "") == permit_key
        ]
        batch_coverage = [
            entry for entry in building_coverage
            if str(entry.get("permitBatchKey") or "") == permit_key
        ]
        batch_counts = merge_counts(
            [entry.get("statusCounts") or {} for entry in batch_coverage]
        ) if batch_coverage else {status: 0 for status in STATUS_LABELS} | {"total": 0}
        permit_batches.append(
            {
                "permit": batch.get("permit") or "未标识预售证",
                "issueDate": batch.get("issueDate") or "",
                "detailUrl": (batch.get("detailUrls") or [""])[0],
                "detailUrls": batch.get("detailUrls") or [],
                "detailStatus": batch.get("detailStatus"),
                "approvedSuites": sum(int(row.get("approvedSuites") or 0) for row in batch_rows),
                "roomStatusTotal": int(batch_counts.get("total") or 0),
                "unknown": int(batch_counts.get("unknown") or 0),
                "buildingCount": len(batch_rows),
                "buildingKeys": [str(row.get("buildingKey") or "") for row in batch_rows if row.get("buildingKey")],
                "errors": list(batch.get("errors") or []),
            }
        )
    for batch in permit_batches:
        batch_status, batch_note = classify_permit_batch(batch)
        batch["coverageStatus"] = batch_status
        batch["coverageNote"] = batch_note
        if batch.get("errors"):
            batch["error"] = "；".join(str(error) for error in batch["errors"])
    permit_summary = aggregate_permit_coverage(permit_batches, expected_total)
    # 统一分类覆盖状态，不再在各处分散 raise
    coverage_status, coverage_note = classify_coverage(
        approved_total=approved_total if status_total > 0 else 0,
        expected_total=expected_total,
        unknown_count=unknown_count,
        building_status_errors=building_status_errors,
        detail_url_failures=missing_detail_urls,
    )
    if coverage_status == COVERAGE_COMPLETE:
        coverage_status = permit_summary["coverageStatus"]
        coverage_note = permit_summary["coverageNote"]
    # 特殊情况：完全没有解析到房源状态
    if status_total <= 0 and coverage_status != COVERAGE_UNAVAILABLE:
        coverage_status = COVERAGE_UNAVAILABLE
        coverage_note = "未解析到任何楼盘表房源状态"
    # 完整闭合时还需要检查 status_total == approved_total
    if coverage_status == COVERAGE_COMPLETE and status_total != approved_total:
        coverage_status = COVERAGE_PARTIAL
        coverage_note = (
            f"楼栋批准{approved_total}套，状态合计{status_total}套，差额{approved_total - status_total}套"
        )
    permit_gate = inventory_closure_gate(
        [str(spec.get("permit") or "") for spec in permit_specs if spec.get("permit")],
        permit_batches,
        coverage_status == COVERAGE_COMPLETE,
    )
    if coverage_status == COVERAGE_COMPLETE and not permit_gate["canClose"]:
        coverage_status = COVERAGE_UNAVAILABLE
        coverage_note = permit_gate["note"]
    residential_total = approved_total
    stats = merge_presell_stats(presell_stats_items)
    inventory_metrics = closed_inventory_metrics(residential_total, counts)
    failure_texts = [coverage_note]
    failure_texts.extend(
        str(error.get("error") or "") for error in building_status_errors
    )
    failure_texts.extend(
        str(error.get("error") or "")
        for attempt in detail_attempts
        for error in attempt.get("errors", [])
        if isinstance(error, dict)
    )
    failure_texts.extend(
        str(error)
        for batch in permit_batches
        for error in batch.get("errors", [])
    )
    failure_class, retryable = ("", False)
    if coverage_status != COVERAGE_COMPLETE:
        failure_class, retryable = classify_failure_texts(failure_texts)
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
        "expectedResidentialTotal": int(expected_total),
        "approvedResidentialTotal": int(approved_total),
        "roomStatusTotal": int(status_total),
        "coverageStatus": coverage_status,
        "coverageComplete": coverage_status == COVERAGE_COMPLETE,
        "coverageNote": coverage_note,
        "failureClass": failure_class,
        "retryable": retryable,
        "permitCoverage": permit_batches,
        "missingPermitBatches": permit_summary.get("missingPermits") or [],
        "permitClosureGate": permit_gate,
        "unsignedSuites": inventory_metrics["remainingSuites"],
        "availableSuites": int(counts.get("available", 0)),
        "bookedSuites": int(counts.get("booked", 0)),
        "contractSignedSuites": int(counts.get("contractSigned", 0)),
        "filedSuites": int(counts.get("filed", 0)),
        "soldOutSuites": int(counts.get("soldOut", 0)),
        "qualificationSuites": int(counts.get("qualification", 0)),
        "signedSuites": inventory_metrics["cumulativeSoldSuites"],
        "statusCounts": counts,
        "buildingStatusErrors": building_status_errors,
        "buildingCount": len(residential_buildings),
        "buildings": residential_buildings,
        "detailAttempts": detail_attempts,
        "buildingCoverage": sorted(building_coverage, key=lambda entry: (str(entry.get("buildingName")), str(entry.get("buildingId")))),
        **stats,
    }
    # 审计备注按覆盖状态区分措辞
    if coverage_status == COVERAGE_COMPLETE:
        result["auditNote"] = build_audit_note(len(residential_buildings), int(residential_total), counts, stats)
    else:
        result["auditNote"] = (
            f"住建委楼盘表{len(residential_buildings)}栋住宅楼房源状态复核："
            f"共{status_total}套(批准{approved_total}套)，{coverage_note}；"
            f"状态={coverage_status}，不能更新正式库存。"
        )
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


def scrape_project_with_retry(
    item: dict[str, Any],
    delay: float,
    timeout: int,
    max_workers: int,
    retry_attempts: int,
    retry_delay: float,
    retry_timeout_step: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], Exception | None]:
    attempts: list[dict[str, Any]] = []
    last_error: Exception | None = None
    total_attempts = max(0, retry_attempts) + 1
    attempt_index = 0
    while attempt_index < total_attempts:
        current_timeout = timeout + attempt_index * max(0, retry_timeout_step)
        current_workers = max_workers if attempt_index == 0 else 1
        try:
            result = scrape_project(
                item,
                delay=delay,
                timeout=current_timeout,
                max_workers=current_workers,
                retry_attempts=retry_attempts,
                retry_delay=retry_delay,
                retry_timeout_step=retry_timeout_step,
            )
            if attempts:
                result["retryAttempts"] = len(attempts)
                result["retryErrors"] = attempts
            return result, attempts, None
        except Exception as exc:
            last_error = exc
            retryable = is_retryable_error(exc)
            attempts.append(
                {
                    "attempt": attempt_index + 1,
                    "timeout": current_timeout,
                    "maxWorkers": current_workers,
                    "retryable": retryable,
                    "error": str(exc),
                }
            )
            if not retryable or attempt_index >= total_attempts - 1:
                break
            project_name = item.get("dashboardName") or item.get("name") or primary_url(item)
            print(
                f"  临时失败，准备第{attempt_index + 2}次重跑 {project_name}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(max(0, retry_delay))
            attempt_index += 1
    return None, attempts, last_error


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
            "officialSoldOutSuites",
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
        "presaleIssueRecords": project.get("presaleIssueRecords") or [],
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
                "presaleIssueRecords": project.get("presaleIssueRecords") or [],
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
    status_counts = dict(result.get("statusCounts") or {})
    status_counts.setdefault("available", result.get("availableSuites"))
    status_counts.setdefault("contractSigned", result.get("contractSignedSuites"))
    status_counts.setdefault("filed", result.get("filedSuites"))
    status_counts.setdefault("soldOut", result.get("soldOutSuites"))
    metrics = closed_inventory_metrics(result.get("residentialTotal"), status_counts)
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
        "expectedResidentialTotal": result.get("expectedResidentialTotal"),
        "approvedResidentialTotal": result.get("approvedResidentialTotal"),
        "roomStatusTotal": result.get("roomStatusTotal"),
        "coverageComplete": bool(result.get("coverageComplete")),
        "coverageStatus": result.get("coverageStatus", COVERAGE_COMPLETE),
        "coverageNote": result.get("coverageNote", ""),
        "failureClass": result.get("failureClass", ""),
        "retryable": bool(result.get("retryable")),
        "permitCoverage": result.get("permitCoverage") or [],
        "missingPermitBatches": result.get("missingPermitBatches") or [],
        "permitClosureGate": result.get("permitClosureGate") or {},
        "remainingSuites": metrics["remainingSuites"],
        "availableSuites": metrics["availableSuites"],
        "cumulativeSoldSuites": metrics["cumulativeSoldSuites"],
        "contractSignedSuites": result["contractSignedSuites"],
        "filedSuites": result["filedSuites"],
        "soldOutSuites": result.get("soldOutSuites", 0),
        "bookedSuites": result["bookedSuites"],
        "signedStatsSuites": result.get("signedStatsSuites"),
        "retryAttempts": result.get("retryAttempts", 0),
        "fetchedAt": result["fetchedAt"],
        "evidenceUrl": result["url"],
        "evidenceUrls": result.get("urls") or [result["url"]],
        "statusCounts": result.get("statusCounts") or {},
        "buildingCount": result.get("buildingCount"),
        "buildingCoverage": result.get("buildingCoverage") or [],
        "screenshotInfo": result["screenshotInfo"],
        "auditNote": result["auditNote"],
    }


def write_snapshot(path: Path, results: list[dict[str, Any]], failures: list[dict[str, Any]]) -> None:
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    projects_by_name = {
        item["projectName"]: item
        for item in (project_snapshot(result) for result in results)
    }
    failure_by_name = {
        str(item.get("projectName") or ""): item
        for item in failures
        if item.get("projectName")
    }
    if path.exists() and failure_by_name:
        try:
            previous_snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous_snapshot = {}
        for previous in previous_snapshot.get("projects") or []:
            project_name = str(previous.get("projectName") or "")
            if not project_name or project_name in projects_by_name or project_name not in failure_by_name:
                continue
            # 兼容启用新版门禁前的历史快照：旧记录没有 coverageComplete，
            # 不能因为缺少新字段就直接丢弃；但旧值不能冒充新版 complete。
            is_legacy_snapshot = "coverageComplete" not in previous and "coverageStatus" not in previous
            if previous.get("coverageComplete") is False and previous.get("coverageStatus") != "legacy":
                continue
            preserved = dict(previous)
            preserved["preservedFromPrevious"] = True
            preserved["latestFailureAt"] = failure_by_name[project_name].get("fetchedAt") or generated_at
            if is_legacy_snapshot:
                preserved["coverageStatus"] = "legacy"
                preserved["coverageNote"] = "沿用旧版历史快照，尚未按新版逐证/逐楼栋闭合门禁复核"
            projects_by_name[project_name] = preserved
            failure_by_name[project_name]["oldValueRetained"] = True
    snapshot = {
        "generatedAt": generated_at,
        "scope": "页面全部已匹配住建委项目 + 页面未覆盖的新开盘住宅补充项目；取证时间按住建委预售许可证发证日期；总套数/累计已售取自同一住建委住宅楼盘表范围，剩余套数=住宅总套数-累计已售。",
        "fields": {
            "projectName": "项目名称",
            "firstIssueDate": "首次取证时间",
            "latestIssueDate": "最新取证时间",
            "totalSuites": "住宅总套数",
            "remainingSuites": "剩余套数（住宅总套数-累计已售）",
            "availableSuites": "绿色可售套数（房源状态明细）",
            "cumulativeSoldSuites": "累计已售（已签约+网上联机备案+整栋已售完）",
            "screenshotInfo": "项目详情截图字段和证据链接信息",
        },
        "projects": [projects_by_name[name] for name in sorted(projects_by_name)],
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
    project["officialSoldOutSuites"] = result.get("soldOutSuites", 0)
    permit_gate = result.get("permitClosureGate") or {}
    project["officialPermitClosureComplete"] = bool(permit_gate.get("canClose"))
    project["officialMissingPermitBatches"] = permit_gate.get("missingPermits") or []
    project["officialSignedStatsSuites"] = result.get("signedStatsSuites")
    project["officialSignedStatsArea"] = result.get("signedStatsArea")
    project["officialSignedStatsAvgPrice"] = result.get("signedStatsAvgPrice")
    project["officialSignedSuites"] = result["signedSuites"]
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
        + ";\nconst PROJECT_METADATA_OVERRIDES"
    )
    return text[: match.start()] + replacement + text[match.end() :]


def render_override_entry(result: dict[str, Any]) -> str:
    permit_gate = result.get("permitClosureGate") or {}
    lines = [
        f'  "{result["dashboardName"]}": {{',
        f"    unsignedSuites: {result['unsignedSuites']},",
        f"    availableSuites: {result['availableSuites']},",
        f"    bookedSuites: {result['bookedSuites']},",
        f"    contractSignedSuites: {result['contractSignedSuites']},",
        f"    filedSuites: {result['filedSuites']},",
        f"    soldOutSuites: {result.get('soldOutSuites', 0)},",
        f"    permitClosureComplete: {js_string(bool(permit_gate.get('canClose')))},",
        f"    missingPermitBatches: {js_string(permit_gate.get('missingPermits') or [])},",
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
    permit_gate = result.get("permitClosureGate") or {}
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
        f"    officialSoldOutSuites: {result.get('soldOutSuites', 0)},",
        f"    officialPermitClosureComplete: {js_string(bool(permit_gate.get('canClose')))},",
        f"    officialMissingPermitBatches: {js_string(permit_gate.get('missingPermits') or [])},",
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
    parser.add_argument("--retry-attempts", type=int, default=2)
    parser.add_argument("--retry-delay", type=float, default=1.0)
    parser.add_argument("--retry-timeout-step", type=int, default=8)
    parser.add_argument(
        "--validation-rounds",
        type=int,
        default=5,
        help="全量校验失败项目的循环轮数；每轮只重抓上一轮失败项目",
    )
    parser.add_argument("--max-projects", type=int, default=0)
    parser.add_argument("--project-name", action="append", default=[])
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
    if args.project_name:
        selected_names = {str(name).strip() for name in args.project_name if str(name).strip()}
        watchlist = [
            item for item in watchlist
            if str(item.get("dashboardName") or item.get("name") or "") in selected_names
        ]
        if not watchlist:
            raise ValueError(f"未找到指定项目: {', '.join(sorted(selected_names))}")
    if args.max_projects > 0:
        watchlist = watchlist[: args.max_projects]

    results: list[dict[str, Any]] = []
    pending: list[tuple[dict[str, Any], list[dict[str, Any]]]] = [(item, []) for item in watchlist]
    final_failures_by_name: dict[str, dict[str, Any]] = {}
    validation_round = 1
    max_validation_rounds = max(1, args.validation_rounds)
    while pending and validation_round <= max_validation_rounds:
        print(
            f"=== 全量闭合校验第 {validation_round}/{max_validation_rounds} 轮："
            f"待抓取 {len(pending)} 个项目 ===",
            file=sys.stderr,
            flush=True,
        )
        next_pending: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for index, (item, previous_attempts) in enumerate(pending, 1):
            project_name = str(item.get("dashboardName") or item.get("name") or "")
            print(
                f"[第{validation_round}轮 {index}/{len(pending)}] 抓取 {project_name}",
                file=sys.stderr,
                flush=True,
            )
            result, attempts, exc = scrape_project_with_retry(
                item,
                delay=args.delay,
                timeout=args.timeout,
                max_workers=args.max_workers,
                retry_attempts=args.retry_attempts,
                retry_delay=args.retry_delay,
                retry_timeout_step=args.retry_timeout_step,
            )
            annotated_attempts = [
                {**attempt, "validationRound": validation_round}
                for attempt in attempts
            ]
            all_attempts = [*previous_attempts, *annotated_attempts]
            if result:
                result["validationRound"] = validation_round
                result["retryAttempts"] = len(all_attempts)
                if all_attempts:
                    result["retryErrors"] = all_attempts
                # 按 coverageStatus 分档：
                # complete → 正式 results，可更新正式库存
                # partial/mismatch/unavailable → 写入 failures，保留结果但不更新正式库存
                coverage_status = result.get("coverageStatus", COVERAGE_COMPLETE)
                if coverage_status == COVERAGE_COMPLETE:
                    results.append(result)
                    final_failures_by_name.pop(project_name, None)
                    print(
                        f"  {project_name}: 完整闭合 {result['residentialTotal']}套",
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    coverage_note = result.get("coverageNote", "")
                    # 非 complete 结果记入 failures，但保留完整抓取数据供人工复查
                    failure = {
                        "projectName": project_name,
                        "source": item.get("source") or "dashboard",
                        "dashboardCollection": item.get("dashboardCollection") or "",
                        "dashboardId": item.get("dashboardId"),
                        "expectedResidentialTotal": int(parse_number(item.get("residentialTotal")) or 0),
                        "url": primary_url(item),
                        "urls": item_urls(item),
                        "error": f"coverageStatus={coverage_status}: {coverage_note}",
                        "coverageStatus": coverage_status,
                        "coverageNote": coverage_note,
                        "failureClass": result.get("failureClass") or "",
                        "retryable": bool(result.get("retryable")),
                        "partialResult": {
                            "residentialTotal": result.get("residentialTotal"),
                            "approvedResidentialTotal": result.get("approvedResidentialTotal"),
                            "roomStatusTotal": result.get("roomStatusTotal"),
                            "availableSuites": result.get("availableSuites"),
                            "signedSuites": result.get("signedSuites"),
                            "buildingCount": result.get("buildingCount"),
                            "buildingStatusErrors": result.get("buildingStatusErrors"),
                            "failureClass": result.get("failureClass") or "",
                            "retryable": bool(result.get("retryable")),
                            "permitCoverage": result.get("permitCoverage") or [],
                            "missingPermitBatches": result.get("missingPermitBatches") or [],
                        },
                        "validationRounds": validation_round,
                        "retryAttempts": max(0, len(all_attempts) - 1),
                        "attemptCount": len(all_attempts),
                        "attempts": all_attempts,
                        "oldValueRetained": False,
                        "fetchedAt": result.get("fetchedAt") or datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    final_failures_by_name[project_name] = failure
                    print(
                        f"  {project_name}: {coverage_status} — {coverage_note}",
                        file=sys.stderr,
                        flush=True,
                    )
                    # 只有空壳/网络失败回到 retry loop；结构、口径和颜色
                    # 问题直接停在失败审计通道，避免无效消耗请求。
                    if result.get("retryable"):
                        next_pending.append((item, all_attempts))
                continue
            failure = {
                "projectName": project_name,
                "source": item.get("source") or "dashboard",
                "dashboardCollection": item.get("dashboardCollection") or "",
                "dashboardId": item.get("dashboardId"),
                "expectedResidentialTotal": int(parse_number(item.get("residentialTotal")) or 0),
                "url": primary_url(item),
                "urls": item_urls(item),
                "error": str(exc) if exc else "未知错误",
                "failureClass": (
                    "network" if attempts and attempts[-1].get("retryable") else "structure"
                ),
                "retryable": bool(attempts and attempts[-1].get("retryable")),
                "validationRounds": validation_round,
                "retryAttempts": max(0, len(all_attempts) - 1),
                "attemptCount": len(all_attempts),
                "attempts": all_attempts,
                "oldValueRetained": False,
                "fetchedAt": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            }
            final_failures_by_name[project_name] = failure
            print(
                f"  第{validation_round}轮失败 {project_name}: {failure['error']}",
                file=sys.stderr,
                flush=True,
            )
            if failure["retryable"]:
                next_pending.append((item, all_attempts))
        if next_pending:
            failed_names = "、".join(
                str(item.get("dashboardName") or item.get("name") or "")
                for item, _ in next_pending
            )
            print(
                f"第 {validation_round} 轮未闭合 {len(next_pending)} 个项目：{failed_names}",
                file=sys.stderr,
                flush=True,
            )
            if validation_round < max_validation_rounds:
                time.sleep(max(0, args.retry_delay))
        pending = next_pending
        validation_round += 1

    failures = [final_failures_by_name[name] for name in sorted(final_failures_by_name)]

    if not results:
        write_snapshot(args.snapshot, [], failures)
        print(json.dumps({"projects": [], "failures": failures, "snapshot": str(args.snapshot)}, ensure_ascii=False, indent=2))
        return 1

    update_history(args.history, results)
    write_snapshot(args.snapshot, results, failures)
    # 只有 coverageComplete 的结果才更新 dashboard
    complete_results = [r for r in results if r.get("coverageStatus") == COVERAGE_COMPLETE]
    dashboard_changed = apply_dashboard(args.dashboard, complete_results) if args.apply_dashboard and complete_results else False
    if args.sync_watchlist_from_dashboard and args.apply_dashboard and results:
        write_watchlist(args.watchlist, load_official_projects_from_dashboard(args.dashboard))
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
                        "coverageStatus": item.get("coverageStatus", COVERAGE_COMPLETE),
                        "retryAttempts": item.get("retryAttempts", 0),
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
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
