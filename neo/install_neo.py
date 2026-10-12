"""neo/install_neo.py - 装 Neo 桌面壳自己要用的包（pywebview），不动上游的 requirements.txt。

    python neo/install_neo.py            装包 + 检查 WebView2 运行时
    python neo/install_neo.py --check    只报告现状，不装

Strata 的 .venv 在就用它，不在就用当前 Python。pywebview 缺了不影响功能：
neo/manager.py 会退回浏览器界面（neo/console_server.py 是标准库写的）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .paths import safe_resolve

NEO_DIR = safe_resolve(Path(__file__).parent)
ROOT = NEO_DIR.parent
REQ = NEO_DIR / "requirements-neo.txt"


def venv_python() -> str:
    for cand in (ROOT / ".venv" / "Scripts" / "python.exe", ROOT / ".venv" / "bin" / "python"):
        if cand.exists():
            return str(cand)
    return sys.executable


def has(mod: str, python: str) -> bool:
    code = f"import importlib.util as u; raise SystemExit(0 if u.find_spec('{mod}') else 1)"
    return subprocess.call([python, "-c", code]) == 0


def webview2_installed() -> bool:
    if sys.platform != "win32":
        return True
    import winreg
    keys = (r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
            r"Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key in keys:
            try:
                with winreg.OpenKey(root, key) as h:
                    if str(winreg.QueryValueEx(h, "pv")[0]).strip():
                        return True
            except OSError:
                continue
    return False


def report(python: str) -> int:
    print(f"Python: {python}")
    print(f"  pywebview : {'有' if has('webview', python) else '没有'}")
    print(f"  WebView2  : {'有' if webview2_installed() else '没有（Windows 需要它才有原生窗口）'}")
    print(f"  模型配置  : {len(list(ROOT.glob('strata-*.json')))} 个 strata-*.json")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    python = venv_python()
    if "--check" in argv:
        return report(python)
    print(f"往 {python} 装 Neo 桌面壳的包 ...")
    rc = subprocess.call([python, "-m", "pip", "install", "-r", str(REQ)])
    if rc:
        print("装包失败。Neo 还是能用：neo/manager.py 会退回浏览器界面。")
        return rc
    if not webview2_installed():
        print("注意：没找到 Edge WebView2 运行时，桌面壳会用浏览器界面。")
        print("      想要原生窗口：https://developer.microsoft.com/microsoft-edge/webview2/")
    report(python)
    print()
    print("下一步：双击 NEO-START.bat（Windows），或运行 python neo/manager.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
