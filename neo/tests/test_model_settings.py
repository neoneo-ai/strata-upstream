"""neo/tests/test_model_settings.py - 模型设置页的两块底层：覆盖配置与 GGUF 头部。

neo/model_settings.py：读根目录的 strata-*.json（只读），改动写进 neo/state/configs/
的一份覆盖配置，删掉就是原样。这里守着四件事：默认值怎么补、越界与认不得的键怎么挡、
写出来的 args 长什么样（尤其 --vision 与 --kv-resident 那两个坑）、上游那份不许被动。

neo/gguf_meta.py：只读 GGUF 头部拿层数。层数写在 <架构>.block_count 里，键名带前缀，
所以按后缀认；头部里上千项的分词表要按字节跳过，不能"遇到大的就停"。
"""
from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

if __package__ in (None, ""):                       # 直接跑 python neo/tests/test_model_settings.py 时没有父包
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "neo.tests"

from ._bootstrap import ENGINE, REPO                # noqa: E402

ROOT = REPO                                         # 老写法留下的名字：Neo 仓库的根

from neo import gguf_meta, model_settings, state              # noqa: E402


# ---------------- 一份最小的模型配置 ----------------
def base_cfg(mmproj=True):
    cfg = {
        "model_name": "Qwen Test",
        "exe": "strata.exe",
        "args": ["model.gguf", "--max-context", "65536", "--kv", "int8",
                 "--kv-resident", "32768", "--vram-reserve-mib", "1000", "--vision"],
        "port": 8080,
    }
    if mmproj:
        cfg["vision"] = {"mmproj": "mmproj.gguf", "gpu": True}
    return cfg


class OverlayCase(unittest.TestCase):
    """每个用例一套临时根目录 + 临时 state 目录（state.STATE 换掉，覆盖配置不落进仓库）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "root"
        self.root.mkdir()
        self.cfg_file = self.root / "strata-test.json"
        self.cfg_file.write_text(json.dumps(base_cfg()), encoding="utf-8")
        self.upstream_bytes = self.cfg_file.read_bytes()
        self._old_state = state.STATE
        state.STATE = Path(self._tmp.name) / "state"
        state.STATE.mkdir()
        self.addCleanup(setattr, state, "STATE", self._old_state)

    def write_cfg(self, cfg):
        self.cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
        self.upstream_bytes = self.cfg_file.read_bytes()

    def overlay(self):
        return model_settings.overlay_path(self.cfg_file)

    def overlay_json(self):
        return json.loads(self.overlay().read_text(encoding="utf-8"))


class TestSettingsOf(OverlayCase):
    def test_defaults_when_flags_absent(self):
        s = model_settings.settings_of({"exe": "e", "args": ["m.gguf"]})
        self.assertEqual(s["context"], 0)
        self.assertEqual(s["kv"], "int8")            # 引擎的默认（docs/DETAILS.md）
        self.assertEqual(s["kv_resident"], 0)
        self.assertEqual(s["vram_reserve_mib"], 0)
        self.assertFalse(s["vision"])
        self.assertFalse(s["vision_available"])

    def test_reads_the_flags_that_are_there(self):
        s = model_settings.settings_of(base_cfg())
        self.assertEqual(s["context"], 65536)
        self.assertEqual(s["kv_resident"], 32768)
        self.assertEqual(s["vram_reserve_mib"], 1000)
        self.assertTrue(s["vision"])
        self.assertTrue(s["vision_available"])
        self.assertTrue(s["vision_gpu"])

    def test_comma_in_context_is_accepted(self):
        s = model_settings.settings_of({"exe": "e", "args": ["--max-context", "262,144"]})
        self.assertEqual(s["context"], 262144)

    def test_vision_off_still_reports_the_encoder(self):
        # 关掉图片时段被置空、原段存进 neo_vision_saved —— "有没有编码器"要看两份
        cfg = base_cfg()
        cfg["neo_vision_saved"] = cfg.pop("vision")
        cfg["vision"] = None
        cfg["args"] = [a for a in cfg["args"] if a != "--vision"]
        s = model_settings.settings_of(cfg)
        self.assertFalse(s["vision"])
        self.assertTrue(s["vision_available"])


class TestChoices(OverlayCase):
    def test_current_odd_values_are_listed(self):
        # 用户自己写过的值不能被页面藏掉
        c = model_settings.choices({"context": 12345, "vram_reserve_mib": 1234,
                                    "kv_resident": 4096})
        self.assertIn(12345, [x["value"] for x in c["context"]])
        self.assertIn(1234, [x["value"] for x in c["vram_reserve"]])
        self.assertIn(4096, [x["value"] for x in c["kv_resident"]])

    def test_lists_are_sorted_and_bounded(self):
        c = model_settings.choices({})
        vals = [x["value"] for x in c["context"]]
        self.assertEqual(vals, sorted(vals))
        self.assertEqual(c["limits"]["context"],
                         [model_settings.MIN_CONTEXT, model_settings.MAX_CONTEXT])
class TestValidate(OverlayCase):
    def test_out_of_range_and_unknown_keys_are_refused(self):
        cfg = base_cfg()
        clean, _notes, errors = model_settings.validate({"context": 10}, cfg)
        self.assertNotIn("context", clean)
        self.assertTrue(errors)
        clean, _n, errors = model_settings.validate({"exe": "not-this"}, cfg)
        self.assertEqual(clean, {})
        self.assertTrue(any("不能改" in e for e in errors))

    def test_vision_needs_an_encoder(self):
        clean, _n, errors = model_settings.validate({"vision": True}, base_cfg(mmproj=False))
        self.assertNotIn("vision", clean)
        self.assertTrue(any("没有视觉编码器" in e for e in errors))

    def test_the_page_sends_on_and_off_as_words(self):
        # app.js 的表单传 "on"/"off"。bool("off") 是 True，所以必须逐字认
        clean, _n, errors = model_settings.validate({"vision": "off"}, base_cfg())
        self.assertEqual(clean, {"vision": False})
        self.assertEqual(errors, [])
        clean, _n, errors = model_settings.validate({"vision": "on"}, base_cfg())
        self.assertEqual(clean, {"vision": True})
        clean, _n, errors = model_settings.validate({"vision": "开"}, base_cfg())
        self.assertEqual(clean, {"vision": True})
        clean, _n, errors = model_settings.validate({"vision": "随便"}, base_cfg())
        self.assertNotIn("vision", clean)
        self.assertTrue(any("开或关" in e for e in errors))

    def test_good_values_pass(self):
        clean, notes, errors = model_settings.validate(
            {"context": 131072, "kv": "q4_0", "vram_reserve_mib": 1500}, base_cfg())
        self.assertEqual(errors, [])
        self.assertEqual(clean, {"context": 131072, "kv": "q4_0", "vram_reserve_mib": 1500})
        self.assertTrue(notes, "加上下文与留显存都要给用户一句话")


class TestApply(OverlayCase):
    def test_overlay_written_upstream_untouched(self):
        out = model_settings.apply(self.root, "strata-test.json", {"context": 131072})
        self.assertTrue(out["ok"], out["notes"])
        self.assertTrue(self.overlay().is_file())
        self.assertEqual(self.cfg_file.read_bytes(), self.upstream_bytes,
                         "上游那份 strata-*.json 一个字节都不能动")
        self.assertTrue(out["restart_required"])
        args = self.overlay_json()["args"]
        self.assertEqual(args[args.index("--max-context") + 1], "131072")

    def test_nothing_to_change_says_so(self):
        out = model_settings.apply(self.root, "strata-test.json", {"context": 65536})
        self.assertFalse(out["ok"])
        self.assertTrue(any("四栏都跟现在一样" in n for n in out["notes"]))
        self.assertFalse(self.overlay().is_file())

    def test_unknown_model_is_refused(self):
        out = model_settings.apply(self.root, "strata-none.json", {"context": 8192})
        self.assertFalse(out["ok"])
        self.assertTrue(any("没有这个模型" in n for n in out["notes"]))

    def test_vision_off_drops_flag_and_nulls_the_section(self):
        # serve/server.py:2348（#1322）看见 vision 段还在会把 --vision 加回去，
        # 所以关掉要两件一起做：flag 去掉 + 段置空，原段存进 neo_vision_saved
        out = model_settings.apply(self.root, "strata-test.json", {"vision": False})
        self.assertTrue(out["ok"], out["notes"])
        over = self.overlay_json()
        self.assertNotIn("--vision", over["args"])
        self.assertIsNone(over["vision"])
        self.assertEqual(over["neo_vision_saved"]["mmproj"], "mmproj.gguf")
        self.assertEqual(self.cfg_file.read_bytes(), self.upstream_bytes)

    def test_vision_on_after_off_goes_back_to_the_upstream_file(self):
        # 覆盖配置 = 上游 + 这次的改动。上游本来就开着图片，再要求开等于没改，
        # 于是覆盖配置被删掉 —— 回到上游那份，--vision 与 vision 段都在
        model_settings.apply(self.root, "strata-test.json", {"vision": False})
        self.assertTrue(self.overlay().is_file())
        out = model_settings.apply(self.root, "strata-test.json", {"vision": True})
        self.assertTrue(out["ok"], out["notes"])
        self.assertTrue(out["removed"])
        self.assertFalse(self.overlay().is_file())
        self.assertEqual(self.cfg_file.read_bytes(), self.upstream_bytes)

    def test_a_change_back_to_the_upstream_value_removes_the_overlay(self):
        model_settings.apply(self.root, "strata-test.json", {"context": 131072})
        out = model_settings.apply(self.root, "strata-test.json", {"context": 65536})
        self.assertTrue(out["ok"], out["notes"])
        self.assertTrue(out["removed"])
        self.assertFalse(self.overlay().is_file())

    def test_vision_on_restores_the_section_saved_by_a_earlier_off(self):
        # 上游自己就是关着图片的（vision 段空、原段在 neo_vision_saved）：
        # 开图片要把段还原回去，mmproj 路径 Neo 不猜
        cfg = base_cfg()
        cfg["neo_vision_saved"] = cfg.pop("vision")
        cfg["vision"] = None
        cfg["args"] = [a for a in cfg["args"] if a != "--vision"]
        self.write_cfg(cfg)
        out = model_settings.apply(self.root, "strata-test.json", {"vision": True})
        self.assertTrue(out["ok"], out["notes"])
        over = self.overlay_json()
        self.assertIn("--vision", over["args"])
        self.assertEqual(over["vision"]["mmproj"], "mmproj.gguf")

    def test_kv_resident_zero_drops_the_flag_with_its_value(self):
        # --kv-resident 带值：只删 flag 会把 32768 变成一个孤零零的位置参数，
        # 引擎会把它当模型文件
        out = model_settings.apply(self.root, "strata-test.json", {"kv_resident": 0})
        self.assertTrue(out["ok"], out["notes"])
        args = self.overlay_json()["args"]
        self.assertNotIn("--kv-resident", args)
        self.assertNotIn("32768", args)

    def test_config_path_for_follows_the_overlay(self):
        self.assertEqual(model_settings.config_path_for(self.cfg_file), str(self.cfg_file))
        model_settings.apply(self.root, "strata-test.json", {"context": 131072})
        self.assertEqual(model_settings.config_path_for(self.cfg_file), str(self.overlay()))

    def test_clear_removes_the_overlay(self):
        model_settings.apply(self.root, "strata-test.json", {"context": 131072})
        out = model_settings.clear("strata-test.json", self.cfg_file)
        self.assertTrue(out["ok"] and out["removed"])
        self.assertFalse(self.overlay().is_file())
        self.assertEqual(self.cfg_file.read_bytes(), self.upstream_bytes)

    def test_clear_without_an_overlay_says_it_is_already_plain(self):
        out = model_settings.clear("strata-test.json", self.cfg_file)
        self.assertTrue(out["ok"])
        self.assertFalse(out["removed"])

    def test_view_reports_the_overlay_and_the_restart_flag(self):
        model_settings.apply(self.root, "strata-test.json", {"context": 131072})
        v = model_settings.view(self.root, "strata-test.json", running=True)
        self.assertTrue(v["ok"])
        self.assertTrue(v["overridden"])
        self.assertTrue(v["restart_required"])
        self.assertEqual(v["settings"]["context"], 131072)
        self.assertEqual(v["base"]["context"], 65536)

    def test_view_of_a_missing_model_lists_the_ids(self):
        v = model_settings.view(self.root, "nope.json")
        self.assertFalse(v["ok"])
        self.assertIn("strata-test.json", v["models"])

    def test_view_without_a_name_picks_the_first_config(self):
        # 设置页刚打开、命令行不带 --model 时都是没点名的
        v = model_settings.view(self.root, "")
        self.assertTrue(v["ok"])
        self.assertEqual(v["id"], "strata-test.json")

    def test_view_with_no_config_at_all_says_so(self):
        self.cfg_file.unlink()
        v = model_settings.view(self.root, "")
        self.assertFalse(v["ok"])
        self.assertEqual(v["models"], [])
# ---------------- GGUF 头部 ----------------
def _s(text):
    raw = text.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def _kv(key, vtype, payload):
    return _s(key) + struct.pack("<I", vtype) + payload


def gguf_header(arch="qwen4exp", layers=42, extra_strings=0):
    """一份最小的 GGUF 头部：magic + v3 + 张量数 + 元数据条数 + 键值对。

    extra_strings 模拟分词表那种上千项的字符串数组：它排在 block_count 前面，
    解析器必须按字节跳过它 —— "遇到大的就停"就读不到后面那个键了。
    """
    pairs = []
    if extra_strings:
        table = b"".join(_s(f"tok{i}") for i in range(extra_strings))
        pairs.append(_kv("tokenizer.ggml.tokens", 9,
                         struct.pack("<I", 8) + struct.pack("<Q", extra_strings) + table))
    pairs.append(_kv(f"{arch}.block_count", 4, struct.pack("<I", layers)))
    pairs.append(_kv("general.architecture", 8, _s(arch)))
    return (b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 317)
            + struct.pack("<Q", len(pairs)) + b"".join(pairs))


class TestEstimate(OverlayCase):
    def test_kv_math_is_cells_x_bytes_x_layers(self):
        gguf = self.root / "model.gguf"
        gguf.write_bytes(gguf_header("qwen4exp", 42))
        cfg = base_cfg()
        cfg["path"] = str(gguf)                      # model_file_of 读的就是 path
        est = model_settings.estimate(cfg, model_settings.settings_of(cfg),
                                      gpu={"name": "T", "vram_gb": 24.0}, live={})
        self.assertEqual(est["layers"], 42)
        self.assertEqual(est["kv_cells"], 32768)     # --kv-resident 写的值
        want = 32768 * model_settings.KV_BYTES_PER_CELL["int8"] * 42 / model_settings.GIB
        self.assertAlmostEqual(est["kv_gib"], round(want, 2), places=2)

    def test_reserve_shows_up_in_the_headroom(self):
        est = model_settings.estimate(
            {"exe": "e", "args": []},
            {"context": 8192, "kv": "int8", "vram_reserve_mib": 1500},
            gpu={"name": "T", "vram_gb": 24.0}, live={})
        self.assertEqual(est["reserve_mib"], 1500)
        self.assertAlmostEqual(est["headroom_gib"],
                               round(24.0 - est["total_gib"] - 1500 / 1024.0, 2), places=2)

    def test_no_gpu_means_no_headroom(self):
        est = model_settings.estimate({"exe": "e", "args": []}, {}, gpu={}, live={})
        self.assertIsNone(est["headroom_gib"])

    def test_live_metrics_are_passed_through(self):
        est = model_settings.estimate({"exe": "e", "args": []}, {}, gpu={},
                                      live={"vram_free_mib": 1234})
        self.assertEqual(est["live"]["vram_free_mib"], 1234)


class TestGgufMeta(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, name, blob):
        path = self.dir / name
        path.write_bytes(blob)
        return path

    def test_reads_version_tensors_and_layers(self):
        path = self.write("m.gguf", gguf_header("qwen4exp", 75))
        meta = gguf_meta.read_meta(path)
        self.assertEqual(meta["version"], 3)
        self.assertEqual(meta["tensors"], 317)
        self.assertEqual(meta["qwen4exp.block_count"], 75)
        self.assertEqual(gguf_meta.block_count(path), 75)

    def test_big_string_array_before_the_key_is_skipped(self):
        path = self.write("m.gguf", gguf_header("qwen4exp", 64, extra_strings=5000))
        self.assertEqual(gguf_meta.block_count(path), 64)

    def test_arch_prefix_wins_over_general(self):
        blob = (b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 1) + struct.pack("<Q", 3)
                + _kv("general.architecture", 8, _s("llama"))
                + _kv("llama.block_count", 4, struct.pack("<I", 28))
                + _kv("general.block_count", 4, struct.pack("<I", 99)))
        self.assertEqual(gguf_meta.block_count(self.write("a.gguf", blob)), 28)

    def test_any_block_count_suffix_is_a_fallback(self):
        blob = (b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 1) + struct.pack("<Q", 1)
                + _kv("weird.arch.block_count", 4, struct.pack("<I", 12)))
        self.assertEqual(gguf_meta.block_count(self.write("b.gguf", blob)), 12)

    def test_not_a_gguf_is_an_empty_table(self):
        path = self.write("x.gguf", b"NOTGGUF" + b"\x00" * 64)
        self.assertEqual(gguf_meta.read_meta(path), {})
        self.assertEqual(gguf_meta.block_count(path), 0)

    def test_missing_file_is_an_empty_table(self):
        self.assertEqual(gguf_meta.read_meta(self.dir / "none.gguf"), {})

    def test_truncated_header_is_marked_not_raised(self):
        blob = (b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 1) + struct.pack("<Q", 1)
                + _kv("qwen4exp.block_count", 4, b"\x01"))   # 标量少两个字节
        meta = gguf_meta.read_meta(self.write("t.gguf", blob))
        self.assertTrue(meta.get("truncated"))
        self.assertEqual(gguf_meta.block_count(self.write("t2.gguf", blob)), 0)

    def test_version_1_is_not_read_further(self):
        meta = gguf_meta.read_meta(self.write("v1.gguf", b"GGUF" + struct.pack("<I", 1)
                                              + struct.pack("<I", 3)))
        self.assertEqual(meta["version"], 1)
        self.assertNotIn("tensors", meta)

    def test_absurd_kv_count_stops_the_walk(self):
        blob = (b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 1)
                + struct.pack("<Q", gguf_meta.MAX_N_KV + 1))
        meta = gguf_meta.read_meta(self.write("k.gguf", blob))
        self.assertEqual(meta["tensors"], 1)
        self.assertNotIn("truncated", meta)


if __name__ == "__main__":
    unittest.main()


