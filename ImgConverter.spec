# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
import importlib
from pathlib import Path

datas = [('icon.png', '.'), ('icon.ico', '.')]
binaries = []
hiddenimports = []


def collect_optional_assets(module_name):
    try:
        tmp_ret = collect_all(module_name)
    except (ImportError, ModuleNotFoundError):
        return
    datas.extend(tmp_ret[0])
    binaries.extend(tmp_ret[1])
    hiddenimports.extend(tmp_ret[2])

    # c2pa-python keeps its platform library in a nested ``libs`` directory;
    # collect_all does not consistently classify that DLL across PyInstaller
    # hook releases, so add it explicitly when present.
    try:
        module_spec = importlib.util.find_spec(module_name)
        package_dir = Path(module_spec.origin).parent if module_spec and module_spec.origin else None
        library_dir = package_dir / 'libs' if package_dir else None
        if library_dir and library_dir.is_dir():
            for library in library_dir.iterdir():
                if library.suffix.lower() in {'.dll', '.dylib', '.so'}:
                    binaries.append((str(library), f'{module_name}/libs'))
    except (OSError, ImportError, ModuleNotFoundError):
        pass


for required_module in ('pillow_heif',):
    collect_optional_assets(required_module)

for opt_mod in ('c2pa', 'pillow_jxl', 'rawpy', 'watchdog', 'imagehash', 'ssimulacra2'):
    try:
        importlib.import_module(opt_mod)
        hiddenimports.append(opt_mod)
        collect_optional_assets(opt_mod)
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
