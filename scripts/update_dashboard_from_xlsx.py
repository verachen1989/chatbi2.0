#!/usr/bin/env python3
import argparse
import copy
import datetime as dt
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
DATA_PATTERN = re.compile(r"const DATA = (.*);\nconst state")
METRIC_KEYS = ("suites", "area", "price", "amount")
GROUP_COLORS = {
    "北部组团": "#31d5ff",
    "东北组团": "#b8ff6a",
    "CBD-副中心组团": "#ffcf5a",
    "西部组团": "#ff6aa6",
    "西南组团": "#a78bfa",
    "大亦庄组团": "#5eead4",
}


def col_index(cell_ref):
    letters = re.match(r"[A-Z]+", cell_ref).group(0)
    value = 0
    for char in letters:
        value = value * 26 + ord(char) - 64
    return value - 1


def shared_strings(archive):
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(node.text or "" for node in item.iterfind(".//x:t", NS))
        for item in root.findall("x:si", NS)
    ]


def read_sheet(archive, sheet_path, strings):
    root = ET.fromstring(archive.read(sheet_path))
    rows = []
    for row in root.findall(".//x:sheetData/x:row", NS):
        values = {}
        for cell in row.findall("x:c", NS):
            ref = cell.attrib["r"]
            cell_type = cell.attrib.get("t")
            value_node = cell.find("x:v", NS)
            inline_node = cell.find("x:is", NS)
            value = None
            if cell_type == "inlineStr" and inline_node is not None:
                value = "".join(
                    node.text or "" for node in inline_node.iterfind(".//x:t", NS)
                )
            elif value_node is not None:
                raw = value_node.text
                if cell_type == "s":
                    value = strings[int(raw)]
                elif cell_type == "b":
                    value = raw == "1"
                else:
                    try:
                        value = int(raw)
                    except ValueError:
                        try:
                            value = float(raw)
                        except ValueError:
                            value = raw
            values[col_index(ref)] = value
        if values:
            rows.append([values.get(index) for index in range(max(values) + 1)])
    return rows


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_group(value):
    return clean_text(value).replace("西南组块", "西南组团")


def excel_date(value):
    if not value:
        return ""
    if isinstance(value, (int, float)):
        return (dt.datetime(1899, 12, 30) + dt.timedelta(days=value)).date().isoformat()
    return clean_text(value)


def numeric(value):
    if value in (None, ""):
        return 0
    return float(value)


def month_columns(header):
    result = []
    for column in range(6, len(header), 4):
        label = clean_text(header[column])
        if label:
            result.append((label, column))
    return result


def row_monthly(row, month_defs):
    monthly = {}
    for month, column in month_defs:
        values = []
        for offset in range(4):
            values.append(numeric(row[column + offset] if column + offset < len(row) else 0))
        monthly[month] = dict(zip(METRIC_KEYS, values))
    return monthly


def project_totals(monthly, recent_months):
    suites_all = sum(item["suites"] for item in monthly.values())
    amount_all = sum(item["amount"] for item in monthly.values())
    recent = [monthly.get(month, {}) for month in recent_months]
    suites_recent = sum(item.get("suites", 0) for item in recent)
    area_recent = sum(item.get("area", 0) for item in recent)
    amount_recent = sum(item.get("amount", 0) for item in recent)
    latest = monthly.get(recent_months[-1], {}) if recent_months else {}
    return {
        "suites34": suites_recent,
        "area34": area_recent,
        "amount34": amount_recent,
        "price4": latest.get("price", 0),
        "amountAll": amount_all,
        "suitesAll": suites_all,
    }


def build_projects(rows, old_projects, all_months, source=None):
    header = rows[0]
    sheet_months = month_columns(header)
    recent_months = all_months[-2:]
    old_by_id = {str(item["id"]): item for item in old_projects}
    old_by_name = {clean_text(item["project"]): item for item in old_projects}
    projects = []

    for row_index, row in enumerate(rows[2:], start=1):
        if len(row) < 4 or row[0] in (None, "") or not clean_text(row[3]):
            continue
        project_id = "launch-{}".format(row_index) if source == "recentLaunch" else row[0]
        project_name = clean_text(row[3])
        old = old_by_id.get(str(project_id)) or old_by_name.get(project_name) or {}
        monthly_from_sheet = row_monthly(row, sheet_months)
        monthly = {
            month: copy.deepcopy(
                monthly_from_sheet.get(
                    month, {"suites": 0, "area": 0, "price": 0, "amount": 0}
                )
            )
            for month in all_months
        }
        project = {
            "id": project_id,
            "group": normalize_group(row[1]),
            "plate": clean_text(row[2]),
            "project": project_name,
            "landDate": excel_date(row[4] if len(row) > 4 else None),
            "status": clean_text(row[5] if len(row) > 5 else ""),
            "x": old.get("x"),
            "y": old.get("y"),
            "lat": old.get("lat"),
            "lng": old.get("lng"),
            "monthly": monthly,
        }
        if source:
            project["source"] = source
        project.update(project_totals(monthly, recent_months))
        for field in (
            "district",
            "address",
            "coordSource",
            "coordSourceUrl",
            "coordConfidence",
            "coordSystem",
            "matchedName",
        ):
            if field in old:
                project[field] = old[field]
        projects.append(project)
    return projects


def aggregate_month(projects, month):
    suites = sum(item["monthly"][month]["suites"] for item in projects)
    area = sum(item["monthly"][month]["area"] for item in projects)
    amount = sum(item["monthly"][month]["amount"] for item in projects)
    return {
        "month": month,
        "suites": suites,
        "area": area,
        "amount": amount,
        "price": round(amount * 10000 / area) if area else 0,
    }


def rebuild_data(old_data, main_rows, launch_rows):
    months = [label for label, _ in month_columns(main_rows[0])]
    projects = build_projects(main_rows, old_data["projects"], months)
    launch_projects = build_projects(
        launch_rows, old_data["launchProjects"], months, source="recentLaunch"
    )
    recent_months = months[-2:]
    recent_total = {
        metric: sum(
            project["monthly"][month][metric]
            for project in projects
            for month in recent_months
        )
        for metric in ("suites", "area", "amount")
    }

    group_totals = []
    for group in dict.fromkeys(project["group"] for project in projects):
        group_projects = [project for project in projects if project["group"] == group]
        group_totals.append(
            {
                "group": group,
                "projects": len(group_projects),
                "suites34": sum(project["suites34"] for project in group_projects),
                "amount34": sum(project["amount34"] for project in group_projects),
                "color": old_data.get("colors", {}).get(
                    group, GROUP_COLORS.get(group, "#8ea0b8")
                ),
            }
        )

    result = copy.deepcopy(old_data)
    result["months"] = months
    result["projects"] = projects
    result["launchProjects"] = launch_projects
    result["totals"] = {
        "projects": len(projects),
        "plates": len({project["plate"] for project in projects}),
        "active": sum(project["status"] == "在售" for project in projects),
        "suites34": recent_total["suites"],
        "amount34": recent_total["amount"],
        "area34": recent_total["area"],
        "avgPrice34": round(recent_total["amount"] * 10000 / recent_total["area"])
        if recent_total["area"]
        else 0,
    }
    result["monthlyTotals"] = [aggregate_month(projects, month) for month in months]
    result["groupTotals"] = group_totals
    result["colors"] = {
        group["group"]: group["color"] for group in group_totals
    }
    return result


def latest_full_label(month):
    match = re.match(r"(\d{2})年(\d{1,2})月", month)
    if not match:
        return month
    return "{}年{}月".format(2000 + int(match.group(1)), int(match.group(2)))


def update_html(html, data):
    latest = data["months"][-1]
    recent_months = data["months"][-2:]
    period_options = list(reversed(data["months"]))

    html, count = DATA_PATTERN.subn(
        "const DATA = {};\nconst state".format(
            json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        ),
        html,
        count=1,
    )
    if count != 1:
        raise RuntimeError("Could not replace DATA block")

    html = re.sub(
        r'(<div class="cutoff"><i>▣</i><span>数据截至：</span><b>).*?(</b></div>)',
        r"\g<1>{}\g<2>".format(latest_full_label(latest)),
        html,
        count=1,
    )
    html = re.sub(
        r'(const state = \{[^;\n]*periods:)\[[^\]]*\]',
        r'\1["{}"]'.format(latest),
        html,
        count=1,
    )
    html = re.sub(
        r"const periodOptions = .*?;",
        "const periodOptions = {};".format(
            json.dumps([[month, month] for month in period_options], ensure_ascii=False)
        ),
        html,
        count=1,
    )
    html = re.sub(
        r'(state\.periods = )\[[^\]]*\];',
        r'\1["{}"];'.format(latest),
        html,
        count=1,
    )
    html = re.sub(
        r"const recentLaunchMonths = \[[^\]]*\]",
        "const recentLaunchMonths = {}".format(
            json.dumps(recent_months, ensure_ascii=False)
        ),
        html,
        count=1,
    )
    return html


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("xlsx")
    parser.add_argument("html", nargs="?", default="index.html")
    args = parser.parse_args()
    xlsx_path = Path(args.xlsx)
    html_path = Path(args.html)

    html = html_path.read_text(encoding="utf-8")
    match = DATA_PATTERN.search(html)
    if not match:
        raise RuntimeError("DATA block not found in {}".format(html_path))
    old_data = json.loads(match.group(1))

    with zipfile.ZipFile(str(xlsx_path)) as archive:
        strings = shared_strings(archive)
        main_rows = read_sheet(archive, "xl/worksheets/sheet2.xml", strings)
        launch_rows = read_sheet(archive, "xl/worksheets/sheet3.xml", strings)

    new_data = rebuild_data(old_data, main_rows, launch_rows)
    html_path.write_text(update_html(html, new_data), encoding="utf-8")

    latest = new_data["monthlyTotals"][-1]
    print(
        json.dumps(
            {
                "projects": len(new_data["projects"]),
                "launchProjects": len(new_data["launchProjects"]),
                "months": new_data["months"],
                "latest": latest,
                "cutoff": latest_full_label(new_data["months"][-1]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
