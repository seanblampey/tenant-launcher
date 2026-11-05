"""Single entry point for the packaged TenantLauncher.exe.

  TenantLauncher.exe                      -> the launcher window
  TenantLauncher.exe --customer "Name"    -> one browser worker (what the window spawns per click)
"""
import os
import sys

# Make the bundle / source folder importable and the working directory predictable.
_HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
sys.path.insert(0, _HERE)


def selftest(out_path: str) -> int:
    """TenantLauncher.exe --selftest <file>: writes what the bundle can see (used by build checks)."""
    lines = []
    try:
        import bw, common
        from playwright.sync_api import sync_playwright  # noqa: F401
        import pyotp  # noqa: F401
        lines.append(f"version={common.APP_VERSION}")
        lines.append(f"frozen={common.FROZEN}")
        lines.append(f"bw={bw.BW}")
        lines.append(f"bw_status={bw.status().get('status')}")
        lines.append(f"browser={common.browser_channel()}")
        lines.append(f"csv={common.CSV_PATH}")
        lines.append("ok=1")
    except Exception as e:
        lines.append(f"error={type(e).__name__}: {e}")
        lines.append("ok=0")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(chr(10).join(lines) + chr(10))
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest(sys.argv[sys.argv.index("--selftest") + 1])
    if "--customer" in sys.argv:
        from login_worker import main as worker_main
        return worker_main()
    from app import App
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
