"""neo/tests/test_wizard.py - 安装向导：命令怎么拼、缺什么怎么说、跑起来日志落在哪。

不跑真的 setup.py（那要下载模型）：跑的是临时目录里一个只会打印的 setup.py。
netprobe 的探测结果用假的，不碰网络。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from neo import netprobe, state, wizard                              # noqa: E402

FAKE_NET = {"targets": [], "ok": ["www.modelscope.cn"], "source": "modelscope",
            "reason": "huggingface.co 连不上，www.modelscope.cn 连得上", "at": 0.0}


class SourceCase(unittest.TestCase):
    def setUp(self):
        self._probe = netprobe.probe_cached
        netprobe.probe_cached = lambda timeout=3.0, force=False, connect=None: dict(FAKE_NET)

    def tearDown(self):
        netprobe.probe_cached = self._probe


class TestSetupArgv(SourceCase):
    def test_detected_modelscope_adds_the_flag(self):
        argv = wizard.setup_argv(ROOT, port=8080)
        self.assertIn("--source", argv)
        self.assertEqual(argv[argv.index("--source") + 1], "modelscope")

    def test_user_source_wins(self):
        argv = wizard.setup_argv(ROOT, port=8080, source="huggingface")
        self.assertEqual(argv[argv.index("--source") + 1], "huggingface")

    def test_non_default_source_is_not_tampered_with(self):
        argv = wizard.setup_argv(ROOT, port=8080, source="auto")
        self.assertEqual(argv[argv.index("--source") + 1], "auto")

    def test_non_interactive_flags(self):
        argv = wizard.setup_argv(ROOT, port=8123)
        self.assertIn("--yes", argv)
        self.assertIn("--no-start", argv)
        self.assertEqual(argv[argv.index("--port") + 1], "8123")

    def test_loopback_host_adds_nothing(self):
        argv = wizard.setup_argv(ROOT, port=8080, host="127.0.0.1")
        self.assertNotIn("--host", argv, "默认路径不该出现 --host")

    def test_lan_host_and_key_are_passed_through(self):
        argv = wizard.setup_argv(ROOT, port=8080, host="0.0.0.0", api_key="sk-neo-x")
        self.assertEqual(argv[argv.index("--host") + 1], "0.0.0.0")
        self.assertEqual(argv[argv.index("--api-key") + 1], "sk-neo-x")

    def test_command_targets_the_root_setup_py(self):
        argv = wizard.setup_argv(ROOT, port=8080)
        self.assertEqual(Path(argv[1]), Path(ROOT) / "setup.py")


class TestSteps(SourceCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        super().tearDown()
        self._tmp.cleanup()

    def test_missing_setup_py_is_reported(self):
        items = {x["name"]: x for x in wizard.steps(self.root)}
        self.assertFalse(items["根目录"]["ok"])
        self.assertIn("setup.py", items["根目录"]["text"])

    def test_no_models_is_said_plainly(self):
        (self.root / "setup.py").write_text("print('x')\n", encoding="utf-8")
        items = {x["name"]: x for x in wizard.steps(self.root)}
        self.assertTrue(items["根目录"]["ok"])
        self.assertFalse(items["模型"]["ok"])
        self.assertIn("没有装好的模型", items["模型"]["text"])

    def test_lan_without_key_is_not_ready(self):
        items = {x["name"]: x for x in wizard.steps(self.root, host="0.0.0.0", api_key="")}
        self.assertFalse(items["对外"]["ok"])
        self.assertFalse(wizard.status(self.root, host="0.0.0.0")["ready"])

    def test_net_step_names_the_suggested_source(self):
        items = {x["name"]: x for x in wizard.steps(self.root)}
        self.assertIn("modelscope", items["下载源"]["text"])


class TestRun(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._logs = state.LOGS
        state.LOGS = Path(self._tmp.name) / "logs"
        self._probe = netprobe.probe_cached
        netprobe.probe_cached = lambda timeout=3.0, force=False, connect=None: dict(
            {"targets": [], "ok": [], "source": "huggingface", "reason": "都连不上", "at": 0.0})

    def tearDown(self):
        netprobe.probe_cached = self._probe
        state.LOGS = self._logs
        self._tmp.cleanup()

    def test_missing_setup_py_refuses_to_run(self):
        result = wizard.run_setup(self.root)
        self.assertFalse(result["started"])
        self.assertTrue(result["notes"])

    def test_output_lands_in_the_wizard_log(self):
        (self.root / "setup.py").write_text(
            "import sys\nprint('wizard-line', sys.argv[1:])\n", encoding="utf-8")
        result = wizard.run_setup(self.root, port=8080)
        self.assertTrue(result["started"])
        self.assertEqual(result["code"], 0)
        tail = wizard.log_tail()
        self.assertIn("wizard-line", tail)
        self.assertIn("--yes", tail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
