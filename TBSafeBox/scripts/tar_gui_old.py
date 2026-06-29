
# -*- coding: utf-8 -*-
# scripts/tar_gui.py

import os

import json

import threading

import datetime

import socket

import tkinter as tk

from tkinter import ttk, messagebox, filedialog



# --- Settings manager (defaults + user overrides) ---

class SettingsManager:

    def __init__(self, defaults_path: str, user_path: str, log_cb=None):

        self.defaults_path = defaults_path

        self.user_path = user_path

        self.log_cb = log_cb

        self.defaults = {}

        self.user = {}

        self.settings = {}

    def log(self, s):

        if self.log_cb:

            self.log_cb(f"[settings] {s}")

    def _ensure_user_file(self):

        os.makedirs(os.path.dirname(self.user_path), exist_ok=True)

        if not os.path.exists(self.user_path):

            with open(self.user_path, "w", encoding="utf-8") as f:

                json.dump({"ui": {"selected_tab": "Backup"}}, f, indent=2)

    def load(self):

        try:

            with open(self.defaults_path, "r", encoding="utf-8") as f:

                self.defaults = json.load(f)

        except Exception:

            self.defaults = {}

        self._ensure_user_file()

        try:

            with open(self.user_path, "r", encoding="utf-8") as f:

                self.user = json.load(f)

        except Exception:

            self.user = {}

        self.settings = self.defaults.copy()

        for k, v in self.user.items():

            if isinstance(v, dict) and k in self.settings and isinstance(self.settings[k], dict):

                merged = self.settings[k].copy()

                merged.update(v)

                self.settings[k] = merged

            else:

                self.settings[k] = v

        self.log("loaded")

    def save(self, obj: dict = None):

        if obj is None:

            obj = self.settings

        os.makedirs(os.path.dirname(self.user_path), exist_ok=True)

        with open(self.user_path, "w", encoding="utf-8") as f:

            json.dump(obj, f, indent=2, ensure_ascii=False)

        self.log("saved")



# --- Core ops import ---

from scripts.tar_ops import (

    build_backup_cmd, run_backup, build_restore_cmd, run_restore, verify_sha256,

    run_backup_ssh, run_restore_ssh, verify_sha256_ssh,

    copy_specific_paths_to_windows, health_check_remote, manage_remote_retention,

    manage_local_retention, restore_latest_from_windows,

    list_local_archives, list_remote_archives_for_source, restore_specific_local_to_remote,

    default_remote_dest_dir, find_latest_remote_archive_auto_by_source,

    run_backup_pipeline_to_windows

)



try:

    from scripts.key_wizard import KeyWizard

except Exception:

    KeyWizard = None



# --- ScrollableFrame ---

class ScrollableFrame(ttk.Frame):

    def __init__(self, master, **kwargs):

        super().__init__(master, **kwargs)

        self.columnconfigure(0, weight=1)

        self.rowconfigure(0, weight=1)

        self._canvas = tk.Canvas(self, highlightthickness=0)

        self._canvas.grid(row=0, column=0, sticky="nsew")

        self._vsb = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)

        self._vsb.grid(row=0, column=1, sticky="ns")

        self._canvas.configure(yscrollcommand=self._vsb.set)

        self.content = ttk.Frame(self._canvas)

        self.content.columnconfigure(0, weight=1)

        self._win = self._canvas.create_window((0, 0), window=self.content, anchor="nw")



        def _resize_inner(_):

            self._canvas.itemconfigure(self._win, width=self._canvas.winfo_width())

        self.bind("<Configure>", _resize_inner)



        def _update_region(_):

            self._canvas.configure(scrollregion=self._canvas.bbox("all"))

        self.content.bind("<Configure>", _update_region)



        # 初期表示でも幅/スクロール領域を一度更新（Restoreタブが空白になる問題の回避）

        self.after(0, lambda: (

            self._canvas.itemconfigure(self._win, width=self._canvas.winfo_width()),

            self._canvas.configure(scrollregion=self._canvas.bbox("all"))

        ))



class TarTab(ttk.Frame):

    def __init__(self, master, config_path: str, **kwargs):

        super().__init__(master, **kwargs)

        self.config_path = config_path

        defaults_path = os.path.join(os.path.dirname(config_path), "tar_defaults.json")

        user_path = os.path.join(os.path.dirname(config_path), "user_settings.json")

        self.settings_mgr = SettingsManager(defaults_path, user_path, log_cb=lambda s: print(s))

        self.settings_mgr.load()

        self.cfg = self.settings_mgr.settings



        self.last_archive = None

        self.last_sha = None

        self.last_list = None

        self.sel_source = None

        self.sel_path = None



        self._build_ui()

        self._apply_settings_to_ui()

        self._wire_auto_save()



    def _setup_frame_grid(self, frame: ttk.LabelFrame, stretch_col: int = 1, extra_cols: int = 5):

        for c in range(extra_cols):

            frame.columnconfigure(c, weight=(1 if c == stretch_col else 0))



    def _build_ui(self):

        self.columnconfigure(0, weight=1)

        self.rowconfigure(5, weight=1)



        # Execution

        frm_exec = ttk.LabelFrame(self, text="Execution")

        frm_exec.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 6))

        self._setup_frame_grid(frm_exec, stretch_col=1)

        self.exec_mode = tk.StringVar(value=("SSH" if self.cfg.get("ssh", {}).get("enabled", True) else "Local"))

        ttk.Radiobutton(frm_exec, text="Local", value="Local", variable=self.exec_mode,

                        command=self._update_verify_labels).grid(row=0, column=0, sticky="w", padx=4, pady=2)

        ttk.Radiobutton(frm_exec, text="SSH", value="SSH", variable=self.exec_mode,

                        command=self._update_verify_labels).grid(row=0, column=1, sticky="w", padx=4, pady=2)



        # SSH Settings

        frm_ssh = ttk.LabelFrame(self, text="SSH Settings")

        frm_ssh.grid(row=1, column=0, sticky="ew", padx=8, pady=6)

        self._setup_frame_grid(frm_ssh, stretch_col=1, extra_cols=5)

        ssh = self.cfg.get("ssh", {})

        ttk.Label(frm_ssh, text="Host").grid(row=0, column=0, sticky="w", padx=4, pady=2)

        self.ent_host = ttk.Entry(frm_ssh); self.ent_host.insert(0, ssh.get("host", "")); self.ent_host.grid(row=0, column=1, sticky="ew", padx=4, pady=2)

        ttk.Label(frm_ssh, text="Port").grid(row=0, column=2, sticky="w", padx=4, pady=2)

        self.ent_port = ttk.Entry(frm_ssh, width=6); self.ent_port.insert(0, str(ssh.get("port", 22))); self.ent_port.grid(row=0, column=3, sticky="w", padx=4, pady=2)

        ttk.Label(frm_ssh, text="User").grid(row=1, column=0, sticky="w", padx=4, pady=2)

        self.ent_user = ttk.Entry(frm_ssh, width=20); self.ent_user.insert(0, ssh.get("user", "")); self.ent_user.grid(row=1, column=1, sticky="w", padx=4, pady=2)

        self.use_password = tk.BooleanVar(value=bool(ssh.get("use_password", False)))

        ttk.Checkbutton(frm_ssh, text="Use password", variable=self.use_password).grid(row=1, column=2, sticky="w", padx=4, pady=2)

        ttk.Label(frm_ssh, text="Password").grid(row=1, column=3, sticky="w", padx=4, pady=2)

        self.ent_password = ttk.Entry(frm_ssh, width=20, show="*"); self.ent_password.insert(0, ssh.get("password", "")); self.ent_password.grid(row=1, column=4, sticky="w", padx=4, pady=2)

        ttk.Label(frm_ssh, text="Keyfile").grid(row=2, column=0, sticky="w", padx=4, pady=2)

        self.ent_key = ttk.Entry(frm_ssh); self.ent_key.insert(0, ssh.get("keyfile", "")); self.ent_key.grid(row=2, column=1, columnspan=2, sticky="ew", padx=4, pady=2)

        ttk.Button(frm_ssh, text="Browse", command=lambda: self._browse(self.ent_key)).grid(row=2, column=3, sticky="w", padx=4, pady=2)

        ttk.Button(frm_ssh, text="Generate Key", command=self._open_key_wizard).grid(row=2, column=4, sticky="w", padx=4, pady=2)

        self.use_sudo = tk.BooleanVar(value=bool(ssh.get("use_sudo", False)))

        ttk.Checkbutton(frm_ssh, text="Use sudo (remote)", variable=self.use_sudo).grid(row=3, column=0, sticky="w", padx=4, pady=2)



        # Notebook (Backup / Restore)

        nb = ttk.Notebook(self)

        nb.grid(row=2, column=0, sticky="nsew", padx=8, pady=6)

        self.nb = nb

        self.rowconfigure(2, weight=1)



        # Backup tab

        self.sf_bk = ScrollableFrame(nb); nb.add(self.sf_bk, text="Backup")

        bk = self.sf_bk.content; bk.columnconfigure(0, weight=1)

        frm_paths_bk = ttk.LabelFrame(bk, text="Paths (Backup)")

        frm_paths_bk.grid(row=0, column=0, sticky="ew", padx=8, pady=6)

        self._setup_frame_grid(frm_paths_bk, stretch_col=1, extra_cols=2)

        ttk.Label(frm_paths_bk, text="Source dir:").grid(row=0, column=0, sticky="w", padx=4, pady=2)

        self.ent_src = ttk.Entry(frm_paths_bk); self.ent_src.insert(0, self.cfg.get("source_dir", "/var/spool/epms")); self.ent_src.grid(row=0, column=1, sticky="ew", padx=4, pady=2)



        frm_local_bk = ttk.LabelFrame(bk, text="Local save (Windows)")

        frm_local_bk.grid(row=1, column=0, sticky="ew", padx=8, pady=6)

        self._setup_frame_grid(frm_local_bk, stretch_col=1, extra_cols=3)

        ttk.Label(frm_local_bk, text="Folder:").grid(row=0, column=0, sticky="w", padx=4, pady=2)

        self.ent_local = ttk.Entry(frm_local_bk); self.ent_local.insert(0, self.cfg.get("local_save", "C:/Backup/epms")); self.ent_local.grid(row=0, column=1, sticky="ew", padx=4, pady=2)

        ttk.Button(frm_local_bk, text="Browse", command=lambda: self._browse_dir(self.ent_local)).grid(row=0, column=2, sticky="w", padx=4, pady=2)

        self.var_auto_copy = tk.BooleanVar(value=True)

        ttk.Checkbutton(frm_local_bk, text="Auto copy after backup", variable=self.var_auto_copy).grid(row=1, column=0, columnspan=3, sticky="w", padx=4, pady=2)



        # Transfer method (SFTP/SCP)

        frm_xfer = ttk.LabelFrame(bk, text="Transfer method (SSH)")

        frm_xfer.grid(row=5, column=0, sticky="ew", padx=8, pady=6)

        self._setup_frame_grid(frm_xfer, stretch_col=1, extra_cols=3)

        self.xfer_method = tk.StringVar(value=self.cfg.get("ssh", {}).get("transfer_method", "sftp").upper())

        ttk.Radiobutton(frm_xfer, text="SFTP (default)", value="SFTP", variable=self.xfer_method).grid(row=0, column=0, sticky="w", padx=4, pady=2)

        ttk.Radiobutton(frm_xfer, text="SCP (OpenSSH)", value="SCP", variable=self.xfer_method).grid(row=0, column=1, sticky="w", padx=4, pady=2)



        hc = self.cfg.get("health_check", {})

        frm_hc_bk = ttk.LabelFrame(bk, text="Health Check (remote)")

        frm_hc_bk.grid(row=2, column=0, sticky="ew", padx=8, pady=6)

        self._setup_frame_grid(frm_hc_bk, stretch_col=1, extra_cols=4)

        self.hc_enabled = tk.BooleanVar(value=bool(hc.get("enabled", True)))

        ttk.Checkbutton(frm_hc_bk, text="Enable health check", variable=self.hc_enabled).grid(row=0, column=0, sticky="w", padx=4, pady=2)

        self.hc_require_mount = tk.BooleanVar(value=bool(hc.get("require_mounted", False)))

        ttk.Checkbutton(frm_hc_bk, text="Require mount", variable=self.hc_require_mount).grid(row=0, column=1, sticky="w", padx=4, pady=2)

        ttk.Label(frm_hc_bk, text="Min free (GB)").grid(row=0, column=2, sticky="w", padx=4, pady=2)

        self.hc_minfree = ttk.Entry(frm_hc_bk, width=6); self.hc_minfree.insert(0, str(hc.get("min_free_gb", 5))); self.hc_minfree.grid(row=0, column=3, sticky="w", padx=4, pady=2)



        rt = self.cfg.get("retention", {})

        frm_rt_bk = ttk.LabelFrame(bk, text="Retention")

        frm_rt_bk.grid(row=3, column=0, sticky="ew", padx=8, pady=6)

        self._setup_frame_grid(frm_rt_bk, stretch_col=1, extra_cols=4)

        ttk.Label(frm_rt_bk, text="Remote keep N").grid(row=0, column=0, sticky="w", padx=4, pady=2)

        self.rt_r_keep_n = ttk.Entry(frm_rt_bk, width=6); self.rt_r_keep_n.insert(0, str(rt.get("remote_keep_n", 10))); self.rt_r_keep_n.grid(row=0, column=1, sticky="w", padx=4, pady=2)

        ttk.Label(frm_rt_bk, text="Remote keep days").grid(row=0, column=2, sticky="w", padx=4, pady=2)

        self.rt_r_keep_days = ttk.Entry(frm_rt_bk, width=6); self.rt_r_keep_days.insert(0, str(rt.get("remote_keep_days", 0))); self.rt_r_keep_days.grid(row=0, column=3, sticky="w", padx=4, pady=2)

        ttk.Label(frm_rt_bk, text="Local keep N").grid(row=1, column=0, sticky="w", padx=4, pady=2)

        self.rt_l_keep_n = ttk.Entry(frm_rt_bk, width=6); self.rt_l_keep_n.insert(0, str(rt.get("local_keep_n", 10))); self.rt_l_keep_n.grid(row=1, column=1, sticky="w", padx=4, pady=2)

        ttk.Label(frm_rt_bk, text="Local keep days").grid(row=1, column=2, sticky="w", padx=4, pady=2)

        self.rt_l_keep_days = ttk.Entry(frm_rt_bk, width=6); self.rt_l_keep_days.insert(0, str(rt.get("local_keep_days", 0))); self.rt_l_keep_days.grid(row=1, column=3, sticky="w", padx=4, pady=2)



        frm_opts_bk = ttk.LabelFrame(bk, text="Options")

        frm_opts_bk.grid(row=4, column=0, sticky="ew", padx=8, pady=6)

        self._setup_frame_grid(frm_opts_bk, stretch_col=1, extra_cols=4)

        ttk.Label(frm_opts_bk, text="Compression level (zstd)").grid(row=0, column=0, sticky="w", padx=4, pady=2)

        self.spn_lvl = tk.Spinbox(frm_opts_bk, from_=1, to=22, width=5); self.spn_lvl.delete(0, tk.END); self.spn_lvl.insert(0, int(self.cfg.get("compression_level", 6))); self.spn_lvl.grid(row=0, column=1, sticky="w", padx=4, pady=2)

        ttk.Label(frm_opts_bk, text="Threads (0=auto)").grid(row=0, column=2, sticky="w", padx=4, pady=2)

        self.spn_thr = tk.Spinbox(frm_opts_bk, from_=0, to=64, width=5); self.spn_thr.delete(0, tk.END); self.spn_thr.insert(0, int(self.cfg.get("threads", 0))); self.spn_thr.grid(row=0, column=3, sticky="w", padx=4, pady=2)

        self.var_onefs = tk.BooleanVar(value=bool(self.cfg.get("one_file_system", True)))

        ttk.Checkbutton(frm_opts_bk, text="--one-file-system", variable=self.var_onefs).grid(row=1, column=0, sticky="w", padx=4, pady=2)

        ttk.Label(frm_opts_bk, text="Excludes (comma)").grid(row=2, column=0, sticky="w", padx=4, pady=2)

        self.ent_excl = ttk.Entry(frm_opts_bk); self.ent_excl.insert(0, ",".join(self.cfg.get("excludes", []))); self.ent_excl.grid(row=2, column=1, columnspan=3, sticky="ew", padx=4, pady=2)



        # Restore tab

        self.sf_rs = ScrollableFrame(nb); nb.add(self.sf_rs, text="Restore")

        rs = self.sf_rs.content; rs.columnconfigure(0, weight=1)

        frm_paths_rs = ttk.LabelFrame(rs, text="Paths (Restore)")

        frm_paths_rs.grid(row=0, column=0, sticky="ew", padx=8, pady=6)

        self._setup_frame_grid(frm_paths_rs, stretch_col=1, extra_cols=1)

        ttk.Label(frm_paths_rs, text="Restore parent (remote):").grid(row=0, column=0, sticky="w", padx=4, pady=2)

        self.ent_restore_parent = ttk.Entry(frm_paths_rs); self.ent_restore_parent.insert(0, self.cfg.get("restore_target_parent", "/var/spool")); self.ent_restore_parent.grid(row=0, column=1, sticky="ew", padx=4, pady=2)



        frm_rwr = ttk.LabelFrame(rs, text="Restore (Windows→Remote)")

        frm_rwr.grid(row=1, column=0, sticky="ew", padx=8, pady=6)

        self._setup_frame_grid(frm_rwr, stretch_col=1, extra_cols=3)

        self.var_restore_from_local = tk.BooleanVar(value=True)

        ttk.Checkbutton(frm_rwr, text="From local latest (Windows) via SSH", variable=self.var_restore_from_local).grid(row=0, column=0, columnspan=3, sticky="w", padx=4, pady=2)

        self.var_verify_before = tk.BooleanVar(value=True)

        ttk.Checkbutton(frm_rwr, text="Verify before restore", variable=self.var_verify_before).grid(row=1, column=0, sticky="w", padx=4, pady=2)

        self.var_cleanup_remote = tk.BooleanVar(value=True)

        ttk.Checkbutton(frm_rwr, text="Cleanup remote tmp", variable=self.var_cleanup_remote).grid(row=1, column=1, sticky="w", padx=4, pady=2)

        ttk.Label(frm_rwr, text="Remote tmp dir (optional)").grid(row=2, column=0, sticky="w", padx=4, pady=2)

        self.ent_remote_tmp = ttk.Entry(frm_rwr); self.ent_remote_tmp.insert(0, ""); self.ent_remote_tmp.grid(row=2, column=1, sticky="ew", padx=4, pady=2)

        self.var_keep_old_files = tk.BooleanVar(value=False)

        ttk.Checkbutton(frm_rwr, text="Keep old files (do not overwrite)", variable=self.var_keep_old_files).grid(row=3, column=0, sticky="w", padx=4, pady=2)

        ttk.Button(frm_rwr, text="Select Generation...", command=self._open_generation_selector).grid(row=3, column=1, sticky="w", padx=4, pady=2)

        self.lbl_selected = ttk.Label(frm_rwr, text="Selected: (none)")

        self.lbl_selected.grid(row=4, column=0, columnspan=2, sticky="w", padx=4, pady=(2,4))



        # ActionBar

        self.frm_act = ttk.Frame(self)

        self.frm_act.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 6))

        self.frm_act.columnconfigure(0, weight=1)

        self.frm_act_backup = ttk.Frame(self.frm_act)

        self.frm_act_restore = ttk.Frame(self.frm_act)

        for f in (self.frm_act_backup, self.frm_act_restore):

            f.columnconfigure(0, weight=1)

        # Backup actions

        self.btn_backup = ttk.Button(self.frm_act_backup, text="Run Backup", command=self._do_backup)

        self.btn_verify_bk = ttk.Button(self.frm_act_backup, text="Verify SHA256 (local)", command=self._do_verify)

        self.btn_backup.grid(row=0, column=0, padx=4, pady=2, sticky="w")

        self.btn_verify_bk.grid(row=0, column=1, padx=4, pady=2, sticky="w")

        # Restore actions

        self.btn_restore = ttk.Button(self.frm_act_restore, text="Run Restore", command=self._do_restore)

        self.btn_verify_rs = ttk.Button(self.frm_act_restore, text="Verify SHA256 (remote)", command=self._do_verify)

        self.btn_restore.grid(row=0, column=0, padx=4, pady=2, sticky="w")

        self.btn_verify_rs.grid(row=0, column=1, padx=4, pady=2, sticky="w")



        # Progress

        self.pbar = ttk.Progressbar(self, mode="indeterminate")

        self.pbar.grid(row=4, column=0, sticky="ew", padx=8, pady=(0, 6))



        # Log

        frm_log = ttk.LabelFrame(self, text="Log")

        frm_log.grid(row=5, column=0, sticky="nsew", padx=8, pady=6)

        frm_log.rowconfigure(0, weight=1); frm_log.columnconfigure(0, weight=1)

        log_scroll = ttk.Scrollbar(frm_log, orient="vertical")

        log_scroll.grid(row=0, column=1, sticky="ns")

        self.txt = tk.Text(frm_log, height=12, width=100, yscrollcommand=log_scroll.set)

        self.txt.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

        log_scroll.config(command=self.txt.yview)



        # 初期表示 & タブ連動

        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)  # タブ切替で ActionBar 更新

        self.nb.select(0)  # デフォルトは Backup タブ

        self._on_tab_changed()  # 直ちに ActionBar を描画

        # 両タブの ScrollableFrame を一度リフレッシュ

        self._refresh_scrollable(self.sf_bk)

        self._refresh_scrollable(self.sf_rs)



    # --- helpers ---

    def _on_tab_changed(self, _evt=None):

        idx = self.nb.index(self.nb.select())

        self._show_action_bar("backup" if idx == 0 else "restore")

        self._refresh_scrollable(self.sf_bk if idx == 0 else self.sf_rs)



    def _show_action_bar(self, which: str):

        try:

            self.frm_act_backup.grid_forget()

            self.frm_act_restore.grid_forget()

        except Exception:

            pass

        if which == "backup":

            self.frm_act_backup.grid(row=0, column=0, sticky="w")

        else:

            self.frm_act_restore.grid(row=0, column=0, sticky="w")

        self._update_verify_labels()



    def _refresh_scrollable(self, sf: ScrollableFrame):

        try:

            sf.update_idletasks()

            w = max(sf._canvas.winfo_width(), self.nb.winfo_width())

            sf._canvas.itemconfigure(sf._win, width=w)

            sf._canvas.configure(scrollregion=sf._canvas.bbox("all"))

        except Exception:

            pass



    def _append_log(self, s: str):

        self.txt.insert("end", s + "\n"); self.txt.see("end")



    def _start_busy(self):

        self.pbar.start(10)

        for b in (self.btn_backup, self.btn_restore, self.btn_verify_bk, self.btn_verify_rs):

            b.config(state="disabled")



    def _stop_busy(self):

        self.pbar.stop()

        for b in (self.btn_backup, self.btn_restore, self.btn_verify_bk, self.btn_verify_rs):

            b.config(state="normal")



    def _browse(self, entry):

        path = filedialog.askopenfilename()

        if path:

            entry.delete(0, tk.END); entry.insert(0, path)



    def _browse_dir(self, entry):

        path = filedialog.askdirectory()

        if path:

            entry.delete(0, tk.END); entry.insert(0, path)



    def _ssh_cfg(self):

        return {

            "host": self.ent_host.get(),

            "port": int(self.ent_port.get() or 22),

            "user": self.ent_user.get(),

            "use_password": bool(self.use_password.get()),

            "password": self.ent_password.get(),

            "keyfile": self.ent_key.get(),

            "use_sudo": bool(self.use_sudo.get()),

            "known_hosts_policy": "auto",

            "transfer_method": self.xfer_method.get()  # "SFTP"/"SCP"

        }



    def _open_key_wizard(self):

        if KeyWizard is None:

            messagebox.showinfo("Key Wizard", "KeyWizard モジュールが見つかりません。配布パッケージに scripts/key_wizard.py を追加してください。")

            return

        win = tk.Toplevel(self); win.title("Key Generation Wizard")

        def on_created(path):

            self.ent_key.delete(0, tk.END); self.ent_key.insert(0, path)

        wiz = KeyWizard(win, on_key_created=on_created, ssh_cfg_getter=self._ssh_cfg)

        wiz.pack(fill="both", expand=True)



    def _resolve_latest_remote(self):

        src = self.ent_src.get()

        arch, sha, _ = find_latest_remote_archive_auto_by_source(src, self._ssh_cfg(), self._append_log)

        if sha is None:

            raise FileNotFoundError(f"No .sha256 found for {arch}")

        return arch, sha



    def _update_verify_labels(self):

        idx = self.nb.index(self.nb.select())

        mode = self.exec_mode.get().upper()

        if idx == 0:

            self.btn_verify_bk.config(text="Verify SHA256 (local)")

        else:

            self.btn_verify_rs.config(text=f"Verify SHA256 ({'remote' if mode=='SSH' else 'local'})")



 # --- generation selector ---

    def _open_generation_selector(self):

        win = tk.Toplevel(self)

        win.title("Select Archive Generation")

        win.resizable(True, True)

        win.transient(self.winfo_toplevel()); win.grab_set()  # モーダル（任意）

        nb = ttk.Notebook(win)

        nb.pack(side="top", fill="both", expand=True)



        # --- 高速SSH到達性チェック（3秒でフォールバック） ---

        def _quick_ssh_reachable(cfg: dict, timeout: float = 3.0) -> tuple[bool, str | None]:

            host = (cfg.get("host") or "").strip()

            port = int(cfg.get("port") or 22)

            if not host:

                return False, "empty host"

            try:

                # 到達性のみを確認（成功したら直ちにクローズ）

                with socket.create_connection((host, port), timeout=timeout):

                    return True, None

            except (socket.timeout, OSError) as e:

                return False, str(e)



        # 画面内センタリング＋最小サイズ（高DPI対応）

        def _smart_center(_win, parent=None, min_w=800, min_h=460, margin=24):

            try:

                scale = float(_win.tk.call('tk', 'scaling'))

            except Exception:

                scale = 1.0

            s_min_w = int(min_w * scale); s_min_h = int(min_h * scale)

            _win.update_idletasks()

            req_w = max(_win.winfo_reqwidth(), s_min_w)

            req_h = max(_win.winfo_reqheight(), s_min_h)

            sw = _win.winfo_screenwidth(); sh = _win.winfo_screenheight()

            w = min(req_w, sw - 2 * margin); h = min(req_h, sh - 2 * margin)

            if parent is not None:

                try: parent.update_idletasks()

                except Exception: pass

                px = parent.winfo_rootx(); py = parent.winfo_rooty()

                pw = parent.winfo_width(); ph = parent.winfo_height()

                if pw <= 1 or ph <= 1:

                    parent.update_idletasks(); pw = parent.winfo_width(); ph = parent.winfo_height()

                x = px + (pw - w) // 2; y = py + (ph - h) // 2

            else:

                x = (sw - w) // 2; y = (sh - h) // 2

            x = max(margin, min(x, sw - w - margin))

            y = max(margin, min(y, sh - h - margin))

            _win.geometry(f"{w}x{h}+{x}+{y}")

            _win.minsize(s_min_w, s_min_h)



        def build_tree(parent):

            cols = ("name","size","mtime","sha","lst","path")

            tree = ttk.Treeview(parent, columns=cols, show="headings", height=14)

            for c, t, w, anc in [

                ("name","Name",260,"w"),("size","Size (MB)",90,"e"),("mtime","Modified",160,"center"),

                ("sha","SHA",60,"center"),("lst","LIST",60,"center"),("path","Path",0,"w")

            ]:

                tree.heading(c, text=t); tree.column(c, width=w, anchor=anc)

            ysb = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)

            tree.configure(yscrollcommand=ysb.set)

            tree.pack(side="left", fill="both", expand=True); ysb.pack(side="right", fill="y")

            return tree



        # --- Local タブ ---

        frm_local = ttk.Frame(nb); nb.add(frm_local, text="Local")

        tree_local = build_tree(frm_local)

        try:

            local_dir = self.ent_local.get().strip()

            rows_local = list_local_archives(local_dir, self._append_log)

            if not rows_local:

                ttk.Label(

                    frm_local,

                    text=f"No local archives found in: {local_dir or '(empty)'}\n"

                         f"Expecting files like *.tar.zst"

                ).pack(fill="x", padx=8, pady=6)

            else:

                for r in rows_local:

                    mb = f"{(r.get('size') or 0)/1024/1024:.2f}"

                    mtime = datetime.datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M:%S")

                    tree_local.insert(

                        "", "end",

                        values=(r["name"], mb, mtime,

                                "Y" if r.get("sha_exists") else "-",

                                "Y" if r.get("list_exists") else "-",

                                r["path"])

                    )

                # 先頭を選択（任意）

                first = tree_local.get_children()

                if first:

                    tree_local.selection_set(first[0]); tree_local.focus(first[0])

        except Exception as e:

            messagebox.showerror("Local List Error", str(e))



        # --- Remote タブ ---

        frm_remote = ttk.Frame(nb); nb.add(frm_remote, text="Remote")

        tree_remote = build_tree(frm_remote)



        # Remote 一覧前のヘルスチェック（存在確保）※到達性NGならスキップ

        try:

            dest = default_remote_dest_dir(self.ent_src.get())

            _reachable, _err = _quick_ssh_reachable(self._ssh_cfg(), timeout=3.0)

            if _reachable:

                health_check_remote(dest, self._ssh_cfg(),

                                    require_mounted=False, min_free_gb=0, log_cb=self._append_log)

            else:

                # 事前にヒントを表示しておく（以降の列挙もスキップ）

                ttk.Label(

                    frm_remote,

                    text=f"No archives in {dest}\n(Network unreachable: {_err})"

                ).pack(fill="x", padx=8, pady=6)

                # Remoteタブが空でも操作できるよう、Localタブにフォーカス

                try: nb.select(0)

                except Exception: pass

                # Remote列挙はこの時点で中断

                rows_remote = None

                raise RuntimeError("skip remote listing due to unreachable")



        except Exception as e:

            self._append_log(f"[remote] health_check skipped: {e}")



        try:

            # 到達性OKのときのみ列挙

            if 'rows_remote' not in locals():

                rows_remote = list_remote_archives_for_source(self.ent_src.get(), self._ssh_cfg(), self._append_log)

            if not rows_remote:

                ttk.Label(

                    frm_remote,

                    text=f"No archives in {default_remote_dest_dir(self.ent_src.get())}\n"

                         f"Run Backup (SSH) or copy *.tar.zst to that folder."

                ).pack(fill="x", padx=8, pady=6)

            else:

                for r in rows_remote:

                    mb = f"{(r.get('size') or 0)/1024/1024:.2f}"

                    mtime = datetime.datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M:%S")

                    tree_remote.insert(

                        "", "end",

                        values=(r["name"], mb, mtime,

                                "Y" if r.get("sha_exists") else "-",

                                "Y" if r.get("list_exists") else "-",

                                r["path"])

                    )

        except Exception as e:

            messagebox.showerror("Remote List Error", str(e))



        # ボタン行は常に下部に固定して可視化

        frm_btn = ttk.Frame(win); frm_btn.pack(side="bottom", fill="x")

        def use_selected():

            current = nb.index(nb.select())

            if current == 0:

                sel = tree_local.selection()

                if not sel: return

                vals = tree_local.item(sel[0], "values"); path = vals[5]

                self.sel_source, self.sel_path = "local", path

                self.lbl_selected.config(text=f"Selected: Local -> {os.path.basename(path)}")

            else:

                sel = tree_remote.selection()

                if not sel: return

                vals = tree_remote.item(sel[0], "values"); path = vals[5]

                self.sel_source, self.sel_path = "remote", path

                self.lbl_selected.config(text=f"Selected: Remote -> {os.path.basename(path)}")

            win.destroy()

        ttk.Button(frm_btn, text="Use Selected", command=use_selected).pack(side="left", padx=8, pady=8)

        ttk.Button(frm_btn, text="Cancel", command=win.destroy).pack(side="right", padx=8, pady=8)



        _smart_center(win, self.winfo_toplevel(), min_w=800, min_h=460, margin=24)

        # ★ 一覧の構築が終わった後にセンタリング＆最小サイズを設定

        win.update_idletasks()



        # 画面内センタリング＋最小サイズ（高DPI対応）

        def _smart_center(_win, parent=None, min_w=800, min_h=460, margin=24):

            try:

                scale = float(_win.tk.call('tk', 'scaling'))

            except Exception:

                scale = 1.0

            s_min_w = int(min_w * scale); s_min_h = int(min_h * scale)

            _win.update_idletasks()

            req_w = max(_win.winfo_reqwidth(), s_min_w)

            req_h = max(_win.winfo_reqheight(), s_min_h)

            sw = _win.winfo_screenwidth(); sh = _win.winfo_screenheight()

            w = min(req_w, sw - 2 * margin); h = min(req_h, sh - 2 * margin)

            if parent is not None:

                try: parent.update_idletasks()

                except Exception: pass

                px = parent.winfo_rootx(); py = parent.winfo_rooty()

                pw = parent.winfo_width(); ph = parent.winfo_height()

                if pw <= 1 or ph <= 1:

                    parent.update_idletasks(); pw = parent.winfo_width(); ph = parent.winfo_height()

                x = px + (pw - w) // 2; y = py + (ph - h) // 2

            else:

                x = (sw - w) // 2; y = (sh - h) // 2

            x = max(margin, min(x, sw - w - margin))

            y = max(margin, min(y, sh - h - margin))

            _win.geometry(f"{w}x{h}+{x}+{y}")

            _win.minsize(s_min_w, s_min_h)

 

        def build_tree(parent):

            cols = ("name","size","mtime","sha","lst","path")

            tree = ttk.Treeview(parent, columns=cols, show="headings", height=14)

            for c, t, w, anc in [

                ("name","Name",260,"w"),("size","Size (MB)",90,"e"),("mtime","Modified",160,"center"),

                ("sha","SHA",60,"center"),("lst","LIST",60,"center"),("path","Path",0,"w")

            ]:

                tree.heading(c, text=t); tree.column(c, width=w, anchor=anc)

            ysb = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)

            tree.configure(yscrollcommand=ysb.set)



    # --- actions ---

    def _do_backup(self):

        def work():

            try:

                src = self.ent_src.get()

                local_dir = self.ent_local.get()

                lvl = int(self.spn_lvl.get()); thr = int(self.spn_thr.get())

                excl = [s.strip() for s in self.ent_excl.get().split(",") if s.strip()]

                onefs = bool(self.var_onefs.get())

                dest = default_remote_dest_dir(src)



                if bool(self.hc_enabled.get()) and self.exec_mode.get() == "SSH":

                    try: minfree = int(float(self.hc_minfree.get()))

                    except Exception: minfree = 0

                    health_check_remote(dest, self._ssh_cfg(), require_mounted=bool(self.hc_require_mount.get()),

                                        min_free_gb=minfree, log_cb=self._append_log)



                cmd, archive_path, list_path, sha_path = build_backup_cmd(src, dest, lvl, thr, excl, onefs)

                if self.exec_mode.get() == "SSH":

                    local_arch, local_sha, remote_arch, remote_sha = run_backup_pipeline_to_windows(

                        source_dir=src,

                        local_dir=local_dir,

                        ssh_cfg=self._ssh_cfg(),

                        compression_level=lvl,

                        threads=thr,

                        excludes=excl,

                        one_file_system=onefs,

                        create_remote_copy=True,

                        make_remote_list_and_sha=False,

                        make_local_list=True,

                        log_cb=self._append_log,

                    )

                    self._append_log(f"[pipeline] done: local={local_arch}, remote={remote_arch}")

                    

                    try:

                        lk = int(self.rt_l_keep_n.get() or 0); ld = int(self.rt_l_keep_days.get() or 0)

                    except Exception:

                        lk, ld = 0, 0

                    if lk > 0 or ld > 0:

                        manage_local_retention(local_dir, keep_n=lk, keep_days=ld, log_cb=self._append_log)

                else:

                    if not os.path.isdir(src): raise FileNotFoundError(f"source_dir not found: {src}")

                    if not os.path.isdir(dest): os.makedirs(dest, exist_ok=True)

                    run_backup(cmd, archive_path, list_path, sha_path, self._append_log)

                messagebox.showinfo("Backup", f"Completed: {archive_path}")

            except Exception as e:

                messagebox.showerror("Backup Error", str(e))

            finally:

                self._stop_busy()

        self._start_busy(); threading.Thread(target=work, daemon=True).start()



    def _do_restore(self):

        def work():

            try:

                keep_guard = bool(self.var_keep_old_files.get())

                if self.exec_mode.get() == "SSH":

                    parent = self.ent_restore_parent.get()

                    if getattr(self, "sel_path", None):

                        if self.sel_source == "local":

                            restore_specific_local_to_remote(

                                local_arch=self.sel_path, remote_restore_parent=parent, ssh_cfg=self._ssh_cfg(),

                                verify_before=bool(self.var_verify_before.get()), cleanup_remote=bool(self.var_cleanup_remote.get()),

                                keep_old_files=keep_guard, log_cb=self._append_log,

                                remote_tmp_dir=(self.ent_remote_tmp.get().strip() or None),

                                transfer_method=self.xfer_method.get().lower()

                            )

                            messagebox.showinfo("Restore", f"Completed: restored selected local to {parent}")

                        else:

                            run_restore_ssh(self.sel_path, parent, self._ssh_cfg(), self._append_log, keep_old_files=keep_guard)

                            messagebox.showinfo("Restore", f"Completed: {self.sel_path}")

                        return

                    if bool(self.var_restore_from_local.get()):

                        local_dir = self.ent_local.get()

                        rows = list_local_archives(local_dir, self._append_log)

                        if not rows: raise FileNotFoundError("No local archives found")

                        latest_path = rows[0]["path"]

                        restore_specific_local_to_remote(

                            local_arch=latest_path, remote_restore_parent=parent, ssh_cfg=self._ssh_cfg(),

                            verify_before=bool(self.var_verify_before.get()), cleanup_remote=bool(self.var_cleanup_remote.get()),

                            keep_old_files=keep_guard, log_cb=self._append_log,

                            remote_tmp_dir=(self.ent_remote_tmp.get().strip() or None),

                            transfer_method=self.xfer_method.get().lower()

                        )

                        messagebox.showinfo("Restore", f"Completed: restored latest from {local_dir} to {parent}")

                    else:

                        arch, _sha = self._resolve_latest_remote()

                        run_restore_ssh(arch, parent, self._ssh_cfg(), self._append_log, keep_old_files=keep_guard)

                        messagebox.showinfo("Restore", f"Completed: {arch}")

                else:

                    archive_path = filedialog.askopenfilename(title="Select tar.zst archive",

                        filetypes=[("Zstd tar", "*.tar.zst"), ("All", "*.*")])

                    if not archive_path: return

                    parent = self.ent_restore_parent.get()

                    cmd = build_restore_cmd(archive_path, parent, keep_old_files=keep_guard)

                    run_restore(cmd, self._append_log)

                    messagebox.showinfo("Restore", "Completed")

            except Exception as e:

                messagebox.showerror("Restore Error", str(e))

            finally:

                self._stop_busy()

        self._start_busy(); threading.Thread(target=work, daemon=True).start()



    def _do_verify(self):

        def work():

            try:

                is_backup_tab = (self.nb.index(self.nb.select()) == 0)

                if is_backup_tab:

                    archive_path = filedialog.askopenfilename(

                        title="Select tar.zst archive",

                        filetypes=[("Zstd tar", "*.tar.zst"), ("All", "*.*")]

                    )

                    if not archive_path: return

                    sha_path = filedialog.askopenfilename(

                        title="Select SHA256 file",

                        filetypes=[("SHA256", "*.sha256"), ("All", "*.*")]

                    )

                    if not sha_path: return

                    ok = verify_sha256(archive_path, sha_path, self._append_log)

                    messagebox.showinfo("Verify", f"SHA256 match: {ok}")

                    return



                if self.exec_mode.get() == "SSH":

                    if getattr(self, "sel_path", None):

                        if self.sel_source == "local":

                            local_arch = self.sel_path

                            local_sha = local_arch + ".sha256"

                            if not os.path.isfile(local_arch) or not os.path.isfile(local_sha):

                                raise FileNotFoundError("Selected local archive or .sha256 not found")

                            ok = verify_sha256(local_arch, local_sha, self._append_log)

                            messagebox.showinfo("Verify", f"SHA256 match (local): {ok}\n{local_arch}")

                        else:

                            remote_arch = self.sel_path

                            remote_sha = remote_arch + ".sha256"

                            ok = verify_sha256_ssh(remote_arch, remote_sha, self._ssh_cfg(), self._append_log)

                            messagebox.showinfo("Verify", f"SHA256 match (remote): {ok}\n{remote_arch}")

                        return



                    if bool(self.var_restore_from_local.get()):

                        local_dir = self.ent_local.get()

                        rows = list_local_archives(local_dir, self._append_log)

                        if not rows:

                            messagebox.showwarning("Verify", "No local archives found.\nPlease use 'Select Generation...' to choose a file.")

                            return

                        local_arch = rows[0]["path"]

                        local_sha = local_arch + ".sha256"

                        if not os.path.isfile(local_sha):

                            raise FileNotFoundError(f".sha256 not found: {local_sha}")

                        ok = verify_sha256(local_arch, local_sha, self._append_log)

                        messagebox.showinfo("Verify", f"SHA256 match (local latest): {ok}\n{local_arch}")

                        return



                    try:

                        arch, sha = self._resolve_latest_remote()

                    except FileNotFoundError:

                        messagebox.showerror(

                            "Verify",

                            "No archive found in remote backups directory.\n"

                            "Use 'Select Generation...' to choose a remote file\n"

                            "or turn ON 'From local latest (Windows) via SSH'."

                        )

                        return

                    ok = verify_sha256_ssh(arch, sha, self._ssh_cfg(), self._append_log)

                    messagebox.showinfo("Verify", f"SHA256 match (remote latest): {ok}\n{arch}")

                else:

                    archive_path = filedialog.askopenfilename(

                        title="Select tar.zst archive",

                        filetypes=[("Zstd tar", "*.tar.zst"), ("All", "*.*")]

                    )

                    if not archive_path: return

                    sha_path = filedialog.askopenfilename(

                        title="Select SHA256 file",

                        filetypes=[("SHA256", "*.sha256"), ("All", "*.*")]

                    )

                    if not sha_path: return

                    ok = verify_sha256(archive_path, sha_path, self._append_log)

                    messagebox.showinfo("Verify", f"SHA256 match: {ok}")

            except Exception as e:

                messagebox.showerror("Verify Error", str(e))

            finally:

                self._stop_busy()

        self._start_busy(); threading.Thread(target=work, daemon=True).start()



    # --- settings apply/save ---

    def _apply_settings_to_ui(self):

        s = self.cfg

        self.exec_mode.set("SSH" if s.get("exec_mode","SSH")=="SSH" else "Local")

        ssh = s.get("ssh", {})

        self.ent_host.delete(0, tk.END); self.ent_host.insert(0, ssh.get("host",""))

        self.ent_port.delete(0, tk.END); self.ent_port.insert(0, str(ssh.get("port",22)))

        self.ent_user.delete(0, tk.END); self.ent_user.insert(0, ssh.get("user",""))

        self.use_password.set(bool(ssh.get("use_password", False)))

        self.ent_password.delete(0, tk.END); self.ent_password.insert(0, ssh.get("password",""))

        self.ent_key.delete(0, tk.END); self.ent_key.insert(0, ssh.get("keyfile",""))

        self.use_sudo.set(bool(ssh.get("use_sudo", False)))



        p = s.get("paths", {})

        self.ent_src.delete(0, tk.END); self.ent_src.insert(0, p.get("source_dir","/var/spool/epms"))

        self.ent_restore_parent.delete(0, tk.END); self.ent_restore_parent.insert(0, p.get("restore_parent","/var/spool"))

        self.ent_local.delete(0, tk.END); self.ent_local.insert(0, p.get("local_save","C:/Backup/epms"))



        h = s.get("health", {})

        self.hc_enabled.set(bool(h.get("enabled", True)))

        self.hc_require_mount.set(bool(h.get("require_mounted", False)))

        self.hc_minfree.delete(0, tk.END); self.hc_minfree.insert(0, str(h.get("min_free_gb", 5)))



        r = s.get("retention", {})

        self.rt_r_keep_n.delete(0, tk.END); self.rt_r_keep_n.insert(0, str(r.get("remote_keep_n", 10)))

        self.rt_r_keep_days.delete(0, tk.END); self.rt_r_keep_days.insert(0, str(r.get("remote_keep_days", 0)))

        self.rt_l_keep_n.delete(0, tk.END); self.rt_l_keep_n.insert(0, str(r.get("local_keep_n", 10)))

        self.rt_l_keep_days.delete(0, tk.END); self.rt_l_keep_days.insert(0, str(r.get("local_keep_days", 0)))



        o = s.get("options", {})

        self.spn_lvl.delete(0, tk.END); self.spn_lvl.insert(0, int(o.get("compression_level", 6)))

        self.spn_thr.delete(0, tk.END); self.spn_thr.insert(0, int(o.get("threads", 0)))

        self.var_onefs.set(bool(o.get("one_file_system", True)))

        self.ent_excl.delete(0, tk.END); self.ent_excl.insert(0, ",".join(o.get("excludes", [])))



        ro = s.get("restore_opts", {})

        self.var_restore_from_local.set(bool(ro.get("from_local", True)))

        self.var_verify_before.set(bool(ro.get("verify_before", True)))

        self.var_cleanup_remote.set(bool(ro.get("cleanup_remote", True)))

        self.ent_remote_tmp.delete(0, tk.END); self.ent_remote_tmp.insert(0, ro.get("remote_tmp",""))



        ui = s.get("ui", {})

        want_tab = ui.get("selected_tab", "Backup")

        try:

            idx = 0 if want_tab == "Backup" else 1

            self.nb.select(idx)

            # ActionBar 切替＋選択タブの ScrollableFrame をリフレッシュ

            self._show_action_bar("backup" if idx == 0 else "restore")

            self._refresh_scrollable(self.sf_bk if idx == 0 else self.sf_rs)

        except Exception:

            pass



        self.xfer_method.set(s.get("ssh", {}).get("transfer_method", "sftp").upper())

        self._update_verify_labels()



    def _wire_auto_save(self):

        def save_all(_e=None):

            self._collect_ui_to_settings()

            self.settings_mgr.save()

        entries = [

            self.ent_host, self.ent_port, self.ent_user, self.ent_password, self.ent_key,

            self.ent_src, self.ent_restore_parent, self.ent_local,

            self.hc_minfree, self.rt_r_keep_n, self.rt_r_keep_days,

            self.rt_l_keep_n, self.rt_l_keep_days, self.spn_lvl, self.spn_thr, self.ent_excl,

            self.ent_remote_tmp

        ]

        for w in entries:

            w.bind("<FocusOut>", save_all)



        def on_tab(_):

            self._collect_ui_to_settings()

            self.settings_mgr.save()

        # 既存の _on_tab_changed バインドを上書きしないよう + で追記

        self.nb.bind("<<NotebookTabChanged>>", on_tab, add="+")



    def _collect_ui_to_settings(self):

        s = {}

        s["exec_mode"] = self.exec_mode.get()

        s["ssh"] = {

            "host": self.ent_host.get(),

            "port": int(self.ent_port.get() or 22),

            "user": self.ent_user.get(),

            "use_password": bool(self.use_password.get()),

            "password": self.ent_password.get(),

            "keyfile": self.ent_key.get(),

            "use_sudo": bool(self.use_sudo.get()),

            "transfer_method": self.xfer_method.get().lower()

        }

        s["paths"] = {

            "source_dir": self.ent_src.get(),

            "restore_parent": self.ent_restore_parent.get(),

            "local_save": self.ent_local.get()

        }

        s["health"] = {

            "enabled": bool(self.hc_enabled.get()),

            "require_mounted": bool(self.hc_require_mount.get()),

            "min_free_gb": int(float(self.hc_minfree.get() or 0))

        }

        s["retention"] = {

            "remote_keep_n": int(self.rt_r_keep_n.get() or 0),

            "remote_keep_days": int(self.rt_r_keep_days.get() or 0),

            "local_keep_n": int(self.rt_l_keep_n.get() or 0),

            "local_keep_days": int(self.rt_l_keep_days.get() or 0)

        }

        s["options"] = {

            "compression_level": int(self.spn_lvl.get() or 6),

            "threads": int(self.spn_thr.get() or 0),

            "one_file_system": bool(self.var_onefs.get()),

            "excludes": [t.strip() for t in self.ent_excl.get().split(",") if t.strip()]

        }

        s["restore_opts"] = {

            "from_local": bool(self.var_restore_from_local.get()),

            "verify_before": bool(self.var_verify_before.get()),

            "cleanup_remote": bool(self.var_cleanup_remote.get()),

            "remote_tmp": self.ent_remote_tmp.get()

        }

        idx = self.nb.index(self.nb.select())

        s["ui"] = {"selected_tab": "Backup" if idx == 0 else "Restore"}

        self.cfg = s

        self.settings_mgr.settings = s

