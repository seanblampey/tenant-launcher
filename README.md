# Tenant Launcher

One click per customer: opens their Microsoft 365 admin centre (or Entra, Intune, Exchange,
Defender, Teams, Azure, Purview) in an isolated Chrome profile and signs in with the global
admin login from **Bitwarden**, including the MFA code when the Bitwarden item has TOTP set up.

Each team member signs in with their **own Bitwarden account**. The Bitwarden CLI is bundled
in the installer, so there is nothing else to set up. Customers are discovered automatically
from Bitwarden items whose name starts with `O365 -`.

Nothing is stored by this app except Chrome's own per-customer profile
(`%LOCALAPPDATA%\TenantLauncher\profiles\Tenant-<Customer>`), which holds the signed-in
session the same way a normal browser would, and the customer list (names + Bitwarden item IDs).

## Install (team members)

1. Run `TenantLauncher-Setup-<version>.exe` from the latest GitHub release (per-user, no admin).
2. Have Google Chrome (or Edge) installed.
3. Start **Tenant Launcher**, sign in with your Bitwarden email + master password
   (and two-step code if your account has one).
4. Wait a few seconds for "Vault cached": every `O365 - …` item appears in the list.
5. Double-click a customer.

## Using it

* Double-click a customer (or press Enter) = M365 admin centre.
* Select a customer and press a portal button to open a different portal.
* Several customers can be open at once; each is its own Chrome window/profile.
* Close the Chrome window when done. The next launch reuses the session cookies in that
  profile, so it usually goes straight in. "Stay signed in" and "Don't ask again for 30 days"
  are ticked automatically.
* **Scan now** re-checks Bitwarden for new `O365 -` items (also runs after every sign-in and
  sync). Change the prefix box if your naming is different.
* **Sign out** logs the bundled CLI out of Bitwarden so another team member can sign in.

## What the sign-in automation does

It watches the Microsoft sign-in page and fills whatever step appears:
username -> password -> MFA code (TOTP computed from the Bitwarden item's secret) -> "Stay signed in: Yes".
If Microsoft offers an Authenticator push/number-match instead of a code, it clicks
"Sign in another way" and picks "Use a verification code". If something unexpected shows up
(wrong password, no TOTP on the item, a conditional-access block, password-change prompt)
it stops and leaves the window for you to finish by hand. A per-customer log is written next
to the profiles: `%LOCALAPPDATA%\TenantLauncher\profiles\Tenant-<Customer>.log`.

## Speed

The Bitwarden CLI takes ~3s per call, so the app avoids it on the click path: after you sign
in it reads the vault once into memory, hands the item to the browser worker over a private
pipe (never the command line or environment), and computes the MFA code locally from the
item's TOTP secret. Chrome is up in about a second and each sign-in step is filled as soon as
it appears.

## Building the installer

```
.\Get-BwCli.ps1      # puts bw.exe in bin\ (copies an existing install or downloads the official release)
.\build.ps1          # PyInstaller -> dist\TenantLauncher\, Inno Setup -> installer\TenantLauncher-Setup-<ver>.exe
```

Needs Python 3.12, `pip install -r requirements.txt pyinstaller`, and Inno Setup 6
(`winget install JRSoftware.InnoSetup`). GitHub Actions does the same on every push and
attaches the installer to a Release when you push a tag like `v1.0.1`
(bump `APP_VERSION` in `common.py` first).

## Running from source

```
pip install -r requirements.txt
python tenant_launcher.py
```

From source the customer list lives in `customers.csv` next to the scripts; the installed app
keeps it in `%LOCALAPPDATA%\TenantLauncher\customers.csv`. Both are `Name,BitwardenItem,Notes`.

## Files

| File | Purpose |
|------|---------|
| `tenant_launcher.py` | Entry point: the window, or `--customer "Name"` for one browser worker. |
| `app.py` | The window: customer list, portal buttons, Bitwarden sign-in, auto-add. |
| `login_worker.py` | One per browser window: drives the Microsoft sign-in, stays alive until Chrome closes. |
| `bw.py` | Bitwarden CLI wrapper (login / unlock / list; secrets via env var, never argv). |
| `common.py` | Portal URLs, paths, settings, customer list. `APP_VERSION` lives here. |
| `build.ps1`, `installer.iss`, `Get-BwCli.ps1` | Packaging. |
| `Make-Shortcuts.ps1` | Optional per-customer desktop shortcuts. |

## Worth knowing

* Sessions last as long as the tenant's sign-in frequency / token policy allows. Conditional
  access policies that require a compliant device or block the login still apply.
* Bitwarden accounts protected by a hardware key other than YubiKey OTP, or by Duo, can't sign
  in through the CLI; use an authenticator app or email code for two-step on those accounts.
* Microsoft is pushing partners towards GDAP (Partner Center / Lighthouse) instead of
  per-tenant GA accounts; this tool doesn't change that, it just makes the per-tenant logins
  painless.
* To wipe a customer's saved browser session, delete their folder under
  `%LOCALAPPDATA%\TenantLauncher\profiles`.
