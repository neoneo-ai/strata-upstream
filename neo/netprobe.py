"""neo/netprobe.py - 装模型之前先量一下：这台机器够得着哪个下载源。

只测一件事：能不能连上对方的 443，连上要多少毫秒。不发 HTTP 请求、不下载任何东西，
所以不需要 GPU，也没有网的时候照样能跑（connect 函数可以换掉，测试就是这么跑的）。

为什么要这个：setup.py 认 --source modelscope（setup.py 的 model_source()，
STRATA_SOURCE 是同一个口子），而上游明确不自己换源 —— setup.py 里那句注释写着
"Setup does not switch hosts by itself (#908): ModelScope serves a repository's
current files, not our pinned revision"。所以"这台机器够得着 ModelScope、够不着
Hugging Face"这件事由 Neo 判断，判断完把 --source modelscope 交给 setup.py，
走上游本来就有的口子，不改上游一行。

判断只看"够得着不够得着"，不猜地理位置，也不按 IP 段猜：

    huggingface.co 连不上、modelscope.cn 连得上   ->  modelscope
    两个都连得上                                    ->  huggingface（上游的默认）
    两个都连不上                                    ->  huggingface + 说清没网

两个都连得上时不切源：ModelScope 给的是仓库当前文件，不是钉住的 revision，
上游的默认就是 Hugging Face，Neo 没有测过"切过去更快"就不切。
"""
from __future__ import annotations

import socket
import threading
import time

#: 要量的三个地址。hf-mirror.com 是 setup.py 里 HF_ENDPOINT 那面镜子（#495）：
#: 国内够得着镜子、够不着源站的人不少，镜子连得上就说明"走镜子"这条路可用。
TARGETS = (
    {"host": "huggingface.co", "port": 443, "what": "huggingface"},
    {"host": "hf-mirror.com", "port": 443, "what": "mirror"},
    {"host": "www.modelscope.cn", "port": 443, "what": "modelscope"},
)
TIMEOUT_S = 3.0
CACHE_S = 60.0                                  # 页面每 2 秒轮一次，别每次都去连


def tcp_connect(host: str, port: int = 443, timeout: float = TIMEOUT_S) -> tuple[bool, float]:
    """能不能连上 host:port，连上要多少毫秒。连不上回 (False, 用时)。"""
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, (time.perf_counter() - started) * 1000.0
    except OSError:
        return False, (time.perf_counter() - started) * 1000.0


def probe(targets=TARGETS, timeout: float = TIMEOUT_S, connect=tcp_connect) -> dict:
    """量一遍 targets。connect 可以换掉（测试里给个假的，不碰网络）。"""
    rows = []
    for item in targets:
        ok, ms = connect(item["host"], item.get("port", 443), timeout)
        rows.append({"host": item["host"], "what": item["what"], "ok": bool(ok),
                     "ms": round(ms, 1)})
    source, reason = suggest_source(rows)
    return {"targets": rows, "ok": [r["host"] for r in rows if r["ok"]],
            "source": source, "reason": reason, "at": time.time()}


def reachable(rows, what: str) -> bool:
    return any(r["ok"] and r["what"] == what for r in rows)


def suggest_source(rows) -> tuple[str, str]:
    """(源, 给用户看的一句话)。规则见模块开头。"""
    hf = reachable(rows, "huggingface")
    ms = reachable(rows, "modelscope")
    mirror = reachable(rows, "mirror")
    if ms and not hf:
        return "modelscope", ("huggingface.co 连不上，www.modelscope.cn 连得上 —— "
                              "同一个仓库、同样的文件路径，走 ModelScope")
    if hf and ms:
        return "huggingface", ("两个源都连得上（Hugging Face %.0f ms、ModelScope %.0f ms）："
                               "按上游默认的 Hugging Face 走，不切源"
                               % (_ms_of(rows, "huggingface"), _ms_of(rows, "modelscope")))
    if hf:
        return "huggingface", "huggingface.co 连得上，按默认走"
    if mirror:
        return "huggingface", ("huggingface.co 直连不上，hf-mirror.com 连得上："
                               "给 setup.py 设 HF_ENDPOINT=https://hf-mirror.com 再装"
                               "（--source 里没有镜子这个选项）")
    return "huggingface", ("huggingface.co 和 www.modelscope.cn 都连不上 —— "
                           "先确认这台机器能上网，再跑安装")


def _ms_of(rows, what: str) -> float:
    for r in rows:
        if r["what"] == what:
            return float(r["ms"])
    return 0.0


def setup_source_args(source: str, explicit: str = "") -> list[str]:
    """要不要给 setup.py 加 --source。用户已经说了的（explicit）一个字都不改。"""
    if explicit:
        return ["--source", explicit]
    if source == "modelscope":
        return ["--source", "modelscope"]
    return []


def endpoint_args(source: str, mirror_only: bool = False) -> list[str]:
    """镜子那条路要的是环境变量，不是 --source：回 ["--env", "HF_ENDPOINT=..."] 的形状
    由调用方决定，这里只给值。"""
    if mirror_only:
        return ["HF_ENDPOINT=https://hf-mirror.com"]
    return []


#: 模型页"下载源"那一排的可选项：
#: probe = Neo 的默认（按探测建议）；huggingface / modelscope = setup.py --source 的两个值；
#: mirror = hf-mirror.com 那面镜子，setup.py 的 --source 里没有它，要的是 HF_ENDPOINT。
CHOICES = ("probe", "huggingface", "modelscope", "mirror")
MIRROR_ENDPOINT = "https://hf-mirror.com"


def choice_args(choice: str, rows=None) -> tuple[list[str], dict[str, str]]:
    """选中的源等于什么：(给 setup.py 的参数, 还要额外给它的环境变量)。

    probe / 空 = 用探测建议（两个源都通就什么都不加，走上游默认的 Hugging Face）；
    mirror = 镜子，走 HF_ENDPOINT，--source 里没有这一项；
    huggingface / modelscope / auto = 用户点名的，原样传 --source，一个字不改。
    """
    if choice in ("", "probe"):
        source = probe_cached()["source"] if rows is None else suggest_source(rows)[0]
        return setup_source_args(source), {}
    if choice == "mirror":
        return [], {"HF_ENDPOINT": MIRROR_ENDPOINT}
    return setup_source_args("", choice), {}


def describe(result: dict) -> list[str]:
    """给页面/终端看的几行话。"""
    lines = [result.get("reason") or "还没量"]
    for r in result.get("targets", []):
        lines.append("%s：%s（%.0f ms）" % (r["host"], "连得上" if r["ok"] else "连不上", r["ms"]))
    return lines


# ---------------- 给控制台页面用的缓存 ----------------
_LOCK = threading.Lock()
_CACHE: dict = {}


def probe_cached(timeout: float = TIMEOUT_S, force: bool = False, connect=None) -> dict:
    """缓存 CACHE_S 秒的探测结果：状态页每 2 秒轮一次，不能每次都去连三次 443。"""
    with _LOCK:
        if _CACHE and not force and time.time() - _CACHE.get("at", 0.0) < CACHE_S:
            return _CACHE
        _CACHE.clear()
        _CACHE.update(probe(timeout=timeout) if connect is None
                      else probe(timeout=timeout, connect=connect))
        return dict(_CACHE)


if __name__ == "__main__":                      # python -m neo.netprobe
    res = probe()
    print("建议的源：", res["source"])
    for line in describe(res):
        print(" ", line)
