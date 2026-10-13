"""neo/tests/_bootstrap.py - 测试的统一入口：sys.path 与"仓库 / 引擎"这两个根。

Neo 的测试要知道两个目录。它们今天是同一个，拆开 Neo 之后不一定是：

    REPO    Neo 仓库的根（neo/ 的上一层）：从这里 import neo，读 neo/web 里 Neo 自己的页面
    ENGINE  Strata 引擎的根：读 setup.py、serve/server.py 这些上游文件用的根

ENGINE 就是 neo/paths.py 的 strata_root() 算出来的那一个，与程序运行时用的是同一条规则
（neo/manager.py 的 --root 默认值走的也是它）。所以"测试全绿"说的就是
"测试跑在 Neo 真会用的那个引擎根目录上"，钉住的引擎版本一换，这条立刻看得见。

引擎不在（Neo 单独的仓库、还没装引擎）时 ENGINE 回仓库根，读上游文件的测试自己跳过并说明原因。

用法：每个测试模块开头那两行（ROOT = parents[2] 与 sys.path.insert）换成

    from ._bootstrap import ENGINE, REPO
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

#: Neo 仓库的根：neo/tests/_bootstrap.py 往上两层。
REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from neo import paths                                              # noqa: E402

#: Strata 引擎的根（与 neo/manager.py 的 --root 默认值同一条规则）。
ENGINE = paths.strata_root()[0]


def has_engine(root=None) -> bool:
    """这个 ENGINE 里真的住着引擎吗（setup.py 与 serve/server.py 都在）。"""
    return paths.is_strata_root(Path(root) if root else ENGINE)


def no_engine_reason() -> str:
    """没有引擎时给跳过用的那句话。"""
    return f"{ENGINE} 里没有 setup.py + serve/server.py，读上游文件的测试跳过（{paths.strata_root()[1]}）"


def server_import_deps(root) -> list[str]:
    """引擎 serve/server.py 顶层 import 的包名（用 ast 读，不 import 它、不起它）。"""
    src = Path(root) / "serve" / "server.py"
    if not src.is_file():
        return []
    try:
        tree = ast.parse(src.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return sorted(names)


def missing_server_imports(root=None) -> list[str]:
    """在这台机器上，引擎的服务 import 不到的包（毫秒级，不起进程、不联网）。"""
    root = Path(root) if root else ENGINE
    from importlib.machinery import PathFinder
    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    missing = []
    for name in server_import_deps(root):
        if name in stdlib:
            continue
        try:
            if PathFinder.find_spec(name, [str(root), *sys.path]) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    return missing


def server_ready(root=None) -> tuple[bool, str]:
    """引擎的真服务现在起得来吗；起不来给一句"缺什么、去哪补"。

    为什么要这条（2026-10-13）：监督器的几条用例要起真的 serve/server.py。引擎根目录的
    .venv 没装上游的包时，那个进程 import 就死（实测：ModuleNotFoundError: No module
    named 'jinja2'），监督器按退避一遍遍重启，测试按 90 秒的等待一直等——套件从十几秒
    变成几分钟，看上去就是"卡住"。先花几毫秒看 import 缺不缺：缺就跳过并说清缺哪个包，
    一条用例最多花 0.1 秒，而不是 90 秒。
    """
    root = Path(root) if root else ENGINE
    if not has_engine(root):
        return False, no_engine_reason()
    missing = missing_server_imports(root)
    if missing:
        return False, (f"{root} 里的 serve/server.py 起不动：当前解释器缺 "
                       f"{', '.join(missing)}（在引擎根目录装依赖：{root / 'requirements.txt'}）")
    return True, ""
