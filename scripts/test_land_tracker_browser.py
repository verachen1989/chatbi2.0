"""Browser regression test, including offline operation and mobile viewport."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

from update_land_tracker import read_rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--page", type=Path, default=Path(__file__).resolve().parents[1] / "land_tracker_dashboard_20260614/index.html")
    parser.add_argument("--output", type=Path, default=Path("reports/land-tracker/browser"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    expected = read_rows(args.page.read_text(encoding="utf-8"))[0]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        # Keep CI deterministic; the table and modal must work if AMap is offline.
        page.route("https://**/*", lambda route: route.abort())
        page.goto(args.page.resolve().as_uri())
        rows = page.locator("#tableBody tr")
        assert rows.count() == len(expected)
        confirmed = sum(1 for r in expected for field, item in r.get("fieldEvidence", {}).items()
                        if field != "projectName" and r.get(field) and item.get("status") == "confirmed" and item.get("url"))
        conflicts = sum(1 for r in expected for field, item in r.get("fieldEvidence", {}).items()
                       if field != "projectName" and r.get(field) and item.get("status") == "conflict")
        assert page.locator(".evidence-link").count() == confirmed
        assert page.locator(".review-flag").count() == conflicts
        dates = page.locator("#tableBody tr td:nth-child(7)").all_text_contents()
        assert dates == sorted(dates, reverse=True)
        page.locator("#dealDateSort").click()
        assert page.locator("#tableBody tr td:nth-child(7)").all_text_contents() == sorted(dates)
        page.locator("#dealDateSort").click()
        for value in sorted({r["district"] for r in expected}):
            page.locator("#districtFilter").select_option(value)
            assert rows.count() == sum(r["district"] == value for r in expected)
        page.locator("#resetFilters").click()
        for year in sorted({r["dealDate"][:2] for r in expected}):
            page.locator("#landDateFilter").select_option(year)
            assert rows.count() == sum(r["dealDate"].startswith(year) for r in expected)
        page.locator("#resetFilters").click()
        page.locator("#searchInput").fill(expected[0]["landCode"])
        assert rows.count() == 1
        page.locator("#tableBody .land-link").first.click()
        assert page.locator("#mapModal").get_attribute("aria-hidden") == "false"
        assert page.locator("#mapLandTitle").inner_text() == expected[0]["landName"]
        page.keyboard.press("Escape")
        assert page.locator("#mapModal").get_attribute("aria-hidden") == "true"
        page.locator("#resetFilters").click()
        page.screenshot(path=str(args.output / "desktop.png"))
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.locator("#tableCount").is_visible()
        assert rows.count() == len(expected)
        page.screenshot(path=str(args.output / "mobile.png"))
        assert not errors, errors
        browser.close()
    print(json.dumps({"browser": "passed", "rows": len(expected), "screenshots": str(args.output)}))


if __name__ == "__main__":
    main()
