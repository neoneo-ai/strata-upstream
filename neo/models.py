"""neo/models.py - 可安装的模型清单：读 setup.py 的模型表，按文档给推荐，标出装没装。

清单不抄在 Neo 里。setup.py 顶层的 MODELS（尺寸）与 FAMILIES（版本）就是安装菜单的
全部来源，Neo 用 ast 读这两张表里的字面量（不 import setup.py，不跑它一行代码）：
上游加一个尺寸，Neo 的模型页第二天就跟着有。表里 hf(...) 这类要执行才成立的值读出来
是 None，跳过就行 —— Neo 只要 title / about / download_gb / ram_gb 这些字面量。

推荐按 docs/AI_SETUP.md 第 3 步那张表和 docs/MODELS.md 的 "Pick by RAM"：
32 GB → IQ2_XS（走低内存模式）；48 GB → IQ2_XS（Q2_0 最快）；
64 GB → IQ2_XS（文档推荐）；96 GB 以上 → IQ3_S（Unsloth 的 UD-IQ4_XS 也可以）。
Coder（IQ1_M）在清单里可以装，Neo 不推荐它：它只留一半专家，中文与其它 CJK 的回答会
出错或绕圈（docs/MODELS.md #438），这台机器的界面与用户主要用中文，更不该默认推它。

装没装认两件事：根目录那个 strata-<tag+尺寸>.json 在不在（setup.py 给每个装好的模型
写这个文件，命名是 tag = fam["tag"] + model），以及 neo/engines/strata.py 的 discover()
说它缺不缺东西（模型文件在不在、引擎在不在）—— 配置在而且起得来才算装好。

下载源也在这一页切换（SOURCE_CHOICES）：自动（按 neo/netprobe.py 的探测建议）、
Hugging Face、ModelScope、hf-mirror 镜子。前三个走 setup.py 本来就有的 --source，
镜子走 HF_ENDPOINT 环境变量 —— Neo 不改上游一行，选中的源记在 neo/state/source.json。
"""
from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import netprobe, state
from .engines import make_adapter

SETUP_NAME = "setup.py"
UNSLOTH_RAM_LEFT_GB = 24          # setup.py 的同名常量：内存里留给 OS/引擎/文件缓存的那部分
DEFAULT_FAMILIES = ("qwen", "swift")   # setup.py 里没写 families 的尺寸属于这两个版本
GPU_CACHE_S = 60.0                # nvidia-smi 问一次要几百毫秒，60 秒内不重复问

_TABLES: dict = {}                # (路径, mtime_ns, 大小) -> (MODELS, FAMILIES)
_GPU = {"at": 0.0, "name": "", "vram_gb": 0.0}


def setup_path(root) -> Path:
    return Path(root) / SETUP_NAME


# ---------------- 读 setup.py 的两张表 ----------------
def _literal(node):
    """ast 节点 -> Python 值。字典里遇到要执行才成立的值（hf("repo") 这类调用）就丢掉那一项，
    列表里遇到就丢掉那一项 —— Neo 只要 title / about / download_gb / ram_gb 这些字面量。"""
    if isinstance(node, ast.Dict):
        out = {}
        for key, val in zip(node.keys, node.values):
            got = _literal(key)
            value = _literal(val)
            if got is not None and value is not None:
                out[got] = value
        return out
    if isinstance(node, ast.List):
        return [v for v in (_literal(x) for x in node.elts) if v is not None]
    if isinstance(node, ast.Tuple):
        return tuple(v for v in (_literal(x) for x in node.elts) if v is not None)
    if isinstance(node, ast.Set):
        return {v for v in (_literal(x) for x in node.elts) if v is not None}
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _literal(node.operand)
        if value is None:
            return None
        return value if isinstance(node.op, ast.UAdd) else -value
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None


def _top_dict(source: str, name: str) -> dict:
    """setup.py 顶层那个 dict 字面量（MODELS / FAMILIES）。找不到回空表。"""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        if not isinstance(node.value, ast.Dict):
            return {}
        got = _literal(node.value)
        return got if isinstance(got, dict) else {}
    return {}


def read_tables(root) -> tuple[dict, dict]:
    """(MODELS, FAMILIES)。按文件 mtime 缓存，setup.py 换了就重读；读不到回两个空表。"""
    path = setup_path(root)
    try:
        info = path.stat()
    except OSError:
        return {}, {}
    key = (str(path), info.st_mtime_ns, info.st_size)
    hit = _TABLES.get(key)
    if hit is not None:
        return hit
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, {}
    pair = (_top_dict(source, "MODELS"), _top_dict(source, "FAMILIES"))
    if pair[0] and pair[1]:
        _TABLES.clear()                     # 只留最近这一份，别攒着几个根目录的表
        _TABLES[key] = pair
    return pair


def sizes_of_family(models: dict, family: str) -> list[str]:
    """这个版本有哪些尺寸（与 setup.py 的 names 一条规则）。"""
    return [m for m, d in models.items() if family in d.get("families", DEFAULT_FAMILIES)]


# ---------------- 这台机器 ----------------
def total_ram_gb() -> float:
    """内存总量（GB）。与 setup.py 的 ram_gb() 同一算法：Windows 问 GlobalMemoryStatusEx，
    Linux 读 /proc/meminfo。问不到回 0，调用方要说"读不到"，不能假装这是一台 0 GB 的机器。"""
    if sys.platform == "win32":
        try:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.ullTotalPhys / 2 ** 30
        except Exception:                                    # 问不到就是问不到
            return 0.0
    try:
        with open("/proc/meminfo") as handle:
            for line in handle:
                if line.startswith("MemTotal"):
                    return int(line.split()[1]) * 1024 / 2 ** 30
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def gpu_info(force: bool = False) -> dict:
    """nvidia-smi 问一次显卡（名字 + 显存 GB），60 秒内不重复问。

    问不到回空：AMD 卡没有 nvidia-smi，Neo 不去猜。显存只影响页面上那句提示，
    按内存推荐这件事不需要它；setup.py 自己有完整的显卡探测，安装时它会自己说。
    """
    now = time.time()
    if not force and now - _GPU["at"] < GPU_CACHE_S:
        return dict(_GPU)
    name, vram = "", 0.0
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, timeout=6, text=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in (out.stdout or "").splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) >= 2:
                try:
                    value = float(parts[1])
                except ValueError:
                    continue
                vram = max(vram, value / 1024.0)
                name = name or parts[0]
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    _GPU.update({"at": now, "name": name, "vram_gb": vram})
    return dict(_GPU)


def data_dir(root) -> Path:
    """模型文件落地的地方：setup.py 默认写在根目录旁边的 Strata-data。"""
    root = Path(root)
    cand = root.parent / "Strata-data"
    return cand if cand.is_dir() else root


def disk_free_gb(folder) -> float:
    try:
        return shutil.disk_usage(folder).free / 2 ** 30
    except OSError:
        return 0.0


def machine_info(root, ram=None, gpu=None) -> dict:
    """给页面看的这台机器：内存、显卡、装模型的那个盘还剩多少。"""
    root = Path(root)
    ram = total_ram_gb() if ram is None else float(ram)
    gpu = gpu_info() if gpu is None else dict(gpu)
    folder = data_dir(root)
    free = disk_free_gb(folder)
    vram = float(gpu.get("vram_gb") or 0.0)
    return {
        "ram_gb": round(ram, 1),
        "gpu_name": str(gpu.get("name") or ""),
        "gpu_vram_gb": round(vram, 1),
        "disk_free_gb": round(free, 1),
        "disk_dir": str(folder),
        "note": (f"内存 {ram:.0f} GB"
                 + (f" · 显卡 {gpu.get('name')}（{vram:.0f} GB 显存）" if vram
                    else " · 显卡读不到（AMD 卡正常）")
                 + f" · {folder} 还剩 {free:.0f} GB"),
    }


# ---------------- 按文档推荐 ----------------
# 每一档：上限 GB、推荐的 (family, model)、为什么、"也可以"的 (family, model, 说明) 列表。
# 分档与措辞来自 docs/AI_SETUP.md 第 3 步与 docs/MODELS.md 的 "Pick by RAM"。
RAM_TIERS = (
    (40.0, ("qwen", "IQ2_XS"),
     "32 GB 内存：IQ2_XS 走低内存模式（显存 24 GB 以上最稳，专家从 SSD 读的那部分会慢一些）",
     (("qwen", "Q2_0", "最快（5070 上 94 tok/s 对 79 tok/s），质量与 IQ2_XS 接近"),
      ("coder", "IQ1_M", "只写代码、且主要用英文时可以装（32 GB 整包装得下的就是它）；"
                         "中文回答会出错或绕圈，中文用户不要选（docs/MODELS.md #438）"))),
    (60.0, ("qwen", "IQ2_XS"),
     "48 GB 内存：IQ2_XS（要最快就 Q2_0）；更大的尺寸装不下",
     (("qwen", "Q2_0", "最快（5070 上 94 tok/s 对 79 tok/s），质量与 IQ2_XS 接近"),)),
    (90.0, ("qwen", "IQ2_XS"),
     "64 GB 内存：文档推荐 IQ2_XS；IQ3_XXS / IQ3_S 更好但更慢",
     (("qwen", "IQ3_XXS", "更好一点，慢一些（62 tok/s 对 79 tok/s）"),)),
    (1e9, ("qwen", "IQ3_S"),
     "96 GB 以上：IQ3_S 质量最好（与完整模型持平），速度最慢",
     (("unsloth", "UD-IQ4_XS", "约 4-bit，94 GB 下载；内存不到 ~80 GB 时一部分专家从 SSD 读"),)),
)
CJK_NOTE = "这台机器的用户主要用中文时不要选 Coder：它的中文与其它 CJK 回答会出错或绕圈" \
           "（docs/MODELS.md #438）"
UNKNOWN_RAM_NOTE = "读不到这台机器的内存，按 64 GB 那档推荐（IQ2_XS 是最稳的默认）"


def recommend(ram_gb: float) -> dict:
    """这台内存该装哪一个。分档与措辞来自 docs/AI_SETUP.md 第 3 步与 docs/MODELS.md。"""
    if ram_gb <= 0:
        return {"family": "qwen", "model": "IQ2_XS", "why": UNKNOWN_RAM_NOTE,
                "also": [], "ram_gb": 0.0, "note": CJK_NOTE}
    for ceiling, (family, model), why, also in RAM_TIERS:
        if ram_gb < ceiling:
            return _tier(family, model, why, also, ram_gb)
    return _tier(*(RAM_TIERS[-1][1]), RAM_TIERS[-1][2], RAM_TIERS[-1][3], ram_gb)


def _tier(family, model, why, also, ram_gb) -> dict:
    return {"family": family, "model": model, "why": why,
            "also": [{"family": f, "model": m, "note": n} for f, m, n in also],
            "ram_gb": round(ram_gb, 1), "note": CJK_NOTE}


def resident_budget_gib(arena_gb: float, ram_gb: float) -> int:
    """Unsloth 那两个尺寸：留在内存里的专家有多少 GiB（setup.py 的 resident_budget_gib，kv=0）。"""
    return max(8, min(int(arena_gb / 1.073741824), round(ram_gb) - UNSLOTH_RAM_LEFT_GB))


def fit_of(entry: dict, ram_gb: float) -> tuple[str, str]:
    """装得下吗。判法与 setup.py --check 一致：够 = fits，差 8 GB 以内 = tight，其余 = no。"""
    need = float(entry.get("ram_gb") or 0.0)
    if entry.get("budget"):
        if ram_gb >= need:
            arena = resident_budget_gib(float(entry.get("arena_gb") or 0.0), ram_gb)
            return "fits", (f"装得下：约 {arena} GiB 专家留在内存里，其余从 SSD 读"
                            f"（需要 {need:.0f} GB 内存）")
        return "no", f"装不下：需要 {need:.0f} GB 内存，这台机器 {ram_gb:.0f} GB"
    if ram_gb >= need:
        return "fits", f"装得下（需要约 {need:.0f} GB 内存）"
    if ram_gb >= need - 8:
        return "tight", f"有点紧：需要约 {need:.0f} GB 内存，这台机器 {ram_gb:.0f} GB"
    return "no", f"装不下：需要约 {need:.0f} GB 内存，这台机器 {ram_gb:.0f} GB"


# ---------------- 下载源（模型页那一排切换） ----------------
# 四个可选项，全部走上游本来就有的口子，不改 setup.py 一行：
#   probe        Neo 的默认：按 neo/netprobe.py 量出来的建议（两个都通 = 不加 --source）
#   huggingface  上游默认：钉住的 revision，SHA-256 对上游公布值
#   modelscope   setup.py 的 --source modelscope：仓库当前文件，按 ModelScope 自己公布的 SHA-256
#   mirror       hf-mirror.com 那面镜子：--source 里没有镜子，要的是环境变量 HF_ENDPOINT
SOURCE_CHOICES = (
    {"key": "probe", "title": "自动（按探测建议）", "what": "",
     "note": "两个源都通就走上游默认的 Hugging Face；只有 ModelScope 通就走 ModelScope"},
    {"key": "huggingface", "title": "Hugging Face", "what": "huggingface",
     "note": "上游默认：钉住的 revision，SHA-256 对上游公布值"},
    {"key": "modelscope", "title": "ModelScope", "what": "modelscope",
     "note": "国内常通：给 setup.py 传 --source modelscope，文件按 ModelScope 公布的 SHA-256 校验"},
    {"key": "mirror", "title": "hf-mirror 镜子", "what": "mirror",
     "note": "Hugging Face 的镜子：给 setup.py 设 HF_ENDPOINT=https://hf-mirror.com（--source 里没有镜子）"},
)
SOURCE_KEYS = tuple(c["key"] for c in SOURCE_CHOICES)
DEFAULT_SOURCE = "probe"


def source_choice() -> str:
    """这台机器上记着的那个下载源。没选过、或选得不认识，都回"按探测建议"。"""
    got = str(state.source_choice() or "")
    return got if got in SOURCE_KEYS else DEFAULT_SOURCE


def save_source_choice(source: str) -> str:
    """记下选中的源（认不认得由调用方挡，这里只认表里有的）。"""
    state.save_source_choice(source)
    return source_choice()


def source_info(net=None, chosen=None) -> dict:
    """给模型页的"下载源"那一排：每个够得着吗、多少毫秒、现在选的是哪一个、选它等于什么。

    net 传进来就用（页面轮询时由 console_server 给探测缓存，读接口不联网）；
    chosen 传进来就用（测试里给个假的偏好，不碰 neo/state/）。
    """
    net = dict(net) if net else netprobe.probe_cached()
    rows = list(net.get("targets") or [])
    chosen = source_choice() if chosen is None else str(chosen)
    if chosen not in SOURCE_KEYS:
        chosen = DEFAULT_SOURCE
    choices = []
    for item in SOURCE_CHOICES:
        row = next((r for r in rows if r.get("what") == item["what"]), None)
        args, env = netprobe.choice_args(item["key"], rows=rows)
        choices.append({
            **item,
            "reachable": bool(row and row.get("ok")),
            "ms": float(row.get("ms") or 0.0) if row else 0.0,
            "args": args,
            "env": env,
            "prefix": "".join(f"{k}={v} " for k, v in env.items()),
            "suffix": (" " + " ".join(args)) if args else "",
        })
    picked = next(c for c in choices if c["key"] == chosen)
    suggested = str(net.get("source") or "huggingface")
    effect = (f"给 setup.py 传 {' '.join(picked['args'])}" if picked["args"]
              else (f"给 setup.py 设 {list(picked['env'].keys())[0]}" if picked["env"]
                    else "不加 --source，走上游默认"))
    return {
        "choices": choices,
        "chosen": chosen,
        "chosen_title": picked["title"],
        "chosen_note": picked["note"],
        "prefix": picked["prefix"],
        "suffix": picked["suffix"],
        "effect": effect,
        "suggested": suggested,
        "reason": str(net.get("reason") or ""),
        "targets": rows,
        "at": float(net.get("at") or 0.0),
        "note": f"{net.get('reason') or '还没量'}；现在选的：{picked['title']}（{effect}）",
    }


# ---------------- 清单 ----------------
def config_name(family_tag: str, model: str) -> str:
    """装好之后根目录那个配置文件的名字（setup.py：tag = fam["tag"] + model，全小写）。"""
    return f"strata-{(str(family_tag or '') + model).lower()}.json"


def _discover_installed(root) -> list[dict]:
    try:
        return make_adapter("strata", root).discover()
    except OSError:
        return []


def catalog(root, machine: dict | None = None, installed: list[dict] | None = None,
            sources: dict | None = None) -> dict:
    """可安装清单 + 这台机器 + 推荐 + 下载源。模型页的"下载安装模型"读的就是这一份。"""
    root = Path(root)
    models_tbl, families = read_tables(root)
    machine = dict(machine) if machine else machine_info(root)
    rec = recommend(float(machine.get("ram_gb") or 0.0))
    configs = installed if installed is not None else _discover_installed(root)
    by_file = {Path(str(cfg.get("id") or "")).name.lower(): cfg for cfg in configs}
    by_name = {str(cfg.get("name") or "").lower(): cfg for cfg in configs}
    ram = float(machine.get("ram_gb") or 0.0)
    entries = []
    for fam_key, fam in families.items():
        tag = str(fam.get("tag") or "")
        for model in sizes_of_family(models_tbl, fam_key):
            d = models_tbl[model]
            cfg_name = config_name(tag, model)
            cfg = by_file.get(cfg_name) or by_name.get(f"{str(fam.get('name') or '')}-{model.lower()}")
            entry = {
                "family": fam_key,
                "family_title": str(fam.get("title") or fam_key),
                "model": model,
                "about": str(d.get("about") or ""),
                "download_gb": float(d.get("download_gb") or 0.0),
                "ram_gb": float(d.get("ram_gb") or 0.0),
                "arena_gb": float(d.get("arena_gb") or 0.0),
                "budget": bool(d.get("budget")),
                "experimental": bool(d.get("experimental")),
                "nvidia_only": bool(d.get("nvidia_only")),
                "config": cfg_name,
                "installed": bool(cfg and cfg.get("installed")),
                "broken": bool(cfg and not cfg.get("installed")),
                "installed_file": str((cfg or {}).get("file") or ""),
                "args": f"--family {fam_key} --model {model}",
            }
            fit, fit_text = fit_of(entry, ram)
            entry["fit"] = fit
            entry["fit_text"] = fit_text
            entry["recommended"] = (fam_key, model) == (rec["family"], rec["model"])
            entry["also"] = any((fam_key, model) == (x["family"], x["model"]) for x in rec["also"])
            entry["installable"] = fit != "no"
            entries.append(entry)
    order = {"fits": 0, "tight": 1, "no": 2}
    entries.sort(key=lambda e: (not e["recommended"], order.get(e["fit"], 3),
                                bool(e["experimental"]), e["download_gb"]))
    return {
        "root": str(root),
        "has_setup": setup_path(root).is_file(),
        "machine": machine,
        "recommendation": rec,
        "models": entries,
        "installed_count": sum(1 for e in entries if e["installed"]),
        "sources": source_info() if sources is None else sources,
        "source": "setup.py 的 MODELS / FAMILIES 两张表（ast 读字面量，不跑 setup.py）",
    }


def known(root, family: str, model: str) -> bool:
    """这一项在不在安装菜单里（POST /neo/install 用它挡掉乱传的参数）。"""
    models_tbl, families = read_tables(root)
    if family not in families or not model:
        return False
    return model in sizes_of_family(models_tbl, family)


def main(argv=None) -> int:
    """命令行看一眼可安装清单：python -m neo.models [--root 目录] [--json]"""
    import argparse
    import json as _json

    from .paths import safe_resolve

    p = argparse.ArgumentParser(description="这台机器能装哪些模型、该装哪一个")
    p.add_argument("--root", default=str(safe_resolve(Path(__file__).parent).parent))
    p.add_argument("--json", action="store_true")
    p.add_argument("--source", default="", choices=list(SOURCE_KEYS),
                   help="记下装模型要用的下载源（不给就只读，不改）")
    args = p.parse_args(argv)
    if args.source:
        save_source_choice(args.source)
    data = catalog(safe_resolve(args.root))
    if args.json:
        print(_json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(f"== {data['root']}")
    print(f"  {data['machine']['note']}")
    rec = data["recommendation"]
    print(f"== 推荐：--family {rec['family']} --model {rec['model']}（{rec['why']}）")
    print(f"   {rec['note']}")
    for item in rec["also"]:
        print(f"   也可以：--family {item['family']} --model {item['model']}（{item['note']}）")
    print("== 可安装清单")
    for e in data["models"]:
        mark = "推荐" if e["recommended"] else ("也可" if e["also"] else "    ")
        state_txt = "已装" if e["installed"] else ("装好一半" if e["broken"] else "未装")
        print(f"  [{mark}] {e['family_title']:24s} {e['model']:10s} {state_txt:6s} "
              f"{e['download_gb']:5.1f} GB  {e['fit_text']}")
    src = data["sources"]
    print(f"== 下载源：现在选的 {src['chosen_title']}（{src['effect']}），建议 {src['suggested']}")
    print(f"   {src['reason']}")
    for c in src["choices"]:
        print(f"   {c['title']:18s} {'连得上' if c['reachable'] else '连不上':6s} "
              f"{c['ms']:7.0f} ms  {c['note']}")
    print("   换源：python -m neo.models --source modelscope（写进 neo/state/source.json）")
    if not data["has_setup"]:
        print(f"  {data['root']} 里没有 setup.py —— 这不是 Strata 的根目录")
    return 0


if __name__ == "__main__":
    sys.exit(main())

