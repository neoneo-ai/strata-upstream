# -*- mode: python ; coding: utf-8 -*-
"""neo/NeoStrata.spec - PyInstaller 的打包说明：把 Neo 打成一个 NeoStrata.exe。

    python -m PyInstaller --noconfirm --clean neo/NeoStrata.spec

出来的 exe 是设计成**放进 Strata 的根目录**双击就跑（D:\\Work\\Strata\\NeoStrata.exe）：
根目录就是 exe 所在目录（neo/manager.py 的 default_root()），状态写在
<根目录>\\neo\\state\\，跑服务用的 Python 优先取 <根目录>\\.venv\\Scripts\\python.exe
（neo/state.py 的 python_executable()）。

exe 里**不打**模型、引擎、torch、numpy —— Neo 只是通过 serve/server.py 的命令行参数
启动服务（neo/engines/strata.py），那些都在 Strata 的 .venv 与根目录里，打进来只会
让 exe 变大、版本对不上。

console=False：窗口版没有控制台。顺带绕开控制台被 QuickEdit 选中时写 stdout 卡住
整条线程的那条路（docs/NEO_DESKTOP.md「窗口点了就"未响应"」）。
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

NEO_DIR = Path(SPECPATH)
ROOT_DIR = NEO_DIR.parent

# 控制台页面（app.js / style.css / *.html）打进包里的 web/，运行时从解包目录读
# （neo/paths.py 的 web_dir()）。
# engine_ref.json 是钉住的那一份引擎（neo/engine_setup.py 读它）：exe 单独放在一个
# 目录里、旁边没有 neo/ 时，pin 只能从包里读，所以它必须跟着 exe 走。
datas = [(str(NEO_DIR / "web"), "web"), (str(NEO_DIR / "engine_ref.json"), ".")]

# pywebview 按平台在运行时挑后端（webview.platforms.winforms / edgechromium / win32 ...），
# 这些导入 PyInstaller 看不见，得点名；neo.engines.* 是适配器。
hiddenimports = collect_submodules("webview") + ["neo.engines.strata", "neo.engines.mock"]

a = Analysis(
    [str(NEO_DIR / "neo_entry.py")],
    pathex=[str(ROOT_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "scipy", "PyQt5", "PySide2", "PyQt6", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="NeoStrata",
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