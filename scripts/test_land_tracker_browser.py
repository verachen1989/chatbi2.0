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
    parser.add_argument("--browser-executable", help="Optional installed Chromium/Chrome path")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    expected = read_rows(args.page.read_text(encoding="utf-8"))[0]
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.browser_executable)
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
        preview = next((r for r in expected if r.get("fieldEvidence", {}).get("planningPermit", {}).get("record")
                        and r["fieldEvidence"]["planningPermit"]["status"] == "confirmed"), None)
        if preview:
            page.locator(f'[data-evidence-seq="{preview["seq"]}"]').click()
            assert page.locator("#evidenceDialog").is_visible()
            assert page.locator("#evidenceContent").inner_text().find(preview["fieldEvidence"]["planningPermit"]["record"]["permit"]) >= 0
            assert page.url == args.page.resolve().as_uri()
            page.screenshot(path=str(args.output / "permit-desktop.png"))
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.locator("#evidenceClose").is_visible()
            assert page.locator("#evidenceDialog").evaluate("el => el.scrollWidth <= el.clientWidth")
            page.screenshot(path=str(args.output / "permit-mobile.png"))
            page.keyboard.press("Escape")
            assert not page.locator("#evidenceDialog").is_visible()
            page.set_viewport_size({"width": 1440, "height": 900})
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
        page.set_viewport_size({"width": 1440, "height": 900})
        page.evaluate("""() => {
            const row = LAND_ROWS[0];
            row.linkedProjects = [
                {projectName: '测试项目甲（仅浏览器测试）', projectCode: 'TEST_A'},
                {projectName: '测试项目乙超长名称用于核验换行不遮挡其他项目（仅浏览器测试）', projectCode: 'TEST_B'},
                {projectName: '未提供编码的测试项目', projectCode: ''}
            ];
            render();
        }""")
        project_cell = page.locator("#tableBody tr").first.locator("td").nth(3)
        project_links = project_cell.locator("a.project-link")
        assert project_links.count() == 2
        assert project_links.nth(0).get_attribute("href") == "https://om.gtcloud.cn/#/region/invest/external-data?projectCode=TEST_A"
        assert project_links.nth(1).get_attribute("href") == "https://om.gtcloud.cn/#/region/invest/external-data?projectCode=TEST_B"
        assert project_cell.locator(".project-name").inner_text() == "未提供编码的测试项目"
        for width in (1440, 390):
            page.set_viewport_size({"width": width, "height": 900})
            project_cell.scroll_into_view_if_needed()
            if width > 720:
                page.locator(".table-wrap").evaluate("el => el.scrollLeft = 0")
            assert project_cell.evaluate("el => el.scrollWidth <= el.clientWidth")
            boxes = project_cell.locator("li").evaluate_all("els => els.map(el => ({top: el.getBoundingClientRect().top, bottom: el.getBoundingClientRect().bottom}))")
            assert all(a["bottom"] <= b["top"] for a, b in zip(boxes, boxes[1:]))
            assert project_links.nth(1).evaluate("el => { const r=el.getBoundingClientRect(); return r.right <= innerWidth && r.left >= 0; }")
            assert project_links.nth(1).evaluate("el => { const r=el.getBoundingClientRect(); return document.elementFromPoint(r.left+2, r.top+5)?.closest('a.project-link') === el; }"), "A frozen column covers the project link"
            page.screenshot(path=str(args.output / f"multiple-projects-test-{width}.png"))
        page.locator("#searchInput").fill("TEST_B")
        assert rows.count() == 1

        # This test shell exercises the cross-origin message contract, not the live OM application.
        shell = browser.new_page()
        child_url = "https://verachen1989.github.io/chatbi2.0/land_tracker_dashboard_20260614/index.html"
        source = args.page.read_text(encoding="utf-8")
        shell_html = '<iframe style="width:1400px;height:800px" src="' + child_url + '"></iframe><script>window.requests=[];addEventListener("message", e=>{if(e.origin==="https://verachen1989.github.io")requests.push(e.data);});</script>'
        def offline_shell(route):
            if route.request.url == "https://om.gtcloud.cn/":
                route.fulfill(content_type="text/html", body=shell_html)
            elif route.request.url == child_url:
                route.fulfill(content_type="text/html", body=source)
            else:
                route.abort()
        shell.route("**/*", offline_shell)
        shell.goto("https://om.gtcloud.cn/")
        frame = shell.frames[1]
        frame.wait_for_selector("#tableBody tr")
        frame.evaluate("""() => { LAND_ROWS[0].linkedProjects = [
            {projectName:'测试甲', projectCode:'TEST_A'}, {projectName:'测试乙', projectCode:'TEST_B'}
        ]; render(); }""")
        frame.locator("#tableBody tr").first.locator("a.project-link").nth(1).click()
        shell.wait_for_function("window.requests.length === 1")
        request = shell.evaluate("window.requests[0]")
        assert request["projectCode"] == "TEST_B"
        assert request["url"] == "https://om.gtcloud.cn/#/region/invest/external-data?projectCode=TEST_B"
        assert frame.url == child_url
        shell.close()
        assert not errors, errors
        browser.close()
    print(json.dumps({"browser": "passed", "rows": len(expected), "screenshots": str(args.output)}))


if __name__ == "__main__":
    main()
