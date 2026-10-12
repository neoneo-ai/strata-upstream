"""neo/tests/test_connect_cli.py - `python -m neo.connect` 这条命令本身。

四个 agent 的目录全指到临时目录（CLAUDE_CONFIG_DIR / CODEX_HOME / XDG_CONFIG_HOME /
OPENCLAW_HOME），Neo 的状态文件也指到临时目录，所以 --persist 写的每一个文件都在
临时目录里，测试结束时整块删掉 —— 真实用户的 ~/.claude、~/.codex 一行不动。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from neo import connect, connectors, state, tokens                 # noqa: E402

HOME_VARS = {"CLAUDE_CONFIG_DIR": "claude", "CODEX_HOME": "codex",
             "XDG_CONFIG_HOME": "xdg", "OPENCLAW_HOME": "openclaw"}


class ConnectCliBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._launcher, self._state = state.LAUNCHER, state.STATE
        state.STATE = base / "state"
        state.LAUNCHER = state.STATE / "launcher.json"
        self._was = {v: os.environ.get(v) for v in HOME_VARS}
        for var, name in HOME_VARS.items():
            os.environ[var] = str(base / name)

    def tearDown(self):
        state.LAUNCHER, state.STATE = self._launcher, self._state
        for var, value in self._was.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value
        self._tmp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = connect.main(list(argv))
        return code, out.getvalue()


class TestList(ConnectCliBase):
    def test_list_names_the_four_agents_and_where_they_live(self):
        code, text = self.run_cli("list", "--engine", "mock")
        self.assertEqual(code, 0)
        for name in connectors.AGENTS:
            self.assertIn(name, text)
        self.assertIn(str(Path(os.environ["CLAUDE_CONFIG_DIR"])), text)
        self.assertIn("netprobe", text)

    def test_unknown_agent_says_what_is_available(self):
        code, text = self.run_cli("zzz")
        self.assertEqual(code, 2)
        self.assertIn("不认识的 agent", text)


class TestJson(ConnectCliBase):
    def test_json_carries_the_whole_recipe_and_writes_nothing(self):
        code, text = self.run_cli("claude", "--engine", "mock", "--port", "8123",
                                  "--key", "sk-neo-" + "a" * 32, "--json")
        self.assertEqual(code, 0)
        item = json.loads(text)
        self.assertEqual(item["agent"], "claude")
        self.assertEqual(item["base"], "http://127.0.0.1:8123")
        self.assertEqual(item["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8123")
        self.assertEqual(item["env"]["ANTHROPIC_AUTH_TOKEN"], "sk-neo-" + "a" * 32)
        self.assertEqual(item["unset"][0], "ANTHROPIC_API_KEY")
        self.assertTrue(item["powershell"] and item["posix"])
        self.assertIn("--settings", " ".join(item["command"]))
        for var in HOME_VARS:
            self.assertFalse(Path(os.environ[var]).exists(), f"--json 不该创建 {var} 的目录")
        self.assertFalse(tokens.key_cache_path().exists(), "给了 --key 就不该写 key 缓存")

    def test_base_flag_wins_over_host_and_port(self):
        code, text = self.run_cli("codex", "--engine", "mock", "--port", "8123",
                                  "--base", "http://10.1.2.3:9000/", "--key", "sk-neo-" + "b" * 32,
                                  "--persist", "--json")
        self.assertEqual(code, 0)
        data = json.loads(text)
        self.assertEqual(data["base"], "http://10.1.2.3:9000")
        # Codex 认配置文件里的 base_url：环境变量只带 key，地址写进 config.toml
        self.assertEqual(data["env"][connectors.CODEX_ENV_KEY], "sk-neo-" + "b" * 32)
        written = Path(os.environ["CODEX_HOME"], "config.toml")
        self.assertIn("base_url = \"http://10.1.2.3:9000/v1\"",
                      written.read_text(encoding="utf-8"))

    def test_key_source_is_said_and_the_missing_server_key_is_said(self):
        _, text = self.run_cli("claude", "--engine", "mock", "--key", "sk-neo-" + "c" * 32,
                               "--json")
        notes = json.loads(text)["notes"]
        self.assertIn("--key", notes[0])
        self.assertTrue(any("服务现在没设 key" in n for n in notes))



class TestPersist(ConnectCliBase):
    def test_persist_writes_only_inside_the_temp_homes(self):
        codex_home = Path(os.environ["CODEX_HOME"])
        codex_home.mkdir(parents=True, exist_ok=True)
        originals = {"config.toml": '# 用户自己写的\nmodel = "gpt-x"\n',
                     "neo.config.toml": "# 用户自己写的 profile\n"}
        for name, text in originals.items():
            (codex_home / name).write_text(text, encoding="utf-8")
        code, text = self.run_cli("codex", "--engine", "mock", "--port", "8123",
                                  "--key", "sk-neo-" + "d" * 32, "--persist", "--json")
        self.assertEqual(code, 0)
        write = json.loads(text)["write"]
        self.assertTrue(write["written"])
        files = [Path(p) for p in write["files"]]
        self.assertEqual(len(files), 2)                       # config.toml + neo.config.toml
        for path in files:
            self.assertTrue(str(path).startswith(str(codex_home)), f"{path} 不在临时目录里")
            backup = Path(str(path) + ".neo-bak")
            self.assertTrue(backup.exists(), "写之前要留备份")
            self.assertEqual(backup.read_text(encoding="utf-8"),
                             originals[path.name], "备份要留着用户原来那份")
        self.assertIn("wire_api = \"responses\"", files[0].read_text(encoding="utf-8"))
        self.assertIn("base_url = \"http://127.0.0.1:8123/v1\"",
                      files[0].read_text(encoding="utf-8"))
        self.assertIn('model = "gpt-x"', files[0].read_text(encoding="utf-8"))
        for var in ("CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "OPENCLAW_HOME"):
            self.assertFalse(Path(os.environ[var]).exists(), f"{var} 不该被碰")

    def test_running_it_twice_leaves_one_provider_block(self):
        args = ("codex", "--engine", "mock", "--port", "8123", "--key", "sk-neo-" + "e" * 32,
                "--persist", "--json")
        self.run_cli(*args)
        self.run_cli(*args)
        text = Path(os.environ["CODEX_HOME"], "config.toml").read_text(encoding="utf-8")
        self.assertEqual(text.count("[model_providers.neo]"), 1)
        self.assertEqual(text.count("model_provider = \"neo\""), 1)



    def test_json_flag_writes_and_persist_writes(self):
        _, text = self.run_cli("openclaw", "--engine", "mock", "--key", "sk-neo-" + "f" * 32,
                               "--json")
        self.assertFalse(Path(os.environ["OPENCLAW_HOME"]).exists())
        item = json.loads(text)
        self.assertNotIn("write", item)
        # 打印块只有环境变量和命令；api 名字在配置文件里（下面 --persist 那半段查）
        self.assertTrue(item["config_only"], "openclaw 认配置文件")
        self.assertEqual(Path(item["config_path"]).name, "openclaw.json")
        self.assertEqual(item["powershell"][-1], "openclaw")
        code, text = self.run_cli("openclaw", "--engine", "mock", "--key", "sk-neo-" + "f" * 32,
                                  "--persist", "--json")
        self.assertEqual(code, 0)
        cfg = Path(os.environ["OPENCLAW_HOME"], "openclaw.json")
        self.assertTrue(cfg.exists())
        data = json.loads(cfg.read_text(encoding="utf-8"))
        self.assertEqual(data["models"]["providers"]["neo"]["api"], "openai-completions")
        self.assertEqual(data["agents"]["defaults"]["model"]["primary"], "neo/mock")


if __name__ == "__main__":
    unittest.main(verbosity=2)
