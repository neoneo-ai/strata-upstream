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


#: 状态页与监控页要展示的 /metrics 字段（名字与 serve/server.py 的 /metrics 一致）
METRIC_KEYS = ("active", "queued", "requests", "requests_per_s", "prompt_tokens", "generated_tokens",
               "tokens_per_s", "tokens_per_s_mean", "decode_step_ms", "decode_step_ms_mean",
               "decode_step_ms_max", "decode_step_ms_p95", "decode_tokens", "decode_window",
               "decode_window_tokens", "decode_window_tokens_per_s", "decode_window_ms",
               "decode_window_preferred_ms", "decode_window_target_ms", "decode_window_mode",
               "decode_window_adaptive", "decode_window_adaptive_reason", "decode_window_adaptive_note",
               "decode_window_adaptive_applied", "decode_window_adaptive_applied_ms",
               "decode_window_adaptive_applied_note", "decode_window_adaptive_candidate_ms",
               "decode_window_adaptive_candidate_note", "decode_window_adaptive_candidate_reason",
               "decode_window_adaptive_candidate_applied", "decode_window_adaptive_candidate_applied_ms",
               "decode_window_adaptive_candidate_applied_note", "decode_window_adaptive_candidate",
               "decode_window_adaptive_candidate_target_ms", "decode_window_adaptive_candidate_target",
               "decode_window_adaptive_candidate_target_applied",
               "decode_window_adaptive_candidate_target_applied_ms", "gpu_index", "gpu_name",
               "gpu_total_mib", "gpu_used_mib", "gpu_free_mib", "cpu_pct", "ram_used_gb", "ram_total_gb")


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
        """要启动的那一个配置；没有任何配置时返回 None（Neo 进"未安装"引导态）。"""
        items = self.discover()
        if not items:
            return None
        chosen = next((x for x in items if x.get("id") == self.cfg.get("model")), None)
        return chosen or items[0]

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
            out["metrics"] = {k: metrics.get(k) for k in METRIC_KEYS if k in metrics}
        return out

    # ---- 页面 ----
    def pages(self, port: int, host: str = "127.0.0.1") -> dict:
        here = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
        return {"chat": f"http://{here}:{port}/", "monitor": f"http://{here}:{port}/monitor.html"}

    # ---- 能力 ----
    def capabilities(self) -> dict:
        return {"chat": True, "monitor": True, "models": True, "logs": True}

