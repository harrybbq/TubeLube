# PyInstaller spec for TubeLube — builds a single windowed TubeLube.exe.
#   Build with:  pyinstaller TubeLube.spec
# Produces dist/TubeLube.exe (onefile, no console). ffmpeg / yt-dlp(updates) /
# Deno are still expected on the user's machine — see README.
import os
from PyInstaller.utils.hooks import collect_all

datas = [("templates", "templates")]
binaries = []
hiddenimports = []

# yt_dlp pulls in many extractor submodules + data; webview ships JS/runtime.
for pkg in ("yt_dlp", "webview"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

if os.path.isdir("static"):
    datas.append(("static", "static"))

a = Analysis(
    ["desktop.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="TubeLube",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # UPX can trip antivirus; leave off
    runtime_tmpdir=None,
    console=False,        # windowed app — no console
    icon="TubeLube.ico" if os.path.exists("TubeLube.ico") else None,
)
