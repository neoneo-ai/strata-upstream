"""neo/tests/test_console.py - 控制台服务：接口、静态页、来源检查、设置落盘。

不启动真引擎：用一个假适配器，只验 HTTP 层。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from neo import state                                          # noqa: E402
from neo import console_server as cs                          # noqa: E402
from neo.console_server import ConsoleServer                   # noqa: E402
from neo.engines.base import EngineAdapter                     # noqa: E402
from neo.supervisor import HALTED, Supervisor                  # noqa: E402

from neo.tests.test_supervisor import free_port                # noqa: E402


class FakeAdapter(EngineAdapter):
    """一个永远不会真启动的适配器：让控制台层的测试不依赖进程。"""
    name = "fake"
    title = "Fake"

    def discover(self):
        return [{"id": "fake.json", "name": "fake", "path": "", "file": "", "mtime": 0,
                 "port": 8080, "context": 1024, "vision": False, "backend": "", "installed": True}]

    def plan(self, port, host="127.0.0.1", api_key="", model_id="", gpu=None):
        raise FileNotFoundError("测试：这个适配器不启动进程")

    def probe(self, port, api_key=""):
        return {"engine": "fake", "port": port, "listening": False, "healthy": False,
                "loaded": False, "model": "", "max_context": 0, "metrics": {}}


def req(url, method="GET", body=None, origin=None, host_header=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("content-type", "application/json")
    if origin:
        r.add_header("origin", origin)
    if host_header:
        r.add_header("host", host_header)
    try:
        with urllib.request.urlopen(r, timeout=6) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, \
                resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, \
            e.read().decode("utf-8", "replace")


class RecordingShell:
    """假壳：记录页面点"打开"时服务收到了什么，不去真开浏览器。"""
    def __init__(self):
        self.calls = []

    def open_page(self, which):
        self.calls.append(("page", which))
        return {"ok": True, "url": f"http://127.0.0.1:8080/{which}/"}

    def open_log(self):
        self.calls.append(("log", ""))
        return {"ok": True, "path": str(state.MANAGER_LOG)}


class TestConsole(unittest.TestCase):
    def setUp(self):
        self.was = state.LAUNCHER.exists()
        self.backup = state.LAUNCHER.read_bytes() if self.was else b""
        self.sup = Supervisor(FakeAdapter(ROOT), 8080)
        self.console = ConsoleServer(self.sup, host="127.0.0.1", port=free_port())
        self.shell = RecordingShell()
        self.console.shell = self.shell
        self.url = self.console.start()

    def tearDown(self):
        self.console.stop()
        if self.was:
            state.LAUNCHER.write_bytes(self.backup)
        elif state.LAUNCHER.exists():
            state.LAUNCHER.unlink()

    def get(self, path, **kw):
        return req(self.url.rstrip("/") + path, **kw)

    def post(self, path, body=None, **kw):
        return req(self.url.rstrip("/") + path, method="POST", body=body or {}, **kw)

    def test_status_endpoint(self):
        code, headers, body = self.get("/neo/status")
        self.assertEqual(code, 200)
        self.assertIn("application/json", headers.get("content-type", ""))
        data = json.loads(body)
        for key in ("state", "port", "engine", "events", "pages", "installed", "backoff"):
            self.assertIn(key, data)
        self.assertEqual(data["engine"], "fake")
        self.assertTrue(data["pages"]["chat"].endswith("/"))

    def test_index_and_assets_no_store(self):
        code, headers, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", headers.get("content-type", ""))
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertIn("Neo Strata", body)
        for asset in ("/app.js", "/style.css", "/icon.svg"):
            code, headers, _ = self.get(asset)
            self.assertEqual(code, 200, asset)
            self.assertEqual(headers.get("cache-control"), "no-store", asset)

    def test_start_with_a_broken_engine_halts_with_diagnosis(self):
        self.post("/neo/start")
        state_seen = None
        for _ in range(40):
            data = json.loads(self.get("/neo/status")[2])
            state_seen = data["state"]
            if state_seen == HALTED:
                break
            time.sleep(0.2)
        self.assertEqual(state_seen, HALTED, "起不来的引擎要停在熔断态")
        self.assertIn("不启动进程", data["diagnosis"])

    def test_models_and_log(self):
        data = json.loads(self.get("/neo/models")[2])
        self.assertEqual(data["models"][0]["id"], "fake.json")
        data = json.loads(self.get("/neo/log")[2])
        self.assertIn("text", data)

    def test_foreign_origin_is_refused(self):
        code, _, _ = self.post("/neo/start", origin="http://evil.example")
        self.assertEqual(code, 403)

    def test_foreign_host_is_refused(self):
        code, _, _ = self.get("/neo/status", host_header="evil.example")
        self.assertEqual(code, 403)

    def test_config_roundtrip(self):
        code, _, body = self.post("/neo/config", {"port": 8123, "host": "127.0.0.1"})
        cfg = json.loads(body)
        self.assertEqual(cfg["port"], 8123)
        self.assertEqual(self.sup.port, 8123)
        saved = state.read_json(state.LAUNCHER, {})
        self.assertEqual(saved.get("port"), 8123)
        self.assertNotIn("sekret", json.dumps(cfg), "接口不回显 API key")

    def test_open_page_goes_through_the_service(self):
        """页面点"打开聊天页/监控页"= POST /neo/open；点击路径上不碰窗口线程。"""
        code, _, body = self.post("/neo/open", {"page": "monitor"})
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertTrue(data["url"].endswith("/monitor/"))
        self.assertEqual(self.shell.calls[-1], ("page", "monitor"))
        self.post("/neo/open", {})                       # 不点名就是聊天页
        self.assertEqual(self.shell.calls[-1], ("page", "chat"))

    def test_open_log_goes_through_the_service(self):
        code, _, body = self.post("/neo/open-log")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(self.shell.calls[-1], ("log", ""))

    def test_open_without_a_shell_says_so(self):
        """没有壳（单独跑控制台服务）时要说清打不开，不能让页面干等一个 404。"""
        self.console.httpd.shell = None
        code, _, body = self.post("/neo/open", {"page": "chat"})
        self.assertEqual(code, 503)
        self.assertIn("壳", json.loads(body)["error"])

    def test_open_from_foreign_origin_is_refused(self):
        """这两个接口会真开浏览器 / 打开文件，来源检查必须盖住它们。"""
        code, _, _ = self.post("/neo/open", {"page": "chat"}, origin="http://evil.example")
        self.assertEqual(code, 403)
        self.assertEqual(self.shell.calls, [])

    def test_unknown_path(self):
        self.assertEqual(self.get("/neo/nope")[0], 404)


class TestPortConflict(unittest.TestCase):
    """控制台端口：一个 Neo 占着的时候，第二个 Neo 要换个端口，不能共用一个端口。

    实测 2026-10-12 在用户的机器上：8083 上同时挂着两个 Neo（旧的 PID 9160、新的
    PID 48056），浏览器打开 http://127.0.0.1:8083 连到的是旧的那个，页面显示的是它
    那份旧状态（"未启动"）——用户看到的"状态不对"就是这么来的。
    """

    def setUp(self):
        self.started = []

    def tearDown(self):
        for c in self.started:
            c.stop()

    def console(self, port):
        """一个控制台，约定端口就是 port（strata_port 让它依次试 port..port+5）。"""
        c = ConsoleServer(Supervisor(FakeAdapter(ROOT), 8080), host="127.0.0.1",
                          port=port, strata_port=port - 3)
        self.started.append(c)
        return c

    def test_a_live_neo_on_the_port_is_detected(self):
        port = free_port()
        self.console(port).start()
        info = cs.neo_console_info(port)
        self.assertIsNotNone(info, "端口上有一个活着的 Neo 要认出来")
        self.assertIn("state", info)
        self.assertIsNone(cs.neo_console_info(free_port()), "端口上没人就不能算占用")

    def test_start_skips_a_port_taken_by_another_neo(self):
        taken = free_port()
        self.console(taken).start()
        second = self.console(taken)
        url = second.start()
        self.assertNotEqual(second.port, taken, "端口上已有 Neo 就要换端口，不能共用")
        self.assertIn(str(second.port), url, "页面地址要用换过之后的端口")

    def test_start_says_which_port_the_other_neo_holds(self):
        """一个端口都绑不上时，报错要点名是哪个端口上坐着别的 Neo。"""
        taken = free_port()
        self.console(taken).start()
        blockers = []
        for i in range(1, 6):
            s = socket.socket()
            s.bind(("127.0.0.1", taken + i))
            blockers.append(s)
        self.addCleanup(lambda: [s.close() for s in blockers])
        with self.assertRaises(RuntimeError) as got:
            self.console(taken).start()
        self.assertIn(str(taken), str(got.exception), "要说清哪个端口上已有别的 Neo")

    def test_console_port_is_exclusive(self):
        """Windows 上 SO_REUSEADDR 允许两个进程绑同一个端口；控制台要的是独占绑定。"""
        port = free_port()
        self.console(port).start()
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            with self.assertRaises(OSError):
                s.bind(("127.0.0.1", port))
        finally:
            s.close()


class TestRunningInstance(unittest.TestCase):
    """第二个实例的那句话要说清"在跑的是谁"（neo/manager.py 的 describe_running_instance）。"""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="neo-runinst-"))
        self._instance = state.INSTANCE
        state.INSTANCE = self.tmp / "instance.json"

    def tearDown(self):
        state.INSTANCE = self._instance

    def test_window_mode_names_pid_and_url(self):
        from neo.manager import describe_running_instance
        state.record_instance(root=str(self.tmp), strata_port=8080,
                              console_url="http://127.0.0.1:8083/", mode="window")
        text = describe_running_instance(self.tmp)
        self.assertIn(str(os.getpid()), text)
        self.assertIn("http://127.0.0.1:8083/", text)
        self.assertIn("--force", text, "要说清怎么绕过")
        self.assertNotIn("没有窗口", text)

    def test_browser_mode_says_there_is_no_window(self):
        """实测那条路：pywebview 缺了 → 浏览器界面 → 用户看不到窗口，只看到"已经在运行了"。"""
        from neo.manager import describe_running_instance
        state.record_instance(root=str(self.tmp), strata_port=8080,
                              console_url="http://127.0.0.1:8083/", mode="browser")
        text = describe_running_instance(self.tmp)
        self.assertIn("没有窗口", text)
        self.assertIn("http://127.0.0.1:8083/", text)
        self.assertIn("pywebview", text, "要给出拿到真窗口的办法")

    def test_stale_record_is_explained_and_cleared(self):
        from neo.manager import describe_running_instance
        from neo.tests.test_supervisor import dead_pid
        state.write_json(state.INSTANCE, {"pid": dead_pid(), "root": str(self.tmp),
                                          "mode": "window"})
        text = describe_running_instance(self.tmp)
        self.assertIn("已经没了", text, "记录里的进程没了要说清")
        self.assertFalse(state.INSTANCE.exists(), "过期记录要顺手清掉，下次就正常了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
