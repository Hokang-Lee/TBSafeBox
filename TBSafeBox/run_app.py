
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TBSafeBox  Backup/Restore (GUI) - Entry point for PyInstaller onedir/exe distribution.
"""

import os
import sys
import ctypes
import socket
import argparse
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

# ---- App metadata ----
APP_NAME = "TBSafeBox  Backup/Restore"
APP_VER  = "v3.4"
APP_PORTABLE_DEFAULT = True  # 既定はポータブル（同梱 config/ を利用）

# ---- Import GUI tab (packaged with PyInstaller) ----
from scripts.tar_gui import TarTab

# --- Windowsの高DPI対応（PyInstallerのEXEでも確実に効かせる） ---
def enable_high_dpi_awareness():
    """
    プロセスを高DPI対応に設定。Windows 8.1+ では Per-Monitor DPI Aware(2) を試し、
    失敗したら古い SetProcessDPIAware にフォールバック。
    """
    import ctypes, sys
    if not sys.platform.startswith("win"):
        return
    try:
        # Per-Monitor DPI Aware (Windows 8.1+)
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            # 旧API（Vista+）
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass



def apply_tk_scaling(root, forced_scale: float = 0.0) -> float:
    """
    現在ウィンドウ/モニタの実倍率に合わせて Tk のスケーリングを設定。
    1) CLI強制 (--scale)があればそれを最優先
    2) GetDpiForWindow(hwnd) -> dpi/96
    3) それでも 1.0 近傍なら GetScaleFactorForMonitor(hmon) -> %/100
    4) 最後に GetDpiForSystem() / 既定値でフォールバック
    """
    import ctypes, sys
    if not sys.platform.startswith("win"):
        return 1.0

    # 1) CLI強制
    if forced_scale and forced_scale > 0.0:
        try:
            root.tk.call('tk', 'scaling', forced_scale)
        except Exception:
            pass
        return forced_scale


    # 先頭付近：この1行で十分（失敗時のみフォールバックへ）
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)  # Per‑Monitor V2
    except Exception:
        pass
    # 以降の set_dpi_awareness / enable_high_dpi_awareness はフォールバックに残すか削除

    # ★ ウィンドウの実体化（DPI取得の安定化）
    try:
        root.update_idletasks()
    except Exception:
        pass

    # 2) ウィンドウDPI
    dpi = 96
    scale = 1.0
    try:
        hwnd = root.winfo_id()
        dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
        scale = max(dpi, 96) / 96.0
    except Exception:
        pass

    # 3) モニタ拡大率（%）のフォールバック
    if scale <= 1.01:
        try:
            MONITOR_DEFAULTTONEAREST = 2
            hmon = ctypes.windll.user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
            sf = ctypes.c_int(100)
            ctypes.windll.shcore.GetScaleFactorForMonitor(hmon, ctypes.byref(sf))
            scale = max(sf.value, 100) / 100.0
        except Exception:
            pass

    # 4) System DPI の最後のフォールバック
    if scale <= 1.01:
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
            scale = max(dpi, 96) / 96.0
        except Exception:
            scale = 1.25  # 保守的既定（125%）

    # 適用
    try:
        root.tk.call('tk', 'scaling', scale)
    except Exception:
        pass
    return scale

# ------------------ Utility: paths ------------------
def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)

def _app_dir() -> Path:
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

def _user_data_dir() -> Path:
    # %APPDATA%/TarBackupRestore（Windows）／~/.local/share/TarBackupRestore（Linux）など
    base = Path(os.getenv("APPDATA") or Path.home() / ".local" / "share")
    return Path(base) / "TBSafeBox"

def resolve_paths(use_portable: bool) -> dict:
    """
    Return dict with APP_DIR, CONFIG_DIR, LOG_DIR, SENTINEL (config.json path).
    """
    app_dir = _app_dir()
    if use_portable:
        config_dir = app_dir / "config"
    else:
        config_dir = _user_data_dir()
    log_dir = config_dir / "logs"
    sentinel = config_dir / "config.json"
    return {
        "APP_DIR": app_dir,
        "CONFIG_DIR": config_dir,
        "LOG_DIR": log_dir,
        "SENTINEL": sentinel
    }


# ------------------ Utility: logging ------------------
def setup_logging(log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "app.log"
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # ローテーション: 1MB × 5世代
    handler = RotatingFileHandler(str(log_path), maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(fmt)
    logger.addHandler(handler)

    # コンソールは開発時のみ参考（exe配布では省略してOK）
    if not _is_frozen():
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    logging.info("%s %s started", APP_NAME, APP_VER)
    logging.info("Log file: %s", log_path)


# ------------------ Utility: exceptions ------------------
def install_excepthook():
    def _show_and_log(exc_type, exc, tb):
        import traceback
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        logging.error("Uncaught exception:\n%s", msg)
        try:
            messagebox.showerror("Unexpected Error", msg)
        except Exception:
            pass
    sys.excepthook = _show_and_log


# ------------------ Utility: single instance ------------------
class SingleInstance:
    """
    Simple single-instance guard using localhost TCP port.
    Default port 51324 (arbitrary). Use --no-single-instance to disable.
    """
    def __init__(self, port=51324):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.bind(("127.0.0.1", port))
            self._sock.listen(1)
            self.ok = True
        except OSError:
            self.ok = False

    def close(self):
        try:
            self._sock.close()
        except Exception:
            pass


# ------------------ DPI awareness ------------------
def set_dpi_awareness():
    # Windows 高DPI対策
    try:
        if sys.platform.startswith("win"):
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ------------------ Tk theme ------------------
def apply_theme(root: tk.Tk):
    try:
        sty = ttk.Style(root)
        # Windows優先: vista → xpnative → clam → default
        for cand in ("vista", "xpnative", "clam", "default"):
            try:
                sty.theme_use(cand)
                break
            except Exception:
                continue
        # LabelFrame 内側余白の微調整
        sty.configure("TLabelframe", padding=(6, 4, 6, 6))
        sty.configure("TLabelframe.Label", padding=(2, 0, 2, 0))
        # スケーリング（高DPI環境で文字が小さすぎる場合に調整）
        # root.call("tk", "scaling", 1.2)
    except Exception:
        pass


# ------------------ main ------------------
def main(argv=None):
    argv = argv or sys.argv[1:]

    # CLI オプション
    ap = argparse.ArgumentParser(
        prog="TarBackupRestore",
        description=f"{APP_NAME} ({APP_VER}) - Health + Retention"
    )
    g_mode = ap.add_mutually_exclusive_group()
    g_mode.add_argument("--portable", action="store_true", help="Use local 'config/' alongside the exe (default).")
    g_mode.add_argument("--user-data", action="store_true", help="Use per-user data directory (e.g., %APPDATA%/TBSafeBox).")
    ap.add_argument("--reset-settings", action="store_true", help="Reset user_settings.json on startup.")
    ap.add_argument("--force-backup-tab", action="store_true", help="Force initial tab to Backup.")
    ap.add_argument("--force-restore-tab", action="store_true", help="Force initial tab to Restore.")
    ap.add_argument("--no-single-instance", action="store_true", help="Allow multiple instances.")
    ap.add_argument("--listen-port", type=int, default=51324, help="Single-instance guard port (default: 51324).")
    opts = ap.parse_args(argv)

    # モード選択
    use_portable = APP_PORTABLE_DEFAULT
    if opts.user_data:
        use_portable = False
    elif opts.portable:
        use_portable = True

    # 既存:
    paths = resolve_paths(use_portable)
    config_dir: Path = paths["CONFIG_DIR"]
    log_dir:    Path = paths["LOG_DIR"]
    sentinel:   Path = paths["SENTINEL"]

    # --- ここから安全フォールバック（そのままでOK） ---
    def _safe_ensure_dir(p: Path) -> Path:
        if p.exists() and not p.is_dir():
            alt = _user_data_dir()
            alt.mkdir(parents=True, exist_ok=True)
            return alt
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            alt = _user_data_dir()
            alt.mkdir(parents=True, exist_ok=True)
            return alt

    config_dir = _safe_ensure_dir(config_dir)
    log_dir    = config_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # ★★★ フォールバック後に sentinel を“今の” config_dir で再定義 ★★★
    sentinel   = config_dir / "config.json"


    # 既定値ファイルの存在チェック（無ければ最低限のものを生成）
    defaults_path = config_dir / "tar_defaults.json"
    if not defaults_path.exists():
        defaults_path.write_text(
            """{
  "exec_mode": "SSH",
  "ssh": { "host": "", "port": 22, "user": "root", "use_password": false,
           "password": "", "keyfile": "", "use_sudo": false, "transfer_method": "sftp" },
  "paths": { "source_dir": "/var/spool/epms", "restore_parent": "/var/spool", "local_save": "C:/Backup/epms" },
  "health": { "enabled": true, "require_mounted": false, "min_free_gb": 5 },
  "retention": { "remote_keep_n": 10, "remote_keep_days": 0, "local_keep_n": 10, "local_keep_days": 0 },
  "options": { "compression_level": 6, "threads": 0, "one_file_system": true, "excludes": [] },
  "restore_opts": { "from_local": true, "verify_before": true, "cleanup_remote": true, "remote_tmp": "" },
  "ui": { "selected_tab": "Backup" }
}""",
            encoding="utf-8"
        )

    # sentinel（存在しなくてもよいが、パスベースとして作成）
    if not sentinel.exists():
        sentinel.write_text("{}", encoding="utf-8")

    # ユーザー設定のリセット（任意）
    user_settings = config_dir / "user_settings.json"
    if opts.reset_settings and user_settings.exists():
        try:
            user_settings.unlink()
        except Exception:
            pass

    # ログ設定
    setup_logging(log_dir)
    install_excepthook()

    # 単一インスタンス
    guard = None
    if not opts.no_single_instance:
        guard = SingleInstance(port=opts.listen_port)
        if not guard.ok:
            messagebox.showwarning(APP_NAME, f"{APP_NAME} は既に起動しています。")
            return 1

    # DPI 対応
    set_dpi_awareness()

    # ★ Tkインスタンスを作る前に DPI awareness を設定
    enable_high_dpi_awareness()

    # Tk 準備
    root = tk.Tk()
    root.title(f"{APP_NAME} ({APP_VER} Health + Retention)")

    # ★ 先に実体化してからスケール計算
    root.update_idletasks()
    scale = apply_tk_scaling(root)  # （forced_scale を渡すなら引数追加）

    # ★ 初期サイズをスケールに合わせて拡大
    base_w, base_h = 980, 720
    root.geometry(f"{int(base_w * scale)}x{int(base_h * scale)}")

    # 親フレーム
    container = ttk.Frame(root)
    container.pack(fill="both", expand=True)
    container.columnconfigure(0, weight=1)
    container.rowconfigure(0, weight=1)

    apply_theme(root)  # ← ここではスケーリングを触らない（前項で削除済み）


    # 既定フォントの拡大
    try:
        import tkinter.font as tkfont
        for fname in ("TkDefaultFont","TkTextFont","TkFixedFont","TkMenuFont",
                    "TkHeadingFont","TkIconFont","TkTooltipFont"):
            f = tkfont.nametofont(fname)
            size = f.cget("size")
            if isinstance(size, int) and size > 0:
                f.configure(size=int(round(size * scale)))
    except Exception:
        pass

    # ttk スタイルの行高／パディング
    try:
        sty = ttk.Style(root)
        row_h   = int(22 * scale)             # Treeview行高（標準22px基準）
        btn_pad = (int(8 * scale), int(4 * scale))
        ent_ipy = int(3 * scale)

        sty.configure("Treeview", rowheight=row_h)
        sty.configure("TButton", padding=btn_pad)
        sty.configure("TEntry",  padding=(4, ent_ipy, 4, ent_ipy))
        sty.configure("TLabelframe",
                    padding=(int(6*scale), int(4*scale), int(6*scale), int(6*scale)))
        sty.configure("TLabelframe.Label",
                    padding=(int(2*scale), 0, int(2*scale), 0))
    except Exception:
        pass

    # GUI 本体（Notebookタブ）
    tab = TarTab(container, config_path=str(sentinel))
    tab.grid(row=0, column=0, sticky="nsew")

    # 初期タブの強制（任意）
    if opts.force_backup_tab:
        try:
            tab.nb.select(0)
            tab._on_tab_changed()
        except Exception:
            pass
    elif opts.force_restore_tab:
        try:
            tab.nb.select(1)
            tab._on_tab_changed()
        except Exception:
            pass

    # メインループ
    try:
        root.mainloop()
    finally:
        if guard:
            guard.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
