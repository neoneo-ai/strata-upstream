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
from neo.engines.base import EngineAdapter, STRATA_MONITOR_PATH, flatten_metrics, metric_num      # noqa: E402
from neo.engines.strata import list_configs                    # noqa: E402


def write_config(folder: Path, name: str, **over):
    stem = name[:-len(".json")].replace("strata-", "")
    cfg = {"model_name": stem, "path": str(folder / (stem + ".gguf")),
           "port": 8080, "args": {"--context": 32768, "--flash-attention": True}}
    cfg.update(over)
    (folder / name).write_text(json.dumps(cfg), encoding="utf-8")
    return folder / name


def make_exe(folder: Path, name: str = "strata.exe"):
    """装好的 Strata 一定有引擎 exe（setup.py 的 check_install 也这么要求）：
    测试要的是"装好了"的样子，所以建一个空文件占位，plan() 只看它在不在。"""
    (folder / name).write_text("#!/bin/sh\n", encoding="utf-8")
    return folder / name


class TestDiscover(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="neo-eng-"))
        make_exe(self.tmp)
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

    def test_flat_args_shape(self):
        """真实 Strata 配置（setup.py 写的）不长 path/args 字典，长一个扁平 args 数组。

        实测 2026-10-12 在 D:\\Work\\Strata：strata-swift-iq3_xxs.json 里模型在
        args 的 --native 后面、上下文在 --max-context 后面、没有 path 键。
        只认 path + 字典 args 时这些配置全被当成"没装"，报错还因为
        cfg["path"] 是空的而写成"模型文件不在："（后面什么都没有）。
        """
        cfg = {"model_name": "swift-1.5-iq3_xxs", "port": 8080,
               "args": ["--native", r"D:\Work\Strata-data\models\swift-IQ3_XXS\m.gguf",
                        "--pack", r"D:\Work\Strata-data\packs\swift-iq3_xxs",
                        "--max-context", 202072, "--mtp", "mtp.gguf", "--gpu", "0"]}
        (self.tmp / "strata-flat.json").write_text(json.dumps(cfg), encoding="utf-8")
        item = next(x for x in list_configs(self.tmp) if x["id"] == "strata-flat.json")
        self.assertEqual(item["path"], r"D:\Work\Strata-data\models\swift-IQ3_XXS\m.gguf")
        self.assertEqual(item["context"], 202072, "--max-context 优先于 --context")
        self.assertEqual(item["port"], 8080)
        self.assertEqual(item["name"], "swift-1.5-iq3_xxs")
        self.assertIn("--mtp", item["args"], "扁平 args 原样带过去")
        self.assertIn("模型文件不在", "；".join(item["missing"]), "模型不在 Strata-data 里要标出来")

    def test_no_exe_is_not_installed(self):
        """模型在、引擎 exe 不在 = 没装好：要标出来，plan() 要说清缺的是 exe。"""
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="neo-eng-noexe-"))
        write_config(tmp, "strata-a.json")
        (tmp / "a.gguf").write_text("x", encoding="utf-8")
        item = list_configs(tmp)[0]
        self.assertFalse(item["installed"], "没有 strata.exe 不能算装好了")
        self.assertTrue(any("exe" in m for m in item["missing"]), "缺的要写清是 exe")
        with self.assertRaises(FileNotFoundError) as got:
            make_adapter("strata", tmp).plan(8080)
        self.assertIn("exe", str(got.exception))

    def test_default_skips_uninstallable(self):
        """没点名时挑一个能启动的；全都不行才回第一个，让报错说清缺了什么。"""
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="neo-eng-def-"))
        make_exe(tmp)
        write_config(tmp, "strata-bad.json")                       # 最新，但模型不在
        time.sleep(0.02)
        write_config(tmp, "strata-good.json")
        (tmp / "good.gguf").write_text("x", encoding="utf-8")
        # 写入顺序：strata-good.json 最新 -> 排最前
        self.assertEqual(make_adapter("strata", tmp).default_config()["id"], "strata-good.json")
        (tmp / "good.gguf").unlink()
        self.assertEqual(make_adapter("strata", tmp).default_config()["id"], "strata-good.json",
                         "全都起不来时回最前的那个，报错说的是它")
        named = make_adapter("strata", tmp, {"model": "strata-bad.json"}).default_config()
        self.assertEqual(named["id"], "strata-bad.json", "点名的哪怕起不来也要回它")


class TestPlan(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="neo-plan-"))
        make_exe(self.tmp)
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

    def test_missing_model_message_is_complete(self):
        """报错要说清缺了什么。实测 2026-10-12 的 manager.log 里那条是空的"模型文件不在："
        —— 后面什么都没有，用户只能看到五个字。"""
        (self.tmp / "x.gguf").unlink()
        with self.assertRaises(FileNotFoundError) as got:
            self.adapter.plan(8080)
        msg = str(got.exception)
        self.assertIn("strata-x.json", msg, "要说清是哪个配置起不来")
        self.assertIn("模型文件不在", msg)
        self.assertFalse(msg.rstrip().endswith("："), "消息后面不能是空的")

    def test_log_path_for_reads_config_log(self):
        """脚本先起来的服务的日志 = 配置里的 log 键（run-*.bat 写的就是它）。

        Neo 接手那个服务之后，日志页要指向同一个文件；实测 2026-10-12 在
        D:\\Work\\Strata：接手成功了、服务在跑，日志页是空的。
        """
        cfg = json.loads((self.tmp / "strata-x.json").read_text(encoding="utf-8"))
        cfg["log"] = "strata-x.log"
        (self.tmp / "strata-x.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.assertEqual(self.adapter.log_path_for("strata-x.json"),
                         str(self.tmp / "strata-x.log"))
        self.assertEqual(make_adapter("mock", ROOT).log_path_for("mock"), "",
                         "没有 log 键就是没有，不要编一个路径出来")

    def test_pages(self):
        pages = self.adapter.pages(8080)
        self.assertEqual(pages["chat"], "http://127.0.0.1:8080/")
        # 实测 2026-10-12 在跑着的服务上：/monitor.html 是 404，监控页是聊天页的 #monitor 标签
        self.assertEqual(pages["monitor"], "http://127.0.0.1:8080/#monitor")

    def test_pages_monitor_goes_through_the_console(self):
        """监控页走 Neo 的只读通道，聊天页照旧直连。

        上游的面板端点在设了 API key 之后要 key，内嵌的上游页面没有地方填 key（它的 key
        存在自己的 localStorage 里），设了 token 之后卡片整页空掉。Neo 不改上游一个字节，
        由 Neo 代发并附上它自己的 key（neo/console_server.py 的 /strata/）。
        聊天页要 POST 才能发消息，Neo 不替页面花用户的 token，所以它继续直连 8080。
        """
        pages = self.adapter.pages(8080, "127.0.0.1", 8083)
        self.assertEqual(pages["chat"], "http://127.0.0.1:8080/", "聊天页不借 Neo 的 key")
        self.assertEqual(pages["monitor"], "http://127.0.0.1:8083/strata/#monitor")
        self.assertEqual(pages["monitor"], f"http://127.0.0.1:8083{STRATA_MONITOR_PATH}")
        # 绑 0.0.0.0 时页面地址写本机，不把 0.0.0.0 写进浏览器地址栏
        self.assertEqual(self.adapter.pages(8080, "0.0.0.0", 8083)["monitor"],
                         "http://127.0.0.1:8083/strata/#monitor")
        # 没有控制台端口（CLI 起的裸服务）时退回直连，行为与改动前一致
        self.assertEqual(self.adapter.pages(8080, "127.0.0.1", None)["monitor"],
                         "http://127.0.0.1:8080/#monitor")

    def test_mock_adapter(self):
        plan = make_adapter("mock", ROOT).plan(8095)
        self.assertEqual(plan.argv[plan.argv.index("--engine") + 1], "mock")
        self.assertEqual(plan.argv[plan.argv.index("--port") + 1], "8095")
        self.assertTrue(Path(plan.argv[1]).exists(), "mock 跑的是仓库里的 serve/server.py")


#: 一次真应答的 /metrics，实测 2026-10-12 在本机（RTX 5090 D v2 / Windows 11 / Python 3.13）：
#: serve/server.py --engine mock --port 28181，跑过一次 /v1/chat/completions（6 个 token）后取。
#: 这就是 flatten_metrics 要吃的形状 —— 卡片上的数字全从这里来，名字对不上就是"—"。
REAL_METRICS = {
    "engine": {"model": "qwen3.8-flash-next", "max_context": 32768, "images": False},
    "live": {"state": "idle", "queued": 0, "phase": None, "prompt_tokens": None,
             "prompt_read": None, "prompt_total": None, "generated": None, "max_tokens": None,
             "elapsed_s": None, "tok_s": None, "tok_s_mean": None,
             "prefill_tok_s_mean": None, "tok_s_window_s": None},
    "requests": [{"projection": None, "time": 1791809883.12, "duration_s": 0.0, "finish": "stop",
                  "prompt_tokens": 246, "reused": None, "output_tokens": 6, "prompt_total": 246,
                  "prompt_read": None, "prompt_ms": None, "decode_ms": None,
                  "decode_tok_s": None, "hit_rate": None, "pcie_share": None}],
    "requests_kept": 1,
    "totals": {"since": 1791809882.73, "requests": 1, "prompt_tokens": 246, "reused": 0,
               "output_tokens": 6, "prompt_ms": 0.0, "decode_ms": 0.0,
               "drafts_offered": 0, "drafts_accepted": 0},
    "hardware": {"gpu_util": 1, "gpu_mem_used": 25195446272, "gpu_mem_total": 25642926080,
                 "gpu_temp": 50, "gpu_power": 79.627, "gpu_power_limit": 575.0,
                 "gpu_pcie_gen": 5, "gpu_pcie_width": 8, "gpu_pcie_rx_mb": 3.05,
                 "cpu": 9.0, "ram_used": 75824189440, "ram_total": 100515143680,
                 "disk_read_mb": 0.0366, "disk_write_mb": 0.3666,
                 "tok_s": 0.0, "tok_s_mean": 0, "prefill_tok_s_mean": 0.0},
    "hardware_static": {"gpu_name": "NVIDIA GeForce RTX 5090 D v2", "gpu_count": 1,
                        "gpu_note": None, "cpu_name": "AMD Ryzen 9 9950X3D",
                        "cores": 16, "threads": 32, "psutil": True},
    "history": {"gpu_util": [1, 2], "ram_used": [75824189440]},
    "time": 1791809885.6,
}


class TestMetricsFlatten(unittest.TestCase):
    """状态页六张卡片读的是 /metrics 的哪几节、算出什么数。

    实测 2026-10-12 用户的窗口截图：生成速度/解码步长/显存/内存/在跑的请求五张卡全是"—"，
    "上下文"有数（202072，那条走 /health），"请求"那一栏写的是
    "请求 [object Object],[object Object]"。原因在 neo/engines/base.py：它按 41 个扁平键
    在 /metrics 顶层挑，那些名字上游一个都没发过，只有 requests 在、而且它是最近 12 条
    请求的记录列表 —— 列表被写进文字就是 [object Object]。
    """

    def setUp(self):
        self.m = flatten_metrics(REAL_METRICS)

    def test_counts_are_numbers(self):
        self.assertEqual(self.m["requests"], 1, "累计条数，不是那 12 条记录")
        self.assertEqual(self.m["requests_kept"], 1)
        self.assertEqual(self.m["queued"], 0)
        self.assertEqual(self.m["active"], 0, "live.state=idle 时没有请求在跑")

    def test_hardware_numbers(self):
        self.assertEqual(self.m["gpu_used_mib"], 24028)      # 25195446272 字节
        self.assertEqual(self.m["gpu_total_mib"], 24455)     # 25642926080 字节
        self.assertEqual(self.m["gpu_free_mib"], 427)
        self.assertEqual(self.m["ram_used_gb"], 70.6)        # 75824189440 字节
        self.assertEqual(self.m["ram_total_gb"], 93.6)
        self.assertEqual(self.m["cpu_pct"], 9.0)
        self.assertEqual(self.m["gpu_name"], "NVIDIA GeForce RTX 5090 D v2")

    def test_nothing_a_page_could_print_as_object_object(self):
        for key, value in self.m.items():
            self.assertNotIsInstance(value, (list, dict), f"{key} 是列表/对象，页面会写出 [object Object]")
        self.assertNotIsInstance(self.m["requests"], list)

    def test_idle_has_no_invented_speed_number(self):
        self.assertNotIn("tokens_per_s", self.m, "没在生成时不编一个 0 出来，页面显示\"—\"")
        self.assertNotIn("tokens_per_s_mean", self.m)

    def test_decode_step_ms_from_totals(self):
        self.assertEqual(self.m["decode_step_ms"], 0.0, "mock 的 decode_ms 是 0.0，6 个 token")
        self.assertEqual(flatten_metrics({"totals": {"decode_ms": 1200.0, "output_tokens": 100}})
                         ["decode_step_ms"], 12.0)
        self.assertNotIn("decode_step_ms", flatten_metrics({"totals": {"decode_ms": 5.0, "output_tokens": 0}}),
                         "没生成过 token 就不报步长")

    def test_parallel_running_count(self):
        flat = flatten_metrics({"live": {"state": "generating", "running": 3, "queued": 2,
                                         "tok_s": 41.7, "generated": 12, "phase": "decode"}})
        self.assertEqual((flat["active"], flat["queued"], flat["tokens_per_s"]), (3, 2, 41.7))
        self.assertEqual((flat["phase"], flat["generated"]), ("decode", 12))

    def test_solo_busy_counts_one(self):
        self.assertEqual(flatten_metrics({"live": {"state": "reading", "queued": 0}})["active"], 1)

    def test_empty_and_broken_payloads_are_survivable(self):
        for payload in ({}, None, [], {"live": "not a dict", "totals": None,
                                        "hardware": [1, 2], "hardware_static": {"gpu_name": None}}):
            flat = flatten_metrics(payload)
            self.assertEqual(flat["active"], 0)
            self.assertFalse([k for k, v in flat.items() if isinstance(v, (list, dict))])
        self.assertIsNone(metric_num("12"))
        self.assertIsNone(metric_num(True))
        self.assertIsNone(metric_num(float("nan")))
        self.assertEqual(metric_num(3.5), 3.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
