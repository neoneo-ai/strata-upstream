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


def js_depth_lines(text: str) -> list[int]:
    """app.js 每一行开头的花括号深度。跳过注释、字符串、模板串与正则字面量。

    返回的第 k 项就是第 k 行（从 0 数）行首的深度。测试用它盯"顶层"这件事：
    一个函数体的花括号漏了，后面整段顶层代码就落进它的身体里，行首缩进看不出来，
    深度能看出来。
    """
    depths = [0]
    depth = 0
    i, n = 0, len(text)
    prev = ""                                   # 上一个代码字符（判断这里是正则还是除法）
    REGEX_AFTER = "=(,:[!&|?{};+-*%~^<>"

    def nl(from_i: int, to_i: int) -> None:     # 跳过多行字面量时保持行号对齐
        for k in range(from_i, to_i):
            if text[k] == "\n":
                depths.append(depth)

    while i < n:
        ch = text[i]
        if ch == "\n":
            depths.append(depth)
            i += 1
        elif ch == "/" and text[i + 1:i + 2] == "/":
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif ch == "/" and text[i + 1:i + 2] == "*":
            j = text.find("*/", i + 2)
            end = n if j < 0 else j + 2
            nl(i, end)
            i = end
        elif ch in "'\"":
            j = i + 1
            while j < n and text[j] != ch:
                j += 2 if text[j] == "\\" else 1
            nl(i, min(j + 1, n))
            prev = ch
            i = min(j + 1, n)
        elif ch == "`":
            j = i + 1
            while j < n and text[j] != "`":
                if text[j] == "\\":
                    j += 2
                elif text[j] == "$" and text[j + 1:j + 2] == "{":
                    inner, k = 1, j + 2
                    while k < n and inner:
                        if text[k] == "{":
                            inner += 1
                        elif text[k] == "}":
                            inner -= 1
                        k += 1
                    nl(j, k)
                    j = k
                else:
                    j += 1
            nl(i, min(j + 1, n))
            prev = "`"
            i = min(j + 1, n)
        elif ch == "/" and (not prev or prev in REGEX_AFTER):
            j = i + 1
            while j < n and text[j] != "/":
                j += 2 if text[j] == "\\" else 1
            j += 1
            while j < n and text[j].isalpha():
                j += 1                            # 正则的 flags：/x/gi
            nl(i, min(j, n))
            prev = "/"
            i = min(j, n)
        elif ch in "{}":
            depth += 1 if ch == "{" else -1
            prev = ch
            i += 1
        else:
            if not ch.isspace():
                prev = ch
            i += 1
    depths.append(depth)
    return depths


class TestAppJsTopLevelWiring(unittest.TestCase):
    """app.js 里 render / poll / ensureFrame 必须在顶层。

    实测 2026-10-12 在真开的窗口里（pywebview 6.2.1 / WebView2，页面 http://127.0.0.1:8083/）：
    num() 的花括号漏了一个，从 render 到所有按钮接线全落进 num 的身体里，文件末尾那句
    poll() 就成了 ReferenceError —— 页面的资源列表里一次 /neo/status 都没有，状态胶囊
    停在"未启动"，聊天与监控两个内嵌框的 src 停在 about:blank（1062x677 的空白框）。
    行首缩进看不出这种损坏（被吞掉的代码本来就顶格写），花括号深度能看出来。
    """

    TOP_LEVEL = ("function render(", "async function poll(", "function ensureFrame(",
                 "function num(", "async function loadModels(", "async function loadLog(",
                 "async function loadConfig(", "function pill(", "function note(")

    def setUp(self):
        self.js = (ROOT / "neo" / "web" / "app.js").read_text(encoding="utf-8")
        self.lines = self.js.splitlines()
        self.depths = js_depth_lines(self.js)

    def test_braces_balance(self):
        self.assertEqual(self.depths[-1], 0, "app.js 的花括号没配平")
        self.assertTrue(all(d >= 0 for d in self.depths), "花括号在某一行变成了负数")

    def test_key_functions_are_top_level(self):
        for name in self.TOP_LEVEL:
            hits = [k for k, ln in enumerate(self.lines) if ln.lstrip().startswith(name)]
            self.assertTrue(hits, f"app.js 里找不到 {name}")
            for k in hits:
                self.assertEqual(self.depths[k], 0,
                                 f"{name} 不在顶层：第 {k + 1} 行，深度 {self.depths[k]}")

    def test_poll_is_started_at_top_level(self):
        """文件末尾的 poll() 与两个 setInterval 要在顶层：那是页面唯一的活口。"""
        for needle in ("poll();", "setInterval(poll, 2000);"):
            hits = [k for k, ln in enumerate(self.lines) if ln.strip() == needle]
            self.assertTrue(hits, f"app.js 里没有 {needle}")
            self.assertTrue(any(self.depths[k] == 0 for k in hits),
                            f"{needle} 被吞进函数体里了，页面不会开始轮询")

    def test_status_page_needs_the_status_route(self):
        """轮询要问的那条接口在，页面里的动作接口也在（缺一条就是空页面）。"""
        for route in ("/neo/status", "/neo/models", "/neo/log", "/neo/config",
                      "/neo/open", "/neo/open-log"):
            self.assertIn(route, self.js, f"页面少了 {route}")


class TestBuildNeedsPywebview(unittest.TestCase):
    """打包环境缺 pywebview 时，build_exe 要当场拒绝，不能打出一个没有窗口的 exe。

    实测 2026-10-12 19:58 部署的 NeoStrata.exe 自己的日志：
    "pywebview 不可用（No module named 'webview'），退回浏览器界面" —— 打包时
    collect_submodules("webview") 是空列表，PyInstaller 一声不响地打成功，用户那边
    就看不到任何窗口（聊天页/监控页只能在浏览器里看）。
    """

    def setUp(self):
        from neo import build_exe
        self.build_exe = build_exe
        self._pw = build_exe.pywebview_available
        self._pi = build_exe.pyinstaller_available
        self._build = build_exe.build

    def tearDown(self):
        self.build_exe.pywebview_available = self._pw
        self.build_exe.pyinstaller_available = self._pi
        self.build_exe.build = self._build

    def test_build_refuses_without_pywebview(self):
        self.build_exe.pywebview_available = lambda: False
        with self.assertRaises(SystemExit) as got:
            self.build_exe.build(quiet=True)
        note = str(got.exception)
        self.assertIn("pywebview", note)
        self.assertIn("requirements-neo.txt", note)

    def test_no_webview_flag_is_what_the_user_asks_for(self):
        """加 --no-webview 时不拦 pywebview 这一关：走到下一个检查（pyinstaller）。"""
        self.build_exe.pywebview_available = lambda: False
        self.build_exe.pyinstaller_available = lambda: False
        with self.assertRaises(SystemExit) as got:
            self.build_exe.build(quiet=True, allow_no_webview=True)
        self.assertIn("pyinstaller", str(got.exception))

    def test_cli_flag_reaches_build(self):
        """--no-webview 要真的传到 build(allow_no_webview=True)，默认是拦住的。"""
        seen = []

        def fake_build(dest="", quiet=False, allow_no_webview=False):
            seen.append(allow_no_webview)
            return Path("NeoStrata.exe")

        self.build_exe.build = fake_build
        self.build_exe.main(["--no-webview"])
        self.build_exe.main([])
        self.assertEqual(seen, [True, False])


if __name__ == "__main__":
    unittest.main()
