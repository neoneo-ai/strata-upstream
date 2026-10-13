"""neo/tests/test_netprobe.py - 下载源探测：判断规则与"要不要给 setup.py 加 --source"。

不碰网络：connect 函数一律是假的。
用户点的下载源怎么变成命令与环境（--source huggingface|modelscope / HF_ENDPOINT 镜像 / 自动按探测建议）
在 TestChoiceArgs 里逐条盯着 —— 镜像不能发 `--source mirror`，上游没有这个值。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from neo import netprobe                                            # noqa: E402


def fake_connect(ok_hosts, ms=42.0):
    """一个假的连通函数：ok_hosts 里的地址连得上，其余连不上。"""
    def connect(host, port=443, timeout=3.0):
        return (host in ok_hosts), ms
    return connect


def rows(hf=False, ms=False, mirror=False):
    return netprobe.probe(connect=fake_connect(
        {("huggingface.co" if hf else ""), ("www.modelscope.cn" if ms else ""),
         ("hf-mirror.com" if mirror else "")} - {""}))["targets"]


class TestProbe(unittest.TestCase):
    def test_rows_cover_the_three_hosts(self):
        result = netprobe.probe(connect=fake_connect({"huggingface.co"}))
        hosts = {r["host"] for r in result["targets"]}
        self.assertEqual(hosts, {"huggingface.co", "hf-mirror.com", "www.modelscope.cn"})
        self.assertEqual(result["ok"], ["huggingface.co"])

    def test_latency_is_recorded(self):
        result = netprobe.probe(connect=fake_connect({"huggingface.co"}, ms=12.5))
        row = next(r for r in result["targets"] if r["host"] == "huggingface.co")
        self.assertEqual(row["ms"], 12.5)

    def test_cache_is_not_refreshed_every_call(self):
        calls = []

        def connect(host, port=443, timeout=3.0):
            calls.append(host)
            return True, 1.0
        netprobe._CACHE.clear()
        first = netprobe.probe_cached(connect=connect)
        second = netprobe.probe_cached(connect=connect)
        self.assertEqual(len(calls), len(netprobe.TARGETS), "第二次不该再去连")
        self.assertEqual(first["source"], second["source"])
        netprobe.probe_cached(connect=connect, force=True)
        self.assertEqual(len(calls), 2 * len(netprobe.TARGETS), "force 要真去连")
        netprobe._CACHE.clear()


class TestSuggest(unittest.TestCase):
    def test_only_modelscope_reachable_switches_source(self):
        source, reason = netprobe.suggest_source(rows(hf=False, ms=True))
        self.assertEqual(source, "modelscope")
        self.assertIn("huggingface.co 连不上", reason)

    def test_both_reachable_keeps_the_upstream_default(self):
        source, _ = netprobe.suggest_source(rows(hf=True, ms=True))
        self.assertEqual(source, "huggingface", "两个都通时不切源（上游默认，#908）")

    def test_nothing_reachable_says_so(self):
        source, reason = netprobe.suggest_source(rows())
        self.assertEqual(source, "huggingface")
        self.assertIn("都连不上", reason)

    def test_mirror_only_suggests_the_endpoint_knob(self):
        source, reason = netprobe.suggest_source(rows(mirror=True))
        self.assertEqual(source, "huggingface")
        self.assertIn("HF_ENDPOINT", reason)

    def test_describe_lists_every_host(self):
        lines = netprobe.describe(netprobe.probe(connect=fake_connect({"huggingface.co"})))
        self.assertEqual(len(lines), 1 + len(netprobe.TARGETS))


class TestSetupArgs(unittest.TestCase):
    def test_suggested_modelscope_adds_the_flag(self):
        self.assertEqual(netprobe.setup_source_args("modelscope"), ["--source", "modelscope"])

    def test_huggingface_adds_nothing(self):
        self.assertEqual(netprobe.setup_source_args("huggingface"), [])

    def test_user_choice_wins_over_the_probe(self):
        self.assertEqual(netprobe.setup_source_args("modelscope", "huggingface"),
                         ["--source", "huggingface"])
        self.assertEqual(netprobe.setup_source_args("huggingface", "modelscope"),
                         ["--source", "modelscope"])


class TestChoiceArgs(unittest.TestCase):
    """模型页"下载源"那一排选中一项等于什么（neo/models.py 用的就是这一个函数）。"""

    def test_the_four_keys_the_models_page_offers(self):
        self.assertEqual(netprobe.CHOICES, ("probe", "huggingface", "modelscope", "mirror"))

    def test_probe_follows_the_suggestion(self):
        args, env = netprobe.choice_args("probe", rows=rows(hf=False, ms=True))
        self.assertEqual(args, ["--source", "modelscope"])
        self.assertEqual(env, {})

    def test_probe_with_both_reachable_adds_nothing(self):
        args, _ = netprobe.choice_args("probe", rows=rows(hf=True, ms=True))
        self.assertEqual(args, [], "两个都通就走上游默认，不加 --source")

    def test_a_named_source_is_passed_through_untouched(self):
        self.assertEqual(netprobe.choice_args("modelscope", rows=rows(hf=True, ms=True))[0],
                         ["--source", "modelscope"])
        self.assertEqual(netprobe.choice_args("huggingface", rows=rows())[0],
                         ["--source", "huggingface"])
        self.assertEqual(netprobe.choice_args("auto", rows=rows())[0], ["--source", "auto"])

    def test_the_mirror_is_an_env_var_not_a_source(self):
        args, env = netprobe.choice_args("mirror", rows=rows(mirror=True))
        self.assertEqual(args, [], "--source 里没有镜子这一项")
        self.assertEqual(env, {"HF_ENDPOINT": netprobe.MIRROR_ENDPOINT})

    def test_named_sources_add_no_env(self):
        for key in ("probe", "huggingface", "modelscope"):
            self.assertEqual(netprobe.choice_args(key, rows=rows(hf=True, ms=True))[1], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
