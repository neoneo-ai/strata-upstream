"""neo/tests/test_start.py - `NeoStrata.exe start <agent>` / `python -m neo.start <agent>`。

一行命令的三件事都在这里测：那一行长什么样（根目录有 exe 用 exe，没有用 python）、
配方算得对不对（地址、key、上下文、要清空的变量）、agent 真被带上下拉起来。

四个 agent 的目录与 Neo 的状态文件全指到临时目录，真实用户的 ~/.claude、~/.codex
一行不动；"拉起 agent" 那条测的是临时起的一个 python 子进程，不碰任何真 agent。
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
from unittest import mock

if __package__ in (None, ""):                       # 直接跑 python neo/tests/test_start.py 时没有父包
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "neo.tests"

from ._bootstrap import ENGINE, REPO                # noqa: E402

ROOT = REPO                                         # 老写法留下的名字：Neo 仓库的根

from neo import connectors, neo_entry, start, state, tokens               # noqa: E402

HOME_VARS = {"CLAUDE_CONFIG_DIR": "claude", "CODEX_HOME": "codex",
             "XDG_CONFIG_HOME": "xdg", "OPENCLAW_HOME": "openclaw"}
KEY = "sk-neo-" + "a" * 32


class StartBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "root"
        self.root.mkdir()
        self._launcher, self._state = state.LAUNCHER, state.STATE
        state.STATE = base / "state"
        state.LAUNCHER = state.STATE / "launcher.json"
        self._was = {v: os.environ.get(v) for v in HOME_VARS}
        self._was[start.ENV_SERVER_URL] = os.environ.get(start.ENV_SERVER_URL)
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
            code = start.main(["--root", str(self.root), *argv])
        return code, out.getvalue()


class TestQuickCommand(StartBase):
    def test_no_exe_gives_the_python_line(self):
        self.assertEqual(start.quick_command(self.root, "claude"),
                         "python -m neo.start claude")

    def test_exe_in_the_root_wins(self):
        (self.root / "NeoStrata.exe").write_bytes(b"MZ")
        self.assertEqual(start.quick_command(self.root, "codex"),
                         "NeoStrata.exe start codex")

    def test_list_prints_one_line_per_agent(self):
        code, text = self.run_cli("list", "--engine", "mock")
        self.assertEqual(code, 0)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        self.assertEqual(len(lines), len(connectors.AGENTS))
        for name in connectors.AGENTS:
            self.assertIn(f"neo.start {name}", text)


class TestRecipe(StartBase):
    def test_no_launch_prints_the_paste_block(self):
        code, text = self.run_cli("claude", "--engine", "mock", "--port", "8123",
                                  "--key", KEY, "--model", "qwen3.8-flash-next",
                                  "--no-launch", "--shell", "posix")
        self.assertEqual(code, 0)
        self.assertIn("unset ANTHROPIC_API_KEY", text)
        self.assertIn(f"export ANTHROPIC_AUTH_TOKEN={KEY}", text)
        last = [ln for ln in text.splitlines() if ln.strip()][-1]
        self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:8123", last)
        self.assertTrue(last.startswith("ANTHROPIC_API_KEY="), "最后一行自带全部变量")
        self.assertIn(" claude --model qwen3.8-flash-next --settings ", last)

    def test_json_carries_the_quick_line_and_a_masked_key(self):
        code, text = self.run_cli("claude", "--engine", "mock", "--port", "8123",
                                  "--key", KEY, "--json")
        self.assertEqual(code, 0)
        item = json.loads(text)
        self.assertEqual(item["quick"], "python -m neo.start claude")
        self.assertEqual(item["key"], KEY)
        self.assertEqual(item["key_masked"], tokens.mask_of(KEY))

    def test_context_length_flag_overrides_the_model_window(self):
        code, text = self.run_cli("claude", "--engine", "mock", "--port", "8123",
                                  "--key", KEY, "--model", "qwen3.8-flash-next",
                                  "--context-length", "131072", "--json")
        item = json.loads(text)
        self.assertEqual(item["context"], 131072)
        self.assertEqual(item["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "131072")
        self.assertEqual(item["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "131072")

    def test_server_url_env_points_at_a_remote_strata(self):
        os.environ[start.ENV_SERVER_URL] = "http://10.1.2.3:9000"
        code, text = self.run_cli("claude", "--engine", "mock", "--key", KEY, "--json")
        item = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(item["base"], "http://10.1.2.3:9000")
        self.assertEqual(item["env"]["ANTHROPIC_BASE_URL"], "http://10.1.2.3:9000")

    def test_unknown_agent_says_what_is_available(self):
        code, text = self.run_cli("zzz")
        self.assertEqual(code, 2)
        self.assertIn("不认识的 agent", text)


class TestLaunch(StartBase):
    def item(self, command):
        model = connectors.Model("qwen3.8-flash-next", 202072)
        item = connectors.recipe("claude", "http://127.0.0.1:8123", KEY, model)
        item["key"] = KEY
        self._command = command
        return item

    def test_launch_runs_the_agent_with_our_env(self):
        marker = Path(self._tmp.name) / "child.txt"
        code_line = ("import os,sys;"
                     f"open(r'{marker}','w').write('|'.join(["
                     "os.environ.get('ANTHROPIC_MODEL','<none>'),"
                     "os.environ.get('ANTHROPIC_BASE_URL','<none>'),"
                     "os.environ.get('ANTHROPIC_API_KEY','<unset>'),"
                     "sys.argv[-1]]))")
        item = self.item([sys.executable, "-c", code_line, "x"])
        with mock.patch.object(connectors, "command_for", lambda *a, **k: list(self._command)):
            code = start.launch(item, ["--resume"], new_window=False)
        self.assertEqual(code, 0)
        model, base, cloud, forwarded = marker.read_text(encoding="utf-8").split("|")
        self.assertEqual(model, "qwen3.8-flash-next")
        self.assertEqual(base, "http://127.0.0.1:8123")
        self.assertEqual(cloud, "<unset>", "云端账号的开关变量必须清掉")
        self.assertEqual(forwarded, "--resume", "-- 后面的参数要原样交给 agent")

    def test_launch_env_drops_cloud_flags_and_sets_ours(self):
        env = {"ANTHROPIC_API_KEY": "sk-ant-x", "CLAUDE_CODE_USE_BEDROCK": "1",
               "PATH": os.environ.get("PATH", "")}
        model = connectors.Model("qwen3.8-flash-next", 202072)
        out = start.launch_env("claude", "http://127.0.0.1:8123", KEY, model, env=env)
        self.assertNotIn("ANTHROPIC_API_KEY", out)
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", out)
        self.assertEqual(out["ANTHROPIC_AUTH_TOKEN"], KEY)
        self.assertEqual(out["ANTHROPIC_MODEL"], "qwen3.8-flash-next")

    def test_missing_binary_prints_the_way_out(self):
        item = self.item(["neo-start-no-such-binary"])
        with mock.patch.object(connectors, "command_for", lambda *a, **k: list(self._command)):
            code = start.launch(item, [], new_window=False)
        self.assertEqual(code, 2)

    def test_launch_main_launches_instead_of_printing(self):
        item = self.item([sys.executable, "-c", "pass"])
        with mock.patch.object(connectors, "command_for", lambda *a, **k: list(self._command)), \
                mock.patch.object(start, "stdio_is_console", lambda: True):
            code, text = self.run_cli("claude", "--engine", "mock", "--port", "8123",
                                      "--key", KEY, "--model", "qwen3.8-flash-next")
        self.assertEqual(code, 0)
        self.assertNotIn("ANTHROPIC_BASE_URL", text)
        self.assertIn("模型 qwen3.8-flash-next", text)


class TestExeDispatch(unittest.TestCase):
    def test_start_subcommand_dispatches(self):
        with mock.patch.object(start, "main", lambda argv: 7 if argv == ["claude"] else 0):
            self.assertEqual(neo_entry.dispatch(["start", "claude"]), 7)

    def test_no_subcommand_opens_the_window(self):
        self.assertIsNone(neo_entry.dispatch([]))
        self.assertIsNone(neo_entry.dispatch(["--port", "8080"]))

    def test_windowed_exe_relaunches_itself_in_a_console(self):
        """窗口版 exe（sys.stdout 是空的、sys.frozen 在）跑子命令要先开一个控制台。"""
        fake = type("P", (), {"returncode": 3})
        with mock.patch.object(neo_entry, "needs_console", lambda: True), \
                mock.patch("subprocess.run", lambda *a, **k: fake):
            self.assertEqual(neo_entry.dispatch(["start", "claude"]), 3)

    def test_needs_console_is_false_in_a_terminal(self):
        self.assertFalse(neo_entry.needs_console())


if __name__ == "__main__":
    unittest.main()
