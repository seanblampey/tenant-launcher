"""Update check + one-click install from GitHub Releases.

Works without credentials when the repo is public. For a private repo put a read-only
GitHub token in settings.json as "github_token" (each PC), or make the repo public.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from common import APP_VERSION, FROZEN, load_settings

REPO = "seanblampey/tenant-launcher"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"


@dataclass
class Update:
    version: str
    asset_name: str
    asset_api_url: str      # api asset url (works with token for private repos)
    asset_browser_url: str  # plain download url (public repos)
    notes: str


class UpdateError(Exception):
    pass


def _vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def _headers(accept: str) -> dict:
    h = {"Accept": accept, "User-Agent": f"TenantLauncher/{APP_VERSION}"}
    tok = (load_settings().get("github_token") or os.environ.get("TL_GITHUB_TOKEN") or "").strip()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def check() -> Update | None:
    """Returns an Update if a newer release exists, None if up to date. Raises UpdateError if it can't tell."""
    req = urllib.request.Request(API_LATEST, headers=_headers("application/vnd.github+json"))
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            rel = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise UpdateError("Can't read releases: the GitHub repo is private and no token is configured.")
        raise UpdateError(f"GitHub returned {e.code}.")
    except Exception as e:
        raise UpdateError(f"No connection to GitHub ({type(e).__name__}).")
    tag = rel.get("tag_name") or ""
    if _vtuple(tag) <= _vtuple(APP_VERSION):
        return None
    assets = [a for a in rel.get("assets", []) if a.get("name", "").lower().endswith(".exe")]
    if not assets:
        raise UpdateError(f"Release {tag} has no installer attached.")
    a = assets[0]
    return Update(tag.lstrip("v"), a["name"], a["url"], a["browser_download_url"], rel.get("body") or "")


def download(upd: Update, progress=None) -> Path:
    """Downloads the installer to %TEMP%. progress(done_bytes, total_bytes) is optional."""
    dest = Path(tempfile.gettempdir()) / upd.asset_name
    req = urllib.request.Request(upd.asset_api_url, headers=_headers("application/octet-stream"))
    try:
        with urllib.request.urlopen(req, timeout=60) as r, dest.open("wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk); done += len(chunk)
                if progress:
                    progress(done, total)
    except Exception as e:
        raise UpdateError(f"Download failed: {e}")
    if dest.stat().st_size < 1_000_000:
        raise UpdateError("Downloaded file is too small to be the installer.")
    return dest


def install_and_restart(installer: Path) -> None:
    """Run the installer silently (it closes this app), then relaunch the app. Caller exits afterwards."""
    exe = Path(sys.executable) if FROZEN else None
    # A detached helper (hidden PowerShell: works without a console, unlike cmd's `start`) waits for the
    # silent install to finish, then relaunches the app. This process exits right after spawning it.
    ps = (f'Start-Process -FilePath "{installer}" -Wait '
          f'-ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART","/CLOSEAPPLICATIONS"')
    if exe:
        ps += f'; Start-Process -FilePath "{exe}"'
    # Note: DETACHED_PROCESS must NOT be used here - a console app spawned with it from a windowed
    # process never runs. CREATE_NO_WINDOW alone is hidden, works, and outlives this process.
    subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
                     creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
