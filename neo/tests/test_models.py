"""neo/tests/test_models.py - 模型页的"下载安装模型"：清单从哪来、推荐按什么算、装没装怎么认。

不跑真的 setup.py，也不 import 它：清单是 ast 读它顶层的 MODELS / FAMILIES 两张表，
测试里用的是临时目录里一份假的 setup.py（值里带 hf(...) 这种要执行才成立的调用，
读出来就该被丢掉）。真的 setup.py 只读不跑，看它两张表还在不在（TestRealSetup）。
机器信息（内存 / 显卡 / 盘）在测试里是传进去的假数，不靠跑测试这台机器。
清单里还带"下载源"那一排（source_info()）：四个选项、探测耗时、每个选项的命令前后缀、
记住的那一个（neo/state/source.json，没选过就是按探测建议）。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

if __package__ in (None, ""):                       # 直接跑 python neo/tests/test_models.py 时没有父包
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "neo.tests"

from ._bootstrap import ENGINE, REPO, has_engine, no_engine_reason   # noqa: E402

ROOT = REPO                                         # 老写法留下的名字：Neo 仓库的根（Neo 自己的文件）

from neo import models, state                                          # noqa: E402

FAKE_SETUP = '''"""假的 setup.py：只有 Neo 要读的两张表。"""
MODELS = {
    "IQ2_XS": {"title": "IQ2_XS", "about": "48 GB 内存", "download_gb": 68.0,
               "ram_gb": 48, "arena_gb": 52.0, "files": hf("Repo", "a.bin"),
               "families": ("qwen", "swift")},
    "IQ3_S": {"title": "IQ3_S", "about": "64 GB 内存", "download_gb": 83.6,
              "ram_gb": 62, "arena_gb": 66.0, "files": hf("Repo", "b.bin"),
              "families": ("qwen", "swift")},
    "IQ1_M": {"title": "IQ1_M", "about": "32 GB 内存", "download_gb": 58.4,
              "ram_gb": 32, "arena_gb": 40.0, "files": hf("Repo", "c.bin"),
              "families": ("coder",)},
    "UD-IQ4_XS": {"title": "UD-IQ4_XS", "about": "约 4-bit", "download_gb": 93.7,
                  "ram_gb": 48, "arena_gb": 74.0, "budget": True,
                  "files": hf("Repo", "d.bin"), "families": ("unsloth",)},
}
FAMILIES = {
    "qwen": {"name": "qwen3.8-flash-next", "title": "Qwen3.8-Flash-Next", "tag": "qwen"},
    "coder": {"name": "qwen3.8-flash-next-coder", "title": "Qwen3.8-Flash-Next Coder",
              "tag": "coder"},
    "swift": {"name": "qwen3.8-flash-next-swift", "title": "Swift 1.5", "tag": "swift"},
    "unsloth": {"name": "qwen3.8-flash-next-unsloth", "title": "Qwen3.8-Flash-Next (Unsloth)",
                "tag": "unsloth"},
}
'''

#: 一份假的探测结果（形状与 neo/netprobe.py 的 probe() 给的一样）。
#: 数字是 2026-10-12 那台 PC 的实测值（docs/NEO_DESKTOP.md：HF 196 ms、镜子 4052 ms、
#: ModelScope 36 ms），测试里不联网。
NET = {"targets": [{"host": "huggingface.co", "what": "huggingface", "ok": True, "ms": 196.0},
                   {"host": "hf-mirror.com", "what": "mirror", "ok": True, "ms": 4052.0},
                   {"host": "www.modelscope.cn", "what": "modelscope", "ok": True, "ms": 36.0}],
       "ok": ["huggingface.co", "hf-mirror.com", "www.modelscope.cn"],
       "source": "huggingface", "reason": "两个源都连得上：按上游默认的 Hugging Face 走",
       "at": 0.0}


def fake_root(folder) -> Path:
    """临时目录里放一份假的 setup.py，当作 Strata 根目录。"""
    (Path(folder) / "setup.py").write_text(FAKE_SETUP, encoding="utf-8")
    return Path(folder)


def installed(family_tag: str, model: str, ok: bool = True) -> dict:
    """discover() 的一条返回值，形状与 neo/engines/strata.py 给的一样。"""
    name = models.config_name(family_tag, model)
    return {"id": name, "name": f"{family_tag}-{model.lower()}", "path": "", "file": "",
            "mtime": 0, "port": 8080, "context": 131072, "vision": False,
            "backend": "cuda", "installed": ok}


class TableCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = fake_root(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()


class TestReadTables(TableCase):
    def test_both_tables_are_read_without_running_setup(self):
        models_tbl, families = models.read_tables(self.root)
        self.assertEqual(set(models_tbl), {"IQ2_XS", "IQ3_S", "IQ1_M", "UD-IQ4_XS"})
        self.assertEqual(families["swift"]["title"], "Swift 1.5")

    def test_values_that_need_to_run_are_dropped_not_the_whole_entry(self):
        models_tbl, _ = models.read_tables(self.root)
        self.assertNotIn("files", models_tbl["IQ2_XS"])
        self.assertEqual(models_tbl["IQ2_XS"]["download_gb"], 68.0)

    def test_missing_setup_py_is_two_empty_tables(self):
        self.assertEqual(models.read_tables(self.tmp / "nope"), ({}, {}))

    def test_cache_follows_the_file(self):
        self.assertEqual(len(models.read_tables(self.root)[0]), 4)
        time.sleep(0.02)
        (self.root / "setup.py").write_text(FAKE_SETUP.replace('"IQ1_M"', '"IQ1_M_NEW"'),
                                            encoding="utf-8")
        self.assertIn("IQ1_M_NEW", models.read_tables(self.root)[0])

    def test_sizes_per_family_follows_setup_py_rule(self):
        models_tbl, _ = models.read_tables(self.root)
        self.assertEqual(models.sizes_of_family(models_tbl, "swift"), ["IQ2_XS", "IQ3_S"])
        self.assertEqual(models.sizes_of_family(models_tbl, "coder"), ["IQ1_M"])


class TestRecommend(unittest.TestCase):
    def test_32gb_takes_iq2_xs_and_coder_is_only_listed_not_recommended(self):
        got = models.recommend(32)
        self.assertEqual((got["family"], got["model"]), ("qwen", "IQ2_XS"))
        self.assertIn("低内存模式", got["why"])
        self.assertEqual(got["also"][0]["model"], "Q2_0")
        coder = [a for a in got["also"] if a["family"] == "coder"][0]
        self.assertIn("中文", coder["note"], "Coder 要说清中文不行，且它只是\"也可以\"里的一项")

    def test_48gb_takes_iq2_xs_and_names_q2_0_as_the_fast_one(self):
        got = models.recommend(48)
        self.assertEqual((got["family"], got["model"]), ("qwen", "IQ2_XS"))
        self.assertIn("Q2_0", got["why"])
        self.assertEqual(got["also"][0]["model"], "Q2_0")

    def test_64gb_takes_the_documented_default(self):
        got = models.recommend(64)
        self.assertEqual((got["family"], got["model"]), ("qwen", "IQ2_XS"))
        self.assertIn("文档推荐", got["why"])

    def test_128gb_takes_iq3_s_and_offers_unsloth_4bit(self):
        got = models.recommend(128)
        self.assertEqual((got["family"], got["model"]), ("qwen", "IQ3_S"))
        self.assertEqual(got["also"][0]["family"], "unsloth")

    def test_every_tier_says_why_and_warns_about_the_coder(self):
        for ram in (16, 32, 48, 64, 96, 128, 256):
            got = models.recommend(ram)
            self.assertTrue(got["why"], f"{ram} GB 没有理由")
            self.assertIn("不要选 Coder", got["note"])
            self.assertNotEqual(got["family"], "coder", f"{ram} GB 不推荐 Coder")

    def test_ram_read_failure_is_said_not_pretended_to_be_zero(self):
        got = models.recommend(0)
        self.assertIn("读不到", got["why"])
        self.assertEqual((got["family"], got["model"]), ("qwen", "IQ2_XS"))


class TestFit(unittest.TestCase):
    def test_same_verdict_as_setup_py_check(self):
        entry = {"ram_gb": 62, "arena_gb": 66.0}
        self.assertEqual(models.fit_of(entry, 64)[0], "fits")
        self.assertEqual(models.fit_of(entry, 56)[0], "tight")     # 差 6 GB，8 GB 以内
        self.assertEqual(models.fit_of(entry, 40)[0], "no")

    def test_budget_models_say_what_stays_in_ram(self):
        entry = {"ram_gb": 48, "arena_gb": 74.0, "budget": True}
        fit, text = models.fit_of(entry, 64)
        self.assertEqual(fit, "fits")
        self.assertIn("GiB 专家留在内存里", text)

    def test_resident_budget_matches_setup_py(self):
        # setup.py: max(8, min(int(arena / 1.073741824), round(ram) - 24))
        self.assertEqual(models.resident_budget_gib(74.0, 64), 40)   # 64 - 24 是少的
        self.assertEqual(models.resident_budget_gib(74.0, 40), 16)
        self.assertEqual(models.resident_budget_gib(74.0, 30), 8)    # 下限 8 GiB


class TestCatalog(TableCase):
    def machine(self, ram=64.0):
        return {"ram_gb": ram, "gpu_name": "", "gpu_vram_gb": 0.0,
                "disk_free_gb": 400.0, "disk_dir": str(self.root), "note": f"内存 {ram:.0f} GB"}

    def test_one_entry_per_family_size_with_the_install_args(self):
        data = models.catalog(self.root, machine=self.machine(), installed=[])
        keys = {(e["family"], e["model"]) for e in data["models"]}
        self.assertEqual(keys, {("qwen", "IQ2_XS"), ("qwen", "IQ3_S"), ("swift", "IQ2_XS"),
                                ("swift", "IQ3_S"), ("coder", "IQ1_M"), ("unsloth", "UD-IQ4_XS")})
        one = [e for e in data["models"] if e["family"] == "coder"][0]
        self.assertEqual(one["args"], "--family coder --model IQ1_M")
        self.assertEqual(one["family_title"], "Qwen3.8-Flash-Next Coder")

    def test_the_recommended_one_is_marked_and_sorted_first(self):
        data = models.catalog(self.root, machine=self.machine(128.0), installed=[])
        marked = [e for e in data["models"] if e["recommended"]]
        self.assertEqual(len(marked), 1)
        self.assertEqual((marked[0]["family"], marked[0]["model"]), ("qwen", "IQ3_S"))
        self.assertTrue(data["models"][0]["recommended"], "推荐的那一项要排在最前")

    def test_installed_is_read_off_the_root_config_files(self):
        data = models.catalog(self.root, machine=self.machine(),
                              installed=[installed("swift", "IQ3_S")])
        hit = [e for e in data["models"] if e["family"] == "swift" and e["model"] == "IQ3_S"][0]
        self.assertTrue(hit["installed"])
        self.assertEqual(hit["config"], "strata-swiftiq3_s.json")
        other = [e for e in data["models"] if e["family"] == "qwen" and e["model"] == "IQ3_S"][0]
        self.assertFalse(other["installed"])
        self.assertEqual(data["installed_count"], 1)

    def test_half_installed_is_said_plainly(self):
        data = models.catalog(self.root, machine=self.machine(),
                              installed=[installed("qwen", "IQ2_XS", ok=False)])
        hit = [e for e in data["models"] if e["family"] == "qwen" and e["model"] == "IQ2_XS"][0]
        self.assertFalse(hit["installed"])
        self.assertTrue(hit["broken"])

    def test_a_model_that_does_not_fit_is_not_offered(self):
        data = models.catalog(self.root, machine=self.machine(32.0), installed=[])
        big = [e for e in data["models"] if e["model"] == "IQ3_S" and e["family"] == "qwen"][0]
        self.assertEqual(big["fit"], "no")
        self.assertFalse(big["installable"])
        coder = [e for e in data["models"] if e["family"] == "coder"][0]
        self.assertTrue(coder["installable"])

    def test_no_setup_py_says_so(self):
        data = models.catalog(self.tmp / "nope", machine=self.machine(), installed=[])
        self.assertFalse(data["has_setup"])
        self.assertEqual(data["models"], [])

    def test_known_blocks_anything_not_on_the_menu(self):
        self.assertTrue(models.known(self.root, "swift", "IQ2_XS"))
        self.assertFalse(models.known(self.root, "swift", "IQ1_M"))
        self.assertFalse(models.known(self.root, "nonsense", "IQ2_XS"))
        self.assertFalse(models.known(self.root, "qwen", ""))

    def test_the_catalog_carries_the_download_source_row(self):
        data = models.catalog(self.root, machine=self.machine(), installed=[],
                              sources=models.source_info(net=NET, chosen="modelscope"))
        self.assertEqual(data["sources"]["chosen"], "modelscope")
        self.assertEqual(data["sources"]["suffix"], " --source modelscope")
        self.assertEqual(len(data["sources"]["choices"]), len(models.SOURCE_KEYS))

class TestMachine(TableCase):
    def test_ram_is_read_off_this_machine(self):
        ram = models.total_ram_gb()
        self.assertGreater(ram, 1.0, "读内存这条不能悄悄回 0")

    def test_gpu_probe_never_raises_and_always_answers(self):
        got = models.gpu_info(force=True)
        self.assertIn("name", got)
        self.assertIn("vram_gb", got)
        self.assertIsInstance(got["vram_gb"], float)

    def test_machine_note_says_all_three_numbers(self):
        info = models.machine_info(self.root, ram=64.0, gpu={"name": "RTX 5070", "vram_gb": 12.0})
        self.assertEqual(info["ram_gb"], 64.0)
        self.assertIn("内存 64 GB", info["note"])
        self.assertIn("RTX 5070", info["note"])
        self.assertIn("还剩", info["note"])

    def test_no_gpu_is_said_as_a_reading_not_a_zero(self):
        info = models.machine_info(self.root, ram=64.0, gpu={"name": "", "vram_gb": 0.0})
        self.assertIn("读不到", info["note"])

    def test_data_dir_prefers_the_folder_next_to_the_root(self):
        project = self.tmp / "project"
        project.mkdir()
        self.assertEqual(models.data_dir(project).name, "project")   # 旁边没有就用根目录
        (self.tmp / "Strata-data").mkdir()
        self.assertEqual(models.data_dir(project).name, "Strata-data")


class TestSources(unittest.TestCase):
    """模型页的"下载源"那一排：四个可选项、探测数字、选中的记在哪。

    探测结果用假的 NET，偏好写进临时目录里的 source.json —— 不联网、不碰真的 neo/state/。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._source = state.SOURCE
        state.SOURCE = Path(self._tmp.name) / "source.json"

    def tearDown(self):
        state.SOURCE = self._source
        self._tmp.cleanup()

    def _by_key(self, info):
        return {c["key"]: c for c in info["choices"]}

    def test_the_four_choices_and_their_titles(self):
        info = models.source_info(net=NET, chosen="probe")
        self.assertEqual([c["key"] for c in info["choices"]],
                         ["probe", "huggingface", "modelscope", "mirror"])
        self.assertIn("自动", info["choices"][0]["title"])
        self.assertTrue(all(c["note"] for c in info["choices"]))

    def test_each_choice_says_what_it_does_to_setup_py(self):
        by = self._by_key(models.source_info(net=NET, chosen="probe"))
        self.assertEqual(by["probe"]["args"], [], "两个都通 = 不加 --source")
        self.assertEqual(by["huggingface"]["args"], ["--source", "huggingface"])
        self.assertEqual(by["modelscope"]["args"], ["--source", "modelscope"])
        self.assertEqual(by["mirror"]["args"], [])
        self.assertEqual(by["mirror"]["env"], {"HF_ENDPOINT": "https://hf-mirror.com"})

    def test_the_probe_numbers_come_through(self):
        by = self._by_key(models.source_info(net=NET, chosen="probe"))
        self.assertEqual(by["modelscope"]["ms"], 36.0)
        self.assertTrue(by["modelscope"]["reachable"])
        self.assertEqual(by["probe"]["ms"], 0.0, "\"自动\"这一项没有自己的地址，不编数字")
        self.assertFalse(by["probe"]["reachable"])

    def test_the_chosen_one_is_the_one_the_command_will_use(self):
        info = models.source_info(net=NET, chosen="modelscope")
        self.assertEqual(info["suffix"], " --source modelscope")
        self.assertEqual(info["prefix"], "")
        self.assertIn("--source modelscope", info["effect"])
        info = models.source_info(net=NET, chosen="mirror")
        self.assertEqual(info["prefix"], "HF_ENDPOINT=https://hf-mirror.com ")
        self.assertEqual(info["suffix"], "")
        self.assertIn("HF_ENDPOINT", info["effect"])

    def test_the_note_names_the_reason_and_the_choice(self):
        note = models.source_info(net=NET, chosen="modelscope")["note"]
        self.assertIn("两个源都连得上", note)
        self.assertIn("ModelScope", note)

    def test_the_choice_is_remembered_in_neo_state(self):
        self.assertEqual(models.source_choice(), "probe", "没选过就是按探测建议")
        models.save_source_choice("modelscope")
        self.assertEqual(models.source_choice(), "modelscope")
        self.assertTrue(state.SOURCE.is_file())

    def test_an_unknown_choice_falls_back_to_the_probe(self):
        state.save_source_choice("baidu")
        self.assertEqual(models.source_choice(), "probe")

    def test_a_broken_file_is_not_an_error(self):
        state.SOURCE.write_text("{ 这不是 JSON", encoding="utf-8")
        self.assertEqual(models.source_choice(), "probe")

    def test_suggested_comes_from_the_probe(self):
        info = models.source_info(net=NET, chosen="probe")
        self.assertEqual(info["suggested"], "huggingface")
        self.assertEqual(info["reason"], NET["reason"])


class TestRealSetup(unittest.TestCase):
    """真的 setup.py：只读不跑。上游改了模型表，这里要说出来，别等页面空了才发现。

    读的是引擎根目录（_bootstrap.ENGINE）里的 setup.py，不是 Neo 仓库的根：拆开之后
    Neo 仓库里没有 setup.py，引擎住在仓库根下的 strata/（neo/paths.py 的第 4 条）。
    """

    def setUp(self):
        if not has_engine():
            self.skipTest(no_engine_reason())

    def test_the_real_install_menu_is_readable(self):
        models_tbl, families = models.read_tables(ENGINE)
        self.assertIn("IQ3_S", models_tbl)
        self.assertIn("IQ2_XS", models_tbl)
        self.assertIn("IQ1_M", models_tbl)
        self.assertTrue(families["coder"]["tag"].startswith("coder"))
        self.assertIn("unsloth", families)

    def test_the_real_catalog_has_the_documented_sizes(self):
        data = models.catalog(ENGINE, installed=[])
        keys = {(e["family"], e["model"]) for e in data["models"]}
        for pair in (("qwen", "IQ3_S"), ("qwen", "IQ2_XS"), ("qwen", "Q2_0"),
                     ("coder", "IQ1_M"), ("swift", "IQ3_XXS"), ("unsloth", "UD-IQ4_XS")):
            self.assertIn(pair, keys)
        self.assertTrue(all(e["download_gb"] > 0 for e in data["models"]))
        self.assertTrue(all(e["about"] for e in data["models"]))

    def test_recommendation_tiers_match_the_docs_table(self):
        # docs/AI_SETUP.md 第 3 步：32 → IQ2_XS（低内存模式），48 → IQ2_XS，
        # 64 → IQ2_XS，96+ → IQ3_S。Coder 在 32 GB 那档只是"也可以"里的一项。
        self.assertEqual(models.recommend(32)["model"], "IQ2_XS")
        self.assertEqual(models.recommend(48)["model"], "IQ2_XS")
        self.assertEqual(models.recommend(64)["model"], "IQ2_XS")
        self.assertEqual(models.recommend(96)["model"], "IQ3_S")
        self.assertEqual(models.recommend(192)["model"], "IQ3_S")
        for ram in (16, 32, 48, 64, 96, 192):
            self.assertNotEqual(models.recommend(ram)["family"], "coder",
                                f"{ram} GB 不推荐 Coder（中文与 CJK 不行，#438）")
        coder = [a for a in models.recommend(32)["also"] if a["family"] == "coder"]
        self.assertEqual(len(coder), 1, "32 GB 那档要说得出 Coder 这个选项")

    def test_the_real_machine_line_is_readable(self):
        data = models.catalog(ENGINE, installed=[])
        self.assertIn("内存", data["machine"]["note"])
        self.assertTrue(data["recommendation"]["why"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

