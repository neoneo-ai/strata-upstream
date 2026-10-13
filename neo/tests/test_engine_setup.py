"""neo/tests/test_engine_setup.py - 引擎源码树向导：pin 的形状、plan 的每个状态、zip 的摘要闸门。

这一份不联网、不碰 GPU、不动任何真实目录：
  * 引擎树一律在 tempfile 里造（空的 / 有 .git 的 / 脏的 / 按 zip 装的）；
  * Neo 记的那一笔（neo/state/engine.json）与日志（neo/state/logs/engine.log）
    都把 state.STATE / state.LOGS 指到 tempfile，测试不写用户的状态；
  * netprobe.probe_cached 换成假的，测试里一次 443 都不连；
  * run_engine 只测"拒绝"与"zip 那条路"，测不到 git clone（那要联网）。

plan() 是纯函数，所以每个状态都能直接钉住：ready / clone / zip_copy / need_zip /
checkout_pin / dirty / not_a_git_tree。
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

if __package__ in (None, ""):                       # 直接跑 python neo/tests/test_engine_setup.py 时没有父包
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "neo.tests"

from ._bootstrap import ENGINE, REPO                # noqa: E402
from neo import engine_setup, netprobe, paths, state       # noqa: E402

FAKE_URL = "https://github.com/Niko1221/Strata"


def make_ref(sha=None, zip_sha="", **over) -> dict:
    ref = {"url": FAKE_URL, "tag": "v0.1.43.1",
           "sha": sha or ("a" * 40), "zip_sha256": zip_sha, "zip_bytes": 0, "note": ""}
    ref.update(over)
    return ref


def make_zip(path: Path, top: str = "Strata-0.1.43.1", files=("setup.py", "serve/server.py")) -> Path:
    """一份 GitHub archive 形状的 zip：一个顶层目录，里面是源码。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name in files:
            archive.writestr(f"{top}/{name}", "print('x')\n")
        archive.writestr(f"{top}/README.md", "x\n")
    return path


def make_engine_tree(base: Path) -> Path:
    """一个"看着像 Strata"的目录（没有 .git）。"""
    (base / "serve").mkdir(parents=True, exist_ok=True)
    (base / "setup.py").write_text("print('setup')\n", encoding="utf-8")
    (base / "serve" / "server.py").write_text("print('server')\n", encoding="utf-8")
    return base


def make_git_tree(base: Path) -> str:
    """真的 git 树：git init + 一次提交，回那个 commit 的 SHA。没有 git 回空串。"""
    make_engine_tree(base)
    if not engine_setup.git_available():
        return ""

    def run(*args):
        return engine_setup.git_run(["git", *args], cwd=base)

    if run("init", "-q", "-b", "main", ".")[0]:
        return ""
    run("add", "-A")
    if run("-c", "user.email=neo@test", "-c", "user.name=neo", "commit", "-q", "-m", "init")[0]:
        return ""
    return engine_setup.git_head(base)


def make_second_commit(base: Path) -> str:
    """在 make_git_tree 那棵树上再提交一次，回新的 HEAD（旧的那个 commit 还在，能切回去）。"""
    (base / "第二版.txt").write_text("第二个 commit\n", encoding="utf-8")
    engine_setup.git_run(["git", "add", "-A"], cwd=base)
    engine_setup.git_run(["git", "-c", "user.email=neo@test", "-c", "user.name=neo",
                          "commit", "-q", "-m", "second"], cwd=base)
    return engine_setup.git_head(base)


def wide_console():
    """cp936 的字节控制台当 stdout（与 `test_deploy.py` 里那条一样）：中文装得下，U+FFFD 装不下。"""
    import sys

    buffer = io.BytesIO()
    wrapper = io.TextIOWrapper(buffer, encoding="cp936", errors="strict")
    was = sys.stdout
    sys.stdout = wrapper

    def restore():
        if sys.stdout is wrapper:
            sys.stdout.close()
        sys.stdout = was

    return buffer, restore


def quiet(argv):
    """跑 CLI，把 stdout 收进字符串（页面与终端都读这一份输出）。"""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = engine_setup.main(argv)
    return code, buffer.getvalue()


class StateDirBase(unittest.TestCase):
    """把 Neo 的状态目录与日志目录指进 tempfile：测试不碰用户的 neo/state/。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self._state = state.STATE
        self._logs = state.LOGS
        state.STATE = self.tmp / "state"
        state.LOGS = state.STATE / "logs"
        self.addCleanup(self._restore_state)
        self._probe = netprobe.probe_cached

        def fake_probe(timeout=3.0, force=False, connect=None):
            return {"targets": [{"host": "github.com", "what": "github", "ok": True, "ms": 20.0},
                                {"host": "huggingface.co", "what": "huggingface", "ok": True,
                                 "ms": 30.0}],
                    "ok": ["github.com", "huggingface.co"], "source": "huggingface",
                    "reason": "测试给的假探测", "at": 0.0}
        netprobe.probe_cached = fake_probe
        self.addCleanup(self._restore_probe)

    def _restore_state(self):
        state.STATE = self._state
        state.LOGS = self._logs

    def _restore_probe(self):
        netprobe.probe_cached = self._probe


class TestPin(unittest.TestCase):
    def test_the_shipped_pin_reads(self):
        ref = engine_setup.load_ref()
        self.assertEqual(ref["url"], FAKE_URL)
        self.assertTrue(ref["tag"].startswith("v"), "tag 是上游的 release tag（v0.1.43.1 那一类）")
        self.assertEqual(len(ref["sha"]), 40)
        self.assertEqual(len(ref["zip_sha256"]), 64, "zip 退路要有摘要，没有摘要就不认本地 zip")
        self.assertGreater(ref["zip_bytes"], 0)

    def test_pin_file_sits_in_neo(self):
        self.assertEqual(engine_setup.ref_file(), REPO / "neo" / "engine_ref.json")
        self.assertTrue(engine_setup.ref_file().is_file())

    def test_missing_key_raises(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ref.json"
            path.write_text(json.dumps({"url": FAKE_URL, "tag": "v1"}), encoding="utf-8")
            with self.assertRaises(engine_setup.EngineRefError):
                engine_setup.load_ref(path)

    def test_bad_sha_raises(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ref.json"
            path.write_text(json.dumps({"url": FAKE_URL, "tag": "v1", "sha": "not-a-sha"}),
                            encoding="utf-8")
            with self.assertRaises(engine_setup.EngineRefError):
                engine_setup.load_ref(path)

    def test_bad_zip_digest_raises(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ref.json"
            path.write_text(json.dumps({"url": FAKE_URL, "tag": "v1", "sha": "a" * 40,
                                        "zip_sha256": "deadbeef"}), encoding="utf-8")
            with self.assertRaises(engine_setup.EngineRefError):
                engine_setup.load_ref(path)

    def test_not_a_dict_raises(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ref.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(engine_setup.EngineRefError):
                engine_setup.load_ref(path)


class TestCommands(unittest.TestCase):
    def setUp(self):
        self.ref = make_ref(sha="b" * 40)

    def test_archive_url_is_the_tag_zip(self):
        self.assertEqual(engine_setup.archive_url(self.ref),
                         f"{FAKE_URL}/archive/refs/tags/v0.1.43.1.zip")

    def test_clone_is_shallow_and_pinned_to_the_tag(self):
        argv = engine_setup.clone_argv(self.ref, "D:/Neo/strata")
        self.assertEqual(argv[:2], ["git", "clone"])
        self.assertIn("--branch", argv)
        self.assertIn("v0.1.43.1", argv)
        self.assertIn("--depth", argv)
        self.assertIn("1", argv)
        self.assertIn("--single-branch", argv)
        self.assertEqual(Path(argv[-1]), Path("D:/Neo/strata"))

    def test_checkout_detaches_to_the_pinned_sha(self):
        self.assertEqual(engine_setup.checkout_argv(self.ref),
                         ["git", "checkout", "--detach", "b" * 40])


NEEDS_GIT = unittest.skipUnless(engine_setup.git_available(), "这一条要 git")


class TestPlan(StateDirBase):
    def test_missing_tree_with_git_wants_clone(self):
        p = engine_setup.plan(dest=self.tmp / "strata", ref=make_ref(), git_ok=True)
        self.assertEqual(p["state"], engine_setup.STATE_CLONE)
        self.assertIn("--depth", " ".join(engine_setup.clone_argv(make_ref(), p["dest"])))

    def test_missing_tree_without_git_with_a_good_zip_wants_zip(self):
        zip_path = make_zip(self.tmp / "s.zip")
        ref = make_ref(zip_sha=engine_setup.sha256_file(zip_path))
        p = engine_setup.plan(dest=self.tmp / "strata", ref=ref, git_ok=False,
                              local_zip=str(zip_path))
        self.assertEqual(p["state"], engine_setup.STATE_ZIP)
        self.assertTrue(p["zip_ok"])

    def test_missing_tree_without_git_or_zip_needs_a_zip(self):
        p = engine_setup.plan(dest=self.tmp / "strata", ref=make_ref(zip_sha="f" * 64),
                              git_ok=False)
        self.assertEqual(p["state"], engine_setup.STATE_NEED_ZIP)
        self.assertIn("archive/refs/tags", p["notes"][0], "要告诉用户去哪儿下载那一份 zip")

    def test_zip_with_the_wrong_digest_is_not_usable(self):
        zip_path = make_zip(self.tmp / "s.zip")
        p = engine_setup.plan(dest=self.tmp / "strata", ref=make_ref(zip_sha="f" * 64),
                              git_ok=False, local_zip=str(zip_path))
        self.assertEqual(p["state"], engine_setup.STATE_NEED_ZIP)
        self.assertIn("SHA-256", p["notes"][0])

    def test_plain_dir_is_not_a_git_tree(self):
        make_engine_tree(self.tmp / "strata")
        p = engine_setup.plan(dest=self.tmp / "strata", ref=make_ref(), git_ok=True)
        self.assertEqual(p["state"], engine_setup.STATE_NOT_GIT)

    def test_zip_installed_tree_is_ready_by_neos_record(self):
        """zip 解出来没有 .git：HEAD 查不了，凭据是 Neo 记的那一笔（摘要对上才写得进去）。"""
        dest = make_engine_tree(self.tmp / "strata")
        ref = make_ref()
        engine_setup.write_engine_state(dest=str(dest), sha=ref["sha"], tag=ref["tag"], source="zip")
        p = engine_setup.plan(dest=dest, ref=ref, git_ok=False)
        self.assertEqual(p["state"], engine_setup.STATE_READY)

    def test_record_for_another_sha_is_not_ready(self):
        dest = make_engine_tree(self.tmp / "strata")
        engine_setup.write_engine_state(dest=str(dest), sha="c" * 40, tag="v0", source="zip")
        p = engine_setup.plan(dest=dest, ref=make_ref(), git_ok=False)
        self.assertEqual(p["state"], engine_setup.STATE_NOT_GIT)

    @NEEDS_GIT
    def test_clean_tree_at_the_pin_is_ready(self):
        head = make_git_tree(self.tmp / "strata")
        self.assertTrue(head, "git init 没跑起来")
        p = engine_setup.plan(dest=self.tmp / "strata", ref=make_ref(sha=head), git_ok=True)
        self.assertEqual(p["state"], engine_setup.STATE_READY)
        self.assertEqual(p["head"], head)

    @NEEDS_GIT
    def test_tree_at_another_commit_wants_checkout(self):
        head = make_git_tree(self.tmp / "strata")
        p = engine_setup.plan(dest=self.tmp / "strata", ref=make_ref(sha="c" * 40), git_ok=True,
                              own=True)
        self.assertEqual(p["state"], engine_setup.STATE_CHECKOUT)
        self.assertEqual(p["head"], head)

    @NEEDS_GIT
    def test_dirty_tree_is_refused_before_anything_else(self):
        """脏树优先报 dirty：HEAD 对不对都轮不到它 —— Neo 不动用户的改动。"""
        make_git_tree(self.tmp / "strata")
        (self.tmp / "strata" / "本地改动.txt").write_text("用户的改动\n", encoding="utf-8")
        p = engine_setup.plan(dest=self.tmp / "strata", ref=make_ref(sha="c" * 40), git_ok=True,
                              own=True)
        self.assertEqual(p["state"], engine_setup.STATE_DIRTY)

    def test_plan_default_dest_is_the_app_strata(self):
        p = engine_setup.plan(ref=make_ref(), git_ok=True)
        # 两种形状走的是同一条规则（paths.strata_root）：Neo 自己的仓库里没有 setup.py，
        # 落点是 <仓库>/strata；跑在完整 Strata 检出里时解析出来的根就是那棵检出本身，
        # 落点就是它 —— 那里再放一个 strata/ 就是把同一份源码复制两遍。
        expected = REPO if paths.is_strata_root(REPO) else REPO / "strata"
        self.assertEqual(Path(p["dest"]), expected)


class TestExternalRoot(StateDirBase):
    """外置引擎目录：这台机器上别处一键装好的 Strata，Neo 用它、不复制它、不擅自搬它的 HEAD。

    外置树与 Neo 自己 clone 的那一份的三点差别，全在这里钉住：
    版本对不上时报 external_tag（不是 checkout_pin）；脏检查只看跟踪文件的改动
    （Neo 自己的文件在那棵树里按设计就是 untracked）；要动它的 HEAD 得显式 align。
    """

    @NEEDS_GIT
    def test_external_tree_at_another_commit_is_not_moved(self):
        head = make_git_tree(self.tmp / "strata")
        p = engine_setup.plan(dest=self.tmp / "strata", ref=make_ref(sha="c" * 40), git_ok=True,
                              own=False)
        self.assertEqual(p["state"], engine_setup.STATE_EXTERNAL)
        self.assertEqual(p["head"], head)
        self.assertIn("align", p["action"], "要说清怎么才允许动它")

    @NEEDS_GIT
    def test_external_tree_with_neos_untracked_files_is_ready(self):
        """部署树里 Neo 的文件是 untracked（AGENTS.md：那边不提交）：不能因此说它脏。"""
        head = make_git_tree(self.tmp / "strata")
        (self.tmp / "strata" / "NeoStrata.exe").write_text("exe\n", encoding="utf-8")
        (self.tmp / "strata" / "neo").mkdir()
        p = engine_setup.plan(dest=self.tmp / "strata", ref=make_ref(sha=head), git_ok=True,
                              own=False)
        self.assertEqual(p["state"], engine_setup.STATE_READY, p["notes"])

    @NEEDS_GIT
    def test_external_tree_with_a_modified_tracked_file_is_dirty(self):
        make_git_tree(self.tmp / "strata")
        (self.tmp / "strata" / "setup.py").write_text("用户改了上游文件\n", encoding="utf-8")
        p = engine_setup.plan(dest=self.tmp / "strata", ref=make_ref(sha="c" * 40), git_ok=True,
                              own=False)
        self.assertEqual(p["state"], engine_setup.STATE_DIRTY)

    @NEEDS_GIT
    def test_run_refuses_an_external_tree_without_align(self):
        head = make_git_tree(self.tmp / "strata")
        out = engine_setup.run_engine(dest=self.tmp / "strata", ref=make_ref(sha="c" * 40),
                                      setup=False, wait=True)
        self.assertEqual(out["code"], 1)
        self.assertEqual(engine_setup.git_head(self.tmp / "strata"), head, "HEAD 一个字都没动")
        self.assertIn("拒绝", engine_setup.log_tail(max_lines=40))

    @NEEDS_GIT
    def test_align_moves_an_external_tree_to_the_pin(self):
        first = make_git_tree(self.tmp / "strata")
        make_second_commit(self.tmp / "strata")
        out = engine_setup.run_engine(dest=self.tmp / "strata", ref=make_ref(sha=first),
                                      setup=False, wait=True, align=True)
        self.assertEqual(out["code"], 0, out.get("notes"))
        self.assertEqual(engine_setup.git_head(self.tmp / "strata"), first,
                         "外置树切到 pin 的那个 commit（用户点了对齐才动）")

    @NEEDS_GIT
    def test_aligning_an_external_tree_does_not_make_it_neos_own(self):
        """对齐完不记来历：记了就等于把用户的检出当自己的收进闸门，脏检查会变严。"""
        first = make_git_tree(self.tmp / "strata")
        make_second_commit(self.tmp / "strata")
        engine_setup.run_engine(dest=self.tmp / "strata", ref=make_ref(sha=first),
                                setup=False, wait=True, align=True)
        self.assertFalse(engine_setup.is_neo_checkout(self.tmp / "strata"))

    def test_set_and_clear_remembered_root(self):
        state.LAUNCHER = state.STATE / "launcher.json"
        out = engine_setup.set_engine_root(str(self.tmp))
        self.assertTrue(out["ok"], out["notes"])
        self.assertEqual(paths.remembered_root(), str(paths.safe_resolve(self.tmp)))
        cleared = engine_setup.clear_engine_root()
        self.assertTrue(cleared["ok"])
        self.assertEqual(paths.remembered_root(), "")

    def test_set_root_refuses_a_directory_that_is_not_there(self):
        out = engine_setup.set_engine_root(str(self.tmp / "没这个目录"))
        self.assertFalse(out["ok"])
        self.assertIn("不存在", out["notes"][0])
        self.assertEqual(paths.remembered_root(), "")

    def test_set_root_says_when_the_markers_are_missing(self):
        (self.tmp / "bare").mkdir()
        out = engine_setup.set_engine_root(str(self.tmp / "bare"))
        self.assertTrue(out["ok"])
        self.assertIn("setup.py", " ".join(out["notes"]), "要说明这一份起不来服务")

    def test_default_dest_follows_the_remembered_root(self):
        """记了外置目录，--ensure 的默认目标就是它：Neo 不再往 <应用目录>/strata 复制一份。"""
        state.LAUNCHER = state.STATE / "launcher.json"
        make_engine_tree(self.tmp)
        engine_setup.set_engine_root(str(self.tmp))
        self.assertEqual(engine_setup.default_dest(), paths.safe_resolve(self.tmp))
        engine_setup.clear_engine_root()
        # 清掉外置目录后回到默认：Neo 自己的仓库里是 <仓库>/strata，完整 Strata 检出里是那棵检出本身
        expected = REPO if paths.is_strata_root(REPO) else REPO / paths.ENGINE_DIR_NAME
        self.assertEqual(engine_setup.default_dest(), expected)

    def test_status_carries_the_root_and_why(self):
        data = engine_setup.status(dest=self.tmp / "strata", ref=make_ref())
        for key in ("root", "root_reason", "remembered", "own"):
            self.assertIn(key, data, f"引擎卡要读 {key}")
        self.assertEqual([x["name"] for x in data["steps"]][0], "引擎目录")

    def test_cli_set_and_clear_root(self):
        state.LAUNCHER = state.STATE / "launcher.json"
        code, out = quiet(["--set-root", str(self.tmp)])
        self.assertEqual(code, 0, out)
        self.assertIn("记住的引擎目录", out)
        code, out = quiet(["--clear-root"])
        self.assertEqual(code, 0, out)
        self.assertIn("已清除", out)




class TestZipGate(StateDirBase):
    def test_digest_match_is_the_only_way_in(self):
        zip_path = make_zip(self.tmp / "s.zip")
        ref = make_ref(zip_sha=engine_setup.sha256_file(zip_path))
        ok, note = engine_setup.check_zip(zip_path, ref)
        self.assertTrue(ok, note)

    def test_no_zip_says_where_to_download(self):
        ok, note = engine_setup.check_zip("", make_ref(zip_sha="f" * 64))
        self.assertFalse(ok)
        self.assertIn("archive/refs/tags", note)

    def test_missing_file_says_so(self):
        ok, note = engine_setup.check_zip(self.tmp / "没有.zip", make_ref(zip_sha="f" * 64))
        self.assertFalse(ok)
        self.assertIn("不在", note)

    def test_pin_without_a_digest_refuses_every_zip(self):
        zip_path = make_zip(self.tmp / "s.zip")
        ok, note = engine_setup.check_zip(zip_path, make_ref(zip_sha=""))
        self.assertFalse(ok)
        self.assertIn("zip_sha256", note)

    def test_extract_moves_the_top_folder_and_records_the_source(self):
        zip_path = make_zip(self.tmp / "s.zip")
        ref = make_ref(zip_sha=engine_setup.sha256_file(zip_path))
        dest = self.tmp / "strata"
        result = engine_setup.extract_zip(zip_path, dest, ref)
        self.assertTrue(result["ok"], result["notes"])
        self.assertTrue((dest / "setup.py").is_file(), "顶层目录要变成 strata/ 本身")
        self.assertFalse((dest / "Strata-0.1.43.1").exists(), "不能套两层")
        self.assertFalse((self.tmp / ".strata.unpack").exists(), "临时目录要清掉")
        self.assertEqual(engine_setup.read_engine_state().get("source"), "zip")
        self.assertEqual(engine_setup.plan(dest=dest, ref=ref, git_ok=False)["state"],
                         engine_setup.STATE_READY)

    def test_extract_refuses_a_digest_that_does_not_match(self):
        zip_path = make_zip(self.tmp / "s.zip")
        dest = self.tmp / "strata"
        result = engine_setup.extract_zip(zip_path, dest, make_ref(zip_sha="f" * 64))
        self.assertFalse(result["ok"])
        self.assertFalse(dest.exists(), "摘要不对时一个文件都不该解出来")

    def test_extract_refuses_an_existing_dest(self):
        zip_path = make_zip(self.tmp / "s.zip")
        ref = make_ref(zip_sha=engine_setup.sha256_file(zip_path))
        make_engine_tree(self.tmp / "strata")
        result = engine_setup.extract_zip(zip_path, self.tmp / "strata", ref)
        self.assertFalse(result["ok"])
        self.assertIn("已经在了", result["notes"][0])

    def test_extract_refuses_a_zip_with_two_top_dirs(self):
        path = self.tmp / "two.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("A/setup.py", "x\n")
            archive.writestr("B/setup.py", "x\n")
        ref = make_ref(zip_sha=engine_setup.sha256_file(path))
        result = engine_setup.extract_zip(path, self.tmp / "strata", ref)
        self.assertFalse(result["ok"])
        self.assertIn("不是一个顶层目录", result["notes"][0])


class TestRunAndPage(StateDirBase):
    @NEEDS_GIT
    def test_refuses_a_dirty_tree_and_touches_nothing(self):
        make_git_tree(self.tmp / "strata")
        keep = self.tmp / "strata" / "本地改动.txt"
        keep.write_text("用户的改动\n", encoding="utf-8")
        result = engine_setup.run_engine(dest=self.tmp / "strata", ref=make_ref(sha="c" * 40),
                                         setup=False, wait=True)
        self.assertEqual(result["code"], 1)
        self.assertEqual(keep.read_text(encoding="utf-8"), "用户的改动\n")
        self.assertIn("拒绝", engine_setup.log_tail(max_lines=5))

    def test_refuses_a_plain_directory(self):
        make_engine_tree(self.tmp / "strata")
        result = engine_setup.run_engine(dest=self.tmp / "strata", ref=make_ref(),
                                         setup=False, wait=True)
        self.assertEqual(result["code"], 1)

    def test_zip_path_installs_the_tree_and_records_it(self):
        zip_path = make_zip(self.tmp / "s.zip")
        ref = make_ref(zip_sha=engine_setup.sha256_file(zip_path))
        result = engine_setup.run_engine(dest=self.tmp / "strata", ref=ref,
                                         local_zip=str(zip_path), setup=False, wait=True)
        self.assertEqual(result["code"], 0, result.get("notes"))
        self.assertTrue((self.tmp / "strata" / "setup.py").is_file())
        self.assertEqual(engine_setup.read_engine_state().get("sha"), ref["sha"])
        self.assertTrue(engine_setup.engine_log_path().is_file(), "每一步要进 neo/state/logs/engine.log")

    def test_status_has_the_shape_the_page_reads(self):
        make_engine_tree(self.tmp / "strata")
        data = engine_setup.status(dest=self.tmp / "strata", ref=make_ref())
        for key in ("dest", "state", "action", "tag", "sha", "head", "url", "archive",
                    "git", "root", "root_reason", "remembered", "own",
                    "clone_command", "checkout_command", "steps", "ready",
                    "notes", "run", "log_tail"):
            self.assertIn(key, data, f"页面要读 {key}")
        self.assertEqual([x["name"] for x in data["steps"]],
                         ["引擎目录", "引擎源码", "取源码的路", "交给 setup.py"])
        self.assertFalse(data["ready"], "源码没到位时不能报 ready")

    def test_cli_dry_run_prints_the_commands_and_writes_nothing(self):
        code, out = quiet(["--dest", str(self.tmp / "strata"), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("git clone", out)
        self.assertIn("archive/refs/tags", out)
        self.assertFalse(engine_setup.engine_state_path().exists(), "看现状不该写 Neo 的状态")
        self.assertFalse((self.tmp / "strata").exists())

    def test_cli_json_is_json(self):
        code, out = quiet(["--dest", str(self.tmp / "strata"), "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["dest"], str(self.tmp / "strata"))
        self.assertIn(data["state"], engine_setup.STATE_ACTION)

    @NEEDS_GIT
    def test_cli_prints_a_log_tail_the_console_codepage_cannot_hold(self):
        """`engine.log` 里有 cp936 装不下的字节时，`--ensure` 末尾那段要打印出来。

        2026-10-13 实测：`neo/deploy.py` 打印子进程输出尾部时抛
        UnicodeEncodeError（子进程的输出按 utf-8 读，坏字节变成 U+FFFD，
        Windows 中文控制台默认 cp936 装不下它）。这一条把 stdout 换成只认 cp936 的流
        模拟那种控制台：源码树已就位（HEAD 就是 pin），`--ensure` 不联网、不跑 setup.py，
        只看它是把话说完，还是崩在打印上。
        """
        import sys

        head = make_git_tree(self.tmp / "strata")
        self.assertTrue(head, "这一条要 git")
        ref = make_ref(sha=head)
        ref_path = self.tmp / "ref.json"
        ref_path.write_text(json.dumps(ref), encoding="utf-8")
        engine_setup.engine_log_path().parent.mkdir(parents=True, exist_ok=True)
        engine_setup.engine_log_path().write_bytes(
            b"clone \xb2\xb0\xd3\xa3 \xd6\xd0\xce\xc4\n")      # cp936 的字节，utf-8 读不通
        buffer, restore = wide_console()
        self.addCleanup(restore)
        code = engine_setup.main(["--dest", str(self.tmp / "strata"),
                                  "--ref", str(ref_path), "--ensure", "--no-setup"])
        sys.stdout.flush()
        out = buffer.getvalue().decode("utf-8", "replace")
        self.assertEqual(code, 0, out)
        self.assertIn("clone", out)
        self.assertIn("\ufffd", out, "坏字节要变成 U+FFFD 打出来，不能崩")


    def test_cli_reports_a_pin_it_cannot_read(self):
        bad = self.tmp / "ref.json"
        bad.write_text("{}", encoding="utf-8")
        code, out = quiet(["--ref", str(bad), "--check"])
        self.assertEqual(code, 1)
        self.assertIn("pin", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)

