"""neo/tests/test_deploy.py - 换版脚本：三道闸门、清单、写完的 SHA-256 核对。

全在 tempfile 里造两棵树（开发树 / 部署树），不碰 D:\\Work\\Strata，不跑 webcheck 与套件
（post_checks 一律 tests=False 关掉），不联网。部署树是真的 git 检出（git init + 一次提交），
所以引擎闸门三条都能真跑：HEAD 对不对 pin、porcelain 空不空、没有 .git 时怎么报。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

if __package__ in (None, ""):                       # 直接跑 python neo/tests/test_deploy.py 时没有父包
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "neo.tests"

from ._bootstrap import ENGINE, REPO                # noqa: E402
from neo import deploy, engine_setup                # noqa: E402

NEEDS_GIT = unittest.skipUnless(engine_setup.git_available(), "这一条要 git")


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_source(base: Path) -> Path:
    """一棵像样的开发树：neo/ 源码 + 页面 + bat + exe，外加不该带走的东西。"""
    write(base / "neo" / "paths.py", "X = 1\n")
    write(base / "neo" / "manager.py", "Y = 2\n")
    write(base / "neo" / "web" / "app.js", "const a = 1;\n")
    write(base / "neo" / "web" / "index.html", "<html></html>\n")
    write(base / "neo" / "requirements-neo.txt", "pywebview\n")
    write(base / "neo" / "state" / "launcher.json", "{}\n")
    write(base / "neo" / "state" / "logs" / "manager.log", "noise\n")
    write(base / "neo" / "__pycache__" / "paths.pyc", "noise\n")
    write(base / "neo" / "dist" / "NeoStrata.exe", "noise\n")
    write(base / "NEO-START.bat", "@echo off\n")
    write(base / "NeoStrata.exe", "fake-exe-bytes\n")
    return base


def make_target(base: Path, commit: bool = True) -> str:
    """一棵像样的部署树：上游文件 + 一次提交（回 commit SHA）。"""
    write(base / "setup.py", "print('setup')\n")
    write(base / "serve" / "server.py", "print('server')\n")
    write(base / "neo" / "paths.py", "X = 1\n")            # 与源树一样：same
    write(base / "neo" / "manager.py", "旧版本\n")          # 与源树不同：update
    write(base / "neo" / "gone.py", "开发树里没有\n")       # extra
    write(base / "neo" / "state" / "launcher.json", "{}\n")  # 不算 Neo 源码
    if not commit:
        return ""
    def run(*args):
        return engine_setup.git_run(["git", *args], cwd=base)
    if run("init", "-q", "-b", "main", ".")[0]:
        return ""
    run("add", "-A")
    if run("-c", "user.email=neo@test", "-c", "user.name=neo", "commit", "-q", "-m", "init")[0]:
        return ""
    return engine_setup.git_head(base)


def quiet(argv):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = deploy.main(argv)
    return code, buffer.getvalue()


class TestFileList(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_state_cache_and_build_products_do_not_travel(self):
        files = deploy.neo_source_files(make_source(self.tmp / "src"))
        self.assertIn("neo/paths.py", files)
        self.assertIn("neo/web/app.js", files)
        self.assertIn("neo/requirements-neo.txt", files)
        self.assertIn("NEO-START.bat", files)
        joined = "\n".join(files)
        for junk in ("neo/state/", "__pycache__", "neo/dist/", "neo/build/", ".pyc"):
            self.assertNotIn(junk, joined, f"{junk} 不该进部署清单")

    def test_the_exe_is_not_in_the_source_list(self):
        """exe 走自己那一条（要 --exe 指，或开发树根目录那一个），不混在源码清单里。"""
        files = deploy.neo_source_files(make_source(self.tmp / "src"))
        self.assertNotIn("NeoStrata.exe", files)


class TestEngineGate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_no_git_is_not_a_clean_tree(self):
        gate = deploy.engine_gate(self.tmp / "plain", ref=engine_setup.load_ref())
        self.assertFalse(gate["ok"])
        self.assertIn("git", gate["notes"][0])

    @NEEDS_GIT
    def test_head_at_the_pin_and_clean_passes(self):
        head = make_target(self.tmp / "dst")
        gate = deploy.engine_gate(self.tmp / "dst", ref={"sha": head, "tag": "vX", "url": "u",
                                                        "zip_sha256": "", "zip_bytes": 0,
                                                        "note": ""})
        self.assertTrue(gate["ok"], gate["notes"])
        self.assertEqual(gate["head"], head)

    @NEEDS_GIT
    def test_head_not_at_the_pin_fails(self):
        head = make_target(self.tmp / "dst")
        gate = deploy.engine_gate(self.tmp / "dst", ref={"sha": "c" * 40, "tag": "vX", "url": "u",
                                                        "zip_sha256": "", "zip_bytes": 0,
                                                        "note": ""})
        self.assertFalse(gate["ok"])
        self.assertIn(head[:12], gate["notes"][0])

    @NEEDS_GIT
    def test_dirty_tree_fails(self):
        make_target(self.tmp / "dst")
        write(self.tmp / "dst" / "setup.py", "有人改了上游\n")
        head = engine_setup.git_head(self.tmp / "dst")
        gate = deploy.engine_gate(self.tmp / "dst", ref={"sha": head, "tag": "vX", "url": "u",
                                                        "zip_sha256": "", "zip_bytes": 0,
                                                        "note": ""})
        self.assertFalse(gate["ok"])
        self.assertIn("porcelain", gate["notes"][0])

    @NEEDS_GIT
    def test_untracked_neo_files_do_not_make_the_tree_dirty(self):
        """部署树里 Neo 的文件按设计就是 untracked（AGENTS.md：那边不提交）：闸门只看跟踪文件。"""
        make_target(self.tmp / "dst")
        write(self.tmp / "dst" / "NeoStrata.exe", "untracked exe\n")
        write(self.tmp / "dst" / "neo" / "web" / "app.js", "untracked page\n")
        head = engine_setup.git_head(self.tmp / "dst")
        gate = deploy.engine_gate(self.tmp / "dst", ref=ref_at(head))
        self.assertTrue(gate["ok"], gate["notes"])


def ref_at(sha: str) -> dict:
    return {"url": "https://github.com/Niko1221/Strata", "tag": "vX", "sha": sha,
            "zip_sha256": "", "zip_bytes": 0, "note": ""}


class TestPlan(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.src = make_source(self.tmp / "src")
        self.dst = self.tmp / "dst"
        self.head = make_target(self.dst)
        self.ref = ref_at(self.head)
        self.exe = str(self.src / "NeoStrata.exe")

    def plan(self):
        return deploy.plan(self.src, self.dst, exe_path=self.exe, ref=self.ref)

    def test_each_file_is_new_update_or_same(self):
        p = self.plan()
        kinds = {x["rel"]: x["kind"] for x in p["actions"]}
        self.assertEqual(kinds["neo/paths.py"], "same")
        self.assertEqual(kinds["neo/manager.py"], "update")
        self.assertEqual(kinds["neo/web/app.js"], "new")
        self.assertTrue(p["ok"], p["notes"])

    def test_extra_files_in_the_target_are_reported(self):
        self.assertEqual(self.plan()["extra"], ["neo/gone.py"])

    def test_a_missing_exe_blocks_the_plan(self):
        p = deploy.plan(self.src, self.dst, exe_path=str(self.tmp / "没有.exe"), ref=self.ref)
        self.assertFalse(p["ok"])
        self.assertIn("不在", " ".join(p["notes"]))

    def test_a_running_instance_blocks_the_plan(self):
        """exe 在跑就锁着文件：instance.json 说它活着，闸门就不给写。"""
        write(self.dst / "neo" / "state" / "instance.json",
              json.dumps({"pid": os.getpid(), "root": str(self.dst)}))
        p = self.plan()
        self.assertFalse(p["ok"])
        self.assertTrue(p["busy"])


class TestApply(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.src = make_source(self.tmp / "src")
        self.dst = self.tmp / "dst"
        self.head = make_target(self.dst)
        self.ref = ref_at(self.head)
        self.exe = str(self.src / "NeoStrata.exe")

    def test_dry_run_writes_nothing(self):
        result = deploy.apply(self.src, self.dst, exe_path=self.exe, ref=self.ref,
                              tests=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["wrote"], [])
        self.assertIn("dry-run", " ".join(result["notes"]))
        self.assertEqual((self.dst / "neo" / "manager.py").read_text(encoding="utf-8"),
                         "旧版本\n")
        self.assertFalse((self.dst / "NeoStrata.exe").exists())

    def test_apply_writes_and_verifies_the_digests(self):
        result = deploy.apply(self.src, self.dst, exe_path=self.exe, ref=self.ref,
                              dry_run=False, tests=False)
        self.assertTrue(result["ok"], result.get("notes"))
        self.assertEqual((self.dst / "neo" / "manager.py").read_text(encoding="utf-8"),
                         "Y = 2\n")
        self.assertEqual((self.dst / "neo" / "web" / "app.js").read_text(encoding="utf-8"),
                         "const a = 1;\n")
        self.assertEqual(deploy.sha256_file(self.dst / "NeoStrata.exe"),
                         deploy.sha256_file(self.src / "NeoStrata.exe"))
        self.assertNotIn("neo/paths.py", result["wrote"], "一样的文件不该重抄")
        self.assertEqual(result["engine_head"], self.head)
        self.assertFalse(list(self.dst.rglob("*.neo-tmp")), "临时文件要清干净")

    def test_apply_refuses_a_tree_that_fails_the_gate(self):
        write(self.dst / "setup.py", "有人改了上游\n")
        result = deploy.apply(self.src, self.dst, exe_path=self.exe, ref=self.ref,
                              dry_run=False, tests=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["wrote"], [])
        self.assertIn("porcelain", " ".join(result["notes"]))

    def test_prune_removes_only_neo_files_the_source_does_not_have(self):
        result = deploy.apply(self.src, self.dst, exe_path=self.exe, ref=self.ref,
                              dry_run=False, prune=True, tests=False)
        self.assertIn("删除 neo/gone.py", result["wrote"])
        self.assertFalse((self.dst / "neo" / "gone.py").exists())
        self.assertTrue((self.dst / "setup.py").is_file(), "上游的文件一个都不动")


class TestCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.src = make_source(self.tmp / "src")
        self.dst = self.tmp / "dst"
        self.head = make_target(self.dst)

    def test_json_plan_is_json_and_says_the_gate_passed(self):
        ref = self.tmp / "ref.json"
        write(ref, json.dumps(ref_at(self.head)))
        code, out = quiet(["--from", str(self.src), "--to", str(self.dst),
                           "--ref", str(ref), "--exe", str(self.src / "NeoStrata.exe"),
                           "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data["plan"]["gate"]["ok"], data["notes"])
        self.assertEqual(data["plan"]["gate"]["head"], self.head)

    def test_the_report_names_the_gate_and_the_counts(self):
        ref = self.tmp / "ref.json"
        write(ref, json.dumps(ref_at(self.head)))
        code, out = quiet(["--from", str(self.src), "--to", str(self.dst),
                           "--ref", str(ref), "--exe", str(self.src / "NeoStrata.exe")])
        self.assertEqual(code, 0)
        self.assertIn("引擎闸门：OK", out)
        self.assertIn("要写的文件", out)
        self.assertIn("neo/gone.py", out)

    def test_a_pin_that_does_not_match_the_target_tree_blocks_the_report(self):
        ref = self.tmp / "ref.json"
        write(ref, json.dumps(ref_at("c" * 40)))
        code, out = quiet(["--from", str(self.src), "--to", str(self.dst),
                           "--ref", str(ref), "--exe", str(self.src / "NeoStrata.exe")])
        self.assertEqual(code, 1)
        self.assertIn("引擎闸门：不过", out)
        self.assertIn(self.head[:12], out)

    def test_to_is_required(self):
        with self.assertRaises(SystemExit):
            quiet(["--json"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
