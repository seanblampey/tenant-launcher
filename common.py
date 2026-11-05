"""Shared bits: portal list, customer list (customers.csv), settings, paths."""
import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

APP_VERSION = "1.0.1"
FROZEN = bool(getattr(sys, "frozen", False))

# Source checkout: files live next to the scripts. Installed exe: exe folder (read-only) + per-user data dir.
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TenantLauncher"
CSV_PATH = (DATA_DIR / "customers.csv") if FROZEN else (ROOT / "customers.csv")
SETTINGS_PATH = DATA_DIR / "settings.json"
PROFILES_DIR = DATA_DIR / "profiles"

DEFAULT_ITEM_PREFIX = "O365 -"   # Bitwarden items whose name starts with this are auto-added as customers

PORTALS = {
    "admin":    ("M365 Admin",  "https://admin.microsoft.com"),
    "entra":    ("Entra",       "https://entra.microsoft.com"),
    "intune":   ("Intune",      "https://intune.microsoft.com"),
    "exchange": ("Exchange",    "https://admin.exchange.microsoft.com"),
    "defender": ("Defender",    "https://security.microsoft.com"),
    "teams":    ("Teams",       "https://admin.teams.microsoft.com"),
    "azure":    ("Azure",       "https://portal.azure.com"),
    "purview":  ("Purview",     "https://purview.microsoft.com"),
}

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
]
EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def browser_channel() -> str:
    """Playwright channel name for an already-installed browser (no download needed)."""
    if any(Path(p).exists() for p in CHROME_PATHS):
        return "chrome"
    if any(Path(p).exists() for p in EDGE_PATHS):
        return "msedge"
    raise RuntimeError("Neither Google Chrome nor Microsoft Edge was found.")


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(d: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(d, indent=1), encoding="utf-8")


@dataclass
class Customer:
    name: str
    bitwarden_item: str
    notes: str = ""

    @property
    def slug(self) -> str:
        return "Tenant-" + re.sub(r"[^A-Za-z0-9]+", "-", self.name.strip()).strip("-")

    @property
    def profile_dir(self) -> Path:
        return PROFILES_DIR / self.slug


def load_customers() -> list[Customer]:
    if not CSV_PATH.exists():
        return []
    out = []
    with CSV_PATH.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = (row.get("Name") or "").strip()
            if not name or name.startswith("#"):
                continue
            out.append(Customer(name, (row.get("BitwardenItem") or "").strip(), (row.get("Notes") or "").strip()))
    return out


def save_customers(customers: list[Customer]) -> None:
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Name", "BitwardenItem", "Notes"])
        for c in customers:
            w.writerow([c.name, c.bitwarden_item, c.notes])


def find_customer(query: str, customers: list[Customer] | None = None) -> Customer | None:
    customers = customers if customers is not None else load_customers()
    q = query.strip().lower()
    exact = [c for c in customers if c.name.lower() == q]
    if len(exact) == 1:
        return exact[0]
    partial = [c for c in customers if q in c.name.lower()]
    return partial[0] if len(partial) == 1 else None
