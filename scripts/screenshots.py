"""Capture the signed-in panel with Playwright. The server must be running."""

import os
from pathlib import Path
from dotenv import dotenv_values
from playwright.sync_api import sync_playwright

if __name__ == "__main__":
    base = os.environ.get("VD_URL", "http://127.0.0.1:8080")
    out = Path("docs/screens")
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1020}, device_scale_factor=1)
        page.goto(base + "/login")
        page.locator("[name=login]").fill("admin")
        page.locator("[name=password]").fill(dotenv_values(".env")["ADMIN_PASSWORD"])
        page.get_by_role("button", name="Войти").click()
        page.wait_for_url(base + "/")
        page.screenshot(path=out / "dashboard.png")
        page.goto(base + "/tickets")
        page.screenshot(path=out / "tickets.png")
        page.locator("a.number").first.click()
        page.screenshot(path=out / "ticket.png")
        browser.close()
