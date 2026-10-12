"""neo/connect.py - 一条命令把一个编码 agent 接到这台机器上的 Strata。

    python -m neo.connect list                     看有哪几个 agent
    python -m neo.connect claude                   打印 Claude Code 的接入命令
    python -m neo.connect codex --persist          写进 ~/.codex/config.toml（先备份）
    python -m neo.connect claude --lan             按局域网地址给

Neo 不改上游，也不启动 agent —— 它把该设的变量、该写的配置、该跑的命令
算清楚，打印成能直接粘进终端的一块（形状照参考实现的 _print_env：
PowerShell 用 Remove-Item + $env:，POSIX 用 unset + export + 一行自包含命令）。

key 的优先级：--key > 环境变量 NEO_API_KEY > neo/state/launcher.json 里那把 >
neo/state/agent_keys.json 里这个 base 记过的 > 现生成一把。
现生成的那把要交给服务才算数：Neo 在跑就用 POST /neo/token（下一次启动生效），
没在跑就把它写进 launcher.json，下次启动带上（--api-key 同一个键）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import connectors, netprobe, state, tokens
from .engines import make_adapter
from .paths import safe_resolve


def resolve_root(value: str = "") -> Path:
    if value:
        return safe_resolve(value)
    from .manager import default_root
    return safe_resolve(default_root())


def resolve_port(args, adapter) -> int:
    if args.port:
        return int(args.port)
    cfg = state.launcher_config()
    try:
        if cfg.get("port"):
            return int(cfg["port"])
    except (TypeError, ValueError):
        pass
    items = adapter.discover()
    if items and items[0].get("port"):
        return int(items[0]["port"])
    return 8080


def resolve_host(args) -> str:
    if args.host:
        return str(args.host)
    if getattr(args, "lan", False):
        return "0.0.0.0"
    cfg = state.launcher_config()
    return str(cfg.get("host") or "127.0.0.1")


def resolve_base(args, port: int, host: str) -> str:
    """agent 要连的那个地址。--base 最大；--lan 用局域网地址；否则本机。"""
    if args.base:
        return str(args.base).rstrip("/")
    if args.lan or host in ("0.0.0.0", "", "::"):
        ips = tokens.lan_bases(port)
        return ips[0] if ips else tokens.loopback_base(port)
    return f"http://{host}:{port}"


def resolve_key(args, base: str) -> tuple[str, str]:
    """(key, 这把 key 是哪儿来的)。"""
    if args.key:
        return str(args.key), "--key"
    env_key = str(os.environ.get(tokens.ENV_KEY) or "")
    if env_key:
        return env_key, f"环境变量 {tokens.ENV_KEY}"
    cfg = state.launcher_config()
    cfg_key = str(cfg.get("api_key") or "")
    if cfg_key:
        return cfg_key, "neo/state/launcher.json"
    saved = tokens.cached_keys(base, "saved") + tokens.cached_keys(base, "minted")
    if saved:
        return saved[0], f"neo/state/agent_keys.json（{base}）"
    key = tokens.new_key()
    tokens.remember_key(base, key, "minted")
    return key, "现生成的"


def resolve_model(args, adapter) -> connectors.Model:
    items = adapter.discover()
    wanted = str(args.model or "")
    for item in items:
        if wanted and wanted in (item["id"], item["name"]):
            return connectors.Model.from_config(item)
    if wanted:
        return connectors.Model(wanted, 0)
    cfg = adapter.default_config() if items else None
    cfg = cfg or (items[0] if items else None)
    if cfg is None:
        return connectors.Model("qwen3.8-flash-next", 0)
    return connectors.Model.from_config(cfg)


def notes_for(key_source: str, api_key_in_server: bool) -> list[str]:
    """key 是哪儿来的、服务认不认，两句话说清。"""
    notes = [f"key 来自：{key_source}"]
    if not api_key_in_server:
        notes.append("服务现在没设 key：这把 key 要先交给服务（Neo 页面里点\"生成 token\"，"
                     "或启动时带 --api-key），否则 agent 不带 key 也能连上")
    return notes


def build(agent: str, args) -> dict:
    root = resolve_root(args.root)
    adapter = make_adapter(args.engine, root)
    port = resolve_port(args, adapter)
    host = resolve_host(args)
    base = resolve_base(args, port, host)
    key, key_source = resolve_key(args, base)
    model = resolve_model(args, adapter)
    item = connectors.recipe(agent, base, key, model)
    cfg = state.launcher_config()
    item["notes"] = notes_for(key_source, bool(cfg.get("api_key"))) + item["notes"]
    item["lan"] = tokens.token_status(port, host, str(cfg.get("api_key") or ""))
    if args.persist:
        item["write"] = connectors.write_config(agent, base, key, model)
        item["notes"] += item["write"].get("notes", [])
    return item


def out(text: str = "") -> None:
    """CLI 的一行输出。用 print 是为了测试能 redirect_stdout 抓到；闸门那边
    一次 print 的一整块（BURST_LINES 行以内）不被裁，JSON 不会变半截。"""
    print(text)


def print_item(item: dict, shell: str = "auto") -> None:
    lines = item["powershell"] if shell == "powershell" else \
        (item["posix"] if shell == "posix"
         else (item["powershell"] if os.name == "nt" else item["posix"]))
    out(f"# {item['title']} → {item['base']}  模型 {item['model']}"
        + (f"（{item['context']} 上下文）" if item["context"] else ""))
    for note in item["notes"]:
        out(f"# {note}")
    out(f"# 配置文件：{item['config_path']}")
    out()
    for line in lines:
        out(line)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="把一个编码 agent 接到这台机器上的 Strata")
    p.add_argument("agent", nargs="?", default="list",
                   help="claude / codex / opencode / openclaw，或 list")
    p.add_argument("--root", default="", help="Strata 的根目录")
    p.add_argument("--engine", default="strata", help="引擎适配器（mock 用于测试）")
    p.add_argument("--port", type=int, default=0, help="服务端口（默认取记住的）")
    p.add_argument("--host", default="", help="服务绑定地址")
    p.add_argument("--lan", action="store_true", help="按局域网地址给（0.0.0.0 那条路）")
    p.add_argument("--base", default="", help="直接指定 agent 要连的地址")
    p.add_argument("--key", default="", help="用这把 key（不生成）")
    p.add_argument("--model", default="", help="模型 id（默认取最近用过的那个配置）")
    p.add_argument("--persist", action="store_true",
                   help="把 provider 写进 agent 的配置文件（先备份成 .neo-bak）")
    p.add_argument("--shell", default="auto", choices=["auto", "powershell", "posix"])
    p.add_argument("--json", action="store_true", help="出 JSON（给页面与脚本用）")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    agent = str(args.agent or "list").strip().lower()
    if agent == "list":
        for name in connectors.AGENTS:
            out(f"{name:<10} {connectors.TITLES[name]:<12} {connectors.config_path(name)}")
        out(f"netprobe 建议的源：{netprobe.probe_cached()['source']}")
        return 0
    if agent not in connectors.AGENTS:
        out(f"不认识的 agent：{agent}（可选 {' / '.join(connectors.AGENTS)}）")
        return 2
    item = build(agent, args)
    if args.json:
        import json
        out(json.dumps(item, ensure_ascii=False, indent=1))
    else:
        print_item(item, args.shell)
    return 0


if __name__ == "__main__":
    sys.exit(main())

