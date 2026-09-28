# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置: python -m PyInstaller rockabuddy.spec --noconfirm
a = Analysis(
    ['app.py'],
    binaries=[],
    datas=[('assets', 'assets')],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name='Rockabuddy',
    console=False,
    icon='assets/icon.ico',
    upx=False,
)
