"""Manual Playwright smoke test for the local workbench.

Usage:
  python scripts/ui_smoke.py "http://127.0.0.1:8080/" --name "UI 测试源"
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--name", default="UI 测试源")
    parser.add_argument("--screenshot", default="data/ui-smoke.png")
    args = parser.parse_args()

    parsed = urllib.parse.urlparse(args.url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.goto(args.url + "#sources", wait_until="networkidle")
        page.click('button:has-text("添加来源")')
        page.fill("#srcName", args.name)
        page.fill("#cfgUrl", "https://example.com/ui-page")
        page.click('button:has-text("保存来源")')
        page.wait_for_timeout(900)
        added = args.name in page.inner_text("body")
        page.screenshot(path=args.screenshot, full_page=True)
        browser.close()

    created = []
    request = urllib.request.Request(base + "/api/sources")
    with urllib.request.urlopen(request, timeout=5) as response:
        created = [
            source for source in json.loads(response.read())["sources"]
            if source["name"] == args.name
        ]
    for source in created:
        purge = urllib.request.Request(
            base + f"/api/sources/{source['id']}?purge=true",
            method="DELETE",
        )
        urllib.request.urlopen(purge, timeout=5).read()

    print(f"source_added={added} cleanup={len(created)} errors={errors}")
    return 0 if added and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())

