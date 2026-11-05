"""Thin wrapper around the Bitwarden CLI (bw).

Secrets never touch disk or the command line: the master password goes in via an
environment variable (--passwordenv) and the session key is kept in memory /
passed to child processes via BW_SESSION.

bw.exe is looked for next to the app first (bin\\bw.exe, bundled by the installer),
then on PATH.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _find_bw() -> str | None:
    here = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    for cand in (here / "bin" / "bw.exe", here / "bw.exe"):
        if cand.exists():
            return str(cand)
    return shutil.which("bw")


BW = _find_bw()


class BwError(Exception):
    pass


class TwoStepRequired(BwError):
    pass


TWO_STEP_METHODS = {"Authenticator app": 0, "Email": 1, "YubiKey": 3}


def _run(args: list[str], session: str | None = None, env: dict | None = None, timeout: int = 90) -> str:
    if not BW:
        raise BwError("Bitwarden CLI (bw.exe) not found. Reinstall Tenant Launcher, or put bw.exe in its bin folder.")
    full_env = dict(os.environ)
    full_env.pop("BW_SESSION", None)
    if env:
        full_env.update(env)
    cmd = [BW, *args, "--nointeraction"]
    if session:
        cmd += ["--session", session]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=full_env, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise BwError(f"bw {args[0]} timed out after {timeout}s")
    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "").strip() or f"bw {' '.join(args)} failed (exit {p.returncode})"
        raise BwError(msg)
    return p.stdout.strip()


def status() -> dict:
    return json.loads(_run(["status"]))


def login(email: str, master_password: str, method: int | None = None, code: str | None = None) -> str:
    """bw login -> session key. Raises TwoStepRequired when the account needs a 2FA code."""
    args = ["login", email, "--passwordenv", "TL_BW_PASSWORD", "--raw"]
    if method is not None and code:
        args += ["--method", str(method), "--code", code]
    try:
        return _run(args, env={"TL_BW_PASSWORD": master_password}, timeout=120)
    except BwError as e:
        msg = str(e)
        low = msg.lower()
        if "two-step" in low or "two step" in low or "2fa" in low or "twofactor" in low:
            raise TwoStepRequired(msg)
        if "already logged in" in low:
            raise BwError("Bitwarden CLI is already logged in as someone else. Use 'Sign out' first.")
        if "incorrect" in low or "invalid" in low:
            raise BwError("Bitwarden rejected that email / master password" + (" / code." if code else "."))
        raise


def logout() -> None:
    try:
        _run(["logout"])
    except BwError:
        pass


def unlock(master_password: str) -> str:
    """Returns a session key. Raises BwError on wrong password."""
    try:
        return _run(["unlock", "--passwordenv", "TL_BW_PASSWORD", "--raw"], env={"TL_BW_PASSWORD": master_password})
    except BwError as e:
        if "Invalid master password" in str(e):
            raise BwError("Invalid master password.")
        raise


def session_is_valid(session: str) -> bool:
    try:
        return json.loads(_run(["status"], session))["status"] == "unlocked"
    except Exception:
        return False


def sync(session: str) -> None:
    _run(["sync"], session, timeout=180)


def get_item(name_or_id: str, session: str) -> dict:
    try:
        return json.loads(_run(["get", "item", name_or_id], session))
    except BwError as e:
        s = str(e)
        if "More than one result" in s:
            raise BwError(f"More than one Bitwarden item matches '{name_or_id}'. "
                          "Use the exact item name or the item ID in customers.csv.")
        if "Not found" in s:
            raise BwError(f"No Bitwarden item called '{name_or_id}'. Check the name (or use the item ID).")
        raise


def get_totp(item_id: str, session: str) -> str | None:
    try:
        code = _run(["get", "totp", item_id], session)
        return code or None
    except BwError:
        return None


def search_items(query: str, session: str) -> list[dict]:
    args = ["list", "items"]
    if query.strip():
        args += ["--search", query.strip()]
    return json.loads(_run(args, session, timeout=180))


def list_login_items(session: str) -> dict[str, dict]:
    """All Login items in the vault keyed by id (one bw call, cached by the app)."""
    return {it["id"]: it for it in search_items("", session) if it.get("login")}


def local_totp(item: dict) -> str | None:
    """Current TOTP code computed locally from the item's secret (no bw call)."""
    secret = ((item.get("login") or {}).get("totp") or "").strip()
    if not secret:
        return None
    try:
        import pyotp
        if secret.lower().startswith("otpauth://"):
            return pyotp.parse_uri(secret).now()
        if secret.lower().startswith("steam://"):
            return None
        return pyotp.TOTP(secret.replace(" ", "").replace("-", "").upper()).now()
    except Exception:
        return None


def login_fields(item: dict) -> tuple[str, str, bool]:
    """(username, password, has_totp) from a bw item; raises if it is not a login item."""
    login_ = item.get("login")
    if not login_:
        raise BwError(f"Bitwarden item '{item.get('name')}' is not a Login item.")
    user = login_.get("username") or ""
    pw = login_.get("password") or ""
    if not user or not pw:
        raise BwError(f"Bitwarden item '{item.get('name')}' is missing a username or password.")
    return user, pw, bool(login_.get("totp"))
