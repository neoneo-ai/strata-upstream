"""neo/tests/test_connectors.py - 四个 agent 的接入配方：合并、备份、幂等、打印块。

全部在临时目录里跑：CLAUDE_CONFIG_DIR / CODEX_HOME / XDG_CONFIG_HOME / OPENCLAW_HOME
四个环境变量指到临时目录，测试不碰用户真的 ~/.claude、~/.codex。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

if __package__ in (None, ""):                       # 直接跑 python neo/tests/test_connectors.py 时没有父包
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "neo.tests"

from ._bootstrap import ENGINE, REPO                # noqa: E402

ROOT = REPO                                         # 老写法留下的名字：Neo 仓库的根

from neo import connectors                                           # noqa: E402

BASE = "http://127.0.0.1:8080"
KEY = "sk-neo-" + "a" * 32
MODEL = connectors.Model("qwen3.8-flash-next", 202072)


def env_for_homes(tmp: Path) -> dict:
    return {"CLAUDE_CONFIG_DIR": str(tmp / "claude"),
            "CODEX_HOME": str(tmp / "codex"),
            "XDG_CONFIG_HOME": str(tmp / "xdg"),
            "OPENCLAW_HOME": str(tmp / "openclaw")}


class TestClaude(unittest.TestCase):
    def test_env_points_at_the_local_server(self):
        env = connectors.claude_env(BASE, KEY, MODEL)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], BASE)
        self.assertNotIn("/v1", env["ANTHROPIC_BASE_URL"])
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], KEY)
        self.assertEqual(env["ANTHROPIC_MODEL"], MODEL.id)

    def test_context_window_is_pinned(self):
        env = connectors.claude_env(BASE, KEY, MODEL)
        self.assertEqual(env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "202072")
        self.assertEqual(env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "202072")
        self.assertEqual(env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "90")

    def test_settings_overlay_is_json_with_the_unset_list(self):
        overlay = json.loads(connectors.claude_settings_overlay(
            MODEL, connectors.claude_env(BASE, KEY, MODEL)))
        self.assertEqual(overlay["availableModels"], [MODEL.id])
        for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK"):
            self.assertIn(name, overlay["env"])
            self.assertEqual(overlay["env"][name], "")

    def test_merge_keeps_the_users_own_keys(self):
        existing = json.dumps({"permissions": {"allow": ["mcp__codegraph__*"]},
                               "hooks": {"UserPromptSubmit": [{"hooks": [
                                   {"type": "command", "command": "codegraph prompt-hook"}]}]}},
                              indent=2)
        text, _ = connectors.merge_claude_settings(existing, BASE, KEY, MODEL)
        merged = json.loads(text)
        self.assertEqual(merged["permissions"]["allow"], ["mcp__codegraph__*"])
        self.assertEqual(merged["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"],
                         "codegraph prompt-hook")
        self.assertEqual(merged["env"]["ANTHROPIC_BASE_URL"], BASE)

    def test_merge_is_idempotent(self):
        text, _ = connectors.merge_claude_settings("", BASE, KEY, MODEL)
        again, _ = connectors.merge_claude_settings(text, BASE, KEY, MODEL)
        self.assertEqual(json.loads(text), json.loads(again))

    def test_unparseable_file_is_left_alone(self):
        text, notes = connectors.merge_claude_settings("{ 这不是 JSON", BASE, KEY, MODEL)
        self.assertIsNone(text)
        self.assertTrue(notes)


class TestCodex(unittest.TestCase):
    def test_provider_table_shape(self):
        table = connectors.codex_provider_table(BASE, KEY)
        self.assertIn("[model_providers.neo]", table)
        self.assertIn(f'base_url = "{BASE}/v1"', table)
        self.assertIn('wire_api = "responses"', table)
        self.assertIn("requires_openai_auth = false", table)
        self.assertIn(f"experimental_bearer_token = {json.dumps(KEY)}", table)
        self.assertIn("stream_idle_timeout_ms = 1200000", table)

    def test_provider_table_without_key_uses_the_env_var(self):
        table = connectors.codex_provider_table(BASE)
        self.assertIn('env_key = "NEO_API_KEY"', table)
        self.assertNotIn("experimental_bearer_token", table)

    def test_merge_keeps_user_sections_and_replaces_ours(self):
        existing = ('model = "qwen3.6-35b-a3b-ud"\n'
                    'model_provider = "lmstudio"\n'
                    'oss_provider = "lmstudio"\n'
                    "\n[projects.'d:\\work']\ntrust_level = \"trusted\"\n"
                    "\n[mcp_servers.codegraph]\ncommand = \"codegraph\"\n"
                    'args = ["serve", "--mcp"]\n')
        merged = connectors.merge_codex_config(existing, BASE, KEY)
        self.assertIn("[projects.'d:\\work']", merged)
        self.assertIn('trust_level = "trusted"', merged)
        self.assertIn("[mcp_servers.codegraph]", merged)
        self.assertIn('model_provider = "neo"', merged)
        self.assertIn('oss_provider = "neo"', merged)
        self.assertNotIn('"lmstudio"', merged.split("[projects")[0])
        self.assertEqual(merged.count("[model_providers.neo]"), 1)

    def test_merge_twice_does_not_stack(self):
        once = connectors.merge_codex_config("", BASE, KEY)
        twice = connectors.merge_codex_config(once, BASE, KEY)
        self.assertEqual(twice.count("[model_providers.neo]"), 1)
        self.assertEqual(once.count("model_provider = "), 1)
        self.assertEqual(twice, once)

    def test_profile_file_pins_the_model(self):
        text = connectors.codex_profile_text(MODEL)
        self.assertIn('model_provider = "neo"', text)
        self.assertIn('model = "qwen3.8-flash-next"', text)
        self.assertIn("model_context_window = 202072", text)


class TestOpenCode(unittest.TestCase):
    def test_provider_entry_has_the_limits(self):
        entry = connectors.opencode_provider(BASE, KEY, MODEL)
        self.assertEqual(entry["npm"], "@ai-sdk/openai-compatible")
        self.assertEqual(entry["options"]["baseURL"], f"{BASE}/v1")
        self.assertEqual(entry["options"]["apiKey"], KEY)
        limit = entry["models"][MODEL.id]["limit"]
        self.assertEqual(limit["context"], 202072)
        self.assertEqual(limit["output"], connectors.opencode_output_limit(202072))

    def test_output_limit_formula(self):
        self.assertEqual(connectors.opencode_output_limit(202072), 32000)
        self.assertEqual(connectors.opencode_output_limit(8192), 2048)
        self.assertEqual(connectors.opencode_compaction_reserved(202072, 32000), 20207)

    def test_merge_sets_the_model_and_provider_lists(self):
        existing = json.dumps({"provider": {"openai": {"npm": "x"}},
                               "enabled_providers": ["openai"],
                               "disabled_providers": ["neo"]})
        text, _ = connectors.merge_opencode_config(existing, BASE, KEY, MODEL)
        config = json.loads(text)
        self.assertIn("openai", config["provider"])
        self.assertIn("neo", config["provider"])
        self.assertEqual(config["model"], f"neo/{MODEL.id}")
        self.assertIn("neo", config["enabled_providers"])
        self.assertNotIn("neo", config.get("disabled_providers", []))
        self.assertTrue(config["compaction"]["auto"])

    def test_unparseable_file_is_left_alone(self):
        text, notes = connectors.merge_opencode_config("{[", BASE, KEY, MODEL)
        self.assertIsNone(text)
        self.assertTrue(notes)


class TestOpenClaw(unittest.TestCase):
    def test_provider_entry(self):
        entry = connectors.openclaw_provider(BASE, KEY, MODEL)
        self.assertEqual(entry["baseUrl"], f"{BASE}/v1")
        self.assertEqual(entry["api"], "openai-completions")
        self.assertEqual(entry["models"][0]["contextWindow"], 202072)
        self.assertTrue(entry["models"][0]["maxTokens"] > 8192,
                        "不写 maxTokens 时 OpenClaw 把每条回答夹在 8192")

    def test_merge_sets_primary_and_kills_the_openai_memory_search(self):
        text, _ = connectors.merge_openclaw_config("", BASE, KEY, MODEL)
        config = json.loads(text)
        self.assertEqual(config["models"]["mode"], "merge")
        self.assertIn("neo", config["models"]["providers"])
        self.assertEqual(config["agents"]["defaults"]["model"]["primary"],
                         f"neo/{MODEL.id}")
        self.assertEqual(config["memory"]["search"]["provider"], "none")
        self.assertFalse(config["env"]["shellEnv"]["enabled"])

    def test_merge_keeps_a_users_own_embedding_config(self):
        existing = json.dumps({"memory": {"search": {"provider": "openai",
                                                     "model": "mine"}}})
        text, _ = connectors.merge_openclaw_config(existing, BASE, KEY, MODEL)
        config = json.loads(text)
        self.assertEqual(config["memory"]["search"]["provider"], "openai")
        self.assertEqual(config["memory"]["search"]["model"], "mine")


class TestWrite(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.env = env_for_homes(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_creates_backup_and_files(self):
        home = Path(self.env["CODEX_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text('model_provider = "ollama"\n', encoding="utf-8")
        result = connectors.write_config("codex", BASE, KEY, MODEL, env=self.env)
        self.assertTrue(result["written"])
        self.assertTrue((home / "config.toml.neo-bak").is_file(), "旧内容要留一份备份")
        self.assertIn('model_provider = "ollama"',
                      (home / "config.toml.neo-bak").read_text(encoding="utf-8"))
        merged = (home / "config.toml").read_text(encoding="utf-8")
        self.assertIn("[model_providers.neo]", merged)
        self.assertTrue((home / "neo.config.toml").is_file(), "profile 层也要写")

    def test_write_refuses_a_broken_file_and_leaves_it_behind(self):
        home = Path(self.env["CLAUDE_CONFIG_DIR"])
        home.mkdir(parents=True, exist_ok=True)
        broken = home / "settings.json"
        broken.write_text("{ 坏了", encoding="utf-8")
        result = connectors.write_config("claude", BASE, KEY, MODEL, env=self.env)
        self.assertFalse(result["written"])
        self.assertEqual(broken.read_text(encoding="utf-8"), "{ 坏了")
        self.assertFalse((home / "settings.json.neo-bak").exists())

    def test_paths_follow_the_env_knobs(self):
        self.assertEqual(connectors.config_path("claude", env=self.env),
                         Path(self.env["CLAUDE_CONFIG_DIR"]) / "settings.json")
        self.assertEqual(connectors.config_path("codex", env=self.env),
                         Path(self.env["CODEX_HOME"]) / "config.toml")
        self.assertEqual(connectors.config_path("opencode", env=self.env),
                         Path(self.env["XDG_CONFIG_HOME"]) / "opencode" / "opencode.json")
        self.assertEqual(connectors.config_path("openclaw", env=self.env),
                         Path(self.env["OPENCLAW_HOME"]) / "openclaw.json")


class TestRecipeAndPrint(unittest.TestCase):
    def test_recipe_has_everything_the_page_needs(self):
        item = connectors.recipe("claude", BASE, KEY, MODEL)
        for key in ("agent", "title", "base", "model", "env", "unset", "command",
                    "config_path", "powershell", "posix", "notes"):
            self.assertIn(key, item)
        self.assertEqual(item["command"][0], "claude")
        self.assertIn("--settings", item["command"])

    def test_every_agent_has_an_env_and_unset_list(self):
        for agent in connectors.AGENTS:
            self.assertTrue(connectors.env_for(agent, BASE, KEY, MODEL), agent)
            self.assertTrue(connectors.unset_list(agent), agent)

    def test_powershell_block_quotes_dollars_and_quotes(self):
        lines = connectors.powershell_block("claude", BASE, KEY, MODEL)
        joined = "\n".join(lines)
        self.assertIn("Remove-Item Env:ANTHROPIC_API_KEY", joined)
        self.assertIn('$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8080"', joined)
        self.assertTrue(lines[-1].startswith("claude --model"), lines[-1])

    def test_posix_last_line_is_self_contained(self):
        lines = connectors.posix_block("claude", BASE, KEY, MODEL)
        last = lines[-1]
        self.assertIn("ANTHROPIC_BASE_URL=", last)
        self.assertIn("claude --model", last)
        self.assertIn("unset ANTHROPIC_API_KEY", "\n".join(lines))

    def test_codex_command_uses_the_profile(self):
        self.assertEqual(connectors.command_for("codex", BASE, KEY, MODEL),
                         ["codex", "--profile", "neo"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


