"""neo/neo_entry.py - 打包成 exe 时的入口脚本。

neo/manager.py 用的是包内相对导入（from . import state），PyInstaller 把包里的模块
当顶层脚本分析时认不出父包，那些导入会全被标成"找不到模块"。所以打包给一个
显式 import 包的脚本，pathex 指到仓库根（neo/NeoStrata.spec 里）。

源码跑不用这个文件：python -m neo.manager。

exe 也当命令行用（neo/start.py 那一行命令）：
    NeoStrata.exe start claude      拉起 Claude Code，端点/token/模型/上下文带上下
    NeoStrata.exe connect claude    只打印接入命令
    NeoStrata.exe models            看这台机器能装哪一个
第一个参数是这几个之一就走命令行，不开窗口；其余（含没有参数）照旧开窗口。
"""
import os
import sys

from neo.manager import main

# exe 当命令行用时分发的子命令 -> 各自模块的 main
CLI = {"start": "neo.start", "connect": "neo.connect", "models": "neo.models",
       "wizard": "neo.wizard"}


def needs_console() -> bool:
    """窗口版 exe（console=False）里没有控制台：sys.stdout 是空的，打印出去就没了。

    命令行子命令要让用户看见输出，所以这种时候把自己在新控制台里再跑一遍
    （PyInstaller 的常规做法）。终端里跑的 python 有真终端，不折腾。
    """
    if os.name != "nt" or not bool(getattr(sys, "frozen", False)):
        return False
    return sys.stdout is None or not getattr(sys.stdout, "isatty", lambda: False)()


def dispatch(argv: list[str]) -> int | None:
    """argv 的第一个参数是命令行子命令就跑它，回退出码；不是就回 None（开窗口）。"""
    name = str(argv[0] if argv else "").strip().lower()
    if name not in CLI:
        return None
    if needs_console():
        import subprocess
        proc = subprocess.run([sys.executable, *argv],
                              creationflags=subprocess.CREATE_NEW_CONSOLE)
        return int(proc.returncode)
    import importlib
    module = importlib.import_module(CLI[name])
    return int(module.main(argv[1:]))


if __name__ == "__main__":
    code = dispatch(sys.argv[1:])
    sys.exit(main() if code is None else code)
