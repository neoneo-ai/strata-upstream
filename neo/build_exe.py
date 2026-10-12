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


def build(dest: str | Path = "", quiet: bool = False) -> Path:
    """跑 PyInstaller，把 NeoStrata.exe 放到 dest（默认仓库根）。回 exe 的路径。"""
    neo_dir = safe_resolve(Path(__file__).parent)
    root = neo_dir.parent
    if not pyinstaller_available():
        raise SystemExit("打包要 pyinstaller：python -m pip install pyinstaller")

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
        print(f"{target}  {(target.stat().st_size / 1_048_576):.1f} MB")
    return target


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="把 Neo 打包成 NeoStrata.exe")
    p.add_argument("--dest", default="", help="exe 放哪（默认仓库根）")
    args = p.parse_args(argv)
    build(args.dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())