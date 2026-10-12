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
