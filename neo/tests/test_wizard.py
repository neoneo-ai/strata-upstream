"""neo/tests/test_wizard.py - 安装向导：命令怎么拼、缺什么怎么说、跑起来日志落在哪。

不跑真的 setup.py（那要下载模型）：跑的是临时目录里一个只会打印的 setup.py。
netprobe 的探测结果用假的，不碰网络。
下载源切换（TestSourceSwitch 与 TestRun）：用户点的源怎么变成 --source 或 HF_ENDPOINT、
run_setup() 把环境真的带进子进程（跑一个打印 os.environ 的桩 setup.py）、wizard.log 头部写清
这次带了什么环境、认不得的源不起子进程。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

if __package__ in (None, ""):                       # 直接跑 python neo/tests/test_wizard.py 时没有父包
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "neo.tests"

from ._bootstrap import ENGINE, REPO                # noqa: E402

ROOT = REPO                                         # 老写法留下的名字：Neo 仓库的根

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

    def test_family_and_model_go_through_as_setup_py_flags(self):
        argv = wizard.setup_argv(ROOT, port=8080, family="coder", model="IQ1_M")
        self.assertEqual(argv[argv.index("--family") + 1], "coder")
        self.assertEqual(argv[argv.index("--model") + 1], "IQ1_M")

    def test_no_family_by_default_leaves_the_choosing_to_setup_py(self):
        argv = wizard.setup_argv(ROOT, port=8080)
        self.assertNotIn("--family", argv, "默认路径不该出现 --family")
        self.assertNotIn("--model", argv, "默认路径不该出现 --model")

    def test_family_comes_before_the_model(self):
        argv = wizard.setup_argv(ROOT, port=8080, family="qwen", model="IQ3_S")
        self.assertLess(argv.index("--family"), argv.index("--model"))


class TestSourceSwitch(SourceCase):
    """模型页选中一个下载源后，setup.py 那条命令长什么样（镜子走 HF_ENDPOINT，不是 --source）。"""

    def test_probe_choice_is_the_same_command_as_no_choice(self):
        self.assertEqual(wizard.setup_argv(ROOT, port=8080),
                         wizard.setup_argv(ROOT, port=8080, source="probe"))

    def test_probe_choice_follows_the_probe(self):
        argv = wizard.setup_argv(ROOT, port=8080, source="probe")
        self.assertEqual(argv[argv.index("--source") + 1], "modelscope",
                         "FAKE_NET 说 huggingface.co 连不上")

    def test_a_named_source_is_explicit_in_the_command(self):
        argv = wizard.setup_argv(ROOT, port=8080, source="modelscope")
        self.assertEqual(argv[argv.index("--source") + 1], "modelscope")
        argv = wizard.setup_argv(ROOT, port=8080, source="huggingface")
        self.assertEqual(argv[argv.index("--source") + 1], "huggingface")

    def test_the_mirror_adds_no_source_flag_but_an_env_var(self):
        argv = wizard.setup_argv(ROOT, port=8080, source="mirror")
        self.assertNotIn("--source", argv, "--source 里没有镜子这一项")
        self.assertIn("--yes", argv)
        self.assertEqual(wizard.source_env("mirror"),
                         {"HF_ENDPOINT": "https://hf-mirror.com"})

    def test_other_sources_add_no_env(self):
        for source in ("", "probe", "auto", "modelscope", "huggingface"):
            self.assertEqual(wizard.source_env(source), {}, f"{source} 不该动环境")

    def test_cli_takes_the_mirror_and_the_probe(self):
        self.assertEqual(wizard.parse_args(["--source", "mirror"]).source, "mirror")
        self.assertEqual(wizard.parse_args(["--source", "probe"]).source, "probe")
        self.assertEqual(wizard.parse_args([]).source, "")


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
        # 这台机器上要是本来设了 HF_ENDPOINT，"没选镜子就不该塞"那条检查就没意义了
        self._endpoint = os.environ.pop("HF_ENDPOINT", "")

    def tearDown(self):
        netprobe.probe_cached = self._probe
        state.LOGS = self._logs
        if self._endpoint:
            os.environ["HF_ENDPOINT"] = self._endpoint
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

    def test_the_family_and_model_reach_the_child_process(self):
        (self.root / "setup.py").write_text(
            "import sys\nprint('picked', sys.argv[1:])\n", encoding="utf-8")
        wizard.run_setup(self.root, family="coder", model="IQ1_M", port=8080)
        tail = wizard.log_tail()
        self.assertIn("'--family', 'coder'", tail)
        self.assertIn("'--model', 'IQ1_M'", tail)

    def test_the_mirror_endpoint_reaches_the_child_and_the_log_line(self):
        (self.root / "setup.py").write_text(
            "import os\nprint('endpoint', os.environ.get('HF_ENDPOINT', ''))\n", encoding="utf-8")
        result = wizard.run_setup(self.root, port=8080, source="mirror")
        self.assertTrue(result["started"])
        self.assertEqual(result["env"], {"HF_ENDPOINT": "https://hf-mirror.com"})
        tail = wizard.log_tail()
        self.assertIn("endpoint https://hf-mirror.com", tail, "子进程要真的带着这个环境变量")
        header = tail.split("=== ")[-1].splitlines()[0]
        self.assertIn("HF_ENDPOINT=https://hf-mirror.com", header, "日志头部要写清带了什么")
        self.assertIn("--yes", header)

    def test_a_normal_source_leaves_the_environment_alone(self):
        (self.root / "setup.py").write_text(
            "import os\nprint('endpoint', os.environ.get('HF_ENDPOINT', 'none'))\n",
            encoding="utf-8")
        wizard.run_setup(self.root, port=8080, source="modelscope")
        tail = wizard.log_tail()
        self.assertIn("endpoint none", tail, "没选镜子就不该塞 HF_ENDPOINT")
        self.assertIn("--source modelscope", tail)

    def test_cli_accepts_a_family(self):
        args = wizard.parse_args(["--family", "coder", "--model", "IQ1_M"])
        self.assertEqual((args.family, args.model), ("coder", "IQ1_M"))
        self.assertEqual(wizard.parse_args([]).family, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
