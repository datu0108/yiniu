# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：亿牛广告看板"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

block_cipher = None
script_dir = Path(SPEC).parent

datas = []
binaries = []
hiddenimports = ["merge_customer_data", "cgi"]

for pkg in ("pandas", "openpyxl", "numpy"):
    tmp = collect_all(pkg)
    datas += tmp[0]
    binaries += tmp[1]
    hiddenimports += tmp[2]

a = Analysis(
    [str(script_dir / "merge_gui.py")],
    pathex=[str(script_dir)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "tkinterdnd2", "matplotlib", "pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="亿牛广告看板",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="亿牛广告看板",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="亿牛广告看板.app",
        icon=None,
        bundle_identifier="com.yiniu.merge.tool",
        info_plist={
            "CFBundleName": "亿牛广告看板",
            "CFBundleDisplayName": "亿牛广告看板",
            "CFBundleVersion": "1.0.0",
            "CFBundleShortVersionString": "1.0.0",
            "NSHighResolutionCapable": True,
            "LSUIElement": False,
            "NSRequiresAquaSystemAppearance": False,
        },
    )
