"""Tenant Launcher - click a customer, land signed-in in their Microsoft admin portal.

Credentials come from Bitwarden (bundled bw CLI) at click time; nothing is stored here.
Each customer gets its own isolated Chrome profile so several tenants can be open at once.
Customers are auto-discovered from Bitwarden items whose name starts with a prefix ("O365 -").
"""
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

import ttkbootstrap as tb

import bw
import updater
from common import (APP_VERSION, CSV_PATH, DATA_DIR, DEFAULT_ITEM_PREFIX, FROZEN, PORTALS, ROOT, Customer,
                    load_customers, load_settings, resource_path, save_customers, save_settings)

PYTHONW = Path(sys.executable).with_name("pythonw.exe")
if not PYTHONW.exists():
    PYTHONW = Path(sys.executable)
WORKER = ROOT / "login_worker.py"
LOG_PATH = DATA_DIR / "app.log"
APP_USER_MODEL_ID = "XenexSystems.TenantLauncher"
THEMES = {"light": "bootstrap-light", "dark": "bootstrap-dark"}


def log_exception(where: str, exc: BaseException) -> None:
    """Windowed exes have no console, so anything unexpected goes to %LOCALAPPDATA%\\TenantLauncher\\app.log."""
    try:
        import time
        import traceback
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {where}: {''.join(traceback.format_exception(exc)).strip()}\n")
    except Exception:
        pass


def _errmsg(exc: BaseException) -> str:
    """Text for the user. Non-Bitwarden errors also get logged with their traceback."""
    if isinstance(exc, (bw.BwError, updater.UpdateError)):
        return str(exc)
    log_exception("background task", exc)
    return f"{type(exc).__name__}: {exc}  (details in {LOG_PATH})"


def _install_crash_logging() -> None:
    sys.excepthook = lambda t, v, tb_: log_exception("unhandled", v)
    threading.excepthook = lambda args: log_exception(f"thread {args.thread.name}", args.exc_value)


class UiQueue:
    """Thread-safe way to run callables on the Tk main thread. Background threads must never
    touch Tk directly (not even widget.after), so they hand work to this queue instead."""

    def __init__(self, widget):
        self._w = widget
        self._q: queue.Queue = queue.Queue()
        widget.after(40, self._pump)

    def __call__(self, fn) -> None:
        self._q.put(fn)

    def _pump(self) -> None:
        try:
            while True:
                fn = self._q.get_nowait()
                try:
                    fn()
                except Exception as e:
                    log_exception("ui task", e)
        except queue.Empty:
            pass
        try:
            if self._w.winfo_exists():
                self._w.after(40, self._pump)
        except Exception:
            pass


def _set_app_user_model_id() -> None:
    """Give the process its own taskbar identity so Windows uses this app's icon, not a cached/generic one."""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass


def _apply_icon(win) -> None:
    try:
        win.iconbitmap(default=str(resource_path("assets/icon.ico")))
        win._icon_img = tk.PhotoImage(file=str(resource_path("assets/icon-256.png")))
        win.iconphoto(True, win._icon_img)
    except Exception as e:
        log_exception("window icon", e)


class SignInDialog(tb.Toplevel):
    """Bitwarden sign-in: email + master password (+ two-step code when the account needs one)."""

    def __init__(self, parent, email: str = "", email_locked: bool = False, unlock_only: bool = False):
        super().__init__(parent)
        self.title("Unlock Bitwarden" if unlock_only else "Sign in to Bitwarden")
        self.resizable(False, False); self.transient(parent); self.grab_set()
        _apply_icon(self)
        self.result: str | None = None          # session key
        self.email_used: str = ""
        self.needs_code = False
        self.unlock_only = unlock_only

        f = tb.Frame(self, padding=(22, 18, 22, 18)); f.pack(fill="both", expand=True)
        tb.Label(f, text="Unlock your vault" if unlock_only else "Sign in with your Bitwarden account",
                 font=("Segoe UI", 13, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        tb.Label(f, text="The customer logins are read from your own vault. Nothing is stored by this app.",
                 bootstyle="secondary", wraplength=380).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 12))

        tb.Label(f, text="Email").grid(row=2, column=0, sticky="e", padx=(0, 10), pady=4)
        self.email = tk.StringVar(value=email)
        self.email_entry = tb.Entry(f, textvariable=self.email, width=36)
        self.email_entry.grid(row=2, column=1, sticky="ew", pady=4)
        if email_locked:
            self.email_entry.state(["disabled"])
        tb.Label(f, text="Master password").grid(row=3, column=0, sticky="e", padx=(0, 10), pady=4)
        self.password = tk.StringVar()
        self.pw_entry = tb.Entry(f, textvariable=self.password, show="•", width=36)
        self.pw_entry.grid(row=3, column=1, sticky="ew", pady=4)

        # two-step row (hidden until needed)
        self.code_frame = tb.Frame(f)
        tb.Label(self.code_frame, text="Two-step code").grid(row=0, column=0, sticky="e", padx=(0, 10), pady=4)
        self.code = tk.StringVar()
        self.code_entry = tb.Entry(self.code_frame, textvariable=self.code, width=10)
        self.code_entry.grid(row=0, column=1, sticky="w", pady=4)
        self.method = tk.StringVar(value="Authenticator app")
        tb.Combobox(self.code_frame, textvariable=self.method, values=list(bw.TWO_STEP_METHODS),
                    state="readonly", width=22).grid(row=0, column=2, padx=(8, 0))
        self.send_btn = tb.Button(self.code_frame, text="Email me a code", bootstyle="link", command=self.send_email_code)
        self.send_btn.grid(row=1, column=1, columnspan=2, sticky="w")

        self.msg = tb.Label(f, text="", bootstyle="danger", wraplength=380, justify="left")
        self.msg.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        btns = tb.Frame(f); btns.grid(row=6, column=0, columnspan=2, sticky="e", pady=(14, 0))
        self.ok = tb.Button(btns, text="Unlock" if unlock_only else "Sign in", bootstyle="primary", width=10,
                            command=lambda: self.submit())
        self.ok.pack(side="right")
        tb.Button(btns, text="Cancel", bootstyle="secondary-outline", width=9, command=self.destroy).pack(side="right", padx=8)
        self.bind("<Return>", lambda _e: self.submit())
        self.ui = UiQueue(self)
        (self.pw_entry if email else self.email_entry).focus_set()
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _say(self, text: str, style: str = "secondary"):
        self.msg.config(text=text, bootstyle=style)

    def show_code_row(self, text: str):
        self.needs_code = True
        self.code_frame.grid(row=4, column=0, columnspan=2, sticky="w")
        self._say(text)
        self.code_entry.focus_set()

    def submit(self):
        email = self.email.get().strip(); pw = self.password.get()
        if not email or not pw:
            self._say("Email and master password are required.", "danger"); return
        method = bw.TWO_STEP_METHODS[self.method.get()] if self.needs_code else None
        code = self.code.get().strip() if self.needs_code else None
        if self.needs_code and not code:
            self._say("Enter the code.", "danger"); return
        self.ok.state(["disabled"]); self._say("Unlocking..." if self.unlock_only else "Signing in..."); self.update_idletasks()

        def work():
            try:
                sess = bw.unlock(pw) if self.unlock_only else bw.login(email, pw, method, code)
                self.ui(lambda: self._done(sess, email))
            except bw.TwoStepRequired as e:
                self.ui(lambda e=e: self._ask_code(e))
            except Exception as e:
                self.ui(lambda m=_errmsg(e): self._error(m))
        threading.Thread(target=work, daemon=True).start()

    def _ask_code(self, exc: "bw.TwoStepRequired"):
        self.ok.state(["!disabled"])
        if exc.multiple:
            self.show_code_row("Bitwarden needs a code. Pick your two-step method and enter the code "
                               "(for Email, press 'Email me a code' first).")
        else:
            self.show_code_row("Bitwarden needs a code: from your authenticator app, or the one it just "
                               "emailed you if your account uses email codes / new-device verification.")

    def send_email_code(self):
        email = self.email.get().strip(); pw = self.password.get()
        if not email or not pw:
            self._say("Email and master password are required.", "danger"); return
        self.send_btn.state(["disabled"]); self._say("Asking Bitwarden to email a code...")
        self.method.set("Email (code sent to you)")

        def work():
            try:
                bw.send_email_code(email, pw)
                self.ui(lambda: self._say("Code emailed. Enter it and press Sign in."))
            except Exception as e:
                self.ui(lambda m=_errmsg(e): self._say(m, "danger"))
            finally:
                self.ui(lambda: self.send_btn.state(["!disabled"]))
        threading.Thread(target=work, daemon=True).start()

    def _error(self, text: str):
        self.ok.state(["!disabled"]); self._say(text, "danger")

    def _done(self, session: str, email: str):
        self.result = session; self.email_used = email
        self.password.set(""); self.destroy()


class App(tb.Window):
    def __init__(self):
        _install_crash_logging()
        _set_app_user_model_id()          # must happen before the first window exists
        self.settings = load_settings()
        super().__init__(themename=THEMES.get(self.settings.get("theme", "light"), THEMES["light"]))
        self.report_callback_exception = lambda t, v, tb_: log_exception("tk callback", v)
        self.title(f"Tenant Launcher {APP_VERSION}")
        _apply_icon(self)
        self.geometry("760x720"); self.minsize(600, 520)
        self.session: str | None = None
        self.user_email: str = ""
        self.items: dict[str, dict] = {}      # customer Login items by id, cached after sign-in
        self.customers: list[Customer] = []
        self.update_info: "updater.Update | None" = None
        self._build()
        self.ui = UiQueue(self)
        self.reload()
        self.after(150, self.ensure_unlocked)
        threading.Thread(target=self._bg_update_check, daemon=True).start()

    # ---------- UI ----------
    def _build(self):
        self.style.configure("Treeview", rowheight=30, font=("Segoe UI", 10))
        self.style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

        # header
        hdr = tb.Frame(self, padding=(18, 14, 18, 8)); hdr.pack(fill="x")
        try:
            self._logo = tk.PhotoImage(file=str(resource_path("assets/icon-256.png"))).subsample(6, 6)
            tb.Label(hdr, image=self._logo).pack(side="left", padx=(0, 12))
        except Exception:
            pass
        titles = tb.Frame(hdr); titles.pack(side="left")
        tb.Label(titles, text="Tenant Launcher", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        self.bw_label = tb.Label(titles, text="Not signed in to Bitwarden", bootstyle="secondary")
        self.bw_label.pack(anchor="w")
        right = tb.Frame(hdr); right.pack(side="right")
        self.update_btn = tb.Button(right, text="Update available", bootstyle="success", command=self.do_update)
        self.theme_btn = tb.Button(right, text="☾  Dark" if self.settings.get("theme", "light") == "light" else "☀  Light",
                                   bootstyle="secondary-outline", command=self.toggle_theme)
        self.theme_btn.pack(side="right", padx=(8, 0))
        tb.Button(right, text="Sign out", bootstyle="secondary-outline", command=self.sign_out).pack(side="right")

        # search
        srow = tb.Frame(self, padding=(18, 4, 18, 6)); srow.pack(fill="x")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh_list())
        tb.Label(srow, text="\U0001F50D").pack(side="left", padx=(0, 6))
        e = tb.Entry(srow, textvariable=self.search_var, font=("Segoe UI", 11)); e.pack(side="left", fill="x", expand=True)
        e.focus_set()
        tb.Label(srow, text="  double-click a customer to open the M365 admin centre", bootstyle="secondary").pack(side="left")

        # bottom bands are packed first (side=bottom) so the list gets the leftover space
        sbar = tb.Frame(self, padding=(18, 4, 18, 8)); sbar.pack(side="bottom", fill="x")
        bottom = tb.Frame(self, padding=(18, 2, 18, 4)); bottom.pack(side="bottom", fill="x")
        tools = tb.Frame(self, padding=(18, 4, 18, 4)); tools.pack(side="bottom", fill="x")
        portals = tb.Labelframe(self, text="Open in", padding=(10, 6)); portals.pack(side="bottom", fill="x", padx=18, pady=(10, 4))

        # list
        mid = tb.Frame(self, padding=(18, 0, 18, 0)); mid.pack(fill="both", expand=True)
        self.tree = tb.Treeview(mid, columns=("item", "notes"), show="headings", selectmode="browse", bootstyle="primary")
        self.tree.heading("item", text="Customer"); self.tree.heading("notes", text="Notes")
        self.tree.column("item", width=320); self.tree.column("notes", width=260)
        sb = tb.Scrollbar(mid, orient="vertical", command=self.tree.yview, bootstyle="round")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self.launch("admin"))
        self.tree.bind("<Return>", lambda _e: self.launch("admin"))

        # portals
        for i, (key, (label, _url)) in enumerate(PORTALS.items()):
            style = "primary" if key == "admin" else "primary-outline"
            tb.Button(portals, text=label, bootstyle=style, command=lambda k=key: self.launch(k)) \
                .grid(row=i // 4, column=i % 4, sticky="ew", padx=3, pady=3)
        for c in range(4):
            portals.columnconfigure(c, weight=1)

        # auto-add + tools
        tb.Label(tools, text="Auto-add vault items starting with").pack(side="left")
        self.prefix_var = tk.StringVar(value=self.settings.get("item_prefix", DEFAULT_ITEM_PREFIX))
        pe = tb.Entry(tools, textvariable=self.prefix_var, width=12); pe.pack(side="left", padx=6)
        pe.bind("<Return>", lambda _e: self.auto_add())
        tb.Button(tools, text="Scan now", bootstyle="info-outline", command=self.auto_add).pack(side="left")
        tb.Button(tools, text="Check for updates", bootstyle="secondary-link", command=lambda: self.check_updates(manual=True)).pack(side="right")
        tb.Button(tools, text="Sync vault", bootstyle="secondary-link", command=self.sync_vault).pack(side="right")

        tb.Button(bottom, text="Add from Bitwarden", bootstyle="secondary-outline", command=self.add_from_bitwarden).pack(side="left")
        tb.Button(bottom, text="Remove", bootstyle="danger-outline", command=self.remove_selected).pack(side="left", padx=6)
        tb.Button(bottom, text="Edit list", bootstyle="secondary-outline", command=self.edit_csv).pack(side="left")
        tb.Button(bottom, text="Reload", bootstyle="secondary-outline", command=self.reload).pack(side="left", padx=6)

        # status bar
        self.status = tk.StringVar(value="Select a customer, then double-click or pick a portal.")
        tb.Label(sbar, textvariable=self.status, bootstyle="secondary", anchor="w").pack(side="left", fill="x", expand=True)
        self.progress = tb.Progressbar(sbar, bootstyle="success-striped", length=160, mode="determinate")

    def set_status(self, text: str):
        self.status.set(text); self.update_idletasks()

    def toggle_theme(self):
        cur = self.settings.get("theme", "light")
        new = "dark" if cur == "light" else "light"
        self.style.theme_use(THEMES[new])
        self.settings["theme"] = new; save_settings(self.settings)
        self.theme_btn.config(text="☾  Dark" if new == "light" else "☀  Light")
        self.style.configure("Treeview", rowheight=30, font=("Segoe UI", 10))

    # ---------- data ----------
    def reload(self):
        if not CSV_PATH.exists():
            save_customers([])
        self.customers = load_customers()
        self.refresh_list()

    def refresh_list(self):
        q = self.search_var.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        for c in sorted(self.customers, key=lambda c: c.name.lower()):
            if q and q not in c.name.lower() and q not in c.notes.lower():
                continue
            tag = "" if c.bitwarden_item else "noitem"
            self.tree.insert("", "end", iid=c.name, values=(c.name, c.notes or ("(no Bitwarden item)" if not c.bitwarden_item else "")), tags=(tag,))
        self.tree.tag_configure("noitem", foreground="#999")
        kids = self.tree.get_children()
        if kids:
            self.tree.selection_set(kids[0]); self.tree.focus(kids[0])

    def selected(self) -> Customer | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return next((c for c in self.customers if c.name == sel[0]), None)

    def edit_csv(self):
        if not CSV_PATH.exists():
            save_customers(self.customers)
        os.startfile(CSV_PATH)

    # ---------- bitwarden ----------
    def ensure_unlocked(self) -> bool:
        if self.session:
            return True
        try:
            st = bw.status()
        except bw.BwError as e:
            messagebox.showerror("Bitwarden", str(e), parent=self); return False
        if st.get("status") == "unauthenticated":
            dlg = SignInDialog(self, email=self.settings.get("bw_email", ""))
        else:
            # Already logged in (vault locked) - just need the master password for this account.
            dlg = SignInDialog(self, email=st.get("userEmail") or self.settings.get("bw_email", ""),
                               email_locked=True, unlock_only=True)
        self.wait_window(dlg)
        if not dlg.result:
            return False
        self.session = dlg.result; self.user_email = dlg.email_used
        self.settings["bw_email"] = self.user_email; save_settings(self.settings)
        self.bw_label.config(text=f"Signed in as {self.user_email}", bootstyle="success")
        threading.Thread(target=self._bg_load, daemon=True).start()
        return True

    def sign_out(self):
        if not messagebox.askyesno("Sign out", "Sign out of Bitwarden on this PC?\n(Use this to switch to another team member's account.)", parent=self):
            return
        bw.logout()
        self.session = None; self.items = {}; self.user_email = ""
        self.bw_label.config(text="Not signed in to Bitwarden", bootstyle="secondary")
        self.after(100, self.ensure_unlocked)

    def _bg_load(self):
        """Cache the customer items once (so launches need no bw calls), auto-add, then sync and re-cache."""
        self._load_items("Vault cached - launches are instant.")
        try:
            bw.sync(self.session)
            self._load_items("Vault synced.")
        except Exception:
            pass

    def _load_items(self, done_msg: str):
        try:
            items = bw.list_login_items(self.session)
        except Exception as e:
            self.ui(lambda m=_errmsg(e): self.set_status(f"Vault cache failed, launches will ask bw directly ({m})"))
            return
        # Keep only what the launcher can use (customer items by prefix, plus anything already in the
        # list) so the rest of the vault is never held in this process's memory.
        prefix = (self.settings.get("item_prefix", DEFAULT_ITEM_PREFIX) or "").lower()
        linked = {c.bitwarden_item for c in self.customers}
        self.items = {k: it for k, it in items.items()
                      if (prefix and (it.get("name") or "").lower().startswith(prefix))
                      or k in linked or it.get("name") in linked}
        del items
        self.ui(lambda: (self.set_status(done_msg), self.auto_add(quiet=True)))

    def cached_item(self, c: Customer) -> dict | None:
        if c.bitwarden_item in self.items:
            return self.items[c.bitwarden_item]
        byname = [it for it in self.items.values() if it.get("name") == c.bitwarden_item]
        return byname[0] if len(byname) == 1 else None

    def sync_vault(self):
        if not self.ensure_unlocked():
            return
        self.set_status("Syncing vault...")
        def run():
            try:
                bw.sync(self.session); self._load_items("Vault synced.")
            except Exception as e:
                self.ui(lambda m=_errmsg(e): self.set_status(f"Sync failed: {m}"))
        threading.Thread(target=run, daemon=True).start()

    # ---------- customers ----------
    def _add_items(self, its: list[dict]) -> int:
        known_ids = {c.bitwarden_item for c in self.customers}
        known_names = {c.name for c in self.customers}
        added = 0
        for it in its:
            if it["id"] in known_ids or it.get("name") in known_ids:
                continue
            name = it.get("name") or it["id"]
            base, n = name, 2
            while name in known_names:
                name = f"{base} ({n})"; n += 1
            has_totp = bool((it.get("login") or {}).get("totp"))
            self.customers.append(Customer(name, it["id"], "" if has_totp else "no TOTP in Bitwarden - MFA will be manual"))
            known_ids.add(it["id"]); known_names.add(name); added += 1
        if added:
            save_customers(self.customers); self.refresh_list()
        return added

    def auto_add(self, quiet: bool = False):
        prefix = self.prefix_var.get().strip()
        if prefix != self.settings.get("item_prefix"):
            self.settings["item_prefix"] = prefix; save_settings(self.settings)
        if not self.items:
            if not quiet:
                self.set_status("Vault not loaded yet - try again in a moment.")
            return
        if not prefix:
            return
        matches = [it for it in self.items.values() if (it.get("name") or "").lower().startswith(prefix.lower())]
        added = self._add_items(matches)
        if added or not quiet:
            self.set_status(f"Auto-add: {added} new customer(s) from {len(matches)} '{prefix}' items.")

    def remove_selected(self):
        c = self.selected()
        if not c:
            return
        if messagebox.askyesno("Remove", f"Remove '{c.name}' from the list?\n(The Bitwarden item and the Chrome profile are left alone. "
                               "It will come back on the next auto-add scan if its name still matches the prefix.)", parent=self):
            self.customers = [x for x in self.customers if x.name != c.name]
            save_customers(self.customers); self.refresh_list()

    def add_from_bitwarden(self):
        if not self.ensure_unlocked():
            return
        dlg = tb.Toplevel(self); dlg.title("Add from Bitwarden"); dlg.geometry("640x460"); dlg.transient(self); dlg.grab_set()
        _apply_icon(dlg)
        f = tb.Frame(dlg, padding=14); f.pack(fill="both", expand=True)
        tb.Label(f, text="Search vault (name / username / URL)").pack(anchor="w")
        qv = tk.StringVar(value=self.prefix_var.get() or "microsoft")
        row = tb.Frame(f); row.pack(fill="x", pady=(4, 8))
        qe = tb.Entry(row, textvariable=qv); qe.pack(side="left", fill="x", expand=True)
        lst = tb.Treeview(f, columns=("name", "user"), show="headings", selectmode="extended", bootstyle="primary")
        lst.heading("name", text="Item"); lst.heading("user", text="Username")
        lst.column("name", width=320); lst.column("user", width=260)
        lst.pack(fill="both", expand=True)
        items: dict[str, dict] = {}

        def do_search(*_):
            lst.delete(*lst.get_children()); items.clear()
            q = qv.get().strip().lower()
            try:
                source = [it for it in bw.search_items(q, self.session) if it.get("login")]
            except bw.BwError as e:
                messagebox.showerror("Bitwarden", str(e), parent=dlg); return
            for it in sorted(source, key=lambda i: (i.get("name") or "").lower()):
                items[it["id"]] = it
                lst.insert("", "end", iid=it["id"], values=(it.get("name"), (it.get("login") or {}).get("username") or ""))
        tb.Button(row, text="Search", bootstyle="primary", command=do_search).pack(side="left", padx=(6, 0))
        tb.Button(row, text="Select all", bootstyle="secondary-outline", command=lambda: lst.selection_set(lst.get_children())).pack(side="left", padx=(6, 0))
        qe.bind("<Return>", do_search)

        def add_selected():
            chosen = [items[i] for i in lst.selection()]
            added = self._add_items(chosen)
            for it in chosen:                      # make them launchable without a re-sync
                self.items[it["id"]] = it
            self.set_status(f"Added {added} customer(s).")
            dlg.destroy()
        btns = tb.Frame(f); btns.pack(fill="x", pady=(10, 0))
        tb.Button(btns, text="Add selected", bootstyle="primary", command=add_selected).pack(side="right")
        tb.Button(btns, text="Cancel", bootstyle="secondary-outline", command=dlg.destroy).pack(side="right", padx=8)
        tb.Label(f, text="Linked by item ID, so renaming the item in Bitwarden later is fine.", bootstyle="secondary").pack(anchor="w", pady=(8, 0))
        dlg.after(100, do_search)

    # ---------- launch ----------
    def launch(self, portal: str):
        c = self.selected()
        if not c:
            self.set_status("Select a customer first."); return
        if not c.bitwarden_item:
            messagebox.showwarning("Tenant Launcher", f"'{c.name}' has no Bitwarden item linked.", parent=self)
            return
        if not self.ensure_unlocked():
            return
        env = dict(os.environ); env["BW_SESSION"] = self.session
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        item = self.cached_item(c)
        if FROZEN:
            cmd = [sys.executable, "--customer", c.name, "--portal", portal]
        else:
            cmd = [str(PYTHONW), str(WORKER), "--customer", c.name, "--portal", portal]
        # Cached creds go to the worker over a private stdin pipe (never argv/env); it falls back to bw if absent.
        proc = subprocess.Popen(cmd, env=env, cwd=str(ROOT), creationflags=flags, stdin=subprocess.PIPE)
        try:
            if item:
                slim = {"id": item["id"], "name": item.get("name"),
                        "login": {k: item["login"].get(k) for k in ("username", "password", "totp")}}
                proc.stdin.write(json.dumps(slim).encode("utf-8"))
        finally:
            proc.stdin.close()
        self.set_status(f"Opening {PORTALS[portal][0]} for {c.name}...")

    # ---------- updates ----------
    def _bg_update_check(self):
        try:
            upd = updater.check()
        except Exception:
            return                                    # quiet on startup; 'Check for updates' reports errors
        if upd:
            self.ui(lambda: self._show_update(upd))

    def _show_update(self, upd: "updater.Update"):
        self.update_info = upd
        self.update_btn.config(text=f"Update to {upd.version}")
        self.update_btn.pack(side="right", padx=(8, 0))
        self.set_status(f"Version {upd.version} is available.")

    def check_updates(self, manual: bool = False):
        self.set_status("Checking for updates...")
        def run():
            try:
                upd = updater.check()
            except Exception as e:
                self.ui(lambda m=_errmsg(e): self.set_status(m)); return
            if upd:
                self.ui(lambda: self._show_update(upd))
            else:
                self.ui(lambda: self.set_status(f"You're on the latest version ({APP_VERSION})."))
        threading.Thread(target=run, daemon=True).start()

    def do_update(self):
        upd = self.update_info
        if not upd:
            return
        if not FROZEN:
            messagebox.showinfo("Update", f"Version {upd.version} is on GitHub:\n{updater.RELEASES_PAGE}\n\n(Running from source - update with git pull.)", parent=self)
            return
        if not messagebox.askyesno("Update", f"Install Tenant Launcher {upd.version} now?\n\nThe app will close, update and reopen. "
                                   "Open browser windows are not affected.", parent=self):
            return
        self.update_btn.state(["disabled"])
        self.progress.pack(side="right"); self.progress["value"] = 0

        def prog(done, total):
            pct = int(done * 100 / total) if total else 0
            self.ui(lambda: (self.progress.config(value=pct), self.set_status(f"Downloading {upd.version}... {pct}%")))

        def run():
            try:
                path = updater.download(upd, prog)
            except Exception as e:
                self.ui(lambda m=_errmsg(e): (self.set_status(m), self.progress.pack_forget(), self.update_btn.state(["!disabled"])))
                return
            self.ui(lambda: self._run_installer(path))
        threading.Thread(target=run, daemon=True).start()

    def _run_installer(self, path: Path):
        self.set_status("Installing... the app will reopen in a moment.")
        updater.install_and_restart(path)
        self.after(400, self.destroy)


if __name__ == "__main__":
    App().mainloop()
