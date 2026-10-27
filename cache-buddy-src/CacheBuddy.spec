# -*- mode: python ; coding: utf-8 -*-
# CacheBuddy.exe: onefile, windowed, the webview UI bundled as data.
# py -m PyInstaller CacheBuddy.spec -> dist\CacheBuddy.exe
import os, sys
from PyInstaller.utils.hooks import collect_submodules
# session-bridge (carry over) is imported at run time by bridge.py, so the
# analysis can't see it: point at the sibling folder and pull it in whole
BRIDGE = os.path.abspath(os.path.join(SPECPATH, '..', 'session-bridge'))
sys.path.insert(0, BRIDGE)
a = Analysis(
    ['cc_buddy.py'],
    pathex=[BRIDGE],
    binaries=[],
    datas=[('ui/app.css', 'ui'), ('ui/app.js', 'ui'), ('ui/ecg.js', 'ui'),
           ('ui/index.html', 'ui'), ('ui/mascots.js', 'ui')],
    hiddenimports=collect_submodules('sessionbridge'),
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
