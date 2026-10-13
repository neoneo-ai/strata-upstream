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

import sys
from pathlib import Path

#: Neo 仓库的根：neo/tests/_bootstrap.py 往上两层。
REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from neo import paths                                              # noqa: E402

#: Strata 引擎的根（与 neo/manager.py 的 --root 默认值同一条规则）。
ENGINE = paths.strata_root()[0]


def has_engine() -> bool:
    """这个 ENGINE 里真的住着引擎吗（setup.py 与 serve/server.py 都在）。"""
    return paths.is_strata_root(ENGINE)


def no_engine_reason() -> str:
    """没有引擎时给跳过用的那句话。"""
    return f"{ENGINE} 里没有 setup.py + serve/server.py，读上游文件的测试跳过（{paths.strata_root()[1]}）"
