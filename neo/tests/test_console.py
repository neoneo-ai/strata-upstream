"""neo/tests/test_console.py - 控制台服务：接口、静态页、来源检查、设置落盘。

不启动真引擎：用一个假适配器，只验 HTTP 层。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from neo import model_settings                                   # noqa: E402
from neo import state                                          # noqa: E402
from neo import console_server as cs                          # noqa: E402
from neo.console_server import ConsoleServer                   # noqa: E402
from neo.engines.base import EngineAdapter, STRATA_MONITOR_PATH   # noqa: E402
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


class TestModelSettingsEndpoint(unittest.TestCase):
    """模型设置页的接口：读四栏、写覆盖配置、恢复原样。引擎是假的，不起进程。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "root"
        self.root.mkdir()
        self.cfg_file = self.root / "strata-test.json"
        self.cfg_file.write_text(json.dumps({
            "model_name": "Qwen Test", "exe": "strata.exe", "port": 8080,
            "args": ["model.gguf", "--max-context", "65536", "--kv", "int8",
                     "--kv-resident", "32768", "--vram-reserve-mib", "1000", "--vision"],
            "vision": {"mmproj": "mmproj.gguf", "gpu": True}}), encoding="utf-8")
        self.upstream_bytes = self.cfg_file.read_bytes()
        self._old_state = state.STATE
        state.STATE = Path(self._tmp.name) / "state"
        state.STATE.mkdir()
        self.addCleanup(setattr, state, "STATE", self._old_state)
        self.was = state.LAUNCHER.exists()
        self.backup = state.LAUNCHER.read_bytes() if self.was else b""
        self.sup = Supervisor(FakeAdapter(self.root), 8080)
        self.console = ConsoleServer(self.sup, host="127.0.0.1", port=free_port())
        self.console.shell = RecordingShell()
        self.url = self.console.start()

    def tearDown(self):
        self.console.stop()
        if self.was:
            state.LAUNCHER.write_bytes(self.backup)
        elif state.LAUNCHER.exists():
            state.LAUNCHER.unlink()

    def get(self, path):
        return req(self.url.rstrip("/") + path)

    def post(self, path, body):
        return req(self.url.rstrip("/") + path, method="POST", body=body)

    def test_get_lists_the_models_and_the_four_settings(self):
        code, headers, body = self.get("/neo/model/settings")
        self.assertEqual(code, 200)
        self.assertIn("application/json", headers.get("content-type", ""))
        data = json.loads(body)
        self.assertTrue(data["ok"], data.get("error"))
        # 没点名（页面刚打开）就是第一个模型，页面一进来就有得看
        self.assertEqual(data["id"], "strata-test.json")
        self.assertEqual(data["settings"]["context"], 65536)
        self.assertEqual(data["settings"]["vram_reserve_mib"], 1000)
        self.assertTrue(data["settings"]["vision_available"])
        self.assertFalse(data["overridden"])
        self.assertTrue(data["choices"]["context"], "下拉要有得选")
        self.assertEqual(data["estimate"]["kv_cells"], 32768)

    def test_get_with_an_unknown_name_lists_the_ids(self):
        data = json.loads(self.get("/neo/model/settings?model=nope.json")[2])
        self.assertFalse(data["ok"])
        self.assertIn("strata-test.json", data["models"])

    def test_post_writes_an_overlay_and_leaves_the_upstream_file_alone(self):
        code, _h, body = self.post("/neo/model/settings",
                                   {"model": "strata-test.json", "context": 131072})
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"], data["notes"])
        self.assertTrue(data["restart_required"])
        overlay = model_settings.overlay_path(self.cfg_file)
        self.assertTrue(overlay.is_file())
        self.assertEqual(self.cfg_file.read_bytes(), self.upstream_bytes,
                         "上游那份 strata-*.json 一个字节都不能动")
        back = json.loads(self.get("/neo/model/settings?model=strata-test.json")[2])
        self.assertTrue(back["overridden"])
        self.assertEqual(back["settings"]["context"], 131072)
        self.assertEqual(back["base"]["context"], 65536)

    def test_out_of_range_is_refused_with_a_plain_note(self):
        # 与这个服务里别的 POST 一样：业务上挡回来是 200 + ok:false + 给用户看的话
        code, _h, body = self.post("/neo/model/settings",
                                   {"model": "strata-test.json", "context": 10})
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertTrue(data["notes"])
        self.assertFalse(model_settings.overlay_path(self.cfg_file).is_file())

    def test_the_form_shape_the_page_sends_is_accepted(self):
        # app.js 的 msBody()：vision 是 "on"/"off" 字符串，四栏都带上
        code, _h, body = self.post("/neo/model/settings", {
            "model": "strata-test.json", "context": 131072, "kv": "q4_0",
            "kv_resident": 16384, "vram_reserve_mib": 1500, "vision": "off"})
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"], data["notes"])
        over = json.loads(model_settings.overlay_path(self.cfg_file).read_text(encoding="utf-8"))
        self.assertNotIn("--vision", over["args"])
        self.assertEqual(over["args"][over["args"].index("--kv") + 1], "q4_0")

    def test_reset_without_an_overlay_says_it_is_already_plain(self):
        code, _h, body = self.post("/neo/model/settings",
                                   {"model": "strata-test.json", "action": "reset"})
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertFalse(data["removed"])

    def test_reset_removes_the_overlay(self):
        self.post("/neo/model/settings", {"model": "strata-test.json", "context": 131072})
        code, _h, body = self.post("/neo/model/settings",
                                   {"model": "strata-test.json", "action": "reset"})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["removed"])
        self.assertFalse(model_settings.overlay_path(self.cfg_file).is_file())

    def test_unknown_model_is_refused_by_name(self):
        code, _h, body = self.post("/neo/model/settings",
                                   {"model": "nope.json", "context": 8192})
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertIn("没有这个模型", data["notes"][0])


class FakeStrataHandler(BaseHTTPRequestHandler):
    """假上游：面板端点要 Bearer key，静态页照发，写与推理只认 POST。

    它把 Neo 转发过来的 path 与 authorization 头记在 seen 里，好证明
    "key 是 Neo 附的、页面看不见"和"写与推理 Neo 根本没替页面发"。
    """
    key = "sk-neo-test-0123456789"
    seen: list = []

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data) -> None:
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        path = urlsplit(self.path).path
        auth = self.headers.get("authorization", "")
        FakeStrataHandler.seen.append((self.path, auth))
        if path in ("/metrics", "/status", "/config", "/settings", "/mcp",
                    "/api/requests", "/v1/status"):
            if auth != "Bearer " + FakeStrataHandler.key:
                self._json(401, {"error": {"message": "missing or wrong API key"}})
                return
            self._json(200, {"live": {"running": True}, "totals": {"requests": 3}})
        elif path == "/web/app.js":
            self._send(200, "text/javascript; charset=utf-8", b"// upstream app.js\n")
        elif path in ("/", ""):
            self._send(200, "text/html; charset=utf-8", b"<html><body>Strata monitor</body></html>")
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        FakeStrataHandler.seen.append((self.path, self.headers.get("authorization", "")))
        self._json(200, {"ok": True})


class TestStrataChannel(unittest.TestCase):
    """内嵌的上游监控页借 Neo 走的那条只读通道（neo/console_server.py 的 /strata/）。

    为什么要有：上游的面板端点在设了 API key 之后要 key，内嵌的上游页面没有地方填 key
    （它的 key 存在自己的 localStorage 里），设了 token 之后监控卡片整页空掉。
    这条通道是 Neo 对上游的全部依赖 —— 上游一个字节的改动都没有。
    """

    @classmethod
    def setUpClass(cls):
        cls.up = free_port()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.up), FakeStrataHandler)
        cls.thread = threading.Thread(target=lambda: cls.httpd.serve_forever(poll_interval=0.05),
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        FakeStrataHandler.seen = []
        self.sup = Supervisor(FakeAdapter(ROOT), self.up, api_key=FakeStrataHandler.key)
        self.console = ConsoleServer(self.sup, host="127.0.0.1", port=free_port())
        self.url = self.console.start()
        self.sup.console_port = self.console.port

    def tearDown(self):
        self.console.stop()

    def get(self, path):
        return req(self.url.rstrip("/") + path)

    def post(self, path, body=None):
        return req(self.url.rstrip("/") + path, method="POST", body=body or {})

    def test_page_and_assets_pass_through(self):
        code, headers, body = self.get("/strata/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", headers.get("content-type", ""))
        self.assertIn("Strata monitor", body)
        code, headers, body = self.get("/strata/web/app.js")
        self.assertEqual(code, 200)
        self.assertIn("text/javascript", headers.get("content-type", ""))
        self.assertIn("upstream app.js", body)

    def test_panel_endpoints_answer_with_neos_key(self):
        """设了 token 之后上游要 key：Neo 附上它自己的那把，页面看不见。"""
        code, _h, body = self.get("/strata/metrics")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["totals"]["requests"], 3)
        path, auth = FakeStrataHandler.seen[-1]
        self.assertEqual(path, "/metrics")
        self.assertEqual(auth, "Bearer " + FakeStrataHandler.key, "key 要由 Neo 附上")
        self.assertNotIn(FakeStrataHandler.key, body, "应答里不能带 key")

    def test_query_string_survives(self):
        code, _h, _b = self.get("/strata/metrics?requests=all")
        self.assertEqual(code, 200)
        self.assertEqual(FakeStrataHandler.seen[-1][0], "/metrics?requests=all")


    def test_writes_are_not_forwarded(self):
        code, _h, body = self.post("/strata/settings", {"shared": True})
        self.assertEqual(code, 405)
        self.assertIn("不外借", json.loads(body)["error"])
        self.assertEqual(FakeStrataHandler.seen, [], "Neo 没替页面写上游")

    def test_inference_is_not_forwarded(self):
        code, _h, _b = self.get("/strata/v1/chat/completions")
        self.assertEqual(code, 405)
        self.assertEqual(FakeStrataHandler.seen, [], "推理不转：不替页面花用户的 token")

    def test_paths_outside_the_menu_are_refused(self):
        for path in ("/strata/shutdown", "/strata/../neo/status", "/strata/web/../../etc/passwd"):
            code, _h, _b = self.get(path)
            self.assertIn(code, (404, 405), path)

    def test_upstream_down_is_502(self):
        """上游没在跑：转发回 502，控制台不能因为转发挂住。"""
        dead = Supervisor(FakeAdapter(ROOT), free_port())
        other = ConsoleServer(dead, host="127.0.0.1", port=free_port())
        url = other.start()
        try:
            code, _h, body = req(url.rstrip("/") + "/strata/metrics")
            self.assertEqual(code, 502)
            self.assertIn("上游没答", json.loads(body)["error"])
        finally:
            other.stop()

    def test_neo_routes_are_not_shadowed(self):
        """/strata 这条前缀不挡 Neo 自己的路由，状态页给的监控地址走这条通道。"""
        code, _h, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("Neo Strata", body)
        code, _h, body = self.get("/neo/status")
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertEqual(data["pages"]["monitor"],
                         f"http://127.0.0.1:{self.console.port}{STRATA_MONITOR_PATH}")
        self.assertEqual(data["pages"]["chat"], f"http://127.0.0.1:{self.up}/")

    def test_channel_path_matches_the_pages_helper(self):
        """pages() 拼的那个路径必须真在这条通道的白名单里（两处常量不能各说各话）。

        归一化与 Handler._strata 一致：/strata 与 /strata/ 都算上游的根，写作 ""。
        """
        self.assertTrue(STRATA_MONITOR_PATH.startswith(cs.STRATA_PATH + "/"))
        rest = STRATA_MONITOR_PATH[len(cs.STRATA_PATH):].split("#")[0]
        tail = rest if rest.startswith("/") else "/" + rest
        self.assertIn("" if tail == "/" else tail, cs.STRATA_READ_PATHS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
