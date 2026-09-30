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
import urllib.parse
import pathlib

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

BASE_URL      = "https://ociservices.gov.in"
LOGIN_URL     = f"{BASE_URL}/welcome"
SCREENSHOT_DIR = pathlib.Path("/tmp/oci_status_screenshots")

# ── Config from environment ──────────────────────────────────────────────────
OCI_EMAIL    = os.environ.get("OCI_EMAIL", "").strip()
OCI_PASSWORD = os.environ.get("OCI_PASSWORD", "").strip()
NTFY_TOPIC   = os.environ.get("NTFY_TOPIC", "").strip()

LAST_KNOWN_STATUS = os.environ.get("LAST_KNOWN_STATUS", "").strip()

GH_RUN_URL    = os.environ.get("GH_RUN_URL", "").strip()
GH_RUN_NUM    = os.environ.get("GH_RUN_NUMBER", "").strip()
GH_TOKEN      = os.environ.get("GH_TOKEN", "").strip()
GH_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "").strip()
# ─────────────────────────────────────────────────────────────────────────────

_shot_index    = 0
_dir_initialized = False


def screenshot(driver, label: str):
    global _shot_index, _dir_initialized
    if not _dir_initialized:
        import shutil
        if SCREENSHOT_DIR.exists():
            shutil.rmtree(SCREENSHOT_DIR)
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        _dir_initialized = True
    path = SCREENSHOT_DIR / f"{_shot_index:02d}_{label}.png"
    driver.save_screenshot(str(path))
    _shot_index += 1
    print(f"  [screenshot] {path.name}")


def stitch_screenshots():
    try:
        from PIL import Image
        images = sorted(SCREENSHOT_DIR.glob("*.png"))
        if not images:
            return None
        imgs = [Image.open(p) for p in images]
        max_w = max(i.width for i in imgs)
        total_h = sum(i.height for i in imgs)
        combined = Image.new("RGB", (max_w, total_h), (255, 255, 255))
        y = 0
        for img in imgs:
            combined.paste(img, (0, y))
            y += img.height
        out = SCREENSHOT_DIR / "run_summary.png"
        combined.save(str(out))
        print(f"  Combined screenshot: {out}")
        return out
    except Exception as e:
        print(f"  Could not stitch screenshots: {e}")
        return None


def build_driver(visible: bool = False) -> webdriver.Chrome:
    opts = Options()
    if not visible:
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1280,900")
    else:
        opts.add_argument("--start-maximized")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    svc = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=svc, options=opts)
    driver.set_page_load_timeout(60)
    return driver


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
    """Update LAST_KNOWN_STATUS GitHub Variable via API."""
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
        print(f"  Updated LAST_KNOWN_STATUS → {new_status!r}")
    except Exception as e:
        print(f"  Could not update GitHub Variable: {e}")


def check_status(visible: bool = False) -> str | None:
    """Log in and return the current application status string, or None on error."""
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] Checking OCI status…")

    driver = build_driver(visible)
    wait   = WebDriverWait(driver, 20)

    try:
        driver.get(LOGIN_URL)
        screenshot(driver, "01_login_page")
        print(f"  Page title: {driver.title}")

        # ── Login ────────────────────────────────────────────────────────────
        # Try common email/password field patterns
        email_el = wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "input[type='email'], input[name*='email'], input[id*='email'], input[name*='user']")
        ))
        email_el.clear()
        email_el.send_keys(OCI_EMAIL)

        pwd_el = driver.find_element(
            By.CSS_SELECTOR, "input[type='password']")
        pwd_el.clear()
        pwd_el.send_keys(OCI_PASSWORD)

        screenshot(driver, "02_credentials_filled")

        # Submit login
        submit = driver.find_element(
            By.CSS_SELECTOR, "button[type='submit'], input[type='submit']")
        submit.click()

        # Wait for dashboard/status page to load
        wait.until(lambda d: d.current_url != LOGIN_URL or
                   len(d.find_elements(By.CSS_SELECTOR, ".status, #status, [class*='status'], [id*='status']")) > 0)
        screenshot(driver, "03_after_login")
        print(f"  Post-login URL: {driver.current_url}")

        # ── Extract status ───────────────────────────────────────────────────
        # Print full page text for debugging on first runs
        body_text = driver.find_element(By.TAG_NAME, "body").text
        print(f"\n  --- Page text (first 1000 chars) ---")
        print(body_text[:1000])
        print(f"  ---")

        # Try common status element patterns
        status_text = None
        for sel in [
            "[class*='status']", "[id*='status']",
            "[class*='stage']",  "[id*='stage']",
            "td.status", ".application-status",
            "#applicationStatus", ".current-status",
        ]:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els:
                t = el.text.strip()
                if t and len(t) > 2:
                    status_text = t
                    print(f"  Status found via '{sel}': {t!r}")
                    break
            if status_text:
                break

        if not status_text:
            # Scan page for known status keywords
            for keyword in ["Application Received", "Under Process", "Under Scrutiny",
                            "Approved", "Dispatched", "Rejected", "Pending"]:
                if keyword.lower() in body_text.lower():
                    status_text = keyword
                    print(f"  Status found via keyword scan: {keyword!r}")
                    break

        if not status_text:
            print("  Could not extract status from page.")
            screenshot(driver, "04_status_not_found")
            return None

        screenshot(driver, "04_status_found")
        return status_text

    except Exception as e:
        print(f"\n  ERROR: {e}")
        import traceback
        traceback.print_exc()
        try:
            screenshot(driver, "error_state")
        except Exception:
            pass
        return None
    finally:
        try:
            stitch_screenshots()
        except Exception:
            pass
        try:
            driver.quit()
        except Exception:
            pass


def run():
    visible = "--visible" in sys.argv
    status = check_status(visible=visible)

    if status is None:
        print("Could not determine status — check screenshots.")
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
