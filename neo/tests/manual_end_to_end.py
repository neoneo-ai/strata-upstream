"""neo/tests/manual_end_to_end.py - 手工端到端检查（不是 unittest 的一部分）。

真起一次桌面壳（pywebview 窗口 + mock 引擎），看控制台接口能不能问到状态，
然后把壳杀掉，确认 Job Object 把 Strata 服务一起带走了、端口空了。

    python neo/tests/manual_end_to_end.py
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from neo import connectors, netprobe                                          # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 28080
CONSOLE = PORT + 3


def get(path, timeout=3):
    with urllib.request.urlopen(f"http://127.0.0.1:{CONSOLE}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def listening(port):
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    print(f"启动桌面壳：mock 引擎，服务端口 {PORT}，控制台 {CONSOLE}")
    shell = subprocess.Popen([sys.executable, "-m", "neo.manager", "--engine", "mock",
                              "--port", str(PORT)], cwd=str(ROOT))
    ready = None
    for i in range(90):
        time.sleep(1.0)
        try:
            s = get("/neo/status")
        except OSError as e:
            print(f"  {i}s 控制台还没起来：{e}")
            continue
        print(f"  {i}s state={s['state']} listening={s['listening']} healthy={s['healthy']} "
              f"pid={s['pid']} 重启={len(s['restarts'])}")
        if s["state"] == "running" and s["healthy"]:
            ready = s
            break
    if ready is None:
        print("FAIL：没等到就绪")
        shell.kill()
        return 1

    print("状态接口：", json.dumps({k: ready[k] for k in
                                    ("state", "model", "port", "pid", "loaded", "max_context")},
                                  ensure_ascii=False))
    print("模型列表：", [m["id"] for m in get("/neo/models")["models"]])
    print("日志尾部：", get("/neo/log?tail=2000")["text"].strip().splitlines()[-1][:70])

    # 接入页的四条只读接口。GET 不许写任何人的配置文件，这里逐条核对。
    paths = {a: connectors.config_path(a) for a in connectors.AGENTS}
    snapshot = {a: (p.stat().st_mtime if p.is_file() else None) for a, p in paths.items()}
    net = get("/neo/net", timeout=20)
    print("下载源：", " / ".join(netprobe.describe(net)))
    lan = get("/neo/lan")
    print("局域网：", json.dumps({k: lan[k] for k in ("host", "base", "api_key_masked", "lan_open")},
                               ensure_ascii=False))
    print("        ", lan["note"], "|", lan["firewall_hint"] or "（环回，不需要防火墙）")
    con = get("/neo/connect")
    print("接入：", con["base"], "模型", con["model"], "上下文", con["context"],
          "有 key =", con["has_api_key"])
    for c in con["recipes"]:
        print(f"  {c['agent']}：{c['config_path']} 命令={' '.join(c['command'])[:60]} "
              f"unset={len(c['unset'])} 项")
    wz = get("/neo/wizard")
    print("向导：", wz["command"])
    print("      ", [f"{s['name']}={'OK' if s['ok'] else '—'}" for s in wz["steps"]],
          "就绪 =", wz["ready"], "在跑 =", wz["run"]["running"])
    for agent, path in paths.items():
        now = path.stat().st_mtime if path.is_file() else None
        if now != snapshot[agent]:
            print(f"FAIL：GET 接口动了 {path}")
            shell.kill()
            return 1
    print("OK：四条只读接口都没写任何配置文件")

    print("杀掉桌面壳（模拟被任务管理器结束）...")
    shell.kill()
    shell.wait(timeout=20)
    time.sleep(3.0)
    left = listening(PORT)
    print("服务端口还开着吗：", left, "（Job Object 应该已经把它带走了）")
    if left:
        print("FAIL：Neo 死了，Strata 服务还留着")
        return 1
    print("OK：壳死之后没有残留进程")
    return 0


if __name__ == "__main__":
    sys.exit(main())
