"""neo/build_exe.py - 把 Neo 打成一个 NeoStrata.exe（neo/NeoStrata.spec）。

    python -m neo.build_exe            打出来放在仓库根：NeoStrata.exe
    python -m neo.build_exe --dest D:\\Work\\Strata

打出来的 exe 是设计成**放进 Strata 的根目录**双击就跑的：exe 找到的"根目录"就是它
自己所在的目录，跑服务用的 Python 优先取那个目录里的 .venv。exe 里不打模型、不打引擎、
不打 torch —— Neo 只通过 serve/server.py 的命令行参数启动服务。

打包要 pyinstaller（只在打包时用，用户那边不用装）：
    python -m pip install pyinstaller
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from .paths import safe_resolve

SPEC = "NeoStrata.spec"
EXE_NAME = "NeoStrata.exe"


def pyinstaller_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("PyInstaller") is not None


def pywebview_available() -> bool:
    """打包环境里有没有 pywebview。"""
    import importlib.util
    return importlib.util.find_spec("webview") is not None


def build(dest: str | Path = "", quiet: bool = False, allow_no_webview: bool = False) -> Path:
    """跑 PyInstaller，把 NeoStrata.exe 放到 dest（默认仓库根）。回 exe 的路径。

    打包环境里没有 pywebview 就拒绝打包：spec 里的 collect_submodules("webview") 在
    缺包时是空列表，PyInstaller 一声不响地打成功，出来的 exe 起来就退回浏览器界面 ——
    用户看不到任何窗口。实测 2026-10-12 19:58 部署的 NeoStrata.exe 自己的日志：
    "pywebview 不可用（No module named 'webview'），退回浏览器界面"。
    """
    neo_dir = safe_resolve(Path(__file__).parent)
    root = neo_dir.parent
    if not pyinstaller_available():
        raise SystemExit("打包要 pyinstaller：python -m pip install pyinstaller")
    if not pywebview_available() and not allow_no_webview:
        raise SystemExit("打包要 pywebview：缺了它打出来的 exe 没有窗口，双击只会开一个浏览器页。\n"
                         "先装上：python -m pip install -r neo/requirements-neo.txt\n"
                         "确实只要浏览器界面（不要原生窗口）就加 --no-webview 再来一次。")

    dist = neo_dir / "dist"
    work = neo_dir / "build"
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           str(neo_dir / SPEC), "--distpath", str(dist), "--workpath", str(work)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=str(root))
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + "\n" + proc.stderr).splitlines()[-25:])
        raise SystemExit(f"PyInstaller 没打成功（退出码 {proc.returncode}）：\n{tail}")

    built = dist / EXE_NAME
    if not built.exists():
        raise SystemExit(f"PyInstaller 说成功了，但 {built} 不在。")

    target = safe_resolve(Path(str(dest) if dest else root)) / EXE_NAME
    if target != built:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(built, target)
    if not quiet:
        print(f"{target}  {(target.stat().st_size / 1_048_576):.1f} MB  "
              f"{'有窗口（pywebview 在包里）' if pywebview_available() else '浏览器界面（exe 里没有 pywebview）'}")
    return target


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="把 Neo 打包成 NeoStrata.exe")
    p.add_argument("--dest", default="", help="exe 放哪（默认仓库根）")
    p.add_argument("--no-webview", action="store_true",
                   help="打包环境里没有 pywebview 也要打（出来的 exe 没有窗口，只有浏览器界面）")
    args = p.parse_args(argv)
    build(args.dest, allow_no_webview=args.no_webview)
    return 0


if __name__ == "__main__":
    sys.exit(main())