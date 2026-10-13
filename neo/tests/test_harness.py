"""neo/tests/test_harness.py - 测试自己的两条规矩：30 秒时间标准、引擎起不动就跳过。

为什么单独一个模块：这两条守的不是 Neo 的功能，是"测试怎么跑"本身——
时间标准（2026-10-13：跑测试必须报时间，单元与套件墙上时间都按 30 秒封顶）写在
`neo/tests/run_all.py` 的 `BUDGET_S` 里，跳过规则写在 `neo/tests/_bootstrap.py` 的
`server_ready` 里。改标准、改跳过规则，都要在这里说清，不然下一次"套件跑五分钟"没人拦。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

if __package__ in (None, ""):                       # 直接跑 python neo/tests/test_harness.py 时没有父包
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "neo.tests"

from ._bootstrap import ENGINE, REPO, server_ready  # noqa: E402
from . import run_all                               # noqa: E402


def tree(root: Path, server_body: str) -> Path:
    """一个"看着像引擎根"的目录：setup.py + serve/server.py，内容随用例给。"""
    (root / "setup.py").write_text("print('setup')\n", encoding="utf-8")
    (root / "serve").mkdir(parents=True, exist_ok=True)
    (root / "serve" / "server.py").write_text(server_body, encoding="utf-8")
    return root


class TestTimeStandard(unittest.TestCase):
    def test_budget_is_thirty_seconds(self):
        self.assertEqual(run_all.BUDGET_S, 30.0)

    def test_unit_timeout_default_is_the_budget(self):
        # --timeout 的默认值就是标准本身：临时放宽要明说，不能悄悄把超时调大
        src = (REPO / "neo" / "tests" / "run_all.py").read_text(encoding="utf-8")
        self.assertIn('"--timeout", type=float, default=BUDGET_S', src)

    def test_run_reports_seconds(self):
        """跑完必报时间：单元逐个报，套件报墙上总时间与串行相加。"""
        src = (REPO / "neo" / "tests" / "run_all.py").read_text(encoding="utf-8")
        self.assertIn("墙上 {total:.1f} 秒", src)
        self.assertIn("串行相加约", src)
        self.assertIn("超预算", src)


class TestServerPreflight(unittest.TestCase):
    """引擎的真服务起不动时，`server_ready` 要说清缺什么（毫秒级，不起进程、不联网）。"""

    def temp(self, prefix: str) -> Path:
        return Path(tempfile.mkdtemp(prefix=prefix))

    def test_missing_package_is_named(self):
        ok, why = server_ready(tree(self.temp("neo-harness-miss-"),
                                      "import zzz_not_installed_pkg\n"))
        self.assertFalse(ok)
        self.assertIn("zzz_not_installed_pkg", why)
        self.assertIn("requirements.txt", why, "要告诉用户去哪补依赖")

    def test_stdlib_imports_are_not_reported_missing(self):
        ok, why = server_ready(tree(self.temp("neo-harness-stdlib-"),
                                    "import json, socket, threading\nfrom pathlib import Path\n"))
        self.assertTrue(ok, why)

    def test_tree_without_an_engine_says_so(self):
        ok, why = server_ready(self.temp("neo-harness-none-"))
        self.assertFalse(ok)
        self.assertIn("setup.py", why)

    def test_the_engine_we_run_against_is_stated(self):
        """当前引擎根要么起得来，要么给出一句原因——不许"起不来也没话说"。"""
        ok, why = server_ready(ENGINE)
        self.assertIsInstance(ok, bool)
        self.assertEqual(bool(why), not ok, "起得来说空话，起不来要给原因")


if __name__ == "__main__":
    unittest.main(verbosity=2)
