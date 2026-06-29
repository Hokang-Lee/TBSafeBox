
# tar_app.spec
block_cipher = None

a = Analysis(
    ['run_app.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('config/tar_defaults.json', 'config'),
        ('scripts/tar_gui.py', 'scripts'),
        ('scripts/tar_ops.py', 'scripts'),
        ('scripts/key_wizard.py', 'scripts'),
    ],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name='TBSafeBox',
    debug=False,
    strip=False,
    upx=False,
    console=False,    # GUI only
    icon='assets/icon.ico'
)
