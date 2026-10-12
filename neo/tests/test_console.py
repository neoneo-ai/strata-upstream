"""neo/tests/test_console.py - 控制台服务：接口、静态页、来源检查、设置落盘。

不启动真引擎：用一个假适配器，只验 HTTP 层。
"""
from __future__ import annotations

import json
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from neo import state                                          # noqa: E402
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


class TestConsole(unittest.TestCase):
    def setUp(self):
        self.was = state.LAUNCHER.exists()
        self.backup = state.LAUNCHER.read_bytes() if self.was else b""
        self.sup = Supervisor(FakeAdapter(ROOT), 8080)
        self.console = ConsoleServer(self.sup, host="127.0.0.1", port=free_port())
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

    def test_unknown_path(self):
        self.assertEqual(self.get("/neo/nope")[0], 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
