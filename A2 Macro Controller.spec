# -*- mode: python ; coding: utf-8 -*-
# Build spec for the main exe, used by build.bat.  Checked into the repo (the
# generic *.spec gitignore is negated for this file) because it carries a size
# trim the plain CLI flags cannot express: OpenCV's bundled FFmpeg video-IO
# DLL (~28 MB) is dropped below, since the app never uses cv2 video capture
# (PyAV decodes the screenrecord stream; cv2 only does imdecode/matchTemplate
# style work, all verified to run without that DLL).
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = ['pynput.keyboard._win32', 'pynput.mouse._win32']
tmp_ret = collect_all('av')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# Size trim: drop OpenCV's FFmpeg video-IO DLL (unused; see header comment).
a.binaries = [b for b in a.binaries
              if 'opencv_videoio_ffmpeg' not in b[0].lower()]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='A2 Macro Controller',
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
)
