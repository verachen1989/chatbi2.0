"""Official project/permit collection. Candidate discovery is not confirmation."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from update_land_tracker import ValidationError, clean, code_key, parse_date

ZJW = "http://bjjs.zjw.beijing.gov.cn"
PLANNING = "https://yewu.ghzrzyw.beijing.gov.cn/zkdncms/cxghspjggsszjsjsgcghxkz"
PLAN_SEARCH = ZJW + "/eportal/ui?pageId=53618409&moduleId=62c764a6f884495dbce365a556cd761a"
FIELDS = ("projectName", "brand", "planningPermit", "constructionPlan", "constructionPermit", "firstPresale", "firstCompletion")
SOURCE_FIELDS = {"planning": "planningPermit", "plan": "constructionPlan", "construction": "constructionPermit", "presale": "firstPresale", "completion": "firstCompletion"}
PERMIT_PATTERN = r"20\d{2}\s*规[自字]\s*[（(][^）)]+[）)]\s*(?:简)?建字\s*\d+\s*号"


def normalized(value):
    return clean(value).replace("–", "-").replace("—", "-").upper()


def parcel_codes(text):
    text = normalized(text)
    pattern = r"(?:(?:[A-Z]{2}\d{2}|FZX)-\d{4}-(?:\d{2,4}(?:-\d+)?|X\d+[A-Z]\d(?:-\d+)?)|(?<![\d-])\d{4}-\d{2,4}(?:-\d+)?|(?<![\d-])\d{2}-\d{3}(?:-\d+)?|DC-L\d+|NY-\d+\(南区\)-\d+)(?![\dA-Z-])"
    result = set()
    for match in re.finditer(pattern, text):
        token = match[0]
        result.add(token)
        # Expand abbreviated sibling parcels, not arbitrary numbers elsewhere.
        suffix = re.match(r"(?:[、,]\d{2,4}(?![\dA-Z-]))+", text[match.end():])
        if suffix and "-" in token:
            prefix = token.rsplit("-", 1)[0] + "-"
            result.update(prefix + n for n in re.findall(r"\d+", suffix[0]))
    return result


def permit_keys(text):
    return {normalized(x) for x in re.findall(PERMIT_PATTERN, text)}


def complete_permit_refs(text):
    remainder = re.sub(PERMIT_PATTERN, "", text)
    return bool(permit_keys(text)) and not re.search(r"[\w\u4e00-\u9fff]", remainder)


def residential_scope(name):
    text = normalized(name)
    if re.search(r"装修|装饰|临时|售楼处|样板间|基坑|土护降|土方|桩基|施工准备", text):
        return False
    # R2 in the land-use name alone does not prove the licensed work is residential.
    scope = text.rsplit("地块", 1)[-1] if "地块" in text else text
    brackets = re.findall(r"\(([^()]*)\)", scope)
    if brackets:
        scope = brackets[-1]
    return bool(re.search(r"住宅楼|住宅及|住宅等|住宅工程|住宅项目|商品住房|商品住宅", scope))


def table_fields(soup):
    result = {}
    for tr in soup.select("tr"):
        cells = tr.find_all(["td", "th"], recursive=False)
        if len(cells) == 2:
            label = clean(cells[0].get_text()).rstrip(":")
            if len(label) <= 35:
                result[label] = cells[1].get_text(" ", strip=True)
    return result


def iso(value):
    if str(value).isdigit() and len(str(value)) == 13:
        return datetime.fromtimestamp(int(value) / 1000, ZoneInfo("Asia/Shanghai")).date().isoformat()
    return parse_date(value).isoformat()


class PublicClient:
    def __init__(self, directory, replay=None, delay=0.5, resume=None):
        self.directory, self.replay, self.delay, self.resume = Path(directory), replay, delay, resume
        self.memory = {}
        self.directory.mkdir(parents=True, exist_ok=True)

    def get(self, url, data=None):
        parsed = urlparse(url)
        allowed = parsed.hostname in {"bjjs.zjw.beijing.gov.cn", "zjw.beijing.gov.cn", "yewu.ghzrzyw.beijing.gov.cn", "www.shougang.com.cn", "www.bcegc.com"}
        if not allowed or parsed.scheme not in {"http", "https"} or parsed.username:
            raise ValidationError("Unapproved public source: " + url)
        request = {"url": url, "data": data}
        key = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if key in self.memory:
            return self.memory[key]
        filename = key + ".txt"
        if self.replay:
            text = (Path(self.replay) / filename).read_text(encoding="utf-8")
        elif self.resume and (Path(self.resume) / filename).exists():
            text = (Path(self.resume) / filename).read_text(encoding="utf-8")
        else:
            time.sleep(self.delay)
            cmd = ["curl", "--ipv4", "--curves", "P-256", "--fail", "--silent", "--show-error", "--connect-timeout", "10", "--max-time", "30", "--retry", "2", "--retry-all-errors"]
            if data is not None:
                cmd += ["--data", urlencode(data)]
            # Redirects are deliberately not followed to login or third-party hosts.
            cmd += ["--write-out", "\n%{http_code}", url]
            fetched = subprocess.run(cmd, capture_output=True, timeout=110)
            if fetched.returncode:
                raise ValidationError(f"Source unavailable {url}: {fetched.stderr.decode(errors='replace')[:300]}")
            payload, status = fetched.stdout.rsplit(b"\n", 1)
            if status != b"200":
                raise ValidationError(f"Unexpected HTTP {status.decode()} from {url}")
            text = payload.decode("utf-8-sig")
        (self.directory / filename).write_text(text, encoding="utf-8")
        (self.directory / (key + ".request.json")).write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        self.memory[key] = text
        return text


def parse_planning(data, url):
    if not isinstance(data, dict) or str(data.get("code")) != "0" or not isinstance(data.get("data"), list):
        raise ValidationError("Planning response schema changed")
    records = []
    for row in data["data"]:
        for field in ("id", "faWenHao", "faWenRiQi", "xiangMuMingCheng", "jianSheDanWei"):
            if not row.get(field):
                raise ValidationError("Missing planning field: " + field)
        records.append({"kind": "planning", "id": row["id"], "name": row["xiangMuMingCheng"],
                        "developer": row["jianSheDanWei"], "location": row.get("jianSheWeiZhi", ""),
                        "district": row.get("jianSheDiQu", ""), "permit": row["faWenHao"],
                        "date": iso(row["faWenRiQi"]), "url": PLANNING + "/esSearchDetail/" + row["id"],
                        "residential": residential_scope(row["xiangMuMingCheng"]), "valid": not row.get("deleted") and str(row.get("shiFouFaZhengBen")) == "1",
                        "raw": row})
    return records


def portal_pagination(soup):
    total = re.search(r"总记录数\s*[:：]\s*(\d+)", soup.get_text(" ", strip=True))
    current, size = soup.select_one("input#currentPage"), soup.select_one("input#pageSize")
    if not total or not current or not size:
        raise ValidationError("Portal pagination missing (not a verified empty result)")
    return int(total[1]), int(current["value"]), int(size["value"])


def parse_portal_list(text, kind, url):
    soup = BeautifulSoup(text, "html.parser")
    total, current, size = portal_pagination(soup)
    records = []
    for tr in soup.select("tr"):
        cells = tr.find_all("td", recursive=False)
        values = [c.get_text(" ", strip=True) for c in cells]
        if kind == "presale" and len(cells) == 3:
            link = cells[0].find("a", href=True)
            if not link or "projectID=" not in link["href"]:
                continue
            records.append({"kind": kind, "name": values[0], "permit": values[1], "date": iso(values[2]), "url": urljoin(ZJW, link["href"])})
        elif kind == "construction" and len(cells) == 6 and cells[0].get("class") == ["format_row"]:
            link = cells[-1].find("a", href=True)
            if not link:
                raise ValidationError("Missing construction detail link")
            records.append({"kind": kind, "name": values[1], "permit": values[2], "developer": values[3], "date": iso(values[4]), "url": urljoin(ZJW, link["href"])})
        elif kind == "completion" and len(cells) == 7 and "format_row" in cells[0].get("class", []):
            records.append({"kind": kind, "name": values[1], "developer": values[2], "permit": values[4], "date": iso(values[6]), "location": "", "district": values[5], "url": url, "residential": residential_scope(values[1]), "valid": True})
    if len(records) != min(size, max(0, total - (current - 1) * size)):
        raise ValidationError(f"{kind} list count mismatch: {len(records)}/{total} page {current}")
    return records, total, current, size


def parse_construction(text, entry):
    fields = table_fields(BeautifulSoup(text, "html.parser"))
    for key in ("工程名称", "建设单位", "发证日期", "施工许可证号"):
        if not fields.get(key):
            raise ValidationError("Construction detail missing " + key)
    if normalized(fields["施工许可证号"]) != normalized(entry["permit"]) or iso(fields["发证日期"]) != entry["date"]:
        raise ValidationError("Construction list/detail mismatch")
    return {**entry, "name": fields["工程名称"], "developer": fields["建设单位"], "district": fields.get("所在区县", ""),
            "location": fields.get("建设地址", ""), "valid": fields.get("证书状态", "") in ("有效", "1"),
            "residential": residential_scope(fields["工程名称"]), "raw": fields}


def parse_presale(text, entry):
    soup = BeautifulSoup(text, "html.parser")
    fields = {}
    for name in ("项目名称", "坐落位置", "开发企业", "预售许可证编号", "发证日期", "建设工程规划许可证编号", "批准预售部位"):
        cell = soup.find(id=name)
        if not cell:
            raise ValidationError("Presale detail missing " + name)
        fields[name] = cell.get_text(" ", strip=True)
    if normalized(fields["预售许可证编号"]) != normalized(entry["permit"]) or iso(fields["发证日期"]) != entry["date"]:
        raise ValidationError("Presale list/detail mismatch")
    buildings = []
    for tr in soup.select("tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 6:
            continue
        values = [x.get_text(" ", strip=True) for x in cells]
        if re.fullmatch(r"\d+", values[1]) and re.fullmatch(r"[\d.]+", values[2]):
            buildings.append({"name": values[0], "suites": int(values[1])})
    residential = any(b["suites"] > 0 and "住宅" in b["name"] and not re.search(r"车库|地下|库房|配套", b["name"]) for b in buildings)
    return {**entry, "name": fields["项目名称"], "developer": fields["开发企业"], "location": fields["坐落位置"],
            "planningRefs": sorted(permit_keys(fields["建设工程规划许可证编号"])), "residential": residential,
            "planningRefsComplete": complete_permit_refs(fields["建设工程规划许可证编号"]),
            "valid": not bool(re.search(r"已注销|已撤销|已作废", soup.get_text())), "approvedPart": fields["批准预售部位"], "buildings": buildings, "raw": fields}


def parse_plan_list(text, url):
    soup = BeautifulSoup(text, "html.parser")
    pages = re.search(r"共\s*(\d+)\s*页", soup.get_text(" ", strip=True))
    if not pages or not soup.find("input", {"name": "filter_LIKE_TITLE"}):
        raise ValidationError("Construction-plan search structure changed")
    records = []
    for link in soup.select("li a[title][href]"):
        if "/xmjsfa/" not in link["href"]:
            continue
        span = link.find_parent("li").find("span")
        if not span:
            raise ValidationError("Plan publication date missing")
        records.append({"kind": "plan", "name": link["title"], "date": iso(span.get_text()), "url": urljoin(ZJW, link["href"])})
    return records, int(pages[1])


def parse_plan(text, entry):
    soup = BeautifulSoup(text, "html.parser")
    body = soup.select_one("#container")
    if not body or not soup.title or normalized(soup.title.get_text()) != normalized(entry["name"]):
        raise ValidationError("Plan detail/title mismatch")
    content = body.get_text(" ", strip=True)
    published = re.search(r"发布时间\s*[:：]\s*(\d{4}年\d{1,2}月\d{1,2}日)", content)
    developer = re.search(r"建设单位\s*[:：]\s*(.*?)\s*20\d{2}\s*年", content)
    area = re.search(r"其中住宅建筑面积\s*([\d,.]+)\s*平方米", content)
    if not published or not developer or iso(published[1]) != entry["date"]:
        raise ValidationError("Plan developer/date missing or inconsistent")
    return {**entry, "developer": developer[1].strip(), "location": "", "permit": "", "residential": bool(area and float(area[1].replace(",", "")) > 0),
            "valid": not bool(re.search(r"变更|调整|撤销|作废", entry["name"])), "planningRefs": sorted(permit_keys(content)),
            "raw": {"title": entry["name"], "developer": developer[1], "published": published[1], "residentialArea": area[1] if area else ""}}


def direct_match(record, rows):
    codes = parcel_codes(record["name"])
    found = []
    for row in rows:
        overlap = codes & parcel_codes(row["landName"])
        if not overlap:
            continue
        context = normalized(" ".join(record.get(k, "") for k in ("name", "district", "location")))
        district = row["district"]
        district_ok = normalized(district).removesuffix("区") in context
        if district == "通州区":
            district_ok = district_ok or "北京城市副中心" in context
        elif district == "经开区":
            district_ok = district_ok or "亦庄" in context or "北京经济技术开发区" in context
        if district_ok:
            found.append((row, sorted(overlap)))
    if len(found) == 1:
        return code_key(found[0][0]["landCode"]), ["规划地块编号:" + ",".join(found[0][1]), "行政区/建设位置一致"]
    return None, ["跨宗地或行政区未确认"]


def query_terms(row):
    codes = sorted(parcel_codes(row["landName"]))
    if codes:
        return codes
    return [row["bidder"]] if "联合体" not in row["bidder"] else []


def presale_match(record, planning_links):
    if not record.get("planningRefsComplete", True):
        return None, []
    matching = [p for ref in record["planningRefs"] for p in planning_links.get(ref, [])
                if normalized(p["developer"]) == normalized(record["developer"])]
    keys = {p["landCode"] for p in matching}
    covered_refs = {normalized(p["permit"]) for p in matching}
    if len(keys) == 1 and covered_refs == set(record["planningRefs"]):
        return next(iter(keys)), ["规划许可证号完全一致", "开发企业与规划建设单位一致"]
    return None, []


class Collector:
    def __init__(self, client, rows, report):
        self.client, self.rows, self.report = client, rows, report
        self.records = {}
        self.developers = {}
        self.planning_links = {}

    def keep(self, record, key=None, basis=None):
        if key is None:
            key, basis = direct_match(record, self.rows)
        record = dict(record, landCode=key, matchBasis=basis or [])
        record_id = record["kind"] + ":" + record["url"] + ":" + record.get("permit", "")
        self.records[record_id] = record
        target = next((r for r in self.rows if code_key(r["landCode"]) == key), None)
        if target and record.get("valid") and parse_date(record["date"]) >= parse_date(target["dealDate"]):
            if record.get("residential") and record.get("developer"):
                self.developers.setdefault(key, set()).add(record["developer"])
            if record["kind"] == "planning":
                self.planning_links.setdefault(normalized(record["permit"]), []).append(record)
        return record

    def planning(self, row):
        for term in query_terms(row):
            total = None
            seen = set()
            for page in range(1, 21):
                url = PLANNING + "/esSearchList?" + urlencode({"gjz": term, "page": page, "limit": 10})
                data = json.loads(self.client.get(url))
                entries = parse_planning(data, url)
                if total is None:
                    total = int(data["count"])
                if total != int(data["count"]) or total > 200:
                    raise ValidationError("Planning pagination changed/too broad")
                for entry in entries:
                    if entry["id"] in seen:
                        raise ValidationError("Planning pagination repeated")
                    seen.add(entry["id"])
                    key, basis = direct_match(entry, self.rows)
                    if key:
                        detail = json.loads(self.client.get(entry["url"]))
                        actual = parse_planning(detail, entry["url"])
                        if len(actual) != 1 or any(actual[0][f] != entry[f] for f in ("id", "permit", "date", "developer")):
                            raise ValidationError("Planning list/detail mismatch")
                        entry = actual[0]
                        actual_key, basis = direct_match(entry, self.rows)
                        if actual_key != key:
                            raise ValidationError("Planning detail parcel differs from list")
                        entry["valid"] = entry["valid"] and not bool(detail.get("listZx"))
                        entry["amendments"] = {k: detail.get(k, []) for k in ("listZx", "listBg", "listYq")}
                    self.keep(entry, key, basis)
                if len(seen) >= total:
                    if len(seen) != total:
                        raise ValidationError("Planning count mismatch")
                    break
            else:
                raise ValidationError("Planning page limit reached")

    def plans(self, row):
        for term in query_terms(row):
            pages, seen = None, set()
            for page in range(1, 21):
                url = PLAN_SEARCH + "&currentPage=" + str(page)
                text = self.client.get(url, {"filter_LIKE_TITLE": term, "filter_LIKE_KEYWORDS": "", "filter_LIKE_CONTENT": ""})
                entries, count = parse_plan_list(text, url)
                if pages is not None and pages != count:
                    raise ValidationError("Plan pagination changed")
                pages = count
                for entry in entries:
                    if entry["url"] in seen:
                        raise ValidationError("Plan pagination repeated")
                    seen.add(entry["url"])
                    if parse_date(entry["date"]) < min(parse_date(r["dealDate"]) for r in self.rows):
                        continue
                    key, basis = direct_match(entry, self.rows)
                    if key:
                        self.keep(parse_plan(self.client.get(entry["url"]), entry), key, basis)
                    else:
                        self.keep(dict(entry, valid=False, residential=False, developer="", permit="", note="标题未匹配主表地块，未采详情"))
                if page >= pages:
                    break
            else:
                raise ValidationError("Plan page limit reached")

    def portal(self, kind, developer):
        page_ids = {"presale": "307670&isTrue=0", "construction": "53618592", "completion": "53618638"}
        developer_fields = {"presale": "developer", "construction": "filter_LIKE_jsdw", "completion": "filter_LIKE_jsdwmc"}
        url = ZJW + "/eportal/ui?pageId=" + page_ids[kind]
        total, seen = None, set()
        for page in range(1, 41):
            data = {developer_fields[kind]: developer, "currentPage": page, "pageSize": 15}
            text = self.client.get(url, data)
            entries, count, current, size = parse_portal_list(text, kind, url + "&" + urlencode(data))
            if current != page or (total is not None and total != count):
                raise ValidationError("Portal pagination changed")
            total = count
            for entry in entries:
                identity = entry["permit"]
                if identity in seen:
                    raise ValidationError("Duplicate permit in portal results")
                seen.add(identity)
                if parse_date(entry["date"]) < min(parse_date(r["dealDate"]) for r in self.rows):
                    continue
                if kind == "presale":
                    record = parse_presale(self.client.get(entry["url"]), entry)
                    key, basis = presale_match(record, self.planning_links)
                    self.keep(record, key, basis)
                elif kind == "construction":
                    self.keep(parse_construction(self.client.get(entry["url"]), entry))
                else:
                    self.keep(entry)
            if len(seen) >= total:
                if len(seen) != total:
                    raise ValidationError("Portal final coverage mismatch")
                return
        raise ValidationError("Portal page limit reached")

    def run(self):
        for i, row in enumerate(self.rows, 1):
            key = code_key(row["landCode"])
            print(f"Enrichment [{i}/{len(self.rows)}] {key}", flush=True)
            for kind, method in (("planning", self.planning), ("plan", self.plans)):
                self.attempt(kind, key, lambda: method(row))
        developers = set().union(*self.developers.values()) if self.developers else set()
        developers.update(r["bidder"] for r in self.rows if "联合体" not in r["bidder"])
        for i, developer in enumerate(sorted(developers), 1):
            print(f"Permit company [{i}/{len(developers)}] {developer}", flush=True)
            for kind in ("construction", "presale", "completion"):
                self.attempt(kind, developer, lambda: self.portal(kind, developer))
        return list(self.records.values())

    def attempt(self, kind, query, fn):
        before = copy.deepcopy(self.records)
        developers, links = copy.deepcopy(self.developers), copy.deepcopy(self.planning_links)
        try:
            fn()
            self.report["queries"].append({"source": kind, "query": query, "status": "passed"})
        except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
            self.records, self.developers, self.planning_links = before, developers, links
            self.report["errors"].append({"source": kind, "query": query, "error": str(error)})
        (self.client.directory.parent / "progress.json").write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")


def collect_identities(client, manifest, rows, report):
    records = []
    for source in manifest.get("identities", []):
        try:
            soup = BeautifulSoup(client.get(source["url"]), "html.parser")
            for tag in soup.select("script,style,nav,footer"):
                tag.decompose()
            content = normalized(soup.get_text(" ", strip=True))
            if not all(normalized(x) in content for x in source["requiredText"]):
                raise ValidationError("Identity evidence changed; assertions not present")
            values = {k: source[k] for k in ("projectName", "brand") if source.get(k)}
            if not values or not all(normalized(value) in content for value in values.values()):
                raise ValidationError("Identity value is not present in its official source")
            target = next(r for r in rows if code_key(r["landCode"]) == code_key(source["landCode"]))
            if not (parcel_codes(target["landName"]) & parcel_codes(" ".join(source["requiredText"]))):
                raise ValidationError("Identity source has no exact parcel evidence")
            records.append({"kind": "identity", "landCode": code_key(target["landCode"]), "name": source.get("projectName", ""), "brand": source.get("brand", ""),
                            "developer": "", "date": source["published"], "url": source["url"], "permit": "", "valid": True, "residential": True,
                            "matchBasis": ["企业官方公告明确所填名称/品牌", "公告包含精确规划地块编号"], "requiredText": source["requiredText"]})
        except (ValueError, KeyError, OSError, StopIteration, subprocess.SubprocessError) as error:
            report["errors"].append({"source": "identity", "url": source["url"], "error": str(error)})
    return records


def page_evidence(field, item):
    record = next(iter(item.get("records", [])), {})
    result = {"status": item["status"], "candidate": item.get("value", "") if record else "",
              "url": record.get("url", ""), "permit": record.get("permit", "")}
    if field == "projectName":
        result["nameType"] = item.get("nameType", "")
    if field == "planningPermit" and record:
        result["record"] = {key: record.get(key, "") for key in ("name", "developer", "district", "location", "permit")}
        result["record"].update(date=parse_date(record["date"]).strftime("%y/%m/%d"),
                                issuer=record.get("raw", {}).get("fzjg", ""),
                                area=record.get("raw", {}).get("jianZhuGuiMo", ""))
    return result


def reconcile(rows, records, today, previous=None):
    result = copy.deepcopy(rows)
    changes, review, evidence = [], [], {}
    previous = previous or {}
    for row in result:
        key = code_key(row["landCode"])
        deal = parse_date(row["dealDate"])
        valid = []
        for record in records:
            if record.get("landCode") != key:
                continue
            if not record.get("valid") or not record.get("residential"):
                review.append({"landCode": key, "kind": record["kind"], "reason": "非住宅、准备工程、变更/失效或用途待核，未回填", "url": record["url"]})
                continue
            day = parse_date(record["date"])
            if day < deal or day > today:
                review.append({"landCode": key, "kind": record["kind"], "reason": "日期超出成交日至核验日范围", "date": record["date"], "url": record["url"]})
                continue
            valid.append(record)
        fields = {}
        for kind, field in SOURCE_FIELDS.items():
            candidates = sorted((r for r in valid if r["kind"] == kind), key=lambda r: (r["date"], r.get("permit", "")))
            if candidates:
                selected = candidates[0]
                fields[field] = {"value": parse_date(selected["date"]).strftime("%y/%m/%d"), "records": candidates, "status": "confirmed"}
        names = sorted({r["name"] for r in valid if r["kind"] == "presale"})
        identities = [r for r in valid if r["kind"] == "identity"]
        name_records = [r for r in identities if r.get("name")]
        brand_records = [r for r in identities if r.get("brand")]
        if name_records:
            names = sorted({r["name"] for r in name_records})
        if brand_records:
            brands = sorted({r["brand"] for r in brand_records})
            fields["brand"] = {"value": " / ".join(brands), "records": brand_records, "status": "confirmed"}
        if names:
            fields["projectName"] = {"value": " / ".join(names), "records": name_records or [r for r in valid if r["kind"] == "presale"], "status": "confirmed", "nameType": "marketing" if name_records else "official"}
        for field in FIELDS:
            old, item = row.get(field, ""), fields.get(field)
            if item:
                value = item["value"]
                if old and normalized(old) != normalized(value):
                    # Official names are retained as aliases, not used to erase marketing names.
                    if field == "projectName":
                        item["status"] = "alias_confirmed"
                    else:
                        item["status"] = "conflict"
                        review.append({"landCode": key, "field": field, "reason": "旧值与官方候选不一致，保留旧值待核", "before": old, "candidate": value, "urls": [r["url"] for r in item["records"]]})
                elif not old:
                    row[field] = value
                    changes.append({"landCode": key, "field": field, "before": old, "after": value, "urls": [r["url"] for r in item["records"]]})
                item["displayValue"] = row.get(field, "")
            elif old:
                old_evidence = previous.get(key, {}).get("fields", {}).get(field, {})
                fields[field] = dict(old_evidence, status="not_reconfirmed") if old_evidence.get("records") else {"value": old, "displayValue": old, "status": "legacy_unverified", "records": []}
            if old and field in SOURCE_FIELDS.values() and parse_date(old) < deal:
                fields.setdefault(field, {})["status"] = "conflict"
                review.append({"landCode": key, "field": field, "reason": "存量节点早于成交，不能视为已核实", "before": old})
        evidence[key] = {"fields": fields}
        row["fieldEvidence"] = {field: page_evidence(field, item) for field, item in fields.items()}
    return result, changes, review, evidence


def enrich(rows, root, report_dir, today, replay=None, resume=None):
    mode = "snapshot" if replay else "resume" if resume else "live"
    report = {"status": "failed", "collectionMode": mode, "queries": [], "errors": [], "changes": [], "review": []}
    client = PublicClient(report_dir / "raw", replay, resume=resume)
    collector = Collector(client, rows, report)
    records = collector.run()
    manifest_path = root / "data/land_tracker_identity_sources.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    records.extend(collect_identities(client, manifest, rows, report))
    previous_path = root / "data/land_tracker_enrichment.json"
    previous = json.loads(previous_path.read_text()).get("records", {}) if previous_path.exists() else {}
    proposed, report["changes"], report["review"], evidence = reconcile(rows, records, today, previous)
    report["candidateCount"] = len(records)
    report["matchedCount"] = sum(bool(r.get("landCode")) for r in records)
    report["fieldCoverage"] = {
        field: {"filled": sum(bool(r.get(field)) for r in proposed),
                "added": sum(c["field"] == field for c in report["changes"]),
                **{status: sum(r.get("fieldEvidence", {}).get(field, {}).get("status") == status for r in proposed)
                   for status in ("confirmed", "alias_confirmed", "conflict", "legacy_unverified", "not_reconfirmed")}}
        for field in FIELDS
    }
    report["unmatched"] = [{k: r.get(k) for k in ("kind", "name", "permit", "date", "url")} for r in records if not r.get("landCode")]
    report["status"] = "passed" if not report["errors"] else "failed"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (report_dir / "candidates.json").write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n")
    lines = ["# 项目与证照采集报告", "", f"状态：{report['status']}；候选 {len(records)} 条；匹配 {report['matchedCount']} 条；补填 {len(report['changes'])} 项；待核 {len(report['review'])} 项；错误 {len(report['errors'])} 项", ""]
    labels = dict(zip(FIELDS, ("项目名称", "品牌", "规划许可", "建设方案", "施工许可", "首个预售", "首个竣工备案")))
    lines += ["| 字段 | 已有值 | 本轮补填 | 确认一致 | 官方别名 | 冲突 | 未重新核实 |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for field, counts in report["fieldCoverage"].items():
        lines.append(f"| {labels[field]} | {counts['filled']} | {counts['added']} | {counts['confirmed']} | {counts['alias_confirmed']} | {counts['conflict']} | {counts['legacy_unverified'] + counts['not_reconfirmed']} |")
    lines += ["", "‘已有值’包含历史值，不代表全部已核实。待核列表也包含非住宅等排除记录，不等于冲突字段数。", ""]
    for title, key in (("补填", "changes"), ("待核", "review"), ("采集错误", "errors")):
        lines += ["## " + title, ""] + ["- " + json.dumps(item, ensure_ascii=False) for item in report[key]] + [""]
    (report_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    payload = {"schemaVersion": 1, "collectionMode": mode, "checkedAt": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"), "records": evidence}
    (report_dir / "proposed_enrichment.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    if report["errors"]:
        raise ValidationError(f"Enrichment incomplete ({len(report['errors'])} errors); see {report_dir / 'report.json'}")
    return proposed, payload, report
