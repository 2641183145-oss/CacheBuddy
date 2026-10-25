# -*- mode: python ; coding: utf-8 -*-
# CacheBuddy.exe: onefile, windowed, the webview UI bundled as data.
# py -m PyInstaller CacheBuddy.spec -> dist\CacheBuddy.exe
a = Analysis(
    ['cc_buddy.py'],
    pathex=[],
    binaries=[],
    datas=[('ui/app.css', 'ui'), ('ui/app.js', 'ui'), ('ui/ecg.js', 'ui'),
           ('ui/index.html', 'ui'), ('ui/mascots.js', 'ui')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy'],   # nothing imports it; it just rode along once
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='CacheBuddy',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='cache_buddy.ico',
)
