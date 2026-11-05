"""Single entry point for the packaged TenantLauncher.exe.

  TenantLauncher.exe                      -> the launcher window
  TenantLauncher.exe --customer "Name"    -> one browser worker (what the window spawns per click)
"""
import os
import sys

# Make the bundle / source folder importable and the working directory predictable.
_HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
sys.path.insert(0, _HERE)


def main() -> int:
    if "--customer" in sys.argv:
        from login_worker import main as worker_main
        return worker_main()
    from app import App
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
