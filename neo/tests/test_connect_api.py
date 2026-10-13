"""neo/tests/test_connect_api.py - M2/M3 加的那几条控制台接口。

不碰网络（探测结果用假的）、不写用户的 agent 配置（connectors.write_config 换成假的）、
launcher.json 用前备份后用（与 test_console.py 一样的做法）。
模型页的"下载安装模型"（GET /neo/catalog / POST /neo/install）也在这里：安装跑的是
临时目录里一份只会打印的假 setup.py，真的下载不跑。
"下载源"那一排（POST /neo/source）也在这里：记住的源要进安装命令、镜像走 HF_ENDPOINT 不进
--source、认不得的源挡回来且不写 source.json。
"复制 token"（POST /neo/token 的 action）也在这里：reveal 原样交回当前 key 且不写任何文件、
没有 key 时明说、非本机来的请求只给掩码、unset 清服务上的 key 而留着缓存、坏 action 不写。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from neo import connectors, models, netprobe, state, tokens, wizard     # noqa: E402
from neo.console_server import ConsoleServer                        # noqa: E402
from neo.engines.base import EngineAdapter                          # noqa: E402
from neo.supervisor import Supervisor                               # noqa: E402

from neo.tests.test_console import req                             # noqa: E402
from neo.tests.test_models import FAKE_SETUP                       # noqa: E402
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

    # ---------------- 复制 token（POST /neo/token 的 action） ----------------
    def test_token_reveal_gives_the_same_key_back_and_writes_nothing(self):
        key = self.post("/neo/token")[1]["api_key"]
        before = state.LAUNCHER.read_bytes()
        code, data = self.post("/neo/token", {"action": "reveal"})
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["api_key"], key, "复制到的就是当前生效的那把")
        self.assertNotIn(key, data["api_key_masked"])
        self.assertEqual(state.LAUNCHER.read_bytes(), before, "reveal 不该写 launcher.json")
        self.assertEqual(tokens.cached_keys("http://127.0.0.1:8080", "minted"), [key],
                         "reveal 不该往 key 缓存里加东西")

    def test_token_reveal_says_there_is_nothing_to_copy(self):
        code, data = self.post("/neo/token", {"action": "reveal"})
        self.assertEqual(code, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["api_key"], "")
        self.assertIn("没有 token", data["note"])

    def test_token_reveal_refuses_a_peer_that_is_not_local(self):
        key = self.post("/neo/token")[1]["api_key"]
        data = self.console.make_token({"action": "reveal"}, loopback=False)
        self.assertFalse(data["ok"])
        self.assertEqual(data["api_key"], "", "非本机来的请求不该拿到完整 key")
        self.assertEqual(data["api_key_masked"], tokens.mask_of(key), "掩码可以给")
        self.assertIn("本机", data["note"])

    def test_token_unset_keeps_the_cache_and_forget_clears_it(self):
        key = self.post("/neo/token")[1]["api_key"]
        self.assertTrue(self.console.make_token({"action": "unset"})["ok"])
        self.assertEqual(self.sup.api_key, "")
        self.assertEqual(state.launcher_config()["api_key"], "")
        self.assertEqual(tokens.cached_keys("http://127.0.0.1:8080", "minted"), [key],
                         "unset 只清服务上的 key，缓存留着")
        self.assertEqual(self.console.make_token({"action": "forget"})["removed"], 0,
                         "服务上没有 key 时 forget 无事可做")
        self.sup.api_key = key
        self.assertEqual(self.console.make_token({"action": "forget"})["removed"], 1)
        self.assertEqual(tokens.cached_keys("http://127.0.0.1:8080", "minted"), [])

    def test_token_an_unknown_action_writes_nothing(self):
        code, data = self.post("/neo/token", {"action": "zzz"})
        self.assertEqual(code, 200)
        self.assertFalse(data["ok"])
        self.assertIn("不认识的 action", data["error"])
        self.assertEqual(self.sup.api_key, "", "坏 action 不该生成 key")
        self.assertFalse(tokens.key_cache_path().exists())

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

    def test_catalog_endpoint_lists_the_install_menu(self):
        code, data = self.get("/neo/catalog")
        self.assertEqual(code, 200)
        self.assertTrue(data["has_setup"], "这个仓库的根目录里就有 setup.py")
        keys = {(m["family"], m["model"]) for m in data["models"]}
        self.assertIn(("qwen", "IQ3_S"), keys)
        self.assertIn(("coder", "IQ1_M"), keys)
        self.assertTrue(data["recommendation"]["why"])
        self.assertGreater(data["machine"]["ram_gb"], 0.0)
        self.assertIn("wizard", data)
        for item in data["models"]:
            self.assertTrue(item["args"].startswith("--family "), item["args"])
            self.assertIn(item["fit"], ("fits", "tight", "no"))

    def test_install_refuses_anything_not_on_the_menu(self):
        code, data = self.post("/neo/install", {"family": "nope", "model": "IQ9"})
        self.assertEqual(code, 200)
        self.assertFalse(data["started"])
        self.assertTrue(data["notes"])
        self.assertFalse(wizard.run_state()["running"], "挡回来时不该起子进程")


    def test_read_endpoints_do_not_write_anything(self):
        for path in ("/neo/net", "/neo/lan", "/neo/connect", "/neo/wizard", "/neo/catalog"):
            self.get(path)
        self.assertFalse(tokens.key_cache_path().exists(), "读接口不该写 key 缓存")
        self.assertFalse(self.written, "GET 不该写任何 agent 的配置")


class TestInstallEndpoint(unittest.TestCase):
    """POST /neo/install 与 GET /neo/catalog 里的安装状态。

    根目录是临时目录里一份只会打印、然后睡 3 秒的假 setup.py —— 装的动作是真的，
    下载是假的。wizard 的日志写在临时 state 里。
    """

    def setUp(self):
        self._probe = netprobe.probe_cached
        netprobe.probe_cached = lambda timeout=3.0, force=False, connect=None: dict(FAKE_NET)
        self._tmp = tempfile.TemporaryDirectory()
        self._state, self._logs = state.STATE, state.LOGS
        self._source = state.SOURCE
        state.STATE = Path(self._tmp.name)
        state.LOGS = state.STATE / "logs"
        state.SOURCE = state.STATE / "source.json"
        self.root = Path(self._tmp.name) / "root"
        self.root.mkdir()
        # 假的 setup.py 要能真跑起来：FAKE_SETUP 是给 ast 读的，值里的 hf(...) 是调用，
        # 直接执行会 NameError 立刻退出（子进程 0.03 秒就没了，"有一个在跑"根本测不出来）
        (self.root / "setup.py").write_text(
            "def hf(repo, name):\n    return [repo, name]\n" + FAKE_SETUP
            + "\nimport sys, time\nprint('install-cmd', sys.argv[1:])\n"
            "time.sleep(0.5)\n", encoding="utf-8")
        self.sup = Supervisor(FakeAdapter(self.root), 8080)
        self.console = ConsoleServer(self.sup, host="127.0.0.1", port=free_port())
        self.url = self.console.start()

    def tearDown(self):
        self._drain()
        self.console.stop()
        netprobe.probe_cached = self._probe
        state.STATE, state.LOGS = self._state, self._logs
        state.SOURCE = self._source
        self._tmp.cleanup()

    def _drain(self):
        """等子进程跑完，别让临时目录带着一个活进程被删掉。"""
        deadline = time.time() + 12.0
        while wizard.run_state()["running"] and time.time() < deadline:
            time.sleep(0.1)

    def post(self, path, payload):
        code, _, text = req(self.url.rstrip("/") + path, method="POST", body=payload or {})
        return code, json.loads(text)

    def get(self, path):
        code, _, body = req(self.url.rstrip("/") + path)
        return code, json.loads(body)

    def test_install_runs_the_command_it_shows(self):
        code, data = self.post("/neo/install", {"family": "coder", "model": "IQ1_M"})
        self.assertEqual(code, 200)
        self.assertTrue(data["started"], data)
        self.assertTrue(data["running"])
        self.assertEqual(data["argv"][data["argv"].index("--family") + 1], "coder")
        self.assertEqual(data["argv"][data["argv"].index("--model") + 1], "IQ1_M")
        self.assertIn("--family coder --model IQ1_M", wizard.log_tail())
        deadline = time.time() + 10.0
        while "install-cmd" not in wizard.log_tail() and time.time() < deadline:
            time.sleep(0.1)
        self.assertIn("--family coder --model IQ1_M", wizard.log_tail())

    def test_install_refuses_a_second_one_while_the_first_is_running(self):
        first = self.post("/neo/install", {"family": "qwen", "model": "IQ2_XS"})
        self.assertTrue(first[1]["started"])
        code, second = self.post("/neo/install", {"family": "swift", "model": "IQ3_S"})
        self.assertEqual(code, 200)
        self.assertFalse(second["started"])
        self.assertTrue(second["running"])
        self.assertIn("安装在跑", " ".join(second["notes"]))

    def test_catalog_shows_the_install_that_is_running(self):
        self.post("/neo/install", {"family": "coder", "model": "IQ1_M"})
        code, data = self.get("/neo/catalog")
        self.assertEqual(code, 200)
        self.assertTrue(data["wizard"]["running"])
        self.assertIn("--family coder", data["log_tail"])

    def test_install_refuses_a_bad_family_and_starts_nothing(self):
        code, data = self.post("/neo/install", {"family": "swift", "model": "IQ1_M"})
        self.assertEqual(code, 200)
        self.assertFalse(data["started"])
        self.assertTrue(data["notes"])
        self.assertFalse(wizard.run_state()["running"])
        self.assertNotIn("install-cmd", wizard.log_tail())

    # ---------------- 下载源切换（POST /neo/source） ----------------
    def test_the_catalog_carries_the_source_row(self):
        code, data = self.get("/neo/catalog")
        src = data["sources"]
        self.assertEqual(code, 200)
        self.assertEqual([c["key"] for c in src["choices"]],
                         ["probe", "huggingface", "modelscope", "mirror"])
        self.assertEqual(src["chosen"], "probe", "没选过就是按探测建议")
        self.assertEqual(src["suggested"], "modelscope", "FAKE_NET 说 huggingface.co 连不上")
        self.assertEqual(src["suffix"], " --source modelscope")
        by = {c["key"]: c for c in src["choices"]}
        self.assertEqual(by["modelscope"]["ms"], 31.0)
        self.assertFalse(by["huggingface"]["reachable"])

    def test_the_source_endpoint_remembers_the_choice(self):
        code, data = self.post("/neo/source", {"source": "modelscope"})
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["chosen"], "modelscope")
        self.assertEqual(models.source_choice(), "modelscope")
        self.assertTrue(state.SOURCE.is_file())

    def test_the_source_endpoint_refuses_an_unknown_one_and_writes_nothing(self):
        code, data = self.post("/neo/source", {"source": "baidu"})
        self.assertEqual(code, 200)
        self.assertFalse(data["ok"])
        self.assertIn("不认识的下载源", data["error"])
        self.assertEqual(models.source_choice(), "probe")
        self.assertFalse(state.SOURCE.exists(), "认不得就不该写 source.json")

    def test_the_remembered_source_reaches_the_install_command(self):
        self.post("/neo/source", {"source": "modelscope"})
        code, data = self.post("/neo/install", {"family": "coder", "model": "IQ1_M"})
        self.assertEqual(code, 200)
        self.assertTrue(data["started"], data)
        self.assertEqual(data["argv"][data["argv"].index("--source") + 1], "modelscope")
        self.assertIn("--source modelscope", wizard.log_tail())

    def test_a_mirror_install_carries_the_endpoint_not_a_source_flag(self):
        code, data = self.post("/neo/install",
                               {"family": "coder", "model": "IQ1_M", "source": "mirror"})
        self.assertEqual(code, 200)
        self.assertTrue(data["started"], data)
        self.assertNotIn("--source", data["argv"], "--source 里没有镜子这一项")
        self.assertEqual(data["env"], {"HF_ENDPOINT": "https://hf-mirror.com"})
        self.assertIn("HF_ENDPOINT=https://hf-mirror.com", wizard.log_tail())

    def test_an_unknown_source_in_the_install_body_starts_nothing(self):
        code, data = self.post("/neo/install",
                               {"family": "coder", "model": "IQ1_M", "source": "baidu"})
        self.assertEqual(code, 200)
        self.assertFalse(data["started"])
        self.assertIn("不认识的下载源", " ".join(data["notes"]))
        self.assertFalse(wizard.run_state()["running"], "挡回来时不该起子进程")

    def test_a_fresh_catalog_does_not_break_the_probe(self):
        code, data = self.get("/neo/catalog?fresh=1")
        self.assertEqual(code, 200)
        self.assertEqual(data["sources"]["suggested"], "modelscope")


if __name__ == "__main__":
    unittest.main(verbosity=2)

