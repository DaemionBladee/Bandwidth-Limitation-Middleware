"""Real Chromium + HTTP API + PostgreSQL + a separate worker process."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

from bandwidth.auth import password_hash
from bandwidth.cli import demo
from bandwidth.db import transaction
from bandwidth.models import Admin


def test_dashboard_full_workflow(tmp_path):
    browser_path = os.environ.get("CHROMIUM_PATH") or shutil.which("chromium")
    if not browser_path:
        pytest.skip("Install Chromium or set CHROMIUM_PATH for the browser integration test")
    demo()
    with transaction() as db:
        db.add(Admin(username="browser-admin", password_hash=password_hash("browser-test-password")))
    env = {**os.environ, "WORKER_INTERVAL_SECONDS": "1"}
    processes = []
    base = "http://127.0.0.1:8766"
    with (tmp_path / "servers.log").open("w") as logs:
        try:
            for cmd in (
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "bandwidth.api:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8766",
                ],
                [sys.executable, "-m", "bandwidth.worker"],
            ):
                processes.append(subprocess.Popen(cmd, env=env, stdout=logs, stderr=logs))
            deadline = time.monotonic() + 15
            while True:
                try:
                    if httpx.get(base + "/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() > deadline:
                    pytest.fail("API did not start; inspect servers.log")
                time.sleep(0.1)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=browser_path, headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1080})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "console",
                    lambda msg: (
                        errors.append(msg.text)
                        if msg.type == "error"
                        and "401" not in msg.text
                        and "ERR_INTERNET_DISCONNECTED" not in msg.text
                        and "ERR_NETWORK_CHANGED" not in msg.text
                        else None
                    ),
                )
                documents = []
                legacy_reads = []
                page.on(
                    "request",
                    lambda req: documents.append(req.url) if req.resource_type == "document" else None,
                )
                page.on(
                    "request",
                    lambda req: (
                        legacy_reads.append(req.url)
                        if req.method == "GET"
                        and req.url.split("?")[0].endswith(
                            ("/api/users", "/api/policies", "/api/status", "/api/audit")
                        )
                        else None
                    ),
                )
                page.goto(base)
                page.get_by_label("Admin username").fill("browser-admin")
                page.get_by_label("Password", exact=True).fill("browser-test-password")
                page.get_by_role("button", name="Sign in").click()
                expect(page.get_by_role("heading", name="Network overview")).to_be_visible()
                expect(page.get_by_text("Mock ISE · local simulation", exact=True)).to_be_visible()
                expect(page.locator("#live-status")).to_contain_text("Live updates", timeout=15000)
                page.get_by_role("button", name="Display preferences").click()
                page.get_by_label("Find display timezone").fill("Yangon")
                page.get_by_label("Display timezone", exact=True).select_option("Asia/Yangon")
                page.get_by_role("button", name="Apply timezone").click()
                expect(page.locator("#display-zone-label")).to_have_text("Asia/Yangon")
                page.get_by_role("button", name="New policy", exact=True).click()
                policy_form = page.locator("#policy-form")
                policy_form.get_by_label("Policy name", exact=True).fill("Browser policy")
                policy_form.get_by_label("Quota (GB, decimal)").fill("2")
                policy_form.get_by_label("Period", exact=True).select_option("daily")
                policy_form.get_by_label("Find policy timezone").fill("Yangon")
                policy_form.get_by_label("Timezone", exact=True).select_option("Asia/Yangon")
                policy_form.get_by_role("button", name="Save policy").click()
                expect(page.locator("#policy-dialog")).not_to_be_visible()
                page.get_by_role("button", name="Add subscriber").click()
                page.get_by_label("Accounting username").fill("browser@example.com")
                page.get_by_role("button", name="Create subscriber").click()
                assign_form = page.locator("#assignment-form")
                expect(page.locator("#assignment-dialog")).to_be_visible()
                assign_form.get_by_label("Policy", exact=True).select_option(label="Browser policy")
                assign_form.get_by_role("button", name="Save assignment").click()
                expect(page.locator("#assignment-dialog")).not_to_be_visible()
                page.get_by_role("button", name="Submit mock usage").click()
                usage_form = page.locator("#usage-form")
                usage_form.get_by_label("Subscriber", exact=True).select_option(label="browser@example.com")
                usage_form.get_by_label("Upload (GB)").fill("1")
                usage_form.get_by_label("Download (GB)").fill("1")
                usage_form.get_by_role("button", name="Queue mock usage").click()
                expect(page.locator("#usage-dialog")).not_to_be_visible()
                # Keep an unfinished form open while another process ingests usage.
                page.get_by_role("button", name="Add subscriber", exact=True).click()
                page.get_by_label("Accounting username").fill("unfinished-draft@example.com")
                row = page.locator("#users-table tr").filter(has_text="browser@example.com")
                expect(row.locator(".badge").filter(has_text="LIMITED")).to_have_text(
                    "LIMITED", timeout=20000
                )
                expect(page.get_by_label("Accounting username")).to_have_value("unfinished-draft@example.com")
                page.locator("#user-dialog .close-dialog").first.click()
                expect(row.get_by_text("MOCK · confirmed", exact=True)).to_be_visible(timeout=15000)
                row.get_by_role("button", name="Actions for browser@example.com").click()
                expect(page.locator("#subscriber-actions")).to_be_visible()
                page.keyboard.press("End")
                expect(page.get_by_role("menuitem", name="Reset usage")).to_be_focused()
                page.keyboard.press("Escape")
                expect(page.locator("#subscriber-actions")).not_to_be_visible()
                expect(row.get_by_role("button", name="Actions for browser@example.com")).to_be_focused()
                row.get_by_role("button", name="History").click()
                expect(page.get_by_role("heading", name="Retained quota periods")).to_be_visible()
                page.get_by_role("button", name="Enforcement", exact=True).click()
                expect(page.locator("#history-content")).to_contain_text("mock_confirmed")
                page.get_by_role("button", name="Usage & resets", exact=True).click()
                page.locator("#detail-reset").click()
                expect(page.locator("#reset-dialog")).to_be_visible()
                page.locator("#reset-dialog").get_by_role("button", name="Cancel").click()
                expect(row.get_by_text("LIMITED", exact=True)).to_be_visible()
                page.locator("#detail-reset").click()
                page.get_by_label("Reason for reset").fill("Browser verified allowance reset")
                page.get_by_role("button", name="Confirm reset", exact=True).click()
                expect(page.locator("#reset-dialog")).not_to_be_visible()
                expect(row.get_by_text("NORMAL", exact=True)).to_be_visible(timeout=15000)
                expect(page.locator("#history-content")).to_contain_text("Browser verified allowance reset")
                expect(page.locator("#history-content")).to_contain_text("2 GB")
                page.locator("#subscriber-dialog .close-dialog").click()
                Path("artifacts").mkdir(exist_ok=True)
                page.evaluate("window.scrollTo(0,0)")
                page.screenshot(path="artifacts/dashboard.png", full_page=True)
                page.get_by_role("button", name="Policy library").click()
                card = page.locator(".policy-card").filter(has_text="Browser policy")
                card.get_by_role("button", name="Edit policy").click()
                policy_form.get_by_label("Quota (GB, decimal)").fill("3")
                expect(policy_form.get_by_label("Timezone", exact=True)).to_have_value("Asia/Yangon")
                policy_form.get_by_role("button", name="Save policy").click()
                expect(page.locator("#policy-dialog")).not_to_be_visible()
                page.get_by_role("button", name="Overview", exact=False).click()
                expect(row.get_by_text("NORMAL", exact=True)).to_be_visible()
                page.get_by_label("Search subscribers").fill("browser@")
                expect(page.locator("#users-table tr")).to_have_count(1)
                page.get_by_role("button", name="Limited", exact=False).click()
                expect(page.locator("#search-empty")).to_be_visible()
                page.get_by_role("button", name="Clear filters").click()
                expect(page.locator("#users-table tr")).to_have_count(2)
                # Recover from a transport interruption without navigation or losing the search.
                page.get_by_label("Search subscribers").fill("browser@")
                page.context.set_offline(True)
                expect(page.locator("#live-status")).to_contain_text("Reconnecting", timeout=10000)
                page.context.set_offline(False)
                expect(page.locator("#live-status")).to_contain_text("Live updates", timeout=20000)
                expect(page.get_by_label("Search subscribers")).to_have_value("browser@")
                page.get_by_label("Search subscribers").fill("")
                assert not legacy_reads, legacy_reads
                assert len(documents) == 1, documents
                page.get_by_role("button", name="Switch to dark theme").click()
                expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                page.evaluate("window.scrollTo(0,0)")
                page.screenshot(path="artifacts/dashboard-dark.png", full_page=True)
                page.get_by_role("button", name="Switch to light theme").click()
                page.set_viewport_size({"width": 390, "height": 844})
                page.evaluate("window.scrollTo(0,0)")
                page.screenshot(path="artifacts/dashboard-mobile.png", full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                assert not errors, errors
                page.reload()
                expect(page.locator("#display-zone-label")).to_have_text("Asia/Yangon")
                expect(page.locator("#live-status")).to_contain_text("Live updates", timeout=15000)
                # Revocation must close an already authenticated event stream.
                session = page.request.get(base + "/api/session").json()
                response = page.request.post(base + "/api/logout", headers={"X-CSRF-Token": session["csrf"]})
                assert response.status == 200
                expect(page.locator("#login-view")).to_be_visible(timeout=20000)
                assert not errors, errors
                browser.close()
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
