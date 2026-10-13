"""neo/model_settings.py - 模型设置页：读一个模型配置里的运行参数，改动写进 Neo 自己的覆盖配置。

页面上能改的四样（上下文长度、KV cache 类型、视觉开关、显存保留）都在模型配置的 args 里，
就是引擎本来就认的那些参数：--max-context / --kv / --vision / --vram-reserve-mib。
serve/server.py 自己**没有**这几个的命令行开关（它的 add_argument 只有 --engine /
--config / --port / --host / --api-key / --gpu / --tokenizer / --lazy / --api-monitor
那一串，实测 serve/server.py:6167-6207），所以 Neo 一行上游都不改：

    读   根目录的 strata-<id>.json（只读，setup.py 写的那个）
    写   neo/state/configs/strata-<id>.neo.json —— 上游那份的副本 + 改过的 args
    起   neo/engines/strata.py 的 plan() 看见覆盖配置在，就把 --config 指到它
         （--config 是 serve/server.py 已有的参数，Neo 不给它加参数）
    重启 走 Neo 已有的 set_model 流程（supervisor.set_model：先停后起）

上游那份 strata-*.json 一个字节都不动：删掉 neo/state/configs 就回到原样，setup.py
随时可以再跑它。写覆盖配置用 state.write_json（临时文件 + os.replace + 旧内容 .bak）。

视觉开关有个坑：serve/server.py:2348（#1322）看见配置里有 "vision" 段而 args 里没有
--vision 时，会**自己把 --vision 加回去**（旧配置只写段不写 flag， advertise 了图片
又拒绝每一张）。所以关掉图片要两件一起做：args 里去掉 --vision，"vision" 段置空，
原段存进覆盖配置的 neo_vision_saved，再打开时还原回去。

显存那一栏是**估算**，公式与出处写在 estimate() 里：KV 常驻 = 常驻单元数 ×
每单元每层的字节数 × 层数（字节数出自 bench/results/2026-09-27-kv-q4/README.md：
q4_0 576 B、int8 1,056 B 每单元每层；fp16 按 int8 的两倍、k8v4 取 int8 与 q4_0 的中间）。
层数读 GGUF 头部（neo/gguf_meta.py）。权重与激活由引擎按显卡自己分，不在这一栏里 ——
服务在跑时把引擎自己报的 expert_cache_mib / vram_free_mib 一起给出去（/metrics 的
engine 节，serve/server.py:3321）。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import gguf_meta, state
from .engines.strata import arg_value, model_file_of

GIB = 2 ** 30
MIB = 2 ** 20

#: setup.py 的 --kv 取值（setup.py:4801 ap.add_argument("--kv", choices=[…])）
KV_CHOICES = ("int8", "q4_0", "k8v4")
#: 每单元每层的字节数（一个单元 = 一层的 K 加 V 的一个 token）
KV_BYTES_PER_CELL = {"fp16": 2112, "int8": 1056, "q4_0": 576, "k8v4": 816}
KV_NOTES = {
    "int8": "默认。测下来与 fp16 分不出差别（bench/results/2026-09-27-kv-q4）",
    "q4_0": "KV 显存省一半，128K 时解码快 3.7%；长文档困惑度 +8-12%",
    "k8v4": "K 8 位、V 4 位；64K 以上把 KV 留在显存里",
}
#: 上下文长度的常用档（setup.py 写的值都在这一串里：65536 / 131072 / 202072 / 262144）
CONTEXT_CHOICES = (8192, 16384, 32768, 65536, 131072, 202072, 262144)
#: 显存保留的常用档：700 = 视觉编码器在 GPU 上时 setup.py 写的（setup.py VISION reserve），
#: 1000 = 本机配置里常见的值，1500 = 也带显示器的 NVIDIA 卡（setup.py DISPLAY_RESERVE_MIB），
#: 3072 = 带 Linux 桌面的 AMD 卡（setup.py DESKTOP_RESERVE_MIB）
RESERVE_CHOICES = (0, 700, 1000, 1500, 3072)
RESERVE_NOTES = {
    0: "全给引擎：专家缓存最大，桌面/游戏要的显存最少",
    700: "图片编码器在显卡上时 setup.py 写的值",
    1500: "这张卡还带显示器时建议留的（setup.py 的提示）",
    3072: "带 Linux 桌面的 AMD 卡：留少了桌面会被 OOM 杀掉（setup.py 的提示）",
}
KV_RESIDENT_CHOICES = (0, 8192, 32768, 65536)
MIN_CONTEXT, MAX_CONTEXT = 1024, 1_048_576
MIN_RESERVE, MAX_RESERVE = 0, 65_535
MIN_KV_RESIDENT, MAX_KV_RESIDENT = 0, 1_048_576
#: 页面上能提交的键，其它一律挡回来
CHANGABLE_KEYS = ("context", "kv", "vision", "vram_reserve_mib", "kv_resident")


def configs_dir() -> Path:
    """覆盖配置放哪：neo/state/configs（state.STATE 在测试里会被换掉，所以每次现取）。"""
    return Path(state.STATE) / "configs"


def overlay_path(cfg_file) -> Path:
    """这个模型配置的覆盖配置路径：strata-x.json -> strata-x.neo.json（在 Neo 的地盘里）。"""
    return configs_dir() / (Path(str(cfg_file)).stem + ".neo.json")


def config_path_for(cfg_file) -> str:
    """启动要用哪份配置：有覆盖配置就用它，没有就用上游那份。plan() 调这个。"""
    over = overlay_path(cfg_file)
    return str(over) if over.is_file() else str(cfg_file)


def read_config(cfg_file) -> dict:
    """读一份模型配置（UTF-8 带 BOM 也认，setup.py 就这么写）。坏了回空表。"""
    try:
        cfg = json.loads(Path(str(cfg_file)).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def settings_of(cfg: dict) -> dict:
    """从配置的 args 里取那四样 + 两个相关的（KV 常驻、专家缓存）。

    默认值按引擎与 setup.py 的默认：--kv 没写就是 int8（docs/DETAILS.md），
    --vram-reserve-mib 没写就是 0（引擎自己那一份默认），--vision 看 args 里有没有。
    """
    args = cfg.get("args")
    args = args if isinstance(args, list) else []
    kv = str(arg_value(args, "--kv") or "")
    vis = cfg.get("vision")
    # 关掉图片时 Neo 把段置空、原段存进 neo_vision_saved（见 apply()），
    # 所以"这个模型有没有编码器"要看两份：段在，或存着的那份在
    section = vis if isinstance(vis, dict) else (cfg.get("neo_vision_saved")
                                                 if isinstance(cfg.get("neo_vision_saved"), dict)
                                                 else None)
    try:
        context = int(str(arg_value(args, "--max-context")).replace(",", ""))
    except (TypeError, ValueError):
        context = 0
    try:
        kv_resident = int(str(arg_value(args, "--kv-resident")).replace(",", ""))
    except (TypeError, ValueError):
        kv_resident = 0
    try:
        reserve = int(str(arg_value(args, "--vram-reserve-mib")))
    except (TypeError, ValueError):
        reserve = 0
    cache = str(arg_value(args, "--expert-cache") or "auto")
    return {
        "context": context if context > 0 else 0,
        "kv": kv if kv in KV_CHOICES else "int8",
        "kv_set": kv in KV_CHOICES,
        "kv_resident": kv_resident if kv_resident > 0 else 0,
        "vision": isinstance(vis, dict) and "--vision" in args,
        "vision_gpu": bool((section or {}).get("gpu")),
        "vision_available": section is not None,
        "vram_reserve_mib": reserve if reserve >= 0 else 0,
        "expert_cache": cache,
        "expert_cache_mib": int(cache) if cache.isdigit() else 0,
    }

# ---------------- 页面上的选择表 ----------------
def _items(values, notes, title_of):
    return [{"value": v, "title": title_of(v), "note": notes.get(v, "")} for v in values]


def choices(settings: dict | None = None) -> dict:
    """四栏的可选项。当前值不在预设里也列出来 —— 用户自己写过的值不能被页面藏掉。"""
    settings = settings or {}
    contexts = list(CONTEXT_CHOICES)
    reserve = list(RESERVE_CHOICES)
    kv_resident = list(KV_RESIDENT_CHOICES)
    current_ctx = int(settings.get("context") or 0)
    current_res = int(settings.get("vram_reserve_mib") or 0)
    current_kv = int(settings.get("kv_resident") or 0)
    if current_ctx and current_ctx not in contexts:
        contexts.append(current_ctx)
    if current_res not in reserve:
        reserve.append(current_res)
    if current_kv and current_kv not in kv_resident:
        kv_resident.append(current_kv)
    for lst in (contexts, reserve, kv_resident):
        lst.sort()
    return {
        "context": _items(contexts, {}, lambda v: f"{v:,}"),
        "kv": _items(KV_CHOICES, KV_NOTES, lambda v: v),
        "kv_resident": _items(kv_resident,
                              {0: "整段上下文都放显存", 32768: "setup.py 在 64K 以上写的值"},
                              lambda v: "整段放显存" if v == 0 else f"{v:,} 单元"),
        "vram_reserve": _items(reserve, RESERVE_NOTES, lambda v: f"{v} MiB"),
        "limits": {"context": [MIN_CONTEXT, MAX_CONTEXT],
                   "vram_reserve": [MIN_RESERVE, MAX_RESERVE],
                   "kv_resident": [MIN_KV_RESIDENT, MAX_KV_RESIDENT]},
    }


# ---------------- 显存估算 ----------------
def file_gib(path) -> float:
    """一个文件多大（GiB）。不在回 0。"""
    try:
        return Path(str(path)).stat().st_size / GIB
    except OSError:
        return 0.0


def live_metrics(port: int, api_key: str = "") -> dict:
    """服务在跑时引擎自己报的那几个显存数（/metrics 的 engine 节）。拿不到回空表。"""
    from .engines.base import http_json
    data = http_json(int(port), "/metrics", api_key=api_key)
    if not isinstance(data, dict):
        return {}
    engine = data.get("engine")
    engine = engine if isinstance(engine, dict) else {}
    keys = ("expert_slots", "expert_cache_mib", "vram_free_mib", "max_context", "kv", "images")
    return {k: engine[k] for k in keys if k in engine}


def estimate(cfg: dict, settings: dict | None = None, gpu: dict | None = None,
             live: dict | None = None) -> dict:
    """显存这一栏的数怎么来的，全写在这里。

    KV 常驻 = 常驻单元数 × 每单元每层的字节数 × 层数。配置里没有 --kv-resident 时
    按整段上下文算（64K 以下 setup.py 不写这个 flag）。层数读 GGUF 头部。
    专家缓存：--expert-cache 写了数字就是 MiB，auto 是引擎按显卡自己算（Neo 不猜）。
    视觉编码器：mmproj 文件多大（只在编码器跑在显卡上时算进显存）。
    权重与激活由引擎自己分，不在这一栏里 —— 服务在跑时把引擎报的数一起给出去。
    """
    settings = settings or settings_of(cfg)
    model = model_file_of(cfg)
    layers = gguf_meta.block_count(model) if model else 0
    cells = int(settings.get("kv_resident") or 0) or int(settings.get("context") or 0)
    per_cell = KV_BYTES_PER_CELL.get(settings.get("kv"), KV_BYTES_PER_CELL["int8"])
    kv_gib = cells * per_cell * layers / GIB
    expert_mib = int(settings.get("expert_cache_mib") or 0)
    vis = cfg.get("vision")
    vision_gib = 0.0
    if settings.get("vision") and settings.get("vision_gpu") and isinstance(vis, dict):
        vision_gib = file_gib(vis.get("mmproj"))
    reserve_mib = int(settings.get("vram_reserve_mib") or 0)
    total_gib = kv_gib + expert_mib / 1024.0 + vision_gib
    gpu_total = float((gpu or {}).get("vram_gb") or 0.0)
    return {
        "layers": layers,
        "kv_cells": cells,
        "kv_mode": "resident" if settings.get("kv_resident") else "full",
        "kv_bytes_per_cell": per_cell,
        "kv_gib": round(kv_gib, 2),
        "expert_cache": settings.get("expert_cache") or "auto",
        "expert_cache_mib": expert_mib,
        "vision_gib": round(vision_gib, 2),
        "reserve_mib": reserve_mib,
        "total_gib": round(total_gib, 2),
        "gpu_name": str((gpu or {}).get("name") or ""),
        "gpu_total_gib": round(gpu_total, 1),
        "headroom_gib": round(gpu_total - total_gib - reserve_mib / 1024.0, 2) if gpu_total else None,
        "live": dict(live or {}),
        "note": ("只算 KV 常驻 + 专家缓存 + 视觉编码器 + 显存保留；权重与激活由引擎按显卡自己分。"
                 "每单元每层的字节数出自 bench/results/2026-09-27-kv-q4/README.md"
                 "（q4_0 576 B、int8 1,056 B），层数读 GGUF 头部的 general.block_count。"),
    }


# ---------------- 校验与写入 ----------------
TRUE_WORDS = ("on", "true", "yes", "1", "开")
FALSE_WORDS = ("off", "false", "no", "0", "关", "")


def _as_bool(value) -> bool:
    """页面传的是 "on" / "off"（表单的写法），也认 true/false/1/0/开/关。

    bool("off") 是 True —— 直接 bool() 会把"关"读成"开"，所以字符串必须逐个认。
    认不得的就报错，不猜。
    """
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    raise ValueError(f"不是开也不是关：{value!r}")


def validate(changes: dict, cfg: dict) -> tuple[dict, list[str], list[str]]:
    """(能写的, 给用户看的话, 挡回来的话)。认不得的键与越界的值都不写。"""
    clean: dict = {}
    notes: list[str] = []
    errors: list[str] = []
    base = settings_of(cfg)
    for key in changes:
        if key not in CHANGABLE_KEYS:
            errors.append(f"这一栏不能改：{key}（能改的只有 {' / '.join(CHANGABLE_KEYS)}）")
    if "context" in changes and not errors:
        try:
            value = int(str(changes["context"]).replace(",", ""))
        except (TypeError, ValueError):
            errors.append("上下文长度要是一个整数")
        else:
            if not MIN_CONTEXT <= value <= MAX_CONTEXT:
                errors.append(f"上下文长度要在 {MIN_CONTEXT:,} 到 {MAX_CONTEXT:,} 之间")
            else:
                clean["context"] = value
                if value > base["context"]:
                    notes.append(f"上下文从 {base['context']:,} 加到 {value:,}：模型加载更久，"
                                 "KV 占的显存也跟着涨")
    if "kv" in changes and not errors:
        value = str(changes["kv"])
        if value not in KV_CHOICES:
            errors.append(f"KV cache 类型只认 {' / '.join(KV_CHOICES)}")
        else:
            clean["kv"] = value
    if "kv_resident" in changes and not errors:
        try:
            value = int(str(changes["kv_resident"]).replace(",", ""))
        except (TypeError, ValueError):
            errors.append("KV 常驻单元数要是一个整数")
        else:
            if not MIN_KV_RESIDENT <= value <= MAX_KV_RESIDENT:
                errors.append(f"KV 常驻单元数要在 {MIN_KV_RESIDENT:,} 到 {MAX_KV_RESIDENT:,} 之间")
            else:
                clean["kv_resident"] = value
    if "vram_reserve_mib" in changes and not errors:
        try:
            value = int(str(changes["vram_reserve_mib"]))
        except (TypeError, ValueError):
            errors.append("显存保留要是一个整数（MiB）")
        else:
            if not MIN_RESERVE <= value <= MAX_RESERVE:
                errors.append(f"显存保留要在 {MIN_RESERVE} 到 {MAX_RESERVE:,} MiB 之间")
            else:
                clean["vram_reserve_mib"] = value
                if value > 0:
                    notes.append(f"留 {value} MiB 显存给别的程序：专家缓存会小一些，"
                                 "生成速度掉几个百分点")
    if "vision" in changes and not errors:
        try:
            want = _as_bool(changes["vision"])
        except ValueError:
            errors.append("图片那一栏要的是开或关（on / off）")
        else:
            if want and not base["vision_available"]:
                errors.append("这个模型没有视觉编码器（配置里没有 vision 段），图片开不了")
            else:
                if want and not base["vision"]:
                    notes.append("开图片：编码器在显卡上时要占约 1.4 GB 显存（docs/DETAILS.md），"
                                 "提示词的块会变小")
                clean["vision"] = want
    return clean, notes, errors


def _set_flag(args: list, flag: str, value) -> list:
    """args 是扁平列表：有这个 flag 就改它后面那个值，没有就追加到末尾。"""
    out = list(args)
    if flag in out:
        i = out.index(flag)
        if i + 1 < len(out):
            out[i + 1] = str(value)
        else:
            out.append(str(value))
    else:
        out += [flag, str(value)]
    return out


def _drop_flag(args: list, flag: str, with_value: bool = False) -> list:
    """去掉一个 flag。--vision 不带值（with_value=False）；--kv-resident 带值，
    连值一起去掉，否则值会变成一个孤零零的位置参数（引擎会把它当模型文件）。"""
    out = list(args)
    while flag in out:
        i = out.index(flag)
        del out[i:(i + 2) if with_value else (i + 1)]
    return out



def apply(root, model_id: str, changes: dict) -> dict:
    """把页面上改过的几样写进覆盖配置（neo/state/configs）。上游那份一个字节都不动。"""
    import time
    item = find_config(root, model_id)
    if item is None:
        return {"ok": False, "overlay": "",
                "notes": [f"没有这个模型：{model_id or '（没点名）'}",
                          "先看 GET /neo/models 里的 id"]}
    cfg = read_config(item["file"])
    if not cfg.get("exe") or not isinstance(cfg.get("args"), list):
        return {"ok": False, "overlay": "",
                "notes": [f"{item['id']} 不是一份模型配置（没有 exe 或 args）"]}
    clean, notes, errors = validate(changes or {}, cfg)
    if errors:
        return {"ok": False, "overlay": "", "notes": errors}
    base = settings_of(cfg)
    # 覆盖配置 = 上游那份 + 这次的改动，所以跟上游一样的键不写进去
    clean = {k: v for k, v in clean.items() if base.get(k) != v}
    if not clean:
        # 这次要的跟上游一样：有覆盖配置就删掉它（回到原样），没有就什么都没发生
        if overlay_path(item["file"]).is_file():
            gone = clear(item["id"], item["file"])
            if not gone["ok"]:
                return {"ok": False, "overlay": "", "notes": gone["notes"]}
            gone["restart_required"] = True
            gone["model"] = item["name"]
            return gone
        return {"ok": False, "overlay": "", "restart_required": False,
                "notes": ["没有要改的：四栏都跟现在一样"]}
    args = list(cfg["args"])
    if "context" in clean:
        args = _set_flag(args, "--max-context", clean["context"])
    if "kv" in clean:
        args = _set_flag(args, "--kv", clean["kv"])
    if "kv_resident" in clean:
        args = _set_flag(args, "--kv-resident", clean["kv_resident"]) if clean["kv_resident"] \
            else _drop_flag(args, "--kv-resident", with_value=True)
    if "vram_reserve_mib" in clean:
        args = _set_flag(args, "--vram-reserve-mib", clean["vram_reserve_mib"])
    out = dict(cfg)
    saved = out.get("neo_vision_saved")
    if "vision" in clean:
        if clean["vision"]:
            section = out.get("vision") if isinstance(out.get("vision"), dict) else saved
            out["vision"] = section if isinstance(section, dict) else None
            if "--vision" not in args:
                args = args + ["--vision"]
        else:
            # 两件一起做：flag 去掉、段置空。serve/server.py:2348（#1322）看见段还在
            # 会把 --vision 加回去，只删 flag 等于没关。原段存进 neo_vision_saved，
            # 再开图片时还原回去（mmproj 路径 Neo 不猜，只能还给人家原来那份）。
            args = _drop_flag(args, "--vision")
            if isinstance(out.get("vision"), dict):
                out["neo_vision_saved"] = out["vision"]
            out["vision"] = None
    out["args"] = args
    out["neo_overlay"] = {"from": str(item["file"]), "at": time.time(),
                          "changed": sorted(clean.keys()), "model": item["name"]}
    path = overlay_path(item["file"])
    try:
        state.write_json(path, out)
    except OSError as e:
        return {"ok": False, "overlay": "", "notes": [f"覆盖配置写不了：{e}"]}
    notes.append("已写进覆盖配置（neo/state/configs），上游那份 strata-*.json 没动")
    notes.append("改了要重启服务才生效：点「重启并应用」，Neo 先停再起")
    return {"ok": True, "overlay": str(path), "model": item["name"], "id": item["id"],
            "settings": settings_of(out), "changed": sorted(clean.keys()),
            "restart_required": True, "notes": notes}


def clear(model_id: str, cfg_file) -> dict:
    """把这个模型的覆盖配置删掉：下一次启动回到上游配置原样。"""
    path = overlay_path(cfg_file)
    if not path.is_file():
        return {"ok": True, "removed": False, "overlay": str(path),
                "notes": ["这个模型没有覆盖配置，本来就是原样"]}
    try:
        path.unlink()
    except OSError as e:
        return {"ok": False, "removed": False, "overlay": str(path), "notes": [f"删不掉：{e}"]}
    return {"ok": True, "removed": True, "overlay": str(path),
            "notes": ["已删掉覆盖配置：重启服务就回到 setup.py 写的那份"]}


def find_config(root, model_id: str):
    """按 id（配置文件名）找那个模型配置。找不到回 None。"""
    from .engines.strata import list_configs
    wanted = str(model_id or "")
    if not wanted:
        return None
    return next((x for x in list_configs(root) if x["id"] == wanted), None)


def pick_config(root, model_id: str = ""):
    """按 id 找配置；只有完全没点名时才用第一个。

    点了名却找不到就回 None —— 认不得的 id 悄悄换成别的模型，用户看到的
    就是另一个模型的数字了。没点名（设置页刚打开、命令行不带 --model）才给第一个。
    """
    wanted = str(model_id or "")
    if wanted:
        return find_config(root, wanted)
    configs = _configs(root)
    return configs[0] if configs else None


def view(root, model_id: str, port: int = 0, api_key: str = "", running: bool = False,
         gpu: dict | None = None) -> dict:
    """模型设置页要的那一屏：当前值 + 可选项 + 显存估算 + 有没有覆盖配置 + 要不要重启。"""
    from . import models as neo_models
    item = pick_config(root, model_id)
    if item is None:
        return {"ok": False, "error": f"没有这个模型：{model_id or '（没点名）'}",
                "models": [x["id"] for x in _configs(root)]}
    cfg = read_config(item["file"])
    over = overlay_path(item["file"])
    effective = read_config(over) if over.is_file() else cfg
    settings = settings_of(effective)
    return {
        "ok": True,
        "id": item["id"],
        "model": item["name"],
        "config": str(item["file"]),
        "overlay": str(over) if over.is_file() else "",
        "overridden": over.is_file(),
        "installed": bool(item["installed"]),
        "missing": item["missing"],
        "settings": settings,
        "base": settings_of(cfg),
        "choices": choices(settings),
        "estimate": estimate(effective, settings,
                             gpu=gpu if gpu is not None else neo_models.gpu_info(),
                             live=live_metrics(port, api_key) if port else {}),
        "restart_required": bool(over.is_file() and running),
        "running": bool(running),
        "keys": list(CHANGABLE_KEYS),
    }


def _configs(root) -> list[dict]:
    from .engines.strata import list_configs
    return list_configs(root)


def main(argv=None) -> int:
    """命令行看/改一个模型的运行参数：

        python -m neo.model_settings --root .                      看最近用过的那个
        python -m neo.model_settings --root . --model strata-x.json --json
        python -m neo.model_settings --root . --set context=65536 kv=q4_0
        python -m neo.model_settings --root . --reset              删掉覆盖配置
    """
    import argparse

    from .paths import safe_resolve

    p = argparse.ArgumentParser(description="看与改一个模型的上下文 / KV / 图片 / 显存保留")
    p.add_argument("--root", default=".")
    p.add_argument("--model", default="", help="模型 id（配置文件名，默认最近用过的那个）")
    p.add_argument("--set", dest="sets", nargs="*", default=[],
                   help="要改的：context=65536 kv=q4_0 vision=off vram_reserve_mib=1500")
    p.add_argument("--reset", action="store_true", help="删掉这个模型的覆盖配置")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    root = safe_resolve(Path(args.root))
    item = pick_config(root, args.model)
    if item is None:
        print(f"{root} 里没有 strata-*.json —— 这台机器还没装模型")
        return 2
    changes: dict = {}
    for pair in args.sets:
        key, _, value = str(pair).partition("=")
        key = key.strip().replace("-", "_")
        if key == "vision":
            changes[key] = value.strip().lower() not in ("off", "no", "false", "0", "")
        else:
            changes[key] = value.strip()
    if args.reset:
        out = clear(item["id"], item["file"])
    elif changes:
        out = apply(root, item["id"], changes)
    else:
        out = view(root, item["id"])
    if args.json:
        import json as _json
        print(_json.dumps(out, ensure_ascii=False, indent=1))
        return 0 if out.get("ok") else 2
    for line in out.get("notes", []):
        print(f"  {line}")
    if not out.get("ok"):
        print(f"没成：{out.get('error') or out.get('notes')}")
        return 2
    if args.reset:
        print(f"  {out['overlay']}")
        return 0
    s = out["settings"]
    est = out["estimate"]
    print(f"== {out['model']}（{out['id']}）")
    print(f"  上下文 {s['context']:,} · KV {s['kv']} · 图片 {'开' if s['vision'] else '关'}"
          f" · 显存保留 {s['vram_reserve_mib']} MiB")
    print(f"  覆盖配置：{out['overlay'] or '没有（用的是 setup.py 写的那份）'}")
    print(f"  要重启才生效：{'是' if out['restart_required'] else '不用'}")
    print(f"  显存估算 {est['total_gib']} GiB（KV {est['kv_gib']} + 专家缓存 "
          f"{est['expert_cache_mib']} MiB + 编码器 {est['vision_gib']}），"
          f"显卡 {est['gpu_total_gib']} GiB，层数 {est['layers']}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
