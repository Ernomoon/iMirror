# PyInstaller spec: builds dist/iMirror/iMirror.exe (one folder, windowed).
# Run from the repo root:  pyinstaller packaging/imirror.spec
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = SPECPATH + "/.."
datas = [(ROOT + "/app/imirror/ui", "imirror/ui")]
binaries = []
hiddenimports = collect_submodules("zeroconf") + ["ifaddr", "serial.tools.list_ports", "esptool"]
for pkg in ("webview", "winrt", "clr_loader", "pythonnet", "esptool"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception as e:  # package missing on this platform
        print("collect_all skipped", pkg, e)

a = Analysis([ROOT + "/packaging/launcher.py"], pathex=[ROOT + "/app"], datas=datas, binaries=binaries,
             hiddenimports=hiddenimports, excludes=["tkinter", "matplotlib", "numpy"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="iMirror", console=False,
          icon=ROOT + "/packaging/icon.ico", version=None, uac_admin=False)
coll = COLLECT(exe, a.binaries, a.datas, name="iMirror")
