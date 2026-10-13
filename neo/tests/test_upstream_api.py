"""neo/tests/test_upstream_api.py - 与上游的接口自检：Neo 用到的命令行参数和 HTTP 字段还在不在。

Neo 不改上游，全靠上游已有的 CLI 与端点。上游改了名字，这条测试要第一时间报出来，
而不是等用户的 Neo 起不来。
模型页"下载源"那一排用的也是上游本来就有的口子（`--source` 的可选值与 `HF_ENDPOINT`），
上游收掉这两个口子时这条测试先响（TestSetupCli 里的 test_the_download_source_knobs_neo_switches_still_exist）。
内嵌的监控页要读的那些端点也一样：Neo 在控制台上开一条只读通道代发（neo/console_server.py
的 /strata/），上游保持原样，所以 TestServerApi 里既钉住"上游没有 Neo 需要的改动"，
也钉住"上游那个页面和它读的端点还在"。
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

    def test_the_monitor_channel_needs_no_upstream_change(self):
        """Neo 的监控页靠 Neo 自己的只读通道（neo/console_server.py 的 /strata/），不靠上游改动。

        这条钉两件事：上游保持原样（Neo 不改它一个字节），且上游那个页面和它读的那些端点
        还在 —— 上游收掉 /api-monitor、/web/ 资源或这些端点时，这条先响。

        2026-10-13 走过另一条路：给 serve/server.py 加"本机读面板免 key"，那要动上游一个
        文件加五个测试文件，上游一更新就得合并 —— 已撤掉，改由 Neo 代发并附上自己的 key。
        上游那边再出现下面这些标记，说明有人把那条路捡回来了，这条先红。
        """
        for needle in ("def loopback_peer(", "_authorized(self, monitor: bool = False)",
                       "_authorized(monitor=True)"):
            self.assertNotIn(needle, self.src,
                             f"serve/server.py 里又出现了 {needle}：Neo 不改上游，"
                             "监控页靠 neo/console_server.py 的 /strata/ 通道")
        self.assertIn('path == "/api-monitor"', self.src, "上游的独立监控页（/api-monitor）没了")
        for prefix in ('"/web/', '"/fonts/'):
            self.assertIn(prefix, self.src, f"上游页面的资源前缀 {prefix} 没了，内嵌页要白屏")
        for path in ("/metrics", "/status", "/config", "/settings", "/mcp", "/api/requests",
                     "/v1/status", "/health", "/props", "/slots"):
            self.assertIn(f'"{path}"', self.src,
                          f"serve/server.py 不再认 {path}，Neo 通道的白名单要跟着改")

    def test_metrics_sections_neo_flattens_still_exist(self):
        """Neo 的状态页卡片读 /metrics 的这四节（neo/engines/base.py 的 flatten_metrics）。

        上游改了这些名字，这条要第一时间响。2026-10-12 那次是反过来的：Neo 按 41 个上游
        从没发过的扁平键读顶层，六张卡片全是"—"、"请求"栏写成 [object Object]。
        """
        block = re.search(r"def metrics\(self, all_requests=False\).*?\n    def ", self.src, re.S)
        self.assertTrue(block, "找不到 Service.metrics()")
        body = block.group(0)
        for key in ('"live"', '"totals"', '"hardware"', '"hardware_static"',
                    '"requests_kept"', '"queued"', '"tok_s"', '"tok_s_mean"',
                    '"prompt_tokens"', '"generated"', "running=", '"phase"'):
            self.assertIn(key, body, f"/metrics 不再返回 {key}，Neo 的 flatten_metrics 要跟着改")

    def test_totals_and_hardware_keys_neo_reads(self):
        for key in ('"requests"', '"output_tokens"', '"decode_ms"', '"prompt_ms"'):
            self.assertIn(key, self.src, f"serve/server.py 的 totals 里不再出现 {key}")
        tel = text(ROOT / "serve" / "telemetry.py")
        for key in ('"cpu"', '"ram_used"', '"ram_total"', '"mem_used"', '"mem_total"', '"util"'):
            self.assertIn(key, tel, f"serve/telemetry.py 不再产出 {key}")

    def test_web_pages_neo_embeds(self):
        self.assertTrue((ROOT / "serve" / "web" / "index.html").is_file())
        self.assertTrue((ROOT / "serve" / "web" / "monitor.html").is_file())
        self.assertIn("monitor.html", self.src)


class TestSetupApi(unittest.TestCase):
    """M2 要用的 setup.py 参数，先把"还在不在"钉住。"""

    def setUp(self):
        self.src = text(ROOT / "setup.py")

    def test_flags_neo_will_use(self):
        for flag in ("--model", "--family", "--port", "--yes", "--check", "--no-start",
                     "--host", "--api-key"):
            self.assertIn(f'"{flag}"', self.src, f"setup.py 里找不到 {flag}")

    def test_model_tables_neo_reads_still_exist(self):
        """neo/models.py 用 ast 读这两张表拼安装清单；上游改名，这边要第一时间响。"""
        self.assertIn("MODELS", self.src, "setup.py 里没有 MODELS 表，清单读不出来了")
        self.assertIn("FAMILIES", self.src, "setup.py 里没有 FAMILIES 表，清单读不出来了")

    def test_the_download_source_knobs_neo_switches_still_exist(self):
        """模型页的"下载源"那一排全走上游本来就有的口子：--source 与 HF_ENDPOINT。

        上游把 --source 的可选值改了、或不读 HF_ENDPOINT 了，Neo 的镜子那条路就断了，
        这条要第一时间响（Neo 不改上游，见 neo/netprobe.py 开头那句 #908）。
        """
        self.assertIn('"--source"', self.src, "setup.py 里找不到 --source")
        self.assertIn("SOURCES", self.src, "setup.py 的 --source 可选值表（SOURCES）没了")
        for value in ("modelscope", "huggingface", "auto"):
            self.assertIn(f'"{value}"', self.src, f"setup.py 的 --source 不再认 {value}")
        self.assertIn("HF_ENDPOINT", self.src, "setup.py 不再读 HF_ENDPOINT，镜子那条路没了")
        self.assertIn("hf-mirror.com", self.src, "上游的镜子地址（hf-mirror.com）改了")

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
