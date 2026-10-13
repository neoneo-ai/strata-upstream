"""neo/tests/test_paths.py - 根目录解析：Neo 到底把哪一份 Strata 当引擎。

规则在 neo/paths.py 的 strata_root()：--root > NEO_STRATA_ROOT > 应用目录本身 >
应用目录/strata > 都没有就回应用目录并说明"这里还没有引擎"。
两条要守住的：

    1. exe 放进 Strata 根目录时（今天所有用户的装法），解析结果必须与改动前逐字节一致；
    2. Neo 单独装在一个空目录里时，认它旁边的 strata/（向导 clone 出来的那一份），
       且那个名字不能与上游 setup.py 的引擎二进制目录 engine/ 撞。

不联网、不碰 GPU、不写任何真实目录（全在 tempfile 里造）。
"""
from __future__ import annotations

import unittest

if __package__ in (None, ""):                       # 直接跑 python neo/tests/test_paths.py 时没有父包
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parents[2]))
    __package__ = "neo.tests"

import tempfile                                      # noqa: E402
from pathlib import Path                             # noqa: E402

from ._bootstrap import ENGINE, REPO                # noqa: E402
from neo import engine_setup, manager, paths, state   # noqa: E402


def engine_dir_ignored(root):
    """这棵树里，引擎源码树目录（`strata/`）被 git 忽略了吗。问不到回 None。

    问的是 git 自己（`git check-ignore`），不是读 `.gitignore` 的文本：
    开发树里那一行在 `.gitignore`（Neo 仓库自己的文件），
    部署树里那一行在 `.git/info/exclude`（上游的 `.gitignore` 是跟踪文件，Neo 不动它）。
    `--no-index` + 目录里一个不存在的路径：不用真 clone 一份才问得出。
    """
    if not engine_setup.git_available():
        return None
    rc, _ = engine_setup.git_run(["git", "check-ignore", "-q", "--no-index",
                                  f"{paths.ENGINE_DIR_NAME}/setup.py"], cwd=root)
    if rc not in (0, 1):
        return None
    return rc == 0


def make_engine_root(base: Path) -> Path:
    """造一个"看起来是 Strata 根目录"的壳：strata_root() 认的那两样都在。"""
    (base / "serve").mkdir(parents=True, exist_ok=True)
    (base / "setup.py").write_text("print('setup')\n", encoding="utf-8")
    (base / "serve" / "server.py").write_text("print('server')\n", encoding="utf-8")
    return base


class TestMarkers(unittest.TestCase):
    def test_both_markers_needed(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            self.assertFalse(paths.is_strata_root(base), "空目录不是引擎根目录")
            (base / "setup.py").write_text("x\n", encoding="utf-8")
            self.assertFalse(paths.is_strata_root(base), "只有 setup.py 不算：起服务靠 serve/server.py")
            make_engine_root(base)
            self.assertTrue(paths.is_strata_root(base))

    def test_marker_list_is_the_two_files_neo_runs(self):
        self.assertEqual(paths.ENGINE_MARKERS, ("setup.py", "serve/server.py"))

    def test_engine_dir_name_is_not_the_binaries_folder(self):
        """向导的源码树叫 strata/，不叫 engine/：上游 setup.py 的 engine/ 是引擎二进制的家。"""
        self.assertEqual(paths.ENGINE_DIR_NAME, "strata")
        self.assertNotEqual(paths.ENGINE_DIR_NAME, "engine")
        ignored = engine_dir_ignored(REPO)
        if ignored is None:
            self.skipTest("这一条要 git（git check-ignore 问这棵树忽略不忽略）")
        self.assertTrue(ignored, "引擎源码树要被 git 忽略：它是 clone 出来的。"
                                 "开发树靠 .gitignore 的 /strata/，"
                                 "部署树靠 .git/info/exclude（neo/deploy.py 补的）")


class TestResolutionOrder(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.app = self.base / "app"
        self.app.mkdir()
        self.pinned = make_engine_root(self.app / "strata")
        self.inplace = make_engine_root(self.base / "strata-tree")
        self.named = make_engine_root(self.base / "somewhere-else")

    def test_explicit_root_wins_and_is_not_marker_checked(self):
        root, why = paths.strata_root(explicit=str(self.named), env={}, app=self.app)
        self.assertEqual(root, self.named)
        self.assertIn("--root", why)
        bare = self.base / "bare"
        bare.mkdir()
        root, _ = paths.strata_root(explicit=str(bare), env={}, app=self.app)
        self.assertEqual(root, bare, "用户点名的目录就照用，不替他挑")

    def test_env_beats_the_app_dir(self):
        root, why = paths.strata_root(env={paths.ENGINE_ENV: str(self.named)}, app=self.app)
        self.assertEqual(root, self.named)
        self.assertIn(paths.ENGINE_ENV, why)

    def test_app_dir_with_markers_wins_over_checkout(self):
        """exe 放进 Strata 根目录时（今天的装法）：结果就是 exe 所在目录，一个字都不变。"""
        root, why = paths.strata_root(env={}, app=self.inplace)
        self.assertEqual(root, self.inplace)
        self.assertIn("根目录", why)

    def test_checkout_used_when_app_is_bare(self):
        root, why = paths.strata_root(env={}, app=self.app)
        self.assertEqual(root, self.pinned)
        self.assertIn("向导", why)

    def test_no_engine_anywhere_returns_app_with_a_reason(self):
        bare = self.base / "empty-app"
        bare.mkdir()
        root, why = paths.strata_root(env={}, app=bare)
        self.assertEqual(root, bare)
        self.assertIn("还没有引擎", why)

    def test_no_engine_leaves_the_manager_default_usable(self):
        """没有引擎时 --root 默认值仍是一个能用的目录：Neo 的窗口要能起来，向导才有地方跑。"""
        bare = self.base / "empty-app2"
        bare.mkdir()
        root, _ = paths.strata_root(env={}, app=bare)
        self.assertTrue(root.is_dir())

    def test_env_is_honored_verbatim(self):
        """NEO_STRATA_ROOT 与 --root 一样是用户点名：指哪儿起哪儿，Neo 不偷偷换目录。"""
        empty = self.base / "empty-target"
        empty.mkdir()
        root, why = paths.strata_root(env={paths.ENGINE_ENV: str(empty)}, app=self.inplace)
        self.assertEqual(root, empty)
        self.assertIn(paths.ENGINE_ENV, why)


class TestAppRoot(unittest.TestCase):
    def test_source_run_app_root_is_the_repo_root(self):
        self.assertEqual(paths.app_root(), REPO)

    def test_engine_checkout_is_beside_the_app(self):
        self.assertEqual(paths.engine_checkout(), REPO / paths.ENGINE_DIR_NAME)

    def test_engine_env_name(self):
        self.assertEqual(paths.ENGINE_ENV, "NEO_STRATA_ROOT")

    def test_real_tree_today(self):
        """今天这份开发树：引擎根目录就是仓库根（exe 与源码都放在 Strata 根目录里）。"""
        root, why = paths.strata_root()
        self.assertEqual(root, REPO)
        self.assertTrue(paths.is_strata_root(root), f"{root} 里该有 setup.py 与 serve/server.py：{why}")
        self.assertEqual(ENGINE, root, "_bootstrap.ENGINE 与运行时解析必须是同一个目录")


class TestWiring(unittest.TestCase):
    """neo/manager.py 与 neo/state.py 用的是同一条规则，不许各算各的。"""

    def test_manager_default_root_matches(self):
        self.assertEqual(Path(manager.default_root()), paths.strata_root()[0])

    def test_state_root_matches(self):
        self.assertEqual(state.ROOT, paths.strata_root()[0])

    def test_default_root_is_a_string(self):
        self.assertIsInstance(manager.default_root(), str)


class TestSafeResolveStillApplies(unittest.TestCase):
    def test_relative_explicit_is_resolved_against_the_app(self):
        with tempfile.TemporaryDirectory() as td:
            app = Path(td)
            root, _ = paths.strata_root(explicit=".", env={}, app=app)
            self.assertTrue(Path(root).is_absolute())
            self.assertEqual(Path(root), paths.safe_resolve(app))

    def test_env_value_is_resolved(self):
        with tempfile.TemporaryDirectory() as td:
            env = {paths.ENGINE_ENV: td}
            root, _ = paths.strata_root(env=env, app=Path(td).parent)
            self.assertEqual(root, paths.safe_resolve(td))


if __name__ == "__main__":
    unittest.main(verbosity=2)
