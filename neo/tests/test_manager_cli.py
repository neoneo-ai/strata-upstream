"""neo/tests/test_manager_cli.py - M3 加的那几个开关各自怎么说（不起窗口、不起服务）。

`--host` / `--lan` / `--api-key` / `--token` / `--connect` 决定的是"服务绑哪、要不要 key、
打印哪份接入命令"。这三件事都在 main() 之前就能算完，所以这里只测这几步：
launcher.json 指到临时目录，四个 agent 的配置目录也指到临时目录，真实用户的东西一行不动。

默认路径必须逐字节不变：三个开关都不给 = 127.0.0.1、没有 key、不打印任何东西。
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from neo import manager, state, tokens                       # noqa: E402
from neo.engines import make_adapter                         # noqa: E402

HOME_VARS = {"CLAUDE_CONFIG_DIR": "claude", "CODEX_HOME": "codex",
             "XDG_CONFIG_HOME": "xdg", "OPENCLAW_HOME": "openclaw"}


class CliBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._launcher = state.LAUNCHER
        state.LAUNCHER = Path(self._tmp.name) / "launcher.json"
        self._was = {v: os.environ.get(v) for v in HOME_VARS}
        for var, name in HOME_VARS.items():
            os.environ[var] = str(Path(self._tmp.name) / name)

    def tearDown(self):
        state.LAUNCHER = self._launcher
        for var, value in self._was.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value
        self._tmp.cleanup()


class TestHostAndKey(CliBase):
    def test_nothing_given_is_the_old_default(self):
        args = manager.parse_args([])
        self.assertEqual(manager.resolve_host(args), "127.0.0.1")
        self.assertEqual(manager.resolve_api_key(args), "")

    def test_lan_flag_binds_everywhere(self):
        args = manager.parse_args(["--lan"])
        self.assertEqual(manager.resolve_host(args), "0.0.0.0")

    def test_host_beats_lan_flag(self):
        args = manager.parse_args(["--lan", "--host", "192.168.1.20"])
        self.assertEqual(manager.resolve_host(args), "192.168.1.20")

    def test_launcher_file_is_used_when_no_flag(self):
        state.save_launcher_config(host="10.0.0.5", api_key="sk-neo-" + "a" * 32)
        args = manager.parse_args([])
        self.assertEqual(manager.resolve_host(args), "10.0.0.5")
        self.assertEqual(manager.resolve_api_key(args), "sk-neo-" + "a" * 32)

    def test_command_line_beats_launcher_file(self):
        state.save_launcher_config(host="10.0.0.5", api_key="sk-neo-" + "a" * 32)
        args = manager.parse_args(["--host", "127.0.0.1", "--api-key", "sk-neo-" + "b" * 32])
        self.assertEqual(manager.resolve_host(args), "127.0.0.1")
        self.assertEqual(manager.resolve_api_key(args), "sk-neo-" + "b" * 32)

    def test_token_flag_mints_one_and_remembers_it(self):
        args = manager.parse_args(["--token"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            key = manager.resolve_api_key(args)
        self.assertTrue(tokens.looks_like_key(key))
        self.assertEqual(state.launcher_config()["api_key"], key)
        self.assertIn(key, out.getvalue())          # 用户得看得见全串才能填进 agent
        self.assertNotIn(key, state.MANAGER_LOG.read_text(encoding="utf-8")
                        if state.MANAGER_LOG.exists() else "")

    def test_token_flag_keeps_the_key_that_is_already_there(self):
        state.save_launcher_config(api_key="sk-neo-" + "c" * 32)
        args = manager.parse_args(["--token"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            key = manager.resolve_api_key(args)
        self.assertEqual(key, "sk-neo-" + "c" * 32)
        self.assertEqual(out.getvalue(), "")        # 已有 key 就不再生成、不打印

    def test_port_priority(self):
        adapter = make_adapter("mock", ROOT)
        self.assertEqual(manager.pick_port(manager.parse_args(["--port", "8123"]), adapter), 8123)
        state.save_launcher_config(port=8321)
        self.assertEqual(manager.pick_port(manager.parse_args([]), adapter), 8321)
        self.assertEqual(manager.pick_port(manager.parse_args(["--port", "8123"]), adapter), 8123)
        state.LAUNCHER.unlink()
        self.assertEqual(manager.pick_port(manager.parse_args([]), adapter), 8095)


class TestConnectBlock(CliBase):
    def _block(self, agent, **kw):
        adapter = make_adapter("mock", ROOT)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            manager.print_connect_block(adapter, 8080, kw.pop("host", "127.0.0.1"),
                                        kw.pop("api_key", ""), agent, **kw)
        return out.getvalue()

    def test_claude_block_names_the_base_and_the_command(self):
        text = self._block("claude", api_key="sk-neo-" + "d" * 32)
        self.assertIn("http://127.0.0.1:8080", text)
        self.assertIn("sk-neo-" + "d" * 32, text)
        self.assertIn("claude --model mock", text)
        self.assertIn("$env:ANTHROPIC_BASE_URL", text)

    def test_printing_a_block_writes_no_ones_file(self):
        self._block("codex", api_key="sk-neo-" + "e" * 32)
        for var, name in HOME_VARS.items():
            self.assertFalse(Path(os.environ[var]).exists(),
                             f"--connect 不该创建 {name} 的目录")

    def test_unknown_agent_is_said_plainly(self):
        text = self._block("zzz")
        self.assertIn("不认识的 agent", text)
        self.assertIn("claude / codex / opencode / openclaw", text)

    def test_no_key_says_how_to_get_one(self):
        text = self._block("claude")
        self.assertIn("token", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
