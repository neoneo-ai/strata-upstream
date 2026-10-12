"""neo/tests/test_upstream_api.py - 与上游的接口自检：Neo 用到的命令行参数和 HTTP 字段还在不在。

Neo 不改上游，全靠上游已有的 CLI 与端点。上游改了名字，这条测试要第一时间报出来，
而不是等用户的 Neo 起不来。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


class TestServerApi(unittest.TestCase):
    def setUp(self):
        self.src = text(ROOT / "serve" / "server.py")

    def test_flags_neo_passes_still_exist(self):
        for flag in ("--engine", "--config", "--port", "--host", "--api-key", "--gpu"):
            self.assertIn(f'"{flag}"', self.src, f"serve/server.py 里找不到 {flag}")

    def test_mock_engine_still_available(self):
        self.assertIn("class MockEngine", self.src)
        self.assertIn('"mock"', self.src, "--engine 的可选值里还要有 mock")

    def test_health_fields_neo_reads(self):
        block = re.search(r'path in \("/health", "/api/health"\).*?self\._json\(200, \{(.*?)\}\)', self.src, re.S)
        self.assertTrue(block, "找不到 /health 的应答")
        for key in ("status", "max_context", "model", "loaded", "api_key"):
            self.assertIn(key, block.group(1), f"/health 不再返回 {key}，Neo 的探活要跟着改")

    def test_metrics_endpoint_exists(self):
        self.assertIn('path == "/metrics"', self.src)

    def test_web_pages_neo_embeds(self):
        self.assertTrue((ROOT / "serve" / "web" / "index.html").is_file())
        self.assertTrue((ROOT / "serve" / "web" / "monitor.html").is_file())
        self.assertIn("monitor.html", self.src)


class TestSetupApi(unittest.TestCase):
    """M2 要用的 setup.py 参数，先把"还在不在"钉住。"""

    def setUp(self):
        self.src = text(ROOT / "setup.py")

    def test_flags_neo_will_use(self):
        for flag in ("--model", "--port", "--yes", "--check", "--no-start", "--host", "--api-key"):
            self.assertIn(f'"{flag}"', self.src, f"setup.py 里找不到 {flag}")

    def test_config_naming_matches(self):
        self.assertIn("strata-", self.src, "配置文件的命名（strata-*.json）是 Neo 发现模型的依据")


class TestNoUpstreamEdits(unittest.TestCase):
    """Neo 的约定：不改上游。上游文件的 git 状态不该被 Neo 改动。"""

    def test_neo_dir_is_self_contained(self):
        files = sorted(p.name for p in (ROOT / "neo").iterdir())
        self.assertIn("manager.py", files)
        self.assertIn("supervisor.py", files)
        self.assertIn("console_server.py", files)
        self.assertIn("state.py", files)
        self.assertIn("jobobj.py", files)
        self.assertIn("web", files)
        self.assertIn("engines", files)
        self.assertIn("tests", files)

    def test_requirements_untouched(self):
        req = text(ROOT / "requirements.txt")
        self.assertNotIn("pywebview", req, "pywebview 只进 neo/requirements-neo.txt，不动上游")


if __name__ == "__main__":
    unittest.main(verbosity=2)
