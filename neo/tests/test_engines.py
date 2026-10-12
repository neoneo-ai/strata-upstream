"""neo/tests/test_engines.py - 引擎适配层：发现模型配置、拼启动命令、没有模型时的表现。"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from neo.engines import make_adapter                           # noqa: E402
from neo.engines.base import EngineAdapter                     # noqa: E402
from neo.engines.strata import list_configs                    # noqa: E402


def write_config(folder: Path, name: str, **over):
    stem = name[:-len(".json")].replace("strata-", "")
    cfg = {"model_name": stem, "path": str(folder / (stem + ".gguf")),
           "port": 8080, "args": {"--context": 32768, "--flash-attention": True}}
    cfg.update(over)
    (folder / name).write_text(json.dumps(cfg), encoding="utf-8")
    return folder / name


class TestDiscover(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="neo-eng-"))
        a = write_config(self.tmp, "strata-a.json", port=8123)
        time.sleep(0.02)
        b = write_config(self.tmp, "strata-b.json")
        (self.tmp / "b.gguf").write_text("x", encoding="utf-8")
        self.a, self.b = a, b
        self.adapter = EngineAdapter(self.tmp)

    def test_list_and_order(self):
        items = list_configs(self.tmp)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["id"], "strata-b.json", "最近写入的排最前（setup.py 的 touch 语义）")
        self.assertEqual(items[1]["port"], 8123)
        self.assertEqual(items[1]["context"], 32768)
        self.assertTrue(items[0]["installed"], "gguf 在要标成已安装")
        self.assertFalse(items[1]["installed"], "gguf 不在要标出来")

    def test_default_picks_latest(self):
        adapter = make_adapter("strata", self.tmp)
        chosen = adapter.default_config()
        self.assertEqual(chosen["id"], "strata-b.json")
        named = make_adapter("strata", self.tmp, {"model": "strata-a.json"}).default_config()
        self.assertEqual(named["id"], "strata-a.json")

    def test_empty_root(self):
        import tempfile
        empty = Path(tempfile.mkdtemp(prefix="neo-eng-empty-"))
        adapter = make_adapter("strata", empty)
        self.assertEqual(adapter.discover(), [])
        self.assertIsNone(adapter.default_config())
        self.assertFalse(adapter.installed())
        with self.assertRaises(FileNotFoundError):
            adapter.plan(8080)

    def test_broken_json_is_skipped(self):
        (self.tmp / "strata-broken.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(len(list_configs(self.tmp)), 2)


class TestPlan(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="neo-plan-"))
        write_config(self.tmp, "strata-x.json", port=8080)
        (self.tmp / "x.gguf").write_text("x", encoding="utf-8")
        self.adapter = make_adapter("strata", self.tmp)

    def test_command_uses_upstream_flags(self):
        plan = self.adapter.plan(8123)
        self.assertTrue(plan.argv[1].replace("\\", "/").endswith("serve/server.py"))
        self.assertEqual(plan.argv[1], str(self.tmp / "serve" / "server.py"))
        self.assertIn("--engine", plan.argv)
        self.assertEqual(plan.argv[plan.argv.index("--engine") + 1], "strata")
        self.assertEqual(plan.argv[plan.argv.index("--port") + 1], "8123")
        self.assertEqual(plan.argv[plan.argv.index("--config") + 1], str(self.tmp / "strata-x.json"))
        self.assertNotIn("--open", plan.argv, "Neo 起来后不该再弹浏览器")
        self.assertEqual(plan.cwd, str(self.tmp))
        self.assertTrue(plan.log_path.endswith(".log"))
        self.assertGreaterEqual(plan.ready_timeout_s, 900)

    def test_extra_flags(self):
        plan = self.adapter.plan(8080, host="0.0.0.0", api_key="sekret", gpu="0,1")
        self.assertEqual(plan.argv[plan.argv.index("--host") + 1], "0.0.0.0")
        self.assertEqual(plan.argv[plan.argv.index("--api-key") + 1], "sekret")
        self.assertEqual(plan.argv[plan.argv.index("--gpu") + 1], "0,1")

    def test_missing_model_file_refused(self):
        (self.tmp / "x.gguf").unlink()
        with self.assertRaises(FileNotFoundError):
            self.adapter.plan(8080)

    def test_pages(self):
        pages = self.adapter.pages(8080)
        self.assertEqual(pages["chat"], "http://127.0.0.1:8080/")
        self.assertEqual(pages["monitor"], "http://127.0.0.1:8080/monitor.html")

    def test_mock_adapter(self):
        plan = make_adapter("mock", ROOT).plan(8095)
        self.assertEqual(plan.argv[plan.argv.index("--engine") + 1], "mock")
        self.assertEqual(plan.argv[plan.argv.index("--port") + 1], "8095")
        self.assertTrue(Path(plan.argv[1]).exists(), "mock 跑的是仓库里的 serve/server.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)
