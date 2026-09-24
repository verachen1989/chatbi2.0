#!/usr/bin/env python3
"""Reconcile public Beijing land results with the embedded static dashboard.

Default: dry run. --apply writes only after the complete crawl validates.
Raw pages, proposed HTML, and a field-level report are saved for every run.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
import sys
import time
import unicodedata
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = Path("land_tracker_dashboard_20260614/index.html")
SOURCES = Path("data/land_tracker_sources.json")
INDEX_URL = "https://ggzyfw.beijing.gov.cn/zpgcjzd/index.html"
DATE_FIELDS = ("dealDate", "planningPermit", "constructionPlan", "constructionPermit", "firstPresale", "firstCompletion")
PROTECTED = ("projectName", "plate", "brand", *DATE_FIELDS[1:])
OFFICIAL_FIELDS = ("landName", "landCode", "district", "dealDate", "bidder", "amount", "floorPrice")
DISTRICTS = {"东": "东城区", "西": "西城区", "朝": "朝阳区", "海": "海淀区", "丰": "丰台区", "石": "石景山区", "门": "门头沟区", "房": "房山区", "通": "通州区", "顺": "顺义区", "昌": "昌平区", "兴": "大兴区", "怀": "怀柔区", "平": "平谷区", "密": "密云区", "延": "延庆区", "开": "经开区"}


class ValidationError(ValueError):
    pass


class SourceUnavailable(ValidationError):
    pass


class IncompleteCrawl(ValidationError):
    pass


def is_transient(error):
    if isinstance(error, requests.exceptions.SSLError):
        return False
    if isinstance(error, (SourceUnavailable, requests.exceptions.Timeout,
                          requests.exceptions.ConnectionError, subprocess.TimeoutExpired)):
        return True
    if isinstance(error, requests.exceptions.HTTPError) and error.response is not None:
        return error.response.status_code == 429 or 500 <= error.response.status_code < 600
    return False


def clean(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value)))


def code_key(value):
    key = clean(value)
    if not re.fullmatch(r"京土储挂\([东西朝海丰石门房通顺昌兴怀平密延开]\)\[20\d{2}\]\d+号", key):
        raise ValidationError(f"Unrecognized land code: {value}")
    return key


def parse_date(value):
    for fmt in ("%Y年%m月%d日", "%Y-%m-%d", "%Y/%m/%d", "%y/%m/%d"):
        try:
            return datetime.strptime(clean(value), fmt).date()
        except ValueError:
            pass
    raise ValidationError(f"Invalid date: {value}")


def number(value, unit):
    text = clean(value).replace(",", "")
    found = re.findall(r"\d+(?:\.\d+)?", text)
    if len(found) != 1 or unit not in text or re.search(r"[-负至~～]", text):
        raise ValidationError(f"Ambiguous number/unit: {value}")
    result = Decimal(found[0])
    if result <= 0:
        raise ValidationError(f"Non-positive number: {value}")
    return result


def parse_list(page, url):
    soup = BeautifulSoup(page, "html.parser")
    links = soup.select("#cmsContent .article-listjy2 a.divtitlejy")
    entries = []
    for link in links:
        target = urljoin(url, link.get("href", ""))
        if not re.fullmatch(r"https://ggzyfw\.beijing\.gov\.cn/zpgcjzd/\d{8}/\d+\.html", target):
            raise ValidationError(f"Unexpected detail URL: {target}")
        text = link.get_text(" ", strip=True)
        project = re.search(r"\bS\d+C\d+\b", text)
        district = re.search(r"【([^】]+)】", text)
        name = link.get("title", "").strip()
        if not name or not project or not district:
            raise ValidationError(f"Incomplete list item: {target}")
        entries.append(dict(url=target, name=name, projectId=project[0], district=district[1]))
    pages = soup.select_one("#cmsContent .pages")
    count = re.search(r"共\s*(\d+)\s*条记录\s*(\d+)\s*/\s*(\d+)\s*页", pages.get_text(" ", strip=True) if pages else "")
    if not entries or not count:
        raise ValidationError("List structure changed or empty response; publication stopped")
    total, current, last = map(int, count.groups())
    if not 1 <= current <= last <= 200 or len(entries) > total:
        raise ValidationError("Invalid pagination")
    return entries, total, current, last


def classify_use(value):
    text = clean(value)
    if re.search(r"居住用地|住宅用地|混合住宅", text):
        return "residential"
    if re.search(r"多功能|混合", text):
        return "review"
    if re.search(r"商业|商务|工业|物流|仓储|教育|医疗|科研|公园|绿地|交通|公用设施|供电|供水|环卫|体育|托幼", text):
        return "excluded"
    return "review"


def parse_detail(page, entry, today):
    soup = BeautifulSoup(page, "html.parser")
    title = soup.select_one(".div-title")
    content = soup.select_one(".newsCon")
    if not title or not content:
        raise ValidationError(f"Detail structure changed: {entry['url']}")
    if entry["projectId"] not in title.get_text():
        raise ValidationError("List/detail project identifier mismatch")
    title_copy = copy.copy(title)
    for paragraph in title_copy.select("p"):
        paragraph.decompose()
    name = title_copy.get_text(" ", strip=True)
    if clean(name) != clean(entry["name"]):
        raise ValidationError("List/detail land name mismatch")
    values = {}
    for tr in content.select("tr"):
        cells = tr.find_all(["td", "th"], recursive=False)
        for i in range(len(cells) - 1):
            label = clean(cells[i].get_text()).rstrip(":")
            if label in ("交易文件编号", "建设用地面积", "规划建筑面积", "用地性质", "地块位置", "成交时间", "成交价格", "竞得人"):
                value = cells[i + 1].get_text(" ", strip=True)
                if label in values and values[label] != value:
                    raise ValidationError(f"Conflicting official label: {label}")
                values[label] = value
    if not values.get("用地性质"):
        raise ValidationError("Missing official land use")
    evidence = dict(entry, landUse=values["用地性质"], rawFields=values)
    status = classify_use(values["用地性质"])
    if status != "residential":
        return status, None, evidence
    # A listing date or auction closing date is never a substitute for the deal date.
    for field in ("交易文件编号", "成交时间", "成交价格", "规划建筑面积", "竞得人"):
        if not values.get(field) or clean(values[field]) in ("无", "-", "暂无"):
            raise ValidationError(f"Missing transaction field: {field}")
    key = code_key(values["交易文件编号"])
    district = DISTRICTS[re.search(r"\((.)\)", key)[1]]
    district_matches = entry["district"] == district or (district == "经开区" and "北京经济技术开发区" in name and entry["district"] in ("市级", "北京经济技术开发区"))
    if not district_matches:
        raise ValidationError("List/detail district mismatch")
    deal = parse_date(values["成交时间"])
    if deal > today:
        raise ValidationError("Future transaction date")
    if deal < date(2025, 1, 1):
        return "before_scope", None, evidence
    amount_wan = number(values["成交价格"], "万元")
    area = number(values["规划建筑面积"], "平方米")
    floor = (amount_wan * 10000 / area).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    row = {"landName": name, "landCode": values["交易文件编号"], "district": district,
           "dealDate": deal.strftime("%y/%m/%d"), "bidder": values["竞得人"],
           "amount": float(amount_wan / 10000), "floorPrice": float(floor)}
    evidence.update(dealDate=deal.isoformat(), amountWan=str(amount_wan), plannedAreaM2=str(area))
    return status, row, evidence


def read_rows(page):
    marker = "const LAND_ROWS = "
    if page.count(marker) != 1:
        raise ValidationError("Expected exactly one LAND_ROWS block")
    start = page.index(marker) + len(marker)
    rows, length = json.JSONDecoder().raw_decode(page[start:])
    if not isinstance(rows, list) or not rows or page[start + length:start + length + 1] != ";":
        raise ValidationError("Invalid dashboard data block")
    return rows, start, start + length


def validate_rows(rows, today):
    seen = set()
    for row in rows:
        key = code_key(row.get("landCode", ""))
        if key in seen:
            raise ValidationError(f"Duplicate land code: {key}")
        seen.add(key)
        for field in ("landName", "district", "bidder"):
            if not row.get(field):
                raise ValidationError(f"Missing {field}: {key}")
        for field in DATE_FIELDS:
            if row.get(field):
                parsed = parse_date(row[field])
                if parsed > today:
                    raise ValidationError(f"Future {field}: {key}")
        if parse_date(row["dealDate"]) < date(2025, 1, 1):
            raise ValidationError(f"Transaction outside scope: {key}")
        for field in ("amount", "floorPrice"):
            value = Decimal(str(row.get(field)))
            if not value.is_finite() or value <= 0:
                raise ValidationError(f"Invalid {field}: {key}")


def merge_rows(old, incoming, today, warnings=None):
    validate_rows(old, today)
    rows = {code_key(r["landCode"]): copy.deepcopy(r) for r in old}
    added, updated = [], []
    seen = set()
    for row in incoming:
        key = code_key(row["landCode"])
        if key in seen:
            raise ValidationError(f"Duplicate official land code: {key}")
        seen.add(key)
        previous = rows.get(key)
        if previous is None:
            rows[key] = {field: "" for field in PROTECTED}
            rows[key].update(row)
            added.append(copy.deepcopy(row))
            continue
        changes = {field: {"before": previous[field], "after": row[field]}
                   for field in OFFICIAL_FIELDS
                   if previous.get(field) != row[field] and not (isinstance(row[field], str) and clean(previous.get(field, "")) == clean(row[field]))}
        if "landName" in changes and (clean(previous["landName"]).startswith(clean(row["landName"])) or len(clean(row["landName"])) < len(clean(previous["landName"])) * 0.9):
            if warnings is not None:
                warnings.append({"landCode": key, "reason": "官网标题变短，保留现有完整名称待核", **changes["landName"]})
            del changes["landName"]
        if "dealDate" in changes or any(field in changes and abs(Decimal(str(row[field])) / Decimal(str(previous[field])) - 1) > Decimal("0.10") for field in ("amount", "floorPrice")):
            raise ValidationError(f"Material historical conflict requires review: {key}: {changes}")
        if changes:
            updated.append({"landCode": key, "fields": changes})
            for field in changes:
                previous[field] = row[field]
    result = sorted(rows.values(), key=lambda r: (parse_date(r["dealDate"]), code_key(r["landCode"])), reverse=True)
    for i, row in enumerate(result, 1):
        row["seq"] = i
    validate_rows(result, today)
    for row in old:
        for field in PROTECTED:
            if rows[code_key(row["landCode"])].get(field) != row.get(field):
                raise ValidationError(f"Enrichment field overwritten: {field}")
    return result, added, updated


def embed_rows(page, rows):
    _, start, end = read_rows(page)
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"), allow_nan=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    result = page[:start] + payload + page[end:]
    result, count = re.subn(r'(<strong[^>]*id="tableCount"[^>]*>)[^<]*(</strong>)', lambda m: m[1] + str(len(rows)) + " 条" + m[2], result)
    if count != 1 or read_rows(result)[0] != rows:
        raise ValidationError("Generated dashboard failed round-trip validation")
    return result


def snapshot_name(url):
    return Path(urlparse(url).path).name


class Fetcher:
    def __init__(self, output, snapshot_dir=None, interval=0.5):
        self.output, self.snapshot_dir, self.interval = output, snapshot_dir, interval
        self.use_curl = False
        self.session = requests.Session()
        retries = Retry(total=3, other=0, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))
        self.session.mount("https://", HTTPAdapter(max_retries=retries))
        self.session.headers["User-Agent"] = "BeijingLandTracker/1.0 (+https://github.com/verachen1989/chatbi2.0)"

    def get(self, url, suffix=""):
        name = snapshot_name(url)
        if self.snapshot_dir:
            content = (self.snapshot_dir / name).read_text(encoding="utf-8-sig")
        else:
            time.sleep(self.interval)
            try:
                if self.use_curl:
                    raise requests.exceptions.SSLError("Use system TLS for this run")
                response = self.session.get(url, timeout=(10, 30))
                response.raise_for_status()
                if urlparse(response.url).hostname != "ggzyfw.beijing.gov.cn":
                    raise ValidationError("Official page redirected to a different host")
                content = response.content.decode("utf-8-sig")
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                self.use_curl = True
                # System TLS and IPv4 also handle EC/IPv6 failures on some runners.
                # Certificate verification stays on; no proxy or guessed IP is used.
                result = subprocess.run(["curl", "--ipv4", "--curves", "prime256v1", "--fail", "--silent", "--show-error", "--proto", "=https",
                                         "--connect-timeout", "10", "--max-time", "45", "--retry", "2", "--retry-all-errors", url],
                                        capture_output=True, timeout=150)
                if result.returncode:
                    error_type = SourceUnavailable if result.returncode in (5, 6, 7, 18, 28, 52, 55, 56) else ValidationError
                    raise error_type("IPv4/system TLS fallback failed: " + result.stderr.decode(errors="replace"))
                content = result.stdout.decode("utf-8-sig")
        self.output.mkdir(parents=True, exist_ok=True)
        (self.output / (name + suffix)).write_text(content, encoding="utf-8")
        return content


def crawl(fetcher, today, report):
    entries, total, current, last = parse_list(fetcher.get(INDEX_URL), INDEX_URL)
    if current != 1:
        raise ValidationError("First list page has incorrect page number")
    first = copy.deepcopy(entries)
    for page in range(2, last + 1):
        url = urljoin(INDEX_URL, f"index_{page}.html")
        more, count, number_, last_ = parse_list(fetcher.get(url), url)
        if (count, number_, last_) != (total, page, last):
            raise ValidationError("Pagination changed during crawl")
        entries.extend(more)
    if len(entries) != total or len({e["url"] for e in entries}) != total or len({e["projectId"] for e in entries}) != total:
        raise ValidationError("List coverage or uniqueness check failed")
    report["listed"] = total
    report["pages"] = last
    incoming, sources = [], {}
    for index, entry in enumerate(entries, 1):
        print(f"[{index}/{total}] {entry['name']}", flush=True)
        try:
            status, row, evidence = parse_detail(fetcher.get(entry["url"]), entry, today)
            evidence["classification"] = status
            report["evidence"].append(evidence)
            if row:
                incoming.append(row)
                key = code_key(row["landCode"])
                if key in sources:
                    raise ValidationError(f"Duplicate official code: {key}")
                sources[key] = evidence
            else:
                report[status].append(evidence)
        except (ValidationError, requests.RequestException, OSError, UnicodeError, subprocess.SubprocessError) as error:
            report["errors"].append({"url": entry["url"], "error": str(error), "retryable": is_transient(error)})
    again, count, current, last_ = parse_list(fetcher.get(INDEX_URL, ".end"), INDEX_URL)
    if (again, count, current, last_) != (first, total, 1, last):
        raise ValidationError("Official list changed while fetching; retry the run")
    if report["errors"] or not incoming:
        raise IncompleteCrawl("Incomplete crawl; see errors in report.json")
    return incoming, sources


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def write_report(report, directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    lines = ["# 地块自动采集核对报告", "", f"运行时间：{report['checkedAt']}", f"状态：{report['status']}；模式：{report['mode']}", "",
             f"官网列表：{report.get('listed', 0)} 条 / {report.get('pages', 0)} 页", f"原有：{report.get('before', 0)}；生成后：{report.get('after', 0)}；新增：{len(report['added'])}；更新：{len(report['updated'])}",
             f"排除非住宅：{len(report['excluded'])}；用途待核：{len(report['review'])}；错误：{len(report['errors'])}", "",
             "官网列表为当前公开窗口，历史记录保留。本成交采集阶段不修改项目、品牌和证照，后续由独立核验阶段处理。", "", "## 新增", ""]
    for row in report["added"]:
        evidence = next(e for e in report["evidence"] if e.get("rawFields", {}).get("交易文件编号") == row["landCode"])
        lines.append(f"- [{row['landName']}]({evidence['url']})：{row['dealDate']}，{row['amount']} 亿元，{row['floorPrice']} 元/㎡，{row['bidder']}")
    for title, key in (("字段修改", "updated"), ("用途待核", "review"), ("存量数据提示", "warnings"), ("错误", "errors")):
        lines.extend(["", f"## {title}", ""])
        lines.extend("- " + json.dumps(item, ensure_ascii=False) for item in report[key])
    (directory / "report.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def run(args):
    report_dir = args.report_dir
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    report = {"checkedAt": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"), "mode": "apply" if args.apply else "dry-run", "status": "failed",
              "added": [], "updated": [], "excluded": [], "before_scope": [], "review": [], "errors": [], "warnings": [], "evidence": []}
    try:
        page_path = args.root / DASHBOARD
        page = page_path.read_text(encoding="utf-8")
        old = read_rows(page)[0]
        report["before"] = len(old)
        validate_rows(old, today)
        incoming, source_updates = crawl(Fetcher(report_dir / "raw", args.snapshot_dir), today, report)
        rows, report["added"], report["updated"] = merge_rows(old, incoming, today, report["warnings"])
        report["after"] = len(rows)
        report["matched"] = len(incoming) - len(report["added"])
        for row in rows:
            for field in DATE_FIELDS[1:]:
                if row.get(field) and parse_date(row[field]) < parse_date(row["dealDate"]):
                    report["warnings"].append({"landCode": row["landCode"], "field": field, "value": row[field], "reason": "节点早于本次成交，保留原值，需人工核验关联批次"})
        sources_path = args.root / SOURCES
        sources = json.loads(sources_path.read_text(encoding="utf-8")) if sources_path.exists() else {"schemaVersion": 1, "records": {}}
        sources["records"].update(source_updates)
        output = embed_rows(page, rows)
        report["htmlSha256"] = hashlib.sha256(output.encode()).hexdigest()
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "proposed.html").write_text(output, encoding="utf-8")
        source_json = json.dumps(sources, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
        (report_dir / "proposed_sources.json").write_text(source_json, encoding="utf-8")
        if args.apply:
            if page_path.read_text(encoding="utf-8") != page:
                raise ValidationError("Dashboard edited during crawl; retry to preserve the newer edits")
            atomic_write(sources_path, source_json)
            atomic_write(page_path, output)
        report["status"] = "passed"
    except (ValidationError, requests.RequestException, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        previous_transient = all(e.get('retryable') for e in report['errors'])
        report['retryable'] = previous_transient and (
            bool(report['errors']) if isinstance(error, IncompleteCrawl) else is_transient(error))
        report["errors"].append({"error": str(error)})
    finally:
        write_report(report, report_dir)
    print(json.dumps({k: report.get(k) for k in ("status", "mode", "listed", "before", "after", "matched")}, ensure_ascii=False))
    print(f"Added: {len(report['added'])}; updated: {len(report['updated'])}; report: {report_dir / 'report.md'}")
    for error in report["errors"]:
        message = json.dumps(error, ensure_ascii=False).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print("::error title=Land tracker validation::" + message)
    return 0 if report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Update dashboard after validation")
    mode.add_argument("--dry-run", action="store_true", help="Report only (default)")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--report-dir", type=Path, default=ROOT / "reports/land-tracker")
    parser.add_argument("--snapshot-dir", type=Path, help="Replay saved raw pages offline")
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
