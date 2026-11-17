# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules

binaries = []
hiddenimports = []
binaries += collect_dynamic_libs('winrt')
hiddenimports += collect_submodules('winrt')


a = Analysis(
    ['coverglow.py'],
    pathex=[],
    binaries=binaries,
    datas=[('fonts/Geist-Regular.ttf', 'fonts'), ('fonts/Geist-SemiBold.ttf', 'fonts'),
           ('fonts/Geist-Medium.ttf', 'fonts'), ('fonts/Geist-OFL.txt', 'fonts')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # unused large parts: AVIF/WebP/color profiles (covers are JPEG), test/dev modules
    excludes=['PIL.AvifImagePlugin', 'PIL._avif', 'PIL.WebPImagePlugin', 'PIL._webp',
              'PIL.ImageCms', 'PIL._imagingcms', 'numpy', 'unittest', 'pydoc', 'doctest',
              'lib2to3', 'tkinter.test', 'idlelib'],
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
    name='Coverglow',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
