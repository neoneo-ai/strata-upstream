"""neo/wizard.py - 安装向导：把 setup.py 的问答拼成一次非交互安装，下载源用探测结果。

Neo 不自己装模型 —— 装模型是 setup.py 的事（docs/AI_SETUP.md 那条路）。
向导做的三件事：

1. 先看这台机器缺什么（根目录对不对、模型装没装、够得着哪个源）。
2. 把 setup.py 拼成一条不用人守着的命令：--yes（接受推荐答案）、
   --no-start（装完不启动，启动交给 Neo 的监督）、--port/--host/--api-key
   （Neo 要的那个端口与 token）、--source（netprobe 量出来的那个源）、
   --family/--model（装哪一个；不给就 setup.py 自己挑，清单见 neo/models.py）。
   这些参数 setup.py 本来就有（neo/tests/test_upstream_api.py 钉着"还在不在"）。
3. 跑它，输出全进 neo/state/logs/wizard.log，页面里能看尾部。

上游明确不自己换源（setup.py 的 model_source() 里那句 #908），所以"这台机器
够得着 ModelScope"这件事由 Neo 判断完再传 --source，不改上游一行。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import netprobe, paths, state, tokens
from .cli import wide_stdout
from .engines import make_adapter
from .paths import resolve_root

SETUP_NAME = "setup.py"
WIZARD_LOG_NAME = "wizard.log"
TAIL_BYTES = 60_000


def setup_path(root) -> Path:
    return Path(root) / SETUP_NAME


def wizard_log_path() -> Path:
    return state.LOGS / WIZARD_LOG_NAME


def setup_argv(root, *, family: str = "", model: str = "", port: int = 0, host: str = "127.0.0.1",
               api_key: str = "", source: str = "", yes: bool = True,
               no_start: bool = True, extra=()) -> list[str]:
    """拼出 setup.py 的那条命令。source 传空 = 用 netprobe 量出来的那个源。"""
    python = state.python_executable(root)
    argv = [python, str(setup_path(root))]
    if family:
        argv += ["--family", str(family)]
    if model:
        argv += ["--model", str(model)]
    if port:
        argv += ["--port", str(int(port))]
    if host and host != "127.0.0.1":
        argv += ["--host", host]
    if api_key:
        argv += ["--api-key", api_key]
    argv += netprobe.choice_args(source)[0]
    if yes:
        argv.append("--yes")
    if no_start:
        argv.append("--no-start")
    argv += [str(x) for x in extra]
    return argv


def source_env(source: str) -> dict[str, str]:
    """选这个源还要额外给 setup.py 什么环境变量（镜子那条路要 HF_ENDPOINT）。

    subprocess 默认会带着 Neo 自己的环境跑，这里只回"多出来的那几个"，
    由 run_setup 合并进去 —— 不整份替换，Neo 的 .venv、PATH 那些都留着。
    """
    return netprobe.choice_args(source)[1]


def steps(root, port: int = 8080, host: str = "127.0.0.1", api_key: str = "") -> list[dict]:
    """这台机器现在缺什么。每项 {name, ok, text}，全 ok 就是可以直接启动。"""
    root = Path(root)
    out = []
    has_setup = setup_path(root).is_file()
    out.append({"name": "根目录", "ok": has_setup,
                "text": (f"{root} 里有 setup.py" if has_setup
                         else f"{root} 里没有 setup.py —— 这不是 Strata 的根目录。"
                              "两条路：python -m neo.engine_setup --ensure 让 Neo 按 pin 自己装一份，"
                              "或 python -m neo.engine_setup --set-root <目录> 用这台机器上"
                              "别处一键装好的那一份（记一次就长期用，不复制目录）")})
    adapter = make_adapter("strata", root)
    items = []
    try:
        items = adapter.discover()
    except OSError:
        pass
    installed = [x for x in items if x.get("installed")]
    out.append({"name": "模型", "ok": bool(installed),
                "text": (f"{len(installed)} 个装好了（{installed[0]['name']} 最近用过）"
                         if installed else
                         "根目录没有装好的模型（strata-*.json 里没有能起得来的）")})
    net = netprobe.probe_cached()
    out.append({"name": "下载源", "ok": True,
                "text": (f"{net['reason']}；建议 --source {net['source']}"
                         if net["source"] == "modelscope" else net["reason"])})
    lan = tokens.token_status(port, host, api_key)
    out.append({"name": "对外", "ok": not (lan["lan_open"] and not lan["has_api_key"]),
                "text": lan["note"]})
    return out


# ---------------- 跑安装 ----------------
_LOCK = threading.Lock()
_RUN = {"running": False, "code": None, "started_at": 0.0, "argv": [], "log": "", "env": {}}


def _mark(**changes) -> None:
    with _LOCK:
        _RUN.update(changes)


def run_state() -> dict:
    with _LOCK:
        return dict(_RUN)


def run_setup(root, *, family: str = "", model: str = "", port: int = 0, host: str = "127.0.0.1",
              api_key: str = "", source: str = "", extra=(),
              wait: bool = True) -> dict:
    """跑 setup.py，输出全进 neo/state/logs/wizard.log。

    wait=False 时后台跑（页面里点"开始安装"就是这条），回 {"started": True, ...}。
    """
    argv = setup_argv(root, family=family, model=model, port=port, host=host, api_key=api_key,
                      source=source, extra=extra)
    env = source_env(source)
    log = wizard_log_path()
    state.ensure()
    if not setup_path(root).is_file():
        return {"started": False, "code": None,
                "notes": [f"{setup_path(root)} 不在，跑不了安装"]}

    def worker():
        _mark(running=True, code=None, started_at=time.time(), argv=argv, log=str(log),
              env=dict(env))
        head = " ".join(f"{k}={v}" for k, v in env.items())
        head = (head + " ") if head else ""
        with open(log, "ab") as handle:
            handle.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {head}{' '.join(argv)}\n"
                         .encode("utf-8", "replace"))
            handle.flush()
            proc = subprocess.Popen(argv, cwd=str(Path(root)), stdout=handle,
                                    stderr=subprocess.STDOUT,
                                    env=({**os.environ, **env} if env else None))
            code = proc.wait()
        _mark(running=False, code=code)

    if wait:
        worker()
        return {"started": True, **run_state()}
    # 先把"有一个在跑了"记下来再起线程：页面 5 秒一轮，第二次点击必须看得见第一次
    _mark(running=True, code=None, started_at=time.time(), argv=argv, log=str(log),
          env=dict(env))
    threading.Thread(target=worker, daemon=True, name="neo-wizard").start()
    return {"started": True, "running": True, "argv": argv, "env": env, "log": str(log)}


def log_tail(max_bytes: int = TAIL_BYTES, max_lines: int = 200) -> str:
    return state.tail_text(wizard_log_path(), max_bytes=max_bytes, max_lines=max_lines)


def status(root, port: int = 8080, host: str = "127.0.0.1", api_key: str = "",
           source: str = "") -> dict:
    """给页面的一份向导现状。source 传空 = 按探测建议。"""
    items = steps(root, port=port, host=host, api_key=api_key)
    argv = setup_argv(root, port=port, host=host, api_key=api_key, source=source)
    env = source_env(source)
    # 选镜像时命令预览要带上那一截环境：setup.py 自己读 HF_ENDPOINT，页面上要看得见
    head = " ".join(f"{k}={v}" for k, v in env.items())
    command = ((head + " ") if head else "") + " ".join(argv)
    return {
        "root": str(Path(root)),
        "steps": items,
        "ready": all(x["ok"] for x in items),
        "net": netprobe.probe_cached(),
        "source": source,
        "env": env,
        "command": command,
        "run": run_state(),
        "log_tail": log_tail(max_lines=40),
    }


def parse_args(argv=None):
    import argparse

    p = argparse.ArgumentParser(description="Neo 安装向导（跑 setup.py，源自动选）")
    p.add_argument("--root", default="",
                   help="Strata 的根目录（不给就按 neo/paths.py 那一条规则，"
                        "认 neo/state/launcher.json 记住的引擎目录、<这里>/strata/ 与 NEO_STRATA_ROOT）")
    p.add_argument("--family", default="", help="setup.py 的 --family（qwen / coder / swift / unsloth）")
    p.add_argument("--model", default="", help="setup.py 的 --model（不给就按推荐）")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--host", default="127.0.0.1", help="0.0.0.0 = 也听局域网（要配 --api-key）")
    p.add_argument("--api-key", default="", help="服务要求客户端带的 key")
    p.add_argument("--source", default="", choices=["", "auto", "probe", "modelscope",
                                                     "huggingface", "mirror"],
                   help="下载源；不给就用探测结果（mirror = 设 HF_ENDPOINT=https://hf-mirror.com）")
    p.add_argument("--check", action="store_true", help="只看现状，不跑安装")
    p.add_argument("--dry-run", action="store_true", help="只把命令打出来")
    return p.parse_args(argv)


def main(argv=None) -> int:
    wide_stdout()
    args = parse_args(argv)
    root = resolve_root(args.root)
    why = paths.strata_root(args.root)[1]
    print(f"== 根目录 {root}（{why}）")
    print("== 现状")
    for item in steps(root, port=args.port, host=args.host, api_key=args.api_key):
        print(f"  [{'OK' if item['ok'] else '—'}] {item['name']}：{item['text']}")
    if not setup_path(root).is_file():
        # 命令预览指的是 <根目录>/setup.py：它不在，就别把一条跑不通的命令摆在最前面
        print("== 命令")
        print("  先让引擎到位：python -m neo.engine_setup --ensure"
              "（Neo 按 pin 自己装）")
        print("  或用别处装好的：python -m neo.engine_setup --set-root <目录>")
        return 0
    print("== 命令")
    argv = setup_argv(root, family=args.family, model=args.model, port=args.port,
                      host=args.host, api_key=args.api_key, source=args.source)
    env = source_env(args.source)
    # 选镜像时命令里多这一截环境（setup.py 自己读 HF_ENDPOINT），预览要看得见
    head = " ".join(f"{k}={v}" for k, v in env.items())
    print("  " + ((head + " ") if head else "") + " ".join(argv))
    if args.check or args.dry_run:
        return 0
    result = run_setup(root, family=args.family, model=args.model, port=args.port, host=args.host,
                       api_key=args.api_key, source=args.source)
    if not result.get("started"):
        print("\n".join(result.get("notes", [])))
        return 1
    print("== 输出（尾部）")
    print(log_tail(max_lines=40))
    return int(result.get("code") or 0)


if __name__ == "__main__":
    sys.exit(main())

