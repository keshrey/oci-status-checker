"""
OCI Application Status Checker — ociservices.gov.in

Logs in with email + password, reads the current application status,
and sends an ntfy.sh notification if the status has changed since last run.

Status is persisted via GitHub Variable LAST_KNOWN_STATUS (updated via API).
"""

import os
import sys
import json
import datetime
import urllib.request
import pathlib

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_URL       = "https://ociservices.gov.in"
LOGIN_URL      = f"{BASE_URL}/welcome"
TRACE_DIR      = pathlib.Path("/tmp/oci_status_trace")
SCREENSHOT_DIR = pathlib.Path("/tmp/oci_status_screenshots")

# ── Config from environment ──────────────────────────────────────────────────
OCI_EMAIL         = os.environ.get("OCI_EMAIL", "").strip()
OCI_PASSWORD      = os.environ.get("OCI_PASSWORD", "").strip()
NTFY_TOPIC        = os.environ.get("NTFY_TOPIC", "").strip()
LAST_KNOWN_STATUS = os.environ.get("LAST_KNOWN_STATUS", "").strip()
GH_RUN_URL        = os.environ.get("GH_RUN_URL", "").strip()
GH_RUN_NUM        = os.environ.get("GH_RUN_NUMBER", "").strip()
GH_TOKEN          = os.environ.get("GH_TOKEN", "").strip()
GH_REPOSITORY     = os.environ.get("GITHUB_REPOSITORY", "").strip()
VISIBLE           = "--visible" in sys.argv
# ─────────────────────────────────────────────────────────────────────────────


def _run_footer() -> str:
    if GH_RUN_URL:
        label = f"Run #{GH_RUN_NUM}" if GH_RUN_NUM else "Actions run"
        return f"\n\n🔗 {label}: {GH_RUN_URL}"
    return ""


def notify(title: str, message: str, priority: str = "urgent"):
    if not NTFY_TOPIC:
        return
    try:
        full_message = message + _run_footer()
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=full_message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": "passport,india"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        print(f"  Notification sent: {title}")
    except Exception as e:
        print(f"  Notification failed: {e}")


def update_last_known_status(new_status: str):
    if not GH_TOKEN or not GH_REPOSITORY:
        print("  Skipping GitHub Variable update (no token/repo).")
        return
    try:
        url = f"https://api.github.com/repos/{GH_REPOSITORY}/actions/variables/LAST_KNOWN_STATUS"
        data = json.dumps({"name": "LAST_KNOWN_STATUS", "value": new_status}).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={
                "Authorization": f"Bearer {GH_TOKEN}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
            method="PATCH"
        )
        urllib.request.urlopen(req, timeout=10)
        print(f"  Updated LAST_KNOWN_STATUS -> {new_status!r}")
    except Exception as e:
        print(f"  Could not update GitHub Variable: {e}")


def check_status() -> str | None:
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] Checking OCI status…")

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    TRACE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not VISIBLE, slow_mo=50 if VISIBLE else 0)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            record_video_dir=str(TRACE_DIR) if not VISIBLE else None,
        )
        context.tracing.start(screenshots=True, snapshots=True, sources=False)
        page = context.new_page()

        try:
            # ── Login ────────────────────────────────────────────────────────
            page.goto(LOGIN_URL, wait_until="networkidle")
            page.screenshot(path=str(SCREENSHOT_DIR / "01_login_page.png"))
            print(f"  Page title: {page.title()}")

            # Fill email
            email_sel = "input[type='email'], input[name*='email' i], input[id*='email' i], input[name*='user' i]"
            page.wait_for_selector(email_sel, timeout=10000)
            page.fill(email_sel, OCI_EMAIL)

            # Fill password
            page.fill("input[type='password']", OCI_PASSWORD)
            page.screenshot(path=str(SCREENSHOT_DIR / "02_credentials_filled.png"))

            # Submit
            page.click("button[type='submit'], input[type='submit']")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(SCREENSHOT_DIR / "03_after_login.png"))
            print(f"  Post-login URL: {page.url}")

            # ── Extract status ───────────────────────────────────────────────
            body_text = page.inner_text("body")
            print(f"\n  --- Page text (first 1000 chars) ---")
            print(body_text[:1000])
            print(f"  ---\n")

            status_text = None

            # Try CSS selectors for status elements
            for sel in [
                "[class*='status' i]", "[id*='status' i]",
                "[class*='stage' i]",  "[id*='stage' i]",
                "td.status", ".application-status", "#applicationStatus",
            ]:
                els = page.query_selector_all(sel)
                for el in els:
                    t = (el.inner_text() or "").strip()
                    if t and len(t) > 2:
                        status_text = t
                        print(f"  Status found via '{sel}': {t!r}")
                        break
                if status_text:
                    break

            # Fallback: keyword scan
            if not status_text:
                for keyword in [
                    "Application Received", "Under Process", "Under Scrutiny",
                    "Approved", "Dispatched", "Rejected", "Pending",
                ]:
                    if keyword.lower() in body_text.lower():
                        status_text = keyword
                        print(f"  Status found via keyword scan: {keyword!r}")
                        break

            if not status_text:
                print("  Could not extract status — check screenshot and trace.")
                page.screenshot(path=str(SCREENSHOT_DIR / "04_status_not_found.png"))
            else:
                page.screenshot(path=str(SCREENSHOT_DIR / "04_status_found.png"))

            return status_text

        except PWTimeout as e:
            print(f"\n  TIMEOUT: {e}")
            page.screenshot(path=str(SCREENSHOT_DIR / "error_timeout.png"))
            return None
        except Exception as e:
            print(f"\n  ERROR: {e}")
            import traceback; traceback.print_exc()
            try:
                page.screenshot(path=str(SCREENSHOT_DIR / "error_state.png"))
            except Exception:
                pass
            return None
        finally:
            context.tracing.stop(path=str(TRACE_DIR / "trace.zip"))
            print(f"  Trace saved: {TRACE_DIR / 'trace.zip'}")
            context.close()
            browser.close()


def run():
    status = check_status()

    if status is None:
        print("Could not determine status — check screenshots and trace.")
        sys.exit(2)

    print(f"\n{'='*50}")
    print(f"Current status: {status!r}")
    print(f"Last known:     {LAST_KNOWN_STATUS!r}")
    print(f"{'='*50}")

    if status != LAST_KNOWN_STATUS:
        print("  Status CHANGED — sending notification.")
        if not LAST_KNOWN_STATUS:
            msg = f"Current OCI application status:\n\n{status}"
            notify("OCI Status Update", msg, priority="default")
        else:
            msg = (f"OCI application status changed!\n\n"
                   f"Before: {LAST_KNOWN_STATUS}\n"
                   f"Now:    {status}")
            priority = "urgent" if any(w in status.lower() for w in ["approved", "dispatched"]) else "high"
            notify("OCI Status Changed!", msg, priority=priority)
        update_last_known_status(status)
    else:
        print("  No change.")

    sys.exit(0)


if __name__ == "__main__":
    run()
