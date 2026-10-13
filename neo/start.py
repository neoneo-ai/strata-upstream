"""neo/start.py - 一行把编码 agent 接到这台机器上的 Strata，并把它拉起来。

    NeoStrata.exe start claude              拉起 Claude Code（端点、token、模型、上下文带上下）
    python -m neo.start claude              源码里跑的同一行
    python -m neo.start claude --no-launch  只打印要粘进终端的那一块（远程机器、WSL）
    python -m neo.start codex --persist     先写进 ~/.codex/config.toml（先备份）再跑
    python -m neo.start claude -- --resume  -- 后面的原样交给 agent

形状照参考实现（unsloth_cli/commands/start.py，6990 行那份）：一个 agent 一行命令，
服务地址、key、模型 id、上下文长度、要清空的云端变量全算好，agent 直接带上下跑起来。
Neo 这一层只多它本来就有的两样：--persist 写配置文件，--context-length 覆盖窗口。

连远程那台 Strata 用两个环境变量（对齐参考实现的 UNSLOTH_STUDIO_URL / UNSLOTH_API_KEY）：

    NEO_SERVER_URL=https://pc2:8080 NEO_API_KEY=sk-neo-... python -m neo.start claude

key 的优先级与 neo/connect.py 相同：--key > NEO_API_KEY > launcher.json 里那把 >
这个地址记过的 > 现生成一把。现生成的那把只写进 neo/state/agent_keys.json：
Neo 不替用户把服务改成"必须带 key"，那一下由用户在「设置」页自己按。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import connect, connectors, state, tokens
from .cli import wide_stdout
from .engines import make_adapter

AGENTS = connectors.AGENTS
ENV_SERVER_URL = "NEO_SERVER_URL"


# ---------------- 那一行命令长什么样 ----------------
def exe_command(root, agent: str) -> str:
    """根目录里有 NeoStrata.exe 时，用户敲的那一行。"""
    exe = Path(str(root)) / "NeoStrata.exe"
    return f"{exe.name} start {agent}" if exe.is_file() else ""


def python_command(agent: str) -> str:
    """源码跑的那一行（没有 exe 时页面给这个）。"""
    return f"python -m neo.start {agent}"


def quick_command(root, agent: str) -> str:
    """「接入」页每一行要复制的那一句。有 exe 用 exe，没 exe 用 python。"""
    return exe_command(root, agent) or python_command(agent)


# ---------------- 算配方 ----------------
def build(agent: str, args) -> dict:
    """一个 agent 的接入配方 + 这一行命令。地址、key、模型、上下文全在这里算好。"""
    root = connect.resolve_root(args.root)
    adapter = make_adapter(args.engine, root)
    port = connect.resolve_port(args, adapter)
    host = connect.resolve_host(args)
    base = connect.resolve_base(args, port, host)
    key, key_source = connect.resolve_key(args, base)
    model = connect.resolve_model(args, adapter)
    if args.context_length:
        model = connectors.Model(model.id, int(args.context_length))
    item = connectors.recipe(agent, base, key, model)
    cfg = state.launcher_config()
    item["notes"] = connect.notes_for(key_source, bool(cfg.get("api_key"))) + item["notes"]
    item["lan"] = tokens.token_status(port, host, str(cfg.get("api_key") or ""))
    item["key"] = key                                   # 启动子进程要用；页面那份走 /neo/connect
    item["key_masked"] = tokens.mask_of(key) if key else ""
    item["quick"] = quick_command(root, agent)
    if args.persist:
        item["write"] = connectors.write_config(agent, base, key, model)
        item["notes"] += item["write"].get("notes", [])
    return item



# ---------------- 拉起来 ----------------
def launch_env(agent: str, base: str, key: str, model, env=None) -> dict:
    """agent 子进程要的那份环境：先清掉云端账号的开关，再盖上我们这几个。

    要清空的理由与参考实现一样（start.py:169 那条注释）：ANTHROPIC_API_KEY、
    CLAUDE_CODE_USE_BEDROCK 这类变量在，provider 路由会把请求带离本地服务。
    """
    out = dict(os.environ if env is None else env)
    for name in connectors.unset_list(agent):
        out.pop(name, None)
    out.update(connectors.env_for(agent, base, key, model))
    return out


def stdio_is_console() -> bool:
    """这个进程有没有真的连着终端。窗口版 exe 没有，终端里的 python -m 有。"""
    try:
        return bool(sys.stdout and sys.stdout.isatty() and sys.stdin and sys.stdin.isatty())
    except (ValueError, OSError):
        return False


def launch(item: dict, passthrough=(), new_window=None) -> int:
    """把 agent 带上下拉起来，等它跑完，回它的退出码。

    new_window=None 时自己挑：进程没有终端（双击窗口版 exe 起来的那种）就开一个新
    控制台跑，agent 的界面才看得见；在终端里跑就接上当前终端，交互照常。
    """
    agent = str(item["agent"])
    base, key = str(item["base"]), str(item.get("key") or "")
    model = connectors.Model(str(item["model"]), int(item.get("context") or 0))
    command = list(connectors.command_for(agent, base, key, model))
    exe = shutil.which(command[0])
    if not exe:
        connect.out(f"没找到 {command[0]} 这个命令：装好它再来，或者加 --no-launch 把命令打印出来。")
        return 2
    env = launch_env(agent, base, key, model)
    argv = [exe, *command[1:], *[str(a) for a in passthrough]]
    if new_window is None:
        new_window = os.name == "nt" and not stdio_is_console()
    flags = subprocess.CREATE_NEW_CONSOLE if (new_window and os.name == "nt") else 0
    try:
        proc = subprocess.run(argv, env=env, creationflags=flags)
    except (OSError, ValueError) as exc:
        connect.out(f"{command[0]} 没起来：{exc}")
        return 2
    return int(proc.returncode)


# ---------------- 命令行 ----------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="一行把编码 agent 接到这台机器上的 Strata 并跑起来")
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
    p.add_argument("--context-length", dest="context_length", type=int, default=0,
                   help="覆盖上下文长度（0 = 用模型配置里那个）")
    p.add_argument("--persist", action="store_true",
                   help="先把 provider 写进 agent 的配置文件（先备份成 .neo-bak）")
    p.add_argument("--no-launch", dest="launch", action="store_false",
                   help="只打印要粘的命令，不启动 agent")
    p.add_argument("--new-window", dest="new_window", action="store_true", default=None,
                   help="在新控制台里跑 agent（窗口版 exe 默认就是这样）")
    p.add_argument("--no-window", dest="new_window", action="store_false", default=None,
                   help="接上当前终端跑 agent")
    p.add_argument("--shell", default="auto", choices=["auto", "powershell", "posix"],
                   help="--no-launch 打印哪种 shell 的块")
    p.add_argument("--json", action="store_true", help="出 JSON（给页面与脚本用）")
    p.add_argument("passthrough", nargs="*", help="-- 之后的参数原样交给 agent")
    return p.parse_args(argv)


def main(argv=None) -> int:
    wide_stdout()
    args = parse_args(argv)
    agent = str(args.agent or "list").strip().lower()
    root = connect.resolve_root(args.root)
    if agent == "list":
        for name in connectors.AGENTS:
            connect.out(f"{name:<10} {connectors.TITLES[name]:<12} "
                        f"{quick_command(root, name)}")
        return 0
    if agent not in connectors.AGENTS:
        connect.out(f"不认识的 agent：{agent}（可选 {' / '.join(connectors.AGENTS)}）")
        return 2
    if not args.base and os.environ.get(ENV_SERVER_URL):
        args.base = str(os.environ[ENV_SERVER_URL])          # 连远程那台 Strata
    item = build(agent, args)
    if args.json:
        import json
        connect.out(json.dumps(item, ensure_ascii=False, indent=1))
        return 0
    if not args.launch:
        connect.print_item(item, args.shell)
        return 0
    connect.out(f"# {item['title']} → {item['base']}  模型 {item['model']}"
                + (f"（{item['context']} 上下文）" if item["context"] else ""))
    for note in item["notes"]:
        connect.out(f"# {note}")
    return launch(item, args.passthrough, new_window=args.new_window)


if __name__ == "__main__":
    sys.exit(main())
