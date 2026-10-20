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
                        errors.append(msg.text) if msg.type == "error" and "401" not in msg.text else None
                    ),
                )
                page.goto(base)
                page.get_by_label("Admin username").fill("browser-admin")
                page.get_by_label("Password", exact=True).fill("browser-test-password")
                page.get_by_role("button", name="Sign in").click()
                expect(page.get_by_role("heading", name="Network overview")).to_be_visible()
                expect(page.get_by_text("Mock ISE · local simulation", exact=True)).to_be_visible()
                page.get_by_role("button", name="New policy").click()
                policy_form = page.locator("#policy-form")
                policy_form.get_by_label("Policy name", exact=True).fill("Browser policy")
                policy_form.get_by_label("Quota (GB, decimal)").fill("2")
                policy_form.get_by_label("Period", exact=True).select_option("daily")
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
                deadline = time.monotonic() + 15
                while True:
                    rows = httpx.get(
                        base + "/api/users", headers={"Authorization": "Bearer test-automation-token"}
                    ).json()
                    user = next(u for u in rows if u["username"] == "browser@example.com")
                    if (
                        user["ise"]
                        and user["ise"]["status"] == "confirmed"
                        and user["ise"]["desired"]["mode"] == "LIMITED"
                    ):
                        break
                    assert time.monotonic() < deadline, "Worker did not ingest and enforce mock quota"
                    time.sleep(0.1)
                page.get_by_role("button", name="Refresh", exact=False).click()
                row = page.locator("#users-table tr").filter(has_text="browser@example.com")
                expect(row.get_by_text("LIMITED", exact=True)).to_be_visible()
                expect(row.get_by_text("MOCK · confirmed", exact=True)).to_be_visible()
                row.get_by_role("button", name="History").click()
                expect(page.get_by_role("heading", name="Retained quota periods")).to_be_visible()
                expect(page.locator("#history-content")).to_contain_text("mock_confirmed")
                page.locator("#history-dialog .close-dialog").click()
                Path("artifacts").mkdir(exist_ok=True)
                page.screenshot(path="artifacts/dashboard.png", full_page=True)
                page.get_by_role("button", name="Policy library").click()
                card = page.locator(".policy-card").filter(has_text="Browser policy")
                card.get_by_role("button", name="Edit policy").click()
                policy_form.get_by_label("Quota (GB, decimal)").fill("3")
                policy_form.get_by_role("button", name="Save policy").click()
                expect(page.locator("#policy-dialog")).not_to_be_visible()
                page.get_by_role("button", name="Overview", exact=False).click()
                expect(row.get_by_text("NORMAL", exact=True)).to_be_visible()
                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path="artifacts/dashboard-mobile.png", full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
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
