"""Thin wrapper around the Bitwarden CLI (bw).

Secrets never touch disk or the command line: the master password goes in via an
environment variable (--passwordenv) and the session key is kept in memory /
passed to child processes via BW_SESSION.

bw.exe is looked for next to the app first (bin/bw.exe, bundled by the installer),
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
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
        if getattr(sys, "_MEIPASS", None):          # PyInstaller onedir puts bundled data in _internal
            roots.append(Path(sys._MEIPASS))
    else:
        roots.append(Path(__file__).resolve().parent)
    for root in roots:
        for cand in (root / "bin" / "bw.exe", root / "bw.exe"):
            if cand.exists():
                return str(cand)
    return shutil.which("bw")


BW = _find_bw()


class BwError(Exception):
    pass


class TwoStepRequired(BwError):
    """Bitwarden wants a code. multiple=True means the account has several two-step methods."""

    def __init__(self, msg: str, multiple: bool = False):
        super().__init__(msg)
        self.multiple = multiple


TWO_STEP_METHODS = {"Authenticator app": 0, "Email (code sent to you)": 1, "YubiKey": 3}

# What `bw login --nointeraction` prints instead of prompting (apps/cli/src/auth/commands/login.command.ts):
#   one two-step method, or new-device verification  -> "Code is required."
#   several two-step methods                          -> "Login failed. No provider selected."
_CODE_REQUIRED = "code is required"
_NO_PROVIDER = "no provider selected"
_BAD_METHOD = "invalid two-step login method"


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
    """bw login -> session key.

    First call with no code. Raises TwoStepRequired when Bitwarden wants a code (two-step login,
    or the new-device email check on accounts without two-step). Call again with method+code;
    if the CLI still wants a code after a valid two-step code, that is new-device verification,
    which the CLI only accepts at its interactive prompt, so we answer that prompt over a pipe.
    """
    args = ["login", email, "--passwordenv", "TL_BW_PASSWORD", "--raw"]
    if method is not None and code:
        args += ["--method", str(method), "--code", code]
    try:
        return _run(args, env={"TL_BW_PASSWORD": master_password}, timeout=120)
    except BwError as e:
        msg = str(e)
        low = msg.lower()
        if _NO_PROVIDER in low:
            raise TwoStepRequired(msg, multiple=True)
        if _CODE_REQUIRED in low or (code and _BAD_METHOD in low):
            if code:
                # Two-step satisfied (or not configured) but a code is still wanted -> new-device email code.
                return _login_interactive(email, master_password, method, code, device_code=code)
            raise TwoStepRequired(msg)
        if "already logged in" in low:
            raise BwError("Bitwarden CLI is already logged in as someone else. Use 'Sign out' first.")
        if "incorrect" in low or "invalid" in low or "not found" in low:
            raise BwError("Bitwarden rejected that " + ("code." if code else "email / master password."))
        raise


def send_email_code(email: str, master_password: str) -> None:
    """Make Bitwarden email a two-step code (accounts with the Email method among several)."""
    try:
        _run(["login", email, "--passwordenv", "TL_BW_PASSWORD", "--raw", "--method", "1"],
             env={"TL_BW_PASSWORD": master_password}, timeout=120)
    except BwError as e:
        low = str(e).lower()
        if _CODE_REQUIRED in low:
            return                      # email was sent, CLI then stopped for lack of a code: expected
        if _BAD_METHOD in low:
            raise BwError("This Bitwarden account doesn't have Email as a two-step method.")
        raise


def _strip_ansi(s: str) -> str:
    import re
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", s).replace("\r", "")


def _login_interactive(email: str, master_password: str, method: int | None, code: str | None,
                       device_code: str, timeout: int = 150) -> str:
    """Run `bw login` without --nointeraction and answer its prompts over stdin.
    Used for new-device verification, which has no command-line flag."""
    import threading
    if not BW:
        raise BwError("Bitwarden CLI (bw.exe) not found.")
    cmd = [BW, "login", email, "--passwordenv", "TL_BW_PASSWORD", "--raw"]
    if method is not None and code:
        cmd += ["--method", str(method), "--code", code]
    env = dict(os.environ)
    env.pop("BW_SESSION", None); env.pop("BW_NOINTERACTION", None)
    env["TL_BW_PASSWORD"] = master_password
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=env, creationflags=_NO_WINDOW)
    err = bytearray()
    answered: set[str] = set()

    def answer(key: str, text: str):
        if key in answered:
            return
        answered.add(key)
        try:
            p.stdin.write((text + "\n").encode("utf-8")); p.stdin.flush()
        except Exception:
            pass

    def pump():
        while True:
            chunk = p.stderr.read(1)
            if not chunk:
                break
            err.extend(chunk)
            tail = err[-400:].decode("utf-8", "replace").lower()
            if "verification required" in tail:
                answer("device", device_code)
            elif "two-step login code" in tail:
                answer("code", code or device_code)
            elif "two-step login method" in tail:
                answer("method", "")             # Enter = first listed method
            elif "master password" in tail:
                answer("pw", master_password)    # should not happen with --passwordenv
    t = threading.Thread(target=pump, daemon=True); t.start()
    try:
        out = p.stdout.read().decode("utf-8", "replace")
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        raise BwError("Bitwarden sign-in timed out waiting for the verification code.")
    finally:
        try:
            p.stdin.close()
        except Exception:
            pass
    t.join(timeout=5)
    if p.returncode != 0:
        lines = [ln.strip() for ln in _strip_ansi(err.decode("utf-8", "replace")).splitlines()
                 if ln.strip() and not ln.strip().startswith("?") and "data file" not in ln]
        msg = lines[-1] if lines else "Bitwarden sign-in failed."
        if "master password" not in msg.lower() and ("invalid" in msg.lower() or "incorrect" in msg.lower()):
            msg = "Bitwarden rejected that code."
        raise BwError(msg)
    session = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if not session:
        raise BwError("Bitwarden signed in but returned no session key.")
    return session


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
