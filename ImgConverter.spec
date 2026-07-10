# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
import importlib

datas = [('icon.png', '.'), ('icon.ico', '.')]
binaries = []
hiddenimports = []
tmp_ret = collect_all('pillow_heif')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

for opt_mod in ('c2pa', 'watchdog', 'imagehash', 'ssimulacra2'):
    try:
        importlib.import_module(opt_mod)
        hiddenimports.append(opt_mod)
    except ImportError:
        pass


a = Analysis(
    ['imgconverter.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['packaging/runtime_hook_mp.py'],
    excludes=[],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ImgConverter',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=['*.pyd', 'Qt6*.dll'],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
