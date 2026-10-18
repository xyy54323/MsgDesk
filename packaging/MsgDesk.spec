# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: MsgDesk.exe (desktop window) + MsgDesk-cli.exe (console)."""

import os

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (os.path.join(ROOT, "msgdesk", "api", "static"), "msgdesk/api/static"),
    (os.path.join(ROOT, "msgdesk", "migrations"), "msgdesk/migrations"),
]
binaries = []
hiddenimports = ["webview.platforms.edgechromium"]

for package in ("webview", "pythonnet", "clr_loader"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

excludes = []
if os.environ.get("MSGDESK_WITH_BROWSER") != "1":
    excludes.append("playwright")

a = Analysis(
    [os.path.join(ROOT, "packaging", "entry.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MsgDesk",
    debug=False,
    strip=False,
    upx=False,
    console=False,
)

cli = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MsgDesk-cli",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    cli,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MsgDesk",
)
