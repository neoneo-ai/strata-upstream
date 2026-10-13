"""neo/tests/test_logging.py - 日志与控制台输出：不放大、不刷屏、路径不跟重解析点走。

三条实测来源：
  1. state.log() 以前用文本模式回写日志尾部，\n 变 \r\n，读回来再写就 \r\r\n ——
     每写一次，前面每一行多一个 \r（6 次写入后第一行带 6 个 \r）。
  2. 一条几十万字符的行（RecursionError 的消息、pywebview 的 WinForms 属性链递归日志）
     原样进日志、进 /neo/log、进日志页、进 .bat 打印的尾部 —— 窗口被刷满。
  3. Path.resolve() 跟着 Windows 同步根的重解析点走：os.readlink() 对这种点回
     ".SyncRoot"，realpath 一路拼回去直到 maximum recursion depth exceeded。
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

if __package__ in (None, ""):                       # 直接跑 python neo/tests/test_logging.py 时没有父包
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "neo.tests"

from ._bootstrap import ENGINE, REPO                # noqa: E402

ROOT = REPO                                         # 老写法留下的名字：Neo 仓库的根

from neo import console_io, state                                # noqa: E402
from neo.paths import MAX_DEPTH, is_reparse, safe_resolve        # noqa: E402


class _TtyStream(io.StringIO):
    """假装自己是控制台（isatty 为真），闸门就该包它。"""

    def isatty(self) -> bool:
        return True


class StateDirBase(unittest.TestCase):
    """把 state 的日志落点换到临时目录，不碰 neo/state/。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._logs, self._manager = state.LOGS, state.MANAGER_LOG
        state.LOGS = self.tmp
        state.MANAGER_LOG = self.tmp / "manager.log"

    def tearDown(self):
        state.LOGS, state.MANAGER_LOG = self._logs, self._manager


class TestManagerLog(StateDirBase):
    def test_no_carriage_return_growth(self):
        """回归：连续写 8 条，文件里一个 \r 都不该有（以前第一行会带 7 个 \r）。"""
        for i in range(8):
            state.log(f"msg {i}")
        raw = state.MANAGER_LOG.read_bytes()
        self.assertNotIn(b"\r", raw, "日志里不该有 \\r：控制台里每个 \\r 把光标送回行首")
        self.assertEqual(len(raw.decode("utf-8").strip().split("\n")), 8)
        self.assertIn("msg 7", raw.decode("utf-8"))

    def test_huge_line_is_capped(self):
        """一条 20 万字符的消息进日志：单行被裁，文件不被撑爆。"""
        state.log("x" * 200_000)
        state.log("正常的一行")
        lines = state.MANAGER_LOG.read_text(encoding="utf-8").split("\n")
        self.assertLess(max(len(ln) for ln in lines), console_io.LINE_MAX + 60)
        self.assertIn("截断", "\n".join(lines))
        self.assertIn("正常的一行", "\n".join(lines))

    def test_log_keeps_history_when_rewriting(self):
        """回写尾部不限行数：前面的条数不会因为清洗被丢掉。"""
        for i in range(30):
            state.log(f"第 {i} 条")
        text = state.MANAGER_LOG.read_text(encoding="utf-8")
        self.assertIn("第 0 条", text)
        self.assertIn("第 29 条", text)
        self.assertNotIn("已省略", text)


class TestTailText(StateDirBase):
    def test_crlf_and_control_chars(self):
        p = self.tmp / "dirty.log"
        p.write_bytes(b"line one\r\r\r\nline two\r\n" + b"y" * 90_000 + b"\n")
        text = state.tail_text(p, 200_000, max_lines=50)
        self.assertNotIn("\r", text)
        self.assertIn("line one", text)
        self.assertIn("line two", text)
        self.assertLess(max(len(ln) for ln in text.split("\n")), console_io.LINE_MAX + 60)

    def test_line_count_cap(self):
        p = self.tmp / "many.log"
        p.write_text("\n".join(f"行 {i}" for i in range(5000)) + "\n", encoding="utf-8")
        lines = state.tail_text(p, 200_000, max_lines=100).split("\n")
        self.assertLessEqual(len(lines), 101)
        self.assertIn("已省略", lines[0])
        self.assertEqual(lines[-2], "行 4999")

    def test_missing_file(self):
        self.assertEqual(state.tail_text(self.tmp / "nope.log"), "")


class TestConsoleGate(unittest.TestCase):
    def test_flood_is_bounded(self):
        """5000 条 30 万字符的递归行灌进闸门：出来的输出是有界的，并说明丢了多少。"""
        sink = io.StringIO()
        gate = console_io.BoundedStream(sink, line_max=200, rate_per_s=20, dup_window_s=5.0)
        flood = ".SyncRoot" * 40_000 + ": maximum recursion depth exceeded"
        for _ in range(5000):
            gate.write(flood + "\n")
        gate.flush()
        out = sink.getvalue()
        self.assertLess(len(out), 20_000, f"闸门后 {len(out)} 字符")
        self.assertNotIn("\r", out)
        self.assertLess(max(len(ln) for ln in out.split("\n")), 300)
        self.assertIn("重复", out)
        self.assertNotIn("丢弃", out)     # 同样的行先被折叠，速率上限轮不到

    def test_rate_cap_drops_excess(self):
        """每秒上限：500 条不同的行灌进来，写出的行数远小于 500，并说明丢了多少。"""
        sink = io.StringIO()
        gate = console_io.BoundedStream(sink, line_max=100, rate_per_s=20, dup_window_s=0.0)
        for i in range(500):
            gate.write(f"第 {i} 行\n")
        gate.flush()
        out = sink.getvalue()
        self.assertLess(len(out.split("\n")), 120)
        self.assertIn("丢弃", out)
        self.assertIn("第 19 行", out)      # 预算用完前写出的最后一行
        self.assertNotIn("第 499 行", out)  # 超预算的行被丢掉并计入说明

    def test_one_print_block_passes_whole(self):
        """一次 print 的 40 行（CLI 的 JSON、接入块）不被裁成半截。"""
        sink = io.StringIO()
        gate = console_io.BoundedStream(sink, line_max=100, rate_per_s=20, dup_window_s=0.0)
        block = "".join(f"第 {i} 行\n" for i in range(40))
        gate.write(block)
        gate.flush()
        out = sink.getvalue()
        self.assertNotIn("丢弃", out)
        self.assertIn("第 0 行", out)
        self.assertIn("第 39 行", out)
        self.assertEqual(len([x for x in out.split("\n") if x.startswith("第")]), 40)

    def test_burst_allowance_is_also_capped(self):
        """整块放行不是免检：一秒内两块 40 行，第二块超出额度照样丢。"""
        sink = io.StringIO()
        gate = console_io.BoundedStream(sink, line_max=100, rate_per_s=20, dup_window_s=0.0,
                                        burst_lines=30)
        gate.write("".join(f"A{i}\n" for i in range(40)))
        gate.write("".join(f"B{i}\n" for i in range(40)))
        gate.flush()
        out = sink.getvalue()
        self.assertIn("A0", out)
        self.assertIn("丢弃", out)
        self.assertNotIn("B39", out)

    def test_normal_output_untouched(self):
        sink = io.StringIO()
        gate = console_io.BoundedStream(sink, rate_per_s=1000)
        gate.write("Neo 控制台：http://127.0.0.1:8083/\n")
        gate.write("没有换行尾的半行")
        gate.flush()
        self.assertIn("Neo 控制台：http://127.0.0.1:8083/", sink.getvalue())
        self.assertIn("没有换行尾的半行", sink.getvalue())

    def test_clean_line(self):
        self.assertEqual(console_io.clean_line("a\r\nb"), "a b")
        self.assertEqual(console_io.clean_line("a\rb"), "a b")
        self.assertEqual(console_io.clean_line("a\x07b"), "a b")
        capped = console_io.clean_line("z" * 5000, 400)
        self.assertTrue(capped.startswith("z" * 400))
        self.assertIn("原长 5000 字符", capped)

    def test_install_is_idempotent(self):
        real_out, real_err = sys.stdout, sys.stderr
        first = None
        try:
            sys.stdout, sys.stderr = _TtyStream(), _TtyStream()
            console_io.install()
            first = sys.stdout
            console_io.install()
            self.assertIs(sys.stdout, first, "已经装过就不重复包一层")
            print("闸门装好后还能正常打印")
        finally:
            sys.stdout, sys.stderr = real_out, real_err
        self.assertIsInstance(first, console_io.BoundedStream)

    def test_install_leaves_a_pipe_alone(self):
        """输出被重定向（管道、文件、测试里的 StringIO）时不包闸门：闸门按行裁，
        实测把 python -m neo.connect --json 的 30 行 JSON 丢掉 26 行，页面拿到半截。"""
        real_out, real_err = sys.stdout, sys.stderr
        try:
            sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
            console_io.install()
            self.assertNotIsInstance(sys.stdout, console_io.BoundedStream)
            self.assertNotIsInstance(sys.stderr, console_io.BoundedStream)
        finally:
            sys.stdout, sys.stderr = real_out, real_err


class TestBoundedTraceback(unittest.TestCase):
    def test_recursion_error_is_not_formatted(self):
        """RecursionError：不格式化上千帧的 traceback，只给一句能照着做的提示。"""
        try:
            raise RecursionError(".SyncRoot" * 50_000 + ": maximum recursion depth exceeded")
        except RecursionError as e:
            text = state.format_exception_bounded(RecursionError, e, e.__traceback__)
        self.assertLess(len(text), state.TRACE_MAX)
        self.assertIn("RecursionError", text)
        self.assertIn("同步目录", text)
        self.assertNotIn(".SyncRoot" * 100, text)

    def test_normal_traceback_bounded(self):
        def deep(n):
            if n == 0:
                raise ValueError("x" * 100_000)
            return deep(n - 1)
        try:
            deep(300)
        except ValueError as e:
            text = state.format_exception_bounded(ValueError, e, e.__traceback__)
        self.assertLess(len(text), state.TRACE_MAX)
        self.assertLessEqual(len(text.split("\n")), state.TRACE_LINES)
        self.assertIn("ValueError", text)

    def test_excepthook_installed(self):
        real = sys.excepthook
        try:
            state.install_excepthook()
            self.assertIsNot(sys.excepthook, real)
            sys.excepthook(ZeroDivisionError, ZeroDivisionError("x" * 90_000), None)
        finally:
            sys.excepthook = real


class TestSafeResolve(unittest.TestCase):
    def test_sync_root_chain_is_truncated(self):
        """os.readlink() 对同步根回 ".SyncRoot"：拼出来的长路径被截断，不递归。"""
        out = safe_resolve("D:\\Work\\Strata" + ("\\.SyncRoot" * 5000))
        self.assertLess(len(str(out).split("\\")), MAX_DEPTH + 4)
        self.assertTrue(str(out).startswith("D:\\Work\\Strata"))

    def test_relative_and_dotdot(self):
        base = Path("D:\\Work\\Strata")
        self.assertEqual(safe_resolve("serve\\server.py", base),
                         Path("D:\\Work\\Strata\\serve\\server.py"))
        self.assertEqual(safe_resolve("..\\other", base), Path("D:\\Work\\other"))

    def test_does_not_touch_filesystem(self):
        out = safe_resolve("D:\\definitely\\not\\here\\x.json")
        self.assertEqual(str(out), "D:\\definitely\\not\\here\\x.json")

    def test_is_reparse_on_plain_dir(self):
        tmp = Path(tempfile.mkdtemp())
        self.assertFalse(is_reparse(tmp))
        self.assertFalse(is_reparse(tmp / "nope"))


class TestInstance(unittest.TestCase):
    """neo/state/instance.json - "已经有一个在跑了"时要说清是谁。

    实测 2026-10-12：pywebview 缺了时 Neo 走浏览器界面，没有窗口，进程在后台挂着；
    用户看不到任何东西，再开一次只看到"已经在运行了"，不知道找谁。
    """

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="neo-inst-"))
        self._instance = state.INSTANCE
        state.INSTANCE = self.tmp / "instance.json"

    def tearDown(self):
        state.INSTANCE = self._instance

    def test_record_read_clear(self):
        state.record_instance(root=str(self.tmp), strata_port=8080,
                              console_url="http://127.0.0.1:8083/", mode="window")
        info = state.instance_info()
        self.assertEqual(info["root"], str(self.tmp))
        self.assertEqual(info["pid"], os.getpid(), "没给 pid 就用当前进程")
        self.assertEqual(info["mode"], "window")
        state.clear_instance()
        self.assertEqual(state.instance_info(), {})

    def test_record_merges(self):
        """起窗口前先记一次，起窗口后只补 mode/url，不能把 root 抹了。"""
        state.record_instance(root=str(self.tmp), strata_port=8080)
        state.record_instance(mode="browser", console_url="http://127.0.0.1:8083/")
        info = state.instance_info()
        self.assertEqual(info["root"], str(self.tmp))
        self.assertEqual(info["mode"], "browser")

    def test_alive_with_own_pid(self):
        state.record_instance(root=str(self.tmp))
        self.assertTrue(state.instance_alive())

    def test_stale_record_is_cleared(self):
        from neo.tests.test_supervisor import dead_pid
        # record_instance() 的 pid 永远是当前进程；这里要的是"记录里那个进程已经没了"
        state.write_json(state.INSTANCE, {"pid": dead_pid(), "root": str(self.tmp)})
        self.assertFalse(state.instance_alive(), "进程没了就是过期记录")
        state.clear_instance()
        self.assertEqual(state.instance_info(), {}, "清掉之后读出来是空的")


if __name__ == "__main__":
    unittest.main()
