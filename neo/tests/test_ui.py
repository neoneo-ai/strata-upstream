"""neo/tests/test_ui.py - 窗口线程不被挂住：闸门不排队、日志常态只追加、
UI 看门狗能把卡住的线程现场写进日志、点击路径不经过 js_api。

对应实测的卡死路子（pywebview 6.2.1，WinForms + WebView2）：
  - platforms/edgechromium.py:225 on_script_notify 在窗口线程上跑，里面的 print 走
    neo 的 stdout 闸门；闸门排队等锁 = 窗口线程挂住。
  - platforms/edgechromium.py:137 evaluate_js 用 Invoke 把结果同步投递给窗口线程；
    窗口线程收不到消息，所有 js_api 调用就都不返回 —— 一次点击就卡住后面每一次。
    现在窗口不挂 js_api，页面只发 HTTP（TestClickPathIsHttpOnly 盯着这条）。
  - 窗口线程跑 Python 委托要抢 GIL，长时间握着 GIL 的纯 Python 段都会让它停住
    （以前 state.log() 每条日志都把整个尾部读出来清洗再整份回写，就是这种段）。
"""
from __future__ import annotations

import io
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from neo import console_io, state, ui_watchdog                     # noqa: E402


class StateDirBase(unittest.TestCase):
    """把 state 的日志落点换到临时目录，不碰 neo/state/。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._logs, self._manager = state.LOGS, state.MANAGER_LOG
        state.LOGS = self.tmp
        state.MANAGER_LOG = self.tmp / "manager.log"

    def tearDown(self):
        state.LOGS, state.MANAGER_LOG = self._logs, self._manager


class _LockHolder(threading.Thread):
    """在另一个线程上把锁占住，模拟"有线程卡在真流的写上"。

    block() 返回时锁已经在别的线程手里；release() 放锁并等它退出。
    """

    def __init__(self, lock):
        super().__init__(daemon=True, name="neo-lock-holder")
        self._lock = lock
        self._held = threading.Event()
        self._go = threading.Event()

    def run(self):
        self._lock.acquire()
        self._held.set()
        self._go.wait(10.0)
        self._lock.release()

    def block(self):
        self.start()
        self._held.wait(5.0)

    def release(self):
        self._go.set()


class TestStreamNeverParksCaller(unittest.TestCase):
    def test_write_with_lock_held_returns_immediately(self):
        """真流被别的线程占着时，write() 不排队：立刻返回，行丢掉并计数。

        占锁的线程模拟"卡在控制台写上"（QuickEdit 选中、管道满）—— 锁必须由
        另一个线程持有才叫争用：RLock 对持锁线程自己是可重入的，同线程 acquire
        不算争用。排队的话 pywebview 的窗口线程会跟着挂住，就是标题栏上的"(未响应)"。
        """
        real = io.StringIO()
        stream = console_io.BoundedStream(real)
        holder = _LockHolder(stream._lock)
        holder.block()
        try:
            started = time.monotonic()
            n = stream.write("窗口线程的一行\n")
            elapsed = time.monotonic() - started
        finally:
            holder.release()
        holder.join()
        self.assertEqual(n, len("窗口线程的一行\n"))
        self.assertLess(elapsed, 0.5, "write() 不该等锁")
        self.assertEqual(stream._busy, 1)
        self.assertNotIn("窗口线程的一行", real.getvalue())

    def test_busy_drop_is_reported(self):
        """丢掉多少行要说出来，不能悄悄丢。"""
        real = io.StringIO()
        stream = console_io.BoundedStream(real)
        holder = _LockHolder(stream._lock)
        holder.block()
        try:
            for i in range(3):
                stream.write(f"行 {i}\n")
        finally:
            holder.release()
        holder.join()
        stream.flush()
        self.assertIn("丢弃 3 行", real.getvalue())

    def test_normal_output_unchanged(self):
        """没人抢锁时，闸门照旧把整行送出去。"""
        real = io.StringIO()
        stream = console_io.BoundedStream(real)
        stream.write("正常的一行\n")
        stream.flush()
        self.assertEqual(real.getvalue(), "正常的一行\n")


class TestLogStaysCheap(StateDirBase):
    def test_append_only(self):
        """常态是追加：一条日志一次追加写，不把整个尾部读出来重洗。"""
        for i in range(20):
            state.log(f"msg {i}")
        raw = state.MANAGER_LOG.read_bytes()
        self.assertNotIn(b"\r", raw)
        lines = raw.decode("utf-8").splitlines()
        self.assertEqual(len(lines), 20)
        self.assertIn("msg 19", lines[-1])

    def test_trim_keeps_the_tail(self):
        """超过 LOG_MAX 才裁：裁完只剩尾部，且不带 \r。"""
        saved = state.LOG_MAX
        state.LOG_MAX = 20_000
        try:
            for i in range(200):
                state.log("y" * 60 + f" {i}")
            raw = state.MANAGER_LOG.read_bytes()
            self.assertLess(len(raw), 40_000)
            self.assertNotIn(b"\r", raw)
            self.assertIn("199", raw.decode("utf-8").splitlines()[-1])
        finally:
            state.LOG_MAX = saved

    def test_log_is_thread_safe(self):
        """监督线程、服务请求线程、看门狗探针线程并发写日志不能互相咬。"""
        def worker(tag):
            for i in range(15):
                state.log(f"{tag}-{i}")
        threads = [threading.Thread(target=worker, args=(f"t{n}",)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = state.MANAGER_LOG.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 90)
        for ln in lines:
            self.assertLessEqual(len(ln), console_io.LINE_MAX + 60)


class TestUiWatchdog(unittest.TestCase):
    def test_no_window_no_watchdog(self):
        self.assertIsNone(ui_watchdog.start(None))

    def test_frames_are_bounded_and_named(self):
        out = ui_watchdog.thread_frames(limit=3)
        self.assertTrue(any("MainThread" in ln for ln in out))
        for ln in out:
            self.assertLessEqual(len(ln), console_io.LINE_MAX + 60)
            self.assertNotIn("\n", ln)

    def test_stalled_window_is_logged(self):
        """evaluate_js 不返回（窗口线程没在收消息）→ 日志里出现"没应答"和线程现场。"""
        release = threading.Event()

        class StalledWindow:
            def evaluate_js(self, script):
                release.wait(10.0)               # 模拟 Invoke 一直等不到窗口线程

        tmp = Path(tempfile.mkdtemp())
        logs, manager = state.LOGS, state.MANAGER_LOG
        state.LOGS = tmp
        state.MANAGER_LOG = tmp / "manager.log"
        try:
            stop = ui_watchdog.start(StalledWindow(), interval_s=0.05, stall_s=0.2)
            text = ""
            deadline = time.monotonic() + 8.0
            # 线程现场是"没应答"那行之后一条条追加的：只等第一行会出现读到半截的日志
            while time.monotonic() < deadline:
                if state.MANAGER_LOG.exists():
                    text = state.MANAGER_LOG.read_text(encoding="utf-8")
                    if "没应答" in text and "neo-ui-probe" in text:
                        break
                time.sleep(0.05)
            stop.set()
            release.set()
            self.assertIn("没应答", text)
            self.assertIn("neo-ui-probe", text)
        finally:
            state.LOGS, state.MANAGER_LOG = logs, manager

    def test_healthy_window_is_not_logged(self):
        """窗口线程正常应答时，看门狗不写日志、不产生噪音。"""
        class HealthyWindow:
            def __init__(self):
                self.calls = 0

            def evaluate_js(self, script):
                self.calls += 1
                return 1

        tmp = Path(tempfile.mkdtemp())
        logs, manager = state.LOGS, state.MANAGER_LOG
        state.LOGS = tmp
        state.MANAGER_LOG = tmp / "manager.log"
        try:
            win = HealthyWindow()
            stop = ui_watchdog.start(win, interval_s=0.05, stall_s=1.0)
            time.sleep(0.6)
            stop.set()
            text = state.MANAGER_LOG.read_text(encoding="utf-8") if state.MANAGER_LOG.exists() else ""
            self.assertNotIn("没应答", text)
            self.assertGreater(win.calls, 3)
        finally:
            state.LOGS, state.MANAGER_LOG = logs, manager


class TestClickPathIsHttpOnly(unittest.TestCase):
    """点击路径上不能有 pywebview 的 js_api（实测 2026-10-12："一点就卡死"）。

    js_api 一次点击要窗口线程两次收消息：收页面的 PostWebMessageAsJson
    （platforms/edgechromium.py:225，在窗口线程上），方法返回后 evaluate_js 再用
    Invoke + semaphore 把结果同步投回窗口线程（edgechromium.py:137）。窗口线程一停，
    第一次点击的 promise 永不返回，脚本消息队列堵住，后面每次点击都排在它后面。
    现在窗口只是容器，页面只发 HTTP 到控制台服务。
    """

    def test_window_kwargs_has_no_js_api(self):
        from neo.manager import window_kwargs
        kwargs = window_kwargs("http://127.0.0.1:8083/", x=10, y=20, w=1200, h=800)
        self.assertNotIn("js_api", kwargs, "挂上 js_api 就是把点击交回窗口线程")
        self.assertEqual(kwargs["url"], "http://127.0.0.1:8083/")
        self.assertEqual((kwargs["x"], kwargs["y"]), (10, 20))
        self.assertNotIn("x", window_kwargs("http://127.0.0.1:8083/", x=-1, y=-1),
                         "窗口跑掉到屏幕外时不写坐标")

    def test_page_never_touches_the_pywebview_bridge(self):
        js = (ROOT / "neo" / "web" / "app.js").read_text(encoding="utf-8")
        # 只看代码，注释里提到 js_api 是解释为什么不用它
        code = "\n".join(ln.split("//")[0] for ln in js.splitlines())
        self.assertNotIn("window.pywebview", code, "页面不许调 js_api")
        self.assertNotIn("js_api", code)
        for route in ("/neo/open", "/neo/open-log"):
            self.assertIn(route, js, f"页面点开的动作要有服务接口：{route}")


if __name__ == "__main__":
    unittest.main()
