"""
OCI Application Status Checker — ociservices.gov.in

Logs in with email + password, reads the current application status,
and sends an ntfy.sh notification if the status has changed since last run.

Status is persisted via GitHub Variable LAST_KNOWN_STATUS (updated via API).
"""

import os
import re
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


def _solve_captcha_claude(img_bytes: bytes) -> str | None:
    """Use Claude Vision API to read captcha — most accurate."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    import base64, json
    b64 = base64.b64encode(img_bytes).decode()
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 50,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": "Read the CAPTCHA text exactly — letters and digits, case-sensitive. Reply with ONLY the captcha characters, no spaces or explanation."}
            ]
        }]
    }
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode(),
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        result = re.sub(r"[^A-Za-z0-9]", "", data["content"][0]["text"].strip())
        print(f"  Claude Vision result: {result!r}")
        return result if result else None
    except Exception as e:
        print(f"  Claude Vision failed ({e}) — falling back to pytesseract")
        return None


def _solve_captcha_tesseract(img_bytes: bytes, attempt_num: int) -> str | None:
    """Fallback OCR via pytesseract."""
    try:
        import pytesseract
        from PIL import Image, ImageFilter, ImageEnhance
        import io

        raw_img = Image.open(io.BytesIO(img_bytes))
        raw_img = raw_img.resize((raw_img.width * 4, raw_img.height * 4), Image.LANCZOS)
        raw_img = raw_img.convert("L")

        light_img = ImageEnhance.Contrast(raw_img).enhance(1.5)
        light_img = light_img.filter(ImageFilter.SHARPEN)

        bin_img = ImageEnhance.Contrast(raw_img).enhance(2.0)
        bin_img = bin_img.filter(ImageFilter.SHARPEN)
        bin_img = bin_img.point(lambda x: 0 if x < 160 else 255, "1").convert("L")

        bin_img.save(SCREENSHOT_DIR / f"captcha_processed_{attempt_num}.png")
        light_img.save(SCREENSHOT_DIR / f"captcha_light_{attempt_num}.png")

        whitelist = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        candidates = []
        for img_variant, label in [(light_img, "light"), (bin_img, "binary")]:
            for oem in [1, 3]:
                for psm in [7, 8, 13]:
                    t = pytesseract.image_to_string(
                        img_variant,
                        config=f"--psm {psm} --oem {oem} -c tessedit_char_whitelist={whitelist}"
                    ).strip().replace(" ", "")
                    if t:
                        print(f"  Tesseract {label} oem={oem} psm={psm}: {t!r} (len={len(t)})")
                        candidates.append(t)

        for t in candidates:
            if len(t) == 6:
                return t
        for t in candidates:
            if 5 <= len(t) <= 7:
                return t
        return None
    except ImportError:
        print("  pytesseract not installed")
        return None
    except Exception as e:
        print(f"  Tesseract error: {e}")
        return None


def _solve_captcha(page, attempt_num: int = 0) -> str | None:
    """Read captcha from page: try Claude Vision first, then pytesseract."""
    CAPTCHA_SEL = (
        "img[src*='captcha' i], img[alt*='captcha' i], .captcha img, "
        "#captchaImg, img[id*='captcha' i]"
    )
    captcha_img = page.query_selector(CAPTCHA_SEL)
    if not captcha_img:
        print("  Could not find captcha image element")
        return None

    src = captcha_img.get_attribute("src") or ""
    print(f"  Captcha img src: {src[:80]}")

    img_bytes = captcha_img.screenshot()
    raw_path = SCREENSHOT_DIR / f"captcha_raw_{attempt_num}.png"
    raw_path.write_bytes(img_bytes)
    print(f"  Raw captcha saved: {raw_path}")

    result = _solve_captcha_claude(img_bytes)
    if result:
        return result

    print("  Claude Vision unavailable — falling back to pytesseract")
    return _solve_captcha_tesseract(img_bytes, attempt_num)


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

            # Click "User Login" in top nav to reach login form
            page.click("text=User Login")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(SCREENSHOT_DIR / "02_login_form.png"))
            print(f"  Login form URL: {page.url}")

            # Fill credentials
            email_sel = "input[placeholder*='Email' i], input[type='email'], input[name*='email' i], input[id*='email' i]"
            page.wait_for_selector(email_sel, timeout=10000)
            page.fill(email_sel, OCI_EMAIL)
            page.fill("input[type='password']", OCI_PASSWORD)

            page.screenshot(path=str(SCREENSHOT_DIR / "03_credentials_filled.png"))

            # Submit — retry up to 3 times on captcha mismatch
            logged_in = False
            for attempt in range(1, 4):
                captcha_text = _solve_captcha(page, attempt_num=attempt)
                print(f"  Captcha OCR attempt {attempt}: {captcha_text!r}")
                if captcha_text:
                    page.fill("input[placeholder*='CAPTCHA' i], input[name*='captcha' i], input[id*='captcha' i]", captcha_text)
                page.click("button:has-text('LOGIN'), button[type='submit'], input[type='submit']")
                page.wait_for_load_state("networkidle")
                print(f"  Post-login URL (attempt {attempt}): {page.url}")

                page_text = page.inner_text("body").lower()
                captcha_error = any(
                    phrase in page_text
                    for phrase in ["captcha mismatch", "invalid captcha", "valid captcha", "enter captcha"]
                )
                if captcha_error:
                    print(f"  Captcha error — reloading login page for fresh captcha…")
                    page.goto(f"{BASE_URL}/onlineOCI/login", wait_until="networkidle")
                    page.wait_for_timeout(500)
                    page.fill(email_sel, OCI_EMAIL)
                    page.fill("input[type='password']", OCI_PASSWORD)
                    continue

                # No captcha error — assume logged in
                logged_in = True
                break

            page.screenshot(path=str(SCREENSHOT_DIR / "04_after_login.png"))

            # ── Navigate to View Status ──────────────────────────────────────
            page.screenshot(path=str(SCREENSHOT_DIR / "04_dashboard.png"))

            # Wait a bit extra for any AJAX status sections to load
            page.wait_for_timeout(2000)

            view_status_link = page.query_selector("a:has-text('View Status'), a:has-text('view status')")
            if view_status_link:
                href = view_status_link.get_attribute("href") or ""
                print(f"  View Status href: {href!r}")
                if href.startswith("javascript:"):
                    # Extract and call the JS function directly
                    js_call = href[len("javascript:"):]
                    print(f"  Calling JS: {js_call}")
                    page.evaluate(js_call)
                else:
                    view_status_link.click()
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(2000)
                page.screenshot(path=str(SCREENSHOT_DIR / "05_status_page.png"))
                print(f"  Status page URL: {page.url}")

            # ── Extract status ───────────────────────────────────────────────
            body_text = page.inner_text("body")
            print(f"\n  --- Page text (first 2000 chars) ---")
            print(body_text[:2000])
            print(f"  ---\n")

            status_text = None

            # 1. Extract status code from modal table: line after "Remark" header
            lines = [l.strip() for l in body_text.splitlines()]
            for i, line in enumerate(lines):
                if line.lower() == "remark" and i + 1 < len(lines):
                    candidate = lines[i + 1].strip()
                    if candidate and candidate.upper() != "N/A" and len(candidate) < 60:
                        status_text = candidate
                        print(f"  Status from modal table: {status_text!r}")
                        break
                    # Keep looking — "N/A" means status is on a later line
                    for j in range(i + 1, min(i + 5, len(lines))):
                        c = lines[j].strip()
                        if c and c.upper() not in ("N/A", "") and len(c) < 60:
                            status_text = c
                            print(f"  Status from modal table (skip N/A): {status_text!r}")
                            break
                    if status_text:
                        break

            # 2. Keyword scan with hyphenated variants
            if not status_text:
                for keyword in [
                    "UNDER-PROCESS", "Under-Process",
                    "Application Received", "Under Process", "Under Scrutiny",
                    "Approved", "Dispatched", "Rejected", "Pending", "Acknowledged",
                ]:
                    if keyword.lower() in body_text.lower():
                        status_text = keyword
                        print(f"  Status from keyword scan: {status_text!r}")
                        break

            # 3. Notes fallback (gives context even without a status code)
            if not status_text:
                notes = re.findall(r"Note\s*:\s*([^\n]+)", body_text)
                if notes:
                    status_text = "Note: " + notes[0].strip()
                    print(f"  Status from Note fallback: {status_text!r}")

            if not status_text:
                print("  Could not extract status — check screenshot and trace.")
                page.screenshot(path=str(SCREENSHOT_DIR / "06_status_not_found.png"))
            else:
                page.screenshot(path=str(SCREENSHOT_DIR / "06_status_found.png"))

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
