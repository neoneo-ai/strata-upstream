"""neo/engines/base.py - 引擎适配器的共同约定。

一个适配器要知道三件事：
    discover()  这台机器上有哪些可启动的东西（模型配置）
    plan()      启动命令长什么样（argv / cwd / env / 日志文件）
    probe()     现在活着吗、模型好了吗、跑得多快

Neo 只用命令行参数、环境变量和 HTTP 端点与上游打交道，不依赖上游的内部函数，
所以上游改了内部实现不会影响 Neo。
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

USER_AGENT = "NeoStrata/0.1"


@dataclass
class LaunchPlan:
    """一次启动的完整计划。监督只认这个结构，不关心是哪个引擎。"""
    argv: list[str]
    cwd: str
    env: dict | None = None
    log_path: str = ""
    port: int = 0
    host: str = "127.0.0.1"
    model: str = ""
    engine: str = ""
    label: str = ""
    ready_timeout_s: int = 900          # 模型加载要多久：大模型在 8GB 显存上要几分钟
    extra: dict = field(default_factory=dict)


def tcp_open(host: str, port: int, timeout: float = 1.2) -> bool:
    """端口上有没有服务在听（不发送任何请求）。"""
    host = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_json(port: int, path: str, timeout: float = 4.0, api_key: str = ""):
    """GET 一个 JSON 端点。拿不到回 None（服务没起来、需要 key、或返回的不是 JSON）。"""
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


#: /metrics 的四节，实测 2026-10-12 在本机（RTX 5090 D v2，serve/server.py --engine mock，端口 28181）：
#:     live             当前请求：state / queued / phase / prompt_tokens / generated / tok_s / tok_s_mean
#:                      （--batch 时另有 running / slots / waiting）
#:     totals           累计：requests / prompt_tokens / reused / output_tokens / prompt_ms / decode_ms
#:     hardware         每秒一采：gpu_util、gpu_mem_used、gpu_mem_total（字节）、gpu_temp、gpu_power、
#:                      cpu（百分比）、ram_used、ram_total（字节）、disk_read_mb、disk_write_mb
#:     hardware_static  gpu_name / gpu_count / gpu_note / cpu_name / cores / threads
#: 顶层的 requests 是最近 12 条请求的**记录列表**（每项一个对象），不是数字。
#: Neo 早先按一套扁平键（tokens_per_s / decode_step_ms / gpu_used_mib / ram_used_gb …共 41 个）
#: 在顶层挑，那些名字上游一个都没发过，只有 requests 在、且它是列表：状态页六张卡片全是"—"，
#: "请求"那一栏把列表直接写进文字，成了"请求 [object Object],[object Object]"
#: （用户 2026-10-12 的窗口截图，本机复现见 NEO_DESKTOP.md）。下面按真实的形状取值。
METRIC_SECTIONS = ("live", "totals", "hardware", "hardware_static")
MIB = 2 ** 20
GB = 2 ** 30


def metric_num(v):
    """只认 int/float，别的一律 None（None、True/False、文字、列表、对象、NaN）。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return None if f != f else f


def flatten_metrics(metrics) -> dict:
    """把 /metrics 的四节压成状态页卡片要的那几个扁平键。上游没有的键就不出现，页面显示"—"。"""

    def section(name):
        got = metrics.get(name) if isinstance(metrics, dict) else None
        return got if isinstance(got, dict) else {}

    metrics = metrics if isinstance(metrics, dict) else {}
    live, totals, hw, static = (section("live"), section("totals"),
                                section("hardware"), section("hardware_static"))
    out: dict = {}

    def put(key, value, digits=None, integer=False):
        n = metric_num(value)
        if n is None:
            return
        out[key] = int(round(n)) if integer else (round(n, digits) if digits is not None else n)

    def put_str(key, value):
        if isinstance(value, str) and value:
            out[key] = value

    # 在跑的请求：--batch 时 live.running 是正在跑的请求数；单路时看 live.state 在不在跑
    running = metric_num(live.get("running"))
    out["active"] = int(running) if running is not None else (
        1 if str(live.get("state") or "") in ("reading", "generating") else 0)
    put("queued", live.get("queued"), integer=True)
    put("requests", totals.get("requests"), integer=True)      # 累计条数，不是那 12 条记录
    put("requests_kept", metrics.get("requests_kept"), integer=True)
    put("tokens_per_s", live.get("tok_s"), digits=1)
    put("tokens_per_s_mean", live.get("tok_s_mean"), digits=1)
    put("prefill_tok_s_mean", live.get("prefill_tok_s_mean"), digits=1)
    # 解码步长：整段解码时间除以生成的 token 数（totals 里的 decode_ms 单位是毫秒）
    decode_ms, gen = metric_num(totals.get("decode_ms")), metric_num(totals.get("output_tokens"))
    if decode_ms is not None and gen is not None and gen > 0:
        out["decode_step_ms"] = round(decode_ms / gen, 2)
    gpu_used, gpu_total = metric_num(hw.get("gpu_mem_used")), metric_num(hw.get("gpu_mem_total"))
    put("gpu_used_mib", None if gpu_used is None else gpu_used / MIB, integer=True)
    put("gpu_total_mib", None if gpu_total is None else gpu_total / MIB, integer=True)
    if gpu_used is not None and gpu_total is not None:
        out["gpu_free_mib"] = int(round((gpu_total - gpu_used) / MIB))
    put("gpu_util", hw.get("gpu_util"), digits=1)
    put("gpu_temp", hw.get("gpu_temp"), digits=1)
    put("gpu_power", hw.get("gpu_power"), digits=1)
    put("cpu_pct", hw.get("cpu"), digits=1)
    put("disk_read_mb", hw.get("disk_read_mb"), digits=1)
    put("disk_write_mb", hw.get("disk_write_mb"), digits=1)
    put("ram_used_gb", None if metric_num(hw.get("ram_used")) is None
        else metric_num(hw.get("ram_used")) / GB, digits=1)
    put("ram_total_gb", None if metric_num(hw.get("ram_total")) is None
        else metric_num(hw.get("ram_total")) / GB, digits=1)
    put_str("gpu_name", static.get("gpu_name"))
    put_str("gpu_note", static.get("gpu_note"))
    put("gpu_count", static.get("gpu_count"), integer=True)
    # 当前请求的现场
    put_str("phase", live.get("phase"))
    put_str("live_state", live.get("state"))
    put("prompt_tokens", live.get("prompt_tokens"), integer=True)
    put("generated", live.get("generated"), integer=True)
    put("elapsed_s", live.get("elapsed_s"), digits=1)
    return out


class EngineAdapter:
    """所有适配器的基类。子类至少要实现 discover() 和 plan()。"""

    name = "base"
    title = "Base"
    #: 这个引擎的启动脚本相对根目录的路径（Strata 是 serve/server.py）
    entry = ""

    def __init__(self, root, cfg: dict | None = None):
        self.root = Path(root)
        self.cfg = dict(cfg or {})

    # ---- 发现 ----
    def discover(self) -> list[dict]:
        """这台机器上可启动的模型，每项 {id, name, path, mtime, context, vision, port}。"""
        return []

    def default_config(self):
        """要启动的那一个配置；没有任何配置时返回 None（Neo 进"未安装"引导态）。

        点名要的那个优先（哪怕它起不来 —— 报错要说的是用户点名的那个）；
        没点名就只挑能启动的，全都不行才回第一个，让报错说清缺了什么。
        """
        items = self.discover()
        if not items:
            return None
        chosen = next((x for x in items if x.get("id") == self.cfg.get("model")), None)
        if chosen:
            return chosen
        return next((x for x in items if x.get("installed")), items[0])

    # ---- 启动 ----
    def plan(self, port: int, host: str = "127.0.0.1", api_key: str = "",
            model_id: str = "", gpu=None) -> LaunchPlan:
        raise NotImplementedError

    # ---- 探活 ----
    def probe(self, port: int, api_key: str = "") -> dict:
        """三态探活：端口在听 → /health 应答 → 模型是否已加载。"""
        out = {"engine": self.name, "port": port, "listening": False, "healthy": False,
               "loaded": False, "model": "", "max_context": 0, "metrics": {}}
        if not tcp_open("127.0.0.1", port):
            return out
        out["listening"] = True
        health = http_json(port, "/health", api_key=api_key)
        if isinstance(health, dict) and health.get("status") == "ok":
            out.update(healthy=True, loaded=bool(health.get("loaded")),
                       model=str(health.get("model") or ""),
                       max_context=int(health.get("max_context") or 0))
        metrics = http_json(port, "/metrics", api_key=api_key)
        if isinstance(metrics, dict):
            out["metrics"] = flatten_metrics(metrics)
        return out

    # ---- 端口与日志（状态页与日志页要用，不依赖启动计划成不成功）----
    def listening(self, port: int) -> bool:
        """端口上有没有服务在听。接手的服务只问这个：/health 一时不答（正在生成）
        不等于服务没了，判成"不见了"会把一个健康的服务重启掉。"""
        return tcp_open("127.0.0.1", port)

    def log_path_for(self, model_id: str = "") -> str:
        """这个模型的日志文件在哪。

        脚本（run-*.bat）先起来的服务，日志写在配置里的 log 键那儿，Neo 接手它之后
        日志页要能显示同一份 —— 否则用户看到的就是"已经启动了，日志页是空的"。
        """
        return ""

    # ---- 页面 ----
    def pages(self, port: int, host: str = "127.0.0.1") -> dict:
        """Strata 自己的两个页面：聊天页和监控页。

        实测 2026-10-12 在跑着的服务上（8080）：/monitor.html 回 404 —— 上游只在
        "/api-monitor" 且配置开了 api_monitor 时才发 monitor.html。监控页是聊天页里
        的一个标签，地址是 http://127.0.0.1:8080/#monitor（serve/web/app.js 的
        showTab 认这个 hash）。Neo 的监控页内嵌的就是这个地址，写 /monitor.html
        时内嵌框是空的（用户看到的"监控是空白页"）。
        """
        here = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
        return {"chat": f"http://{here}:{port}/", "monitor": f"http://{here}:{port}/#monitor"}

    # ---- 能力 ----
    def capabilities(self) -> dict:
        return {"chat": True, "monitor": True, "models": True, "logs": True}

