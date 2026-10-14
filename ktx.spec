# PyInstaller 설정: uv run pyinstaller ktx.spec  →  dist/ktx.exe
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [("ktx/static", "ktx/static")], [], ["keyring.backends.Windows"]
for pkg in ("pykorail", "uvicorn", "keyring"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

a = Analysis(["run.py"], datas=datas, binaries=binaries, hiddenimports=hiddenimports)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="ktx", console=True, upx=False)
