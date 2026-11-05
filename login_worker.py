"""Opens one customer's isolated Chrome profile at a Microsoft portal and signs in
using the credentials (and TOTP) from their Bitwarden item.

Usage:  pythonw login_worker.py --customer "Contoso" [--portal intune]

Normally launched by app.py with BW_SESSION already in the environment. If run
on its own (e.g. from a desktop shortcut) it prompts for the master password.

The process stays alive until the browser window is closed - that keeps the
Chrome profile (and the signed-in session cookies in it) open.
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog

import bw
from common import PORTALS, PROFILES_DIR, browser_channel, find_customer

MS_LOGIN_HOSTS = re.compile(r"https://(login\.microsoftonline\.com|login\.microsoft\.com|login\.live\.com|"
                            r"login\.windows\.net|login\.microsoftonline\.us|device\.login\.microsoftonline\.com)", re.I)

# Selectors for the Microsoft sign-in flow. Classic ids first, newer "lightbox" fallbacks second.
# Microsoft keeps the inactive step's input in the DOM with aria-hidden="true", tabindex="-1" and opacity 0,
# so every field selector excludes those.
_ACTIVE = ':not([aria-hidden="true"]):not([tabindex="-1"])'
SEL_EMAIL = f'input[name="loginfmt"]{_ACTIVE}, input[type="email"]{_ACTIVE}'
SEL_PASSWORD = f'input[name="passwd"]{_ACTIVE}, input[type="password"]{_ACTIVE}'
SEL_OTC = f'input[name="otc"]{_ACTIVE}, input[autocomplete="one-time-code"]{_ACTIVE}'
SEL_SUBMIT = '#idSIButton9, input[type="submit"], button[type="submit"]'
SEL_KMSI = '#KmsiCheckboxField'
SEL_TOTP_REMEMBER = '#idChkBx_SAOTCC_TD'
SEL_APPROVE_PROMPT = '#idDiv_SAOTCAS_Title, #idDiv_SAOTCAS_Description, #idRemoteNGC_DisplaySign'
SEL_OTHER_WAY = '#signInAnotherWay, #idA_PWD_SwitchToCredPicker'
SEL_TOTP_OPTION = 'div[data-value="PhoneAppOTP"], [data-value="PhoneAppOTP"]'
SEL_OTHER_TILE = '#otherTile'
SEL_ERRORS = '#passwordError, #usernameError, #idSpan_SAOTCC_Error_OTC, #idTd_SAOTCS_Error, .alert-error'


def _vis(page, sel: str) -> bool:
    try:
        loc = page.locator(sel).filter(visible=True)
        if loc.count() == 0:
            return False
        box = loc.first.bounding_box(timeout=250)
        return bool(box and box["width"] > 0 and box["height"] > 0)
    except Exception:
        return False


def _text(page, sel: str) -> str:
    try:
        return page.locator(sel).first.inner_text(timeout=250).strip()
    except Exception:
        return ""


def _click(page, sel: str) -> bool:
    try:
        page.locator(sel).first.click(timeout=2000)
        return True
    except Exception:
        return False


def _fill(page, sel: str, value: str) -> None:
    box = page.locator(sel).filter(visible=True).first
    box.click(timeout=2000)
    box.fill(value, timeout=3000)
    try:
        if box.input_value(timeout=1000) != value:
            box.fill("", timeout=1000)
            box.type(value, delay=15)
    except Exception:
        pass


def _wait_step_change(page, sel: str, max_s: float = 8.0) -> None:
    """After clicking Next: wait until that step's field is gone (or the URL changes), instead of a fixed sleep."""
    try:
        start_url = page.url
    except Exception:
        return
    end = time.time() + max_s
    while time.time() < end:
        try:
            if page.url != start_url or not _vis(page, sel) or _text(page, SEL_ERRORS):
                time.sleep(0.25)
                return
        except Exception:
            return
        time.sleep(0.1)


def auto_login(page, username: str, password: str, get_totp, log, timeout: int = 150) -> str:
    """Drive the Microsoft sign-in page until we land on the portal.
    Returns 'ok', 'already', 'manual' (left for the user) or an error string."""
    deadline = time.time() + timeout
    tries = {"email": 0, "pw": 0, "otc": 0, "other": 0}
    seen_login = False
    settled_since = None

    while time.time() < deadline:
        try:
            url = page.url
        except Exception:
            return "closed"

        if not MS_LOGIN_HOSTS.match(url) and not url.startswith("about:"):
            # Off the login host - give the portal 2s to be sure it is not bouncing back.
            settled_since = settled_since or time.time()
            if time.time() - settled_since > 1.0:
                return "ok" if seen_login else "already"
            time.sleep(0.2)
            continue
        settled_since = None
        seen_login = True

        try:
            err = _text(page, SEL_ERRORS)
            if err and ("incorrect" in err.lower() or "doesn't match" in err.lower() or "invalid" in err.lower()):
                return f"Microsoft rejected the sign-in: {err}"

            # Account picker ("Pick an account")
            tile = page.locator(f'[data-test-id="{username}"]')
            if tile.count() and tile.first.is_visible(timeout=250):
                log("picking account tile"); tile.first.click(); time.sleep(1); continue
            if _vis(page, SEL_OTHER_TILE):
                log("clicking 'Use another account'"); _click(page, SEL_OTHER_TILE); time.sleep(1); continue

            # Authenticator push / number-match prompt -> switch to verification code
            if _vis(page, SEL_APPROVE_PROMPT):
                if tries["other"] >= 3:
                    return "manual"
                tries["other"] += 1
                log("push prompt shown - asking for another method")
                if _click(page, SEL_OTHER_WAY):
                    time.sleep(1.2)
                    if _vis(page, SEL_TOTP_OPTION):
                        _click(page, SEL_TOTP_OPTION); time.sleep(1.2)
                else:
                    return "manual"
                continue
            if _vis(page, SEL_TOTP_OPTION) and not _vis(page, SEL_OTC):
                log("choosing verification code"); _click(page, SEL_TOTP_OPTION); time.sleep(1.2); continue

            # Stay signed in?
            if _vis(page, SEL_KMSI):
                log("Stay signed in -> Yes")
                try:
                    page.locator(SEL_KMSI).first.check(timeout=1000)
                except Exception:
                    pass
                _click(page, SEL_SUBMIT); _wait_step_change(page, SEL_KMSI, 5); continue

            email_vis = _vis(page, SEL_EMAIL)
            pw_vis = (not email_vis) and _vis(page, SEL_PASSWORD)
            otc_vis = (not email_vis) and (not pw_vis) and _vis(page, SEL_OTC)

            # TOTP code
            if otc_vis:
                if tries["otc"] >= 3:
                    return "manual"
                code = get_totp()
                if not code:
                    log("no TOTP in Bitwarden item - leaving MFA to you")
                    return "manual"
                tries["otc"] += 1
                log("entering MFA code")
                _fill(page, SEL_OTC, code)
                try:
                    page.locator(SEL_TOTP_REMEMBER).first.check(timeout=800)
                except Exception:
                    pass
                _click(page, SEL_SUBMIT); _wait_step_change(page, SEL_OTC); continue

            # Password
            if pw_vis:
                if tries["pw"] >= 3:
                    return "manual"
                tries["pw"] += 1
                log("entering password")
                _fill(page, SEL_PASSWORD, password)
                _click(page, SEL_SUBMIT); _wait_step_change(page, SEL_PASSWORD); continue

            # Email / username
            if email_vis:
                if tries["email"] >= 3:
                    return "manual"
                tries["email"] += 1
                log("entering username")
                _fill(page, SEL_EMAIL, username)
                _click(page, SEL_SUBMIT); _wait_step_change(page, SEL_EMAIL); continue
        except Exception as e:  # page navigated mid-step etc. - just loop
            if "TargetClosed" in type(e).__name__ or page.is_closed():
                return "closed"
            log(f"(retry: {type(e).__name__})")
        time.sleep(0.3)
    return "manual"


def ask_master_password(parent) -> str | None:
    return simpledialog.askstring("Bitwarden", "Bitwarden master password:", show="*", parent=parent)


def _creds_from_stdin() -> dict | None:
    """app.py pipes the cached Bitwarden item in as JSON. Nothing on stdin -> None (fallback to bw)."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else None
    except Exception:
        return None


def _fetch_item_via_bw(cust, root, out: dict) -> None:
    """Fallback path (shortcut launch / no cache): unlock if needed and fetch the item. ~3s per bw call."""
    session = os.environ.get("BW_SESSION")
    try:
        if not session:
            st = bw.status()
            if st.get("status") == "unauthenticated":
                out["error"] = "Not signed in to Bitwarden. Open Tenant Launcher and sign in first."; return
            pw = ask_master_password(root)
            if not pw:
                out["error"] = "cancelled"; return
            try:
                session = bw.unlock(pw)
            finally:
                pw = None
        out["item"] = bw.get_item(cust.bitwarden_item, session)
        out["session"] = session
    except bw.BwError as e:
        out["error"] = str(e)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--customer", required=True)
    ap.add_argument("--portal", default="admin", choices=list(PORTALS))
    args = ap.parse_args()
    t0 = time.time()

    root = tk.Tk(); root.withdraw()

    def fail(msg: str) -> int:
        if msg != "cancelled":
            messagebox.showerror("Tenant Launcher", msg, parent=root)
        return 1

    cust = find_customer(args.customer)
    if not cust:
        return fail(f"No customer matching '{args.customer}' in customers.csv.")
    if not cust.bitwarden_item:
        return fail(f"'{cust.name}' has no BitwardenItem set in customers.csv.")

    label, url = PORTALS[args.portal]
    cust.profile_dir.mkdir(parents=True, exist_ok=True)
    log_path = PROFILES_DIR / f"{cust.slug}.log"

    def log(msg: str):
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} +{time.time() - t0:5.1f}s  {msg}\n")

    item = _creds_from_stdin()
    fetch: dict = {}
    fetch_thread = None
    if item:
        log("creds from launcher cache")
    else:
        log("no cached creds - fetching from bw in the background")
        fetch_thread = threading.Thread(target=_fetch_item_via_bw, args=(cust, root, fetch), daemon=True)
        fetch_thread.start()

    from playwright.sync_api import sync_playwright
    try:
        channel = browser_channel()
    except RuntimeError as e:
        return fail(str(e))

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(cust.profile_dir), channel=channel, headless=False, no_viewport=True,
            ignore_default_args=["--enable-automation"],
            args=["--start-maximized", "--disable-infobars", "--no-first-run", "--no-default-browser-check"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        log(f"browser up, opening {label}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log(f"goto failed: {e}")

        session = os.environ.get("BW_SESSION")
        if fetch_thread:
            fetch_thread.join()
            if "error" in fetch:
                try:
                    ctx.close()
                except Exception:
                    pass
                return fail(fetch["error"])
            item = fetch["item"]; session = fetch.get("session") or session

        try:
            username, password, has_totp = bw.login_fields(item)
        except bw.BwError as e:
            return fail(str(e))

        def get_totp():
            if not has_totp:
                return None
            code = bw.local_totp(item)
            if not code and session:
                code = bw.get_totp(item["id"], session)
            return code

        log(f"signing in as {username}")
        result = auto_login(page, username, password, get_totp, log)
        log(f"result: {result}")
        if result not in ("ok", "already", "manual", "closed"):
            messagebox.showwarning("Tenant Launcher", f"{cust.name}: {result}\n\nFinish signing in manually.", parent=root)
        password = None

        # Keep the profile open until the user closes the browser window.
        try:
            while ctx.pages:
                time.sleep(1)
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
