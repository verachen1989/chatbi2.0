"""Read confirmed Feishu land links into the static page; never guess project codes.

Requires an authenticated lark-cli. Dry run by default; --apply updates the local
HTML only. This command never writes to Feishu or publishes to GitHub.
"""
import argparse
import copy
import hashlib
import json
import re
import subprocess
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from update_land_tracker import ROOT, DASHBOARD, ValidationError, atomic_write, clean, code_key, embed_rows, parse_date, read_rows
from land_tracker_enrichment import parcel_codes

PROJECT_FIELDS = ("去化表项目名", "克而瑞项目名", "住建委备案名", "projectCode", "关联地块", "地块关联状态")
MATCH_FIELDS = (*PROJECT_FIELDS, "地块关联依据", "开发商", "预售证")
MANAGED_FIELDS = ("关联地块", "地块关联状态", "地块关联依据")
RULE_VERSION = 1


def presale_keys(text):
    return {f"{year}:{int(number)}" for year, number in
            re.findall(r"京房售证字\((20\d{2})\)(\d+)号", clean(text or ""))}


def name_keys(text):
    # Punctuation normalization only. Keep brands and phase numbers intact.
    return {clean(part).replace("·", "").upper() for part in
            re.split(r"[/、;；\n]", text or "") if len(clean(part)) >= 3}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def managed_values(record):
    result = {field: record.get(field) or ([] if field != "地块关联依据" else "") for field in MANAGED_FIELDS}
    result["关联地块"] = [{"id": key} for key in sorted({item["id"] for item in result["关联地块"]})]
    return result


def official_records(row, evidence):
    key = code_key(row["landCode"])
    fields = evidence.get("records", {}).get(key, {}).get("fields", {})
    result = []
    for field in ("firstPresale", "planningPermit"):
        item = fields.get(field, {})
        if item.get("status") != "confirmed":
            continue
        for record in item.get("records", []):
            if (record.get("landCode") != key or record.get("valid") is not True
                    or record.get("residential") is not True
                    or urlparse(record.get("url", "")).hostname not in
                    ("bjjs.zjw.beijing.gov.cn", "yewu.ghzrzyw.beijing.gov.cn")):
                continue
            if parse_date(row["dealDate"]) <= parse_date(record["date"]) <= date.today():
                result.append(record)
    return result


def plan_automatic_links(rows, lands, projects, evidence, previous=None):
    """Plan evidence-backed links; never overwrite confirmed or human-edited cells."""
    previous = previous or {}
    directory = {}
    for land in lands:
        key = code_key(land["地块编号"])
        if key in directory:
            raise ValidationError("Duplicate Feishu land code")
        directory[key] = land["record_id"]
    if len({code_key(row["landCode"]) for row in rows}) != len(rows):
        raise ValidationError("Duplicate dashboard land code")
    if any(code_key(row["landCode"]) not in directory for row in rows):
        raise ValidationError("Refresh the land directory before matching")
    owners = {}
    for project in projects:
        for permit in presale_keys(project.get("预售证")):
            owners.setdefault(permit, set()).add(project["record_id"])
    source = [(row, official_records(row, evidence)) for row in rows]
    source_hash = fingerprint([RULE_VERSION, source, {p: sorted(ids) for p, ids in owners.items()}])
    updates, items, state = {}, [], copy.deepcopy(previous)
    for project in projects:
        record_id = project["record_id"]
        current = managed_values(project)
        old = previous.get(record_id, {})
        signature = fingerprint([source_hash, {k: project.get(k) for k in MATCH_FIELDS if k not in MANAGED_FIELDS}])
        item = {"recordId": record_id, "projectName": next((project.get(k) for k in PROJECT_FIELDS[:3] if project.get(k)), ""),
                "landCodes": [], "reasons": []}
        items.append(item)
        if project.get("地块关联状态") == ["已确认"]:
            item["decision"] = "preserved_confirmed"
            continue
        changed_by_user = old.get("output") is not None and current != old["output"]
        manual = (not old and (current["关联地块"] or current["地块关联依据"]
                              or current["地块关联状态"] == ["需人工确认"]))
        if old.get("manual") or changed_by_user or manual:
            item["decision"] = "preserved_manual"
            state[record_id] = dict(old, manual=True)
            continue
        if old.get("fingerprint") == signature:
            item.update(old.get("result", {}), decision="unchanged")
            continue
        names = set().union(*(name_keys(project.get(k)) for k in PROJECT_FIELDS[:3]))
        permits = presale_keys(project.get("预售证"))
        developer = clean(project.get("开发商") or "")
        parcels = parcel_codes(project.get("住建委备案名") or "")
        strong, weak, conflicts = {}, {}, []
        for row, records in source:
            key = code_key(row["landCode"])
            reasons, suggestions = [], []
            aliases = name_keys(row.get("projectName"))
            for record in records:
                aliases |= name_keys(record.get("name"))
                shared = permits & presale_keys(record.get("permit")) if record.get("kind") == "presale" else set()
                same_entity = bool(developer) and developer == clean(record.get("developer") or "")
                unique = shared and all(len(owners[p]) == 1 for p in shared)
                if shared:
                    if unique and same_entity:
                        reasons.append("预售证号、开发企业与官方记录一致：" + record["permit"] + " " + record["url"])
                    else:
                        suggestions.append("证号命中，但开发企业不一致/缺失或多个项目使用同一证号：" + record["permit"])
                        conflicts.append(suggestions[-1])
                if (record.get("kind") == "planning" and parcels & parcel_codes(row["landName"])
                        & parcel_codes(record.get("name", "") + record.get("location", ""))):
                    if same_entity:
                        reasons.append("备案名中的完整宗地编号、开发企业与官方规划记录一致：" + record["url"])
                    else:
                        suggestions.append("宗地编号命中，开发企业尚未核实")
            if any(a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a)) for a in names for b in aliases):
                suggestions.append("项目名称或备案名相同/包含，仅作候选，未据此确认")
            if reasons:
                strong[key] = sorted(set(reasons))
            elif suggestions:
                weak[key] = sorted(set(suggestions))
        if conflicts:
            for key, reasons in strong.items():
                weak[key] = reasons + sorted(set(conflicts))
            strong = {}
        selected = strong or weak
        item.update(decision="confirmed" if strong else "review" if weak else "unmatched",
                    landCodes=sorted(selected), reasons=[f"{key}：{reason}" for key in sorted(selected) for reason in selected[key]])
        if selected:
            output = {"关联地块": [{"id": directory[key]} for key in sorted(selected)],
                      "地块关联状态": ["已确认" if strong else "需人工确认"],
                      "地块关联依据": "自动匹配规则 v1\n" + "\n".join(item["reasons"])}
        elif old.get("output") and current["关联地块"]:
            # Withdraw stale machine suggestions only, never confirmed relationships.
            output = {"关联地块": [], "地块关联状态": ["未关联"], "地块关联依据": "自动匹配规则 v1：当前没有有效候选"}
        else:
            output = current
        output = managed_values(output)
        if output != current:
            updates[record_id] = output
        state[record_id] = {"fingerprint": signature, "output": output, "result": item}
    return {"updates": updates, "items": items, "state": state}


def decode_records(page, required):
    fields, ids, values = page.get("fields", []), page.get("record_id_list", []), page.get("data", [])
    if (not set(required).issubset(fields) or len(fields) != len(set(fields))
            or len(ids) != len(values) or any(len(row) != len(fields) for row in values)):
        raise ValidationError("Feishu record schema changed; import stopped")
    return [dict(zip(fields, row), record_id=record_id) for record_id, row in zip(ids, values)]


def merge_project_links(rows, lands, projects):
    result = copy.deepcopy(rows)
    by_code = {code_key(row["landCode"]): row for row in result}
    if len(by_code) != len(result):
        raise ValidationError("Duplicate dashboard land code")
    land_records, seen_lands = {}, set()
    for record in lands:
        code = code_key(record.get("地块编号", ""))
        if code in seen_lands:
            raise ValidationError("Duplicate Feishu land code: " + code)
        land_records[record["record_id"]] = code
        seen_lands.add(code)
    for row in result:
        row["linkedProjects"] = []
        row.pop("projectCode", None)
    seen_projects = set()
    for record in projects:
        if record.get("地块关联状态") != ["已确认"]:
            continue
        name = next((record.get(field).strip() for field in PROJECT_FIELDS[:3]
                     if isinstance(record.get(field), str) and record[field].strip()), "")
        code = record.get("projectCode") or ""
        if not isinstance(code, str) or (code.strip() and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", code.strip())):
            raise ValidationError("Invalid projectCode: " + record["record_id"])
        code = code.strip()
        related = record.get("关联地块") or []
        if not name or not related:
            raise ValidationError("Confirmed project requires a name and linked land: " + record["record_id"])
        if code and code in seen_projects:
            raise ValidationError("projectCode is used by multiple project records: " + code)
        if code:
            seen_projects.add(code)
        added = set()
        for relation in related:
            land_code = land_records.get(relation.get("id"))
            if land_code not in by_code:
                raise ValidationError("Linked land is missing from the directory or dashboard")
            if land_code not in added:
                by_code[land_code]["linkedProjects"].append({"projectName": name, "projectCode": code})
                added.add(land_code)
    return result


def lark(cli, *args):
    completed = subprocess.run([cli, "base", *args, "--as", "user"], capture_output=True, text=True, timeout=90)
    if completed.returncode:
        raise ValidationError("Feishu CLI operation failed; check authorization and the write journal before retrying.")
    payload = json.loads(completed.stdout)
    if payload.get("ok") is not True:
        raise ValidationError("Feishu operation did not succeed")
    return payload["data"]


def fetch_records(cli, base, table, fields):
    records, ids, revision = [], set(), None
    for _ in range(100):
        args = ["+record-list", "--base-token", base, "--table-id", table,
                "--format", "json", "--limit", "200", "--offset", str(len(records))]
        for field in fields:
            args.extend(["--field-id", field])
        page = lark(cli, *args)
        if revision is not None and page.get("rev") != revision:
            raise ValidationError("Feishu table changed during pagination; retry the import")
        revision = page.get("rev")
        batch = decode_records(page, fields)
        for record in batch:
            if record["record_id"] in ids:
                raise ValidationError("Repeated Feishu page; import stopped")
            ids.add(record["record_id"])
        records.extend(batch)
        if page.get("has_more") is False:
            return records
        if page.get("has_more") is not True or not batch:
            raise ValidationError("Incomplete Feishu pagination")
    raise ValidationError("Feishu pagination limit exceeded")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Project master table URL, including table ID")
    parser.add_argument("--lark-cli", default="lark-cli")
    parser.add_argument("--page", type=Path, default=ROOT / DASHBOARD)
    parser.add_argument("--report-dir", type=Path, default=ROOT / "reports/land-project-links")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    resolved = lark(args.lark_cli, "+url-resolve", "--url", args.base_url)
    if resolved.get("block_type") != "table":
        raise ValidationError("The URL must select a project table")
    base, table = resolved["base_token"], resolved["table_id"]
    relation = lark(args.lark_cli, "+field-get", "--base-token", base,
                    "--table-id", table, "--field-id", "关联地块")["field"]
    if relation.get("type") != "link" or not relation.get("link_table"):
        raise ValidationError("关联地块 must be a linked-record field")
    lands = fetch_records(args.lark_cli, base, relation["link_table"], ["地块编号"])
    projects = fetch_records(args.lark_cli, base, table, PROJECT_FIELDS)
    source = args.page.read_text(encoding="utf-8")
    rows = merge_project_links(read_rows(source)[0], lands, projects)
    output = embed_rows(source, rows)
    args.report_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(args.report_dir / "proposed.html", output)
    summary = {"applied": args.apply, "lands": len(rows), "projectRecords": len(projects),
               "linkedLands": sum(bool(row["linkedProjects"]) for row in rows),
               "links": sum(bool(project["projectCode"]) for row in rows for project in row["linkedProjects"])}
    atomic_write(args.report_dir / "report.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    if args.apply:
        if args.page.read_text(encoding="utf-8") != source:
            raise ValidationError("Page changed during import; publication stopped")
        atomic_write(args.page, output)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
