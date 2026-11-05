"""Tenant Launcher - click a customer, land signed-in in their Microsoft admin portal.

Credentials come from Bitwarden (bundled bw CLI) at click time; nothing is stored here.
Each customer gets its own isolated Chrome profile so several tenants can be open at once.
Customers are auto-discovered from Bitwarden items whose name starts with a prefix ("O365 -").
"""
import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import bw
from common import (APP_VERSION, CSV_PATH, DEFAULT_ITEM_PREFIX, FROZEN, PORTALS, ROOT, Customer,
                    load_customers, load_settings, resource_path, save_customers, save_settings)

PYTHONW = Path(sys.executable).with_name("pythonw.exe")
if not PYTHONW.exists():
    PYTHONW = Path(sys.executable)
WORKER = ROOT / "login_worker.py"


class SignInDialog(tk.Toplevel):
    """Bitwarden sign-in: email + master password (+ two-step code when the account needs one)."""

    def __init__(self, parent, email: str = "", email_locked: bool = False):
        super().__init__(parent)
        self.title("Sign in to Bitwarden"); self.resizable(False, False); self.transient(parent); self.grab_set()
        self.result: str | None = None          # session key
        self.email_used: str = ""
        self.needs_code = False
        f = ttk.Frame(self, padding=14); f.pack(fill="both", expand=True)
        ttk.Label(f, text="Use your own Bitwarden account (the one with access to the customer logins).").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(f, text="Email:").grid(row=1, column=0, sticky="e", padx=(0, 6), pady=3)
        self.email = tk.StringVar(value=email)
        self.email_entry = ttk.Entry(f, textvariable=self.email, width=38); self.email_entry.grid(row=1, column=1, sticky="ew", pady=3)
        if email_locked:
            self.email_entry.state(["disabled"])
        ttk.Label(f, text="Master password:").grid(row=2, column=0, sticky="e", padx=(0, 6), pady=3)
        self.password = tk.StringVar()
        self.pw_entry = ttk.Entry(f, textvariable=self.password, show="*", width=38); self.pw_entry.grid(row=2, column=1, sticky="ew", pady=3)
        # two-step row (hidden until needed)
        self.code_frame = ttk.Frame(f)
        ttk.Label(self.code_frame, text="Two-step code:").grid(row=0, column=0, sticky="e", padx=(0, 6), pady=3)
        self.code = tk.StringVar()
        self.code_entry = ttk.Entry(self.code_frame, textvariable=self.code, width=12); self.code_entry.grid(row=0, column=1, sticky="w", pady=3)
        self.method = tk.StringVar(value="Authenticator app")
        ttk.Combobox(self.code_frame, textvariable=self.method, values=list(bw.TWO_STEP_METHODS), state="readonly", width=22).grid(row=0, column=2, padx=(8, 0))
        self.send_btn = ttk.Button(self.code_frame, text="Email me a code", command=self.send_email_code)
        self.send_btn.grid(row=1, column=1, columnspan=2, sticky="w", pady=(2, 0))
        self.msg = ttk.Label(f, text="", foreground="#b00", wraplength=340, justify="left")
        self.msg.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        btns = ttk.Frame(f); btns.grid(row=5, column=0, columnspan=2, sticky="e", pady=(10, 0))
        self.ok = ttk.Button(btns, text="Sign in", command=lambda: self.submit()); self.ok.pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=6)
        self.bind("<Return>", lambda _e: self.submit())
        (self.pw_entry if email else self.email_entry).focus_set()
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def show_code_row(self, text: str):
        self.needs_code = True
        self.code_frame.grid(row=3, column=0, columnspan=2, sticky="w")
        self.msg.config(text=text, foreground="#333")
        self.code_entry.focus_set()

    def submit(self):
        email = self.email.get().strip(); pw = self.password.get()
        if not email or not pw:
            self.msg.config(text="Email and master password are required.", foreground="#b00"); return
        self.ok.state(["disabled"]); self.msg.config(text="Signing in...", foreground="#333"); self.update_idletasks()
        method = bw.TWO_STEP_METHODS[self.method.get()] if self.needs_code else None
        code = self.code.get().strip() if self.needs_code else None

        if self.needs_code and not code:
            self.ok.state(["!disabled"]); self.msg.config(text="Enter the code.", foreground="#b00"); return

        def work():
            try:
                sess = bw.login(email, pw, method, code)
                self.after(0, lambda: self._done(sess, email))
            except bw.TwoStepRequired as e:
                self.after(0, lambda: self._ask_code(e))
            except bw.BwError as e:
                self.after(0, lambda: self._error(str(e)))
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
            self.msg.config(text="Email and master password are required.", foreground="#b00"); return
        self.send_btn.state(["disabled"]); self.msg.config(text="Asking Bitwarden to email a code...", foreground="#333")
        self.method.set("Email (code sent to you)")

        def work():
            try:
                bw.send_email_code(email, pw)
                self.after(0, lambda: self.msg.config(text="Code emailed. Enter it and press Sign in.", foreground="#333"))
            except bw.BwError as e:
                self.after(0, lambda: self.msg.config(text=str(e), foreground="#b00"))
            finally:
                self.after(0, lambda: self.send_btn.state(["!disabled"]))
        threading.Thread(target=work, daemon=True).start()

    def _error(self, text: str):
        self.ok.state(["!disabled"]); self.msg.config(text=text, foreground="#b00")

    def _done(self, session: str, email: str):
        self.result = session; self.email_used = email
        self.password.set(""); self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Tenant Launcher {APP_VERSION}")
        try:
            self.iconbitmap(default=str(resource_path("assets/icon.ico")))
        except Exception:
            pass
        self.geometry("600x560")
        self.minsize(480, 400)
        self.session: str | None = None
        self.user_email: str = ""
        self.items: dict[str, dict] = {}      # vault Login items by id, cached after sign-in
        self.customers: list[Customer] = []
        self.settings = load_settings()
        self._build()
        self.reload()
        self.after(150, self.ensure_unlocked)

    # ---------- UI ----------
    def _build(self):
        top = ttk.Frame(self, padding=(10, 10, 10, 4)); top.pack(fill="x")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh_list())
        ttk.Label(top, text="Search:").pack(side="left")
        e = ttk.Entry(top, textvariable=self.search_var); e.pack(side="left", fill="x", expand=True, padx=(4, 8))
        e.focus_set()
        self.bw_label = ttk.Label(top, text="Bitwarden: signed out", foreground="#b00")
        self.bw_label.pack(side="right")

        mid = ttk.Frame(self, padding=(10, 0)); mid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(mid, columns=("item", "notes"), show="headings", selectmode="browse")
        self.tree.heading("item", text="Customer"); self.tree.heading("notes", text="Notes")
        self.tree.column("item", width=280); self.tree.column("notes", width=240)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self.launch("admin"))
        self.tree.bind("<Return>", lambda _e: self.launch("admin"))

        portals = ttk.LabelFrame(self, text="Open in", padding=6); portals.pack(fill="x", padx=10, pady=(6, 2))
        for i, (key, (label, _url)) in enumerate(PORTALS.items()):
            b = ttk.Button(portals, text=label, command=lambda k=key: self.launch(k))
            b.grid(row=i // 4, column=i % 4, sticky="ew", padx=2, pady=2)
        for c in range(4):
            portals.columnconfigure(c, weight=1)

        auto = ttk.Frame(self, padding=(10, 4, 10, 0)); auto.pack(fill="x")
        ttk.Label(auto, text="Auto-add Bitwarden items starting with:").pack(side="left")
        self.prefix_var = tk.StringVar(value=self.settings.get("item_prefix", DEFAULT_ITEM_PREFIX))
        pe = ttk.Entry(auto, textvariable=self.prefix_var, width=14); pe.pack(side="left", padx=4)
        pe.bind("<Return>", lambda _e: self.auto_add())
        ttk.Button(auto, text="Scan now", command=self.auto_add).pack(side="left")

        bottom = ttk.Frame(self, padding=(10, 4, 10, 10)); bottom.pack(fill="x")
        ttk.Button(bottom, text="Add from Bitwarden", command=self.add_from_bitwarden).pack(side="left")
        ttk.Button(bottom, text="Remove", command=self.remove_selected).pack(side="left", padx=4)
        ttk.Button(bottom, text="Edit CSV", command=self.edit_csv).pack(side="left")
        ttk.Button(bottom, text="Reload", command=self.reload).pack(side="left", padx=4)
        ttk.Button(bottom, text="Sign out", command=self.sign_out).pack(side="right")
        ttk.Button(bottom, text="Sync vault", command=self.sync_vault).pack(side="right", padx=4)

        self.status = tk.StringVar(value="Double-click a customer (or select + pick a portal).")
        ttk.Label(self, textvariable=self.status, anchor="w", padding=(10, 0, 10, 6)).pack(fill="x")

    def set_status(self, text: str):
        self.status.set(text); self.update_idletasks()

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
        self.tree.tag_configure("noitem", foreground="#888")
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
            self.wait_window(dlg)
            if not dlg.result:
                return False
            self.session = dlg.result; self.user_email = dlg.email_used
        else:
            # Already logged in (vault locked) - just need the master password for this account.
            self.user_email = st.get("userEmail") or self.settings.get("bw_email", "")
            dlg = SignInDialog(self, email=self.user_email, email_locked=True)
            dlg.title("Unlock Bitwarden")
            dlg.ok.config(text="Unlock")
            dlg.submit = lambda: self._unlock_via(dlg)
            self.wait_window(dlg)
            if not dlg.result:
                return False
            self.session = dlg.result
        self.settings["bw_email"] = self.user_email; save_settings(self.settings)
        self.bw_label.config(text=f"Bitwarden: {self.user_email}", foreground="#070")
        threading.Thread(target=self._bg_load, daemon=True).start()
        return True

    def _unlock_via(self, dlg: SignInDialog):
        pw = dlg.password.get()
        if not pw:
            dlg.msg.config(text="Master password is required.", foreground="#b00"); return
        dlg.ok.state(["disabled"]); dlg.msg.config(text="Unlocking...", foreground="#333"); dlg.update_idletasks()

        def work():
            try:
                sess = bw.unlock(pw)
                dlg.after(0, lambda: dlg._done(sess, dlg.email.get().strip()))
            except bw.BwError as e:
                dlg.after(0, lambda: dlg._error(str(e)))
        threading.Thread(target=work, daemon=True).start()

    def sign_out(self):
        if not messagebox.askyesno("Sign out", "Sign out of Bitwarden on this PC?\n(Use this to switch to another team member's account.)", parent=self):
            return
        bw.logout()
        self.session = None; self.items = {}; self.user_email = ""
        self.bw_label.config(text="Bitwarden: signed out", foreground="#b00")
        self.after(100, self.ensure_unlocked)

    def _bg_load(self):
        """Cache the vault once (so launches need no bw calls), auto-add customers, then sync and re-cache."""
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
            self.after(0, lambda: self.set_status(f"Vault cache failed, launches will ask bw directly ({e})"))
            return
        self.items = items
        self.after(0, lambda: (self.set_status(done_msg), self.auto_add(quiet=True)))

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
            except bw.BwError as e:
                self.after(0, lambda: self.set_status(f"Sync failed: {e}"))
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
        dlg = tk.Toplevel(self); dlg.title("Add from Bitwarden"); dlg.geometry("620x440"); dlg.transient(self); dlg.grab_set()
        f = ttk.Frame(dlg, padding=10); f.pack(fill="both", expand=True)
        ttk.Label(f, text="Search vault (name / username / URL):").pack(anchor="w")
        qv = tk.StringVar(value=self.prefix_var.get() or "microsoft")
        row = ttk.Frame(f); row.pack(fill="x", pady=(2, 6))
        qe = ttk.Entry(row, textvariable=qv); qe.pack(side="left", fill="x", expand=True)
        lst = ttk.Treeview(f, columns=("name", "user"), show="headings", selectmode="extended")
        lst.heading("name", text="Item"); lst.heading("user", text="Username")
        lst.column("name", width=320); lst.column("user", width=260)
        lst.pack(fill="both", expand=True)
        items: dict[str, dict] = {}

        def do_search(*_):
            lst.delete(*lst.get_children()); items.clear()
            q = qv.get().strip().lower()
            source = list(self.items.values())
            if not source:
                try:
                    source = [it for it in bw.search_items(q, self.session) if it.get("login")]
                except bw.BwError as e:
                    messagebox.showerror("Bitwarden", str(e), parent=dlg); return
            for it in sorted(source, key=lambda i: (i.get("name") or "").lower()):
                hay = " ".join([it.get("name") or "", (it.get("login") or {}).get("username") or "",
                                " ".join(u.get("uri") or "" for u in (it.get("login") or {}).get("uris") or [])]).lower()
                if q and q not in hay:
                    continue
                items[it["id"]] = it
                lst.insert("", "end", iid=it["id"], values=(it.get("name"), (it.get("login") or {}).get("username") or ""))
        ttk.Button(row, text="Search", command=do_search).pack(side="left", padx=(4, 0))
        ttk.Button(row, text="Select all", command=lambda: lst.selection_set(lst.get_children())).pack(side="left", padx=(4, 0))
        qe.bind("<Return>", do_search)

        def add_selected():
            added = self._add_items([items[i] for i in lst.selection()])
            self.set_status(f"Added {added} customer(s).")
            dlg.destroy()
        btns = ttk.Frame(f); btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="Add selected", command=add_selected).pack(side="right")
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right", padx=4)
        ttk.Label(f, text="Linked by item ID, so renaming the item in Bitwarden later is fine.", foreground="#666").pack(anchor="w", pady=(6, 0))
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


if __name__ == "__main__":
    App().mainloop()
