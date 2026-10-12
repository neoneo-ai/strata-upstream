"""neo/tests/test_connect_api.py - M2/M3 加的那几条控制台接口。

不碰网络（探测结果用假的）、不写用户的 agent 配置（connectors.write_config 换成假的）、
launcher.json 用前备份后用（与 test_console.py 一样的做法）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from neo import connectors, netprobe, state, tokens                 # noqa: E402
from neo.console_server import ConsoleServer                        # noqa: E402
from neo.engines.base import EngineAdapter                          # noqa: E402
from neo.supervisor import Supervisor                               # noqa: E402

from neo.tests.test_console import req                             # noqa: E402
from neo.tests.test_supervisor import free_port                    # noqa: E402

FAKE_NET = {"targets": [{"host": "www.modelscope.cn", "what": "modelscope", "ok": True,
                         "ms": 31.0},
                        {"host": "huggingface.co", "what": "huggingface", "ok": False,
                         "ms": 3000.0},
                        {"host": "hf-mirror.com", "what": "mirror", "ok": True, "ms": 80.0}],
            "ok": ["www.modelscope.cn", "hf-mirror.com"], "source": "modelscope",
            "reason": "huggingface.co 连不上，www.modelscope.cn 连得上", "at": 0.0}


class FakeAdapter(EngineAdapter):
    name = "fake"
    title = "Fake"

    def discover(self):
        return [{"id": "strata-fake.json", "name": "fake-model", "path": "", "file": "",
                 "mtime": 0, "port": 8080, "context": 131072, "vision": False,
                 "backend": "", "installed": True}]

    def plan(self, port, host="127.0.0.1", api_key="", model_id="", gpu=None):
        raise FileNotFoundError("测试：这个适配器不启动进程")

    def probe(self, port, api_key=""):
        return {"engine": "fake", "port": port, "listening": False, "healthy": False,
                "loaded": False, "model": "", "max_context": 0, "metrics": {}}


class TestConnectApi(unittest.TestCase):
    def setUp(self):
        self._probe = netprobe.probe_cached
        netprobe.probe_cached = lambda timeout=3.0, force=False, connect=None: dict(FAKE_NET)
        self._was_launcher = state.LAUNCHER.exists()
        self._backup = state.LAUNCHER.read_bytes() if self._was_launcher else b""
        self._tmp = tempfile.TemporaryDirectory()
        self._state = state.STATE
        state.STATE = Path(self._tmp.name)
        self._write = connectors.write_config
        self.written = []

        def fake_write(agent, base, key, model, env=None):
            self.written.append((agent, base, key, model.id))
            return {"path": str(Path(self._tmp.name) / f"{agent}.json"),
                    "written": True, "notes": ["测试里没真写文件"], "files": []}
        connectors.write_config = fake_write
        self.sup = Supervisor(FakeAdapter(ROOT), 8080)
        self.console = ConsoleServer(self.sup, host="127.0.0.1", port=free_port())
        self.url = self.console.start()

    def tearDown(self):
        self.console.stop()
        connectors.write_config = self._write
        netprobe.probe_cached = self._probe
        state.STATE = self._state
        self._tmp.cleanup()
        if self._was_launcher:
            state.LAUNCHER.write_bytes(self._backup)
        elif state.LAUNCHER.exists():
            state.LAUNCHER.unlink()

    def get(self, path):
        code, _, body = req(self.url.rstrip("/") + path)
        return code, json.loads(body)

    def post(self, path, body=None):
        code, _, text = req(self.url.rstrip("/") + path, method="POST", body=body or {})
        return code, json.loads(text)

    def test_net_endpoint_reports_the_probe(self):
        code, data = self.get("/neo/net")
        self.assertEqual(code, 200)
        self.assertEqual(data["source"], "modelscope")
        self.assertIn("连不上", data["reason"])

    def test_lan_endpoint_describes_loopback(self):
        code, data = self.get("/neo/lan")
        self.assertEqual(code, 200)
        self.assertFalse(data["lan_open"])
        self.assertEqual(data["base"], "http://127.0.0.1:8080")
        self.assertEqual(data["firewall_hint"], "")

    def test_lan_open_shows_lan_bases_and_firewall(self):
        self.sup.host = "0.0.0.0"
        _, data = self.get("/neo/lan")
        self.assertTrue(data["lan_open"])
        self.assertIn("localport=8080", data["firewall_hint"])

    def test_token_endpoint_mints_and_saves_one(self):
        code, data = self.post("/neo/token")
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(tokens.looks_like_key(data["api_key"]))
        self.assertNotIn(data["api_key"], data["api_key_masked"])
        self.assertEqual(self.sup.api_key, data["api_key"])
        self.assertEqual(state.launcher_config()["api_key"], data["api_key"])
        self.assertIn(data["api_key"], tokens.cached_keys("http://127.0.0.1:8080", "minted"))

    def test_connect_endpoint_lists_the_four_agents(self):
        code, data = self.get("/neo/connect")
        self.assertEqual(code, 200)
        self.assertEqual([r["agent"] for r in data["recipes"]],
                         ["claude", "codex", "opencode", "openclaw"])
        self.assertEqual(data["model"], "fake-model")
        self.assertEqual(data["context"], 131072)
        claude = data["recipes"][0]
        self.assertEqual(claude["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8080")
        self.assertEqual(claude["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "131072")
        codex = data["recipes"][1]
        self.assertEqual(codex["command"], ["codex", "--profile", "neo"])

    def test_connect_endpoint_says_when_there_is_no_key(self):
        _, data = self.get("/neo/connect")
        self.assertFalse(data["has_api_key"])
        self.assertEqual(data["recipes"][0]["env"]["ANTHROPIC_AUTH_TOKEN"], "")

    def test_connect_write_is_asked_by_name(self):
        code, data = self.post("/neo/connect", {"agent": "codex"})
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(self.written[0][0], "codex")
        self.assertEqual(self.written[0][1], "http://127.0.0.1:8080")
        self.assertEqual(self.written[0][3], "fake-model")

    def test_connect_write_rejects_an_unknown_agent(self):
        _, data = self.post("/neo/connect", {"agent": "zzz"})
        self.assertFalse(data["ok"])
        self.assertIn("不认识的 agent", data["error"])

    def test_wizard_endpoint_reports_the_suggested_source(self):
        code, data = self.get("/neo/wizard")
        self.assertEqual(code, 200)
        self.assertIn("--source modelscope", data["command"])
        self.assertEqual([s["name"] for s in data["steps"]],
                         ["根目录", "模型", "下载源", "对外"])

    def test_read_endpoints_do_not_write_anything(self):
        for path in ("/neo/net", "/neo/lan", "/neo/connect", "/neo/wizard"):
            self.get(path)
        self.assertFalse(tokens.key_cache_path().exists(), "读接口不该写 key 缓存")
        self.assertFalse(self.written, "GET 不该写任何 agent 的配置")


if __name__ == "__main__":
    unittest.main(verbosity=2)

