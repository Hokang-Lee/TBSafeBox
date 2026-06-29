
# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

ICON_STR = None
MANIFEST = None

def _spec_dir() -> Path:
    try:
        return Path(__file__).parent.resolve()
    except NameError:
        for arg in sys.argv:
            if arg.endswith(".spec"):
                return Path(arg).resolve().parent
        return Path.cwd().resolve()

SPEC_DIR = _spec_dir()

# アイコン
_icon = SPEC_DIR / "assets" / "icon.ico"
if _icon.exists():
    ICON_STR = str(_icon)

# マニフェスト（存在すればXML文字列）
_manifest = SPEC_DIR / "app.manifest"
if _manifest.exists():
    try:
        MANIFEST = _manifest.read_text(encoding="utf-8")
    except Exception:
        MANIFEST = None

# ---- 収集設定 ----
# scripts 配下のサブモジュールを（scripts がパッケージなら）収集
scripts_hidden = []
try:
    scripts_hidden = collect_submodules('scripts')
except Exception:
    scripts_hidden = []

# 必要なデータファイル（assets, config 等）
datas = []
# アイコンをデータとしても同梱したいなら（コードから参照する可能性がある場合）
if _icon.exists():
    datas.append((str(_icon), 'assets'))

# config/tar_defaults.json を両候補から探索して最初に見つかった方を同梱
for cfg in [SPEC_DIR / 'config' / 'tar_defaults.json',
            SPEC_DIR / 'scripts' / 'config' / 'tar_defaults.json']:
    if cfg.exists():
        datas.append((str(cfg), 'config'))
        break
else:
    # 見つからなければ早期に分かるように明示エラー
    raise FileNotFoundError(f"tar_defaults.json が見つかりません: {SPEC_DIR/'config'} または {SPEC_DIR/'scripts'/'config'}")

# scripts 配下に画像/テンプレ等の“データ”がある場合のみ追加
try:
    datas += collect_data_files('scripts')
except Exception:
    pass

a = Analysis(
    ['scripts/example_main.py'],
    pathex=[str(SPEC_DIR)],
    binaries=[],
    datas=datas,  # ← ひとまとめで渡す
    hiddenimports=['scripts.tar_gui'],  # 必要なら他の scripts.* も追記
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='TBSafeBox',
    debug=False,
    bootloader_ignore_signals=False, strip=False,
    upx=True, upx_exclude=[], runtime_tmpdir=None, 
    console=False,
    disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
    icon=ICON_STR,
    manifest=MANIFEST,
)
