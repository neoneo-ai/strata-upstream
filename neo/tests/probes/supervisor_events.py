"""neo/tests/probes/supervisor_events.py - 杀掉服务后，监督器多久发现。

量法：起一个被监督的服务（neo/tests/mock_server.py），等它就绪，杀掉它的进程，
然后读监督器自己记的事件时间戳（status()["events"]）—— 发现退出、进入退避、
重新就绪各在第几秒。

本机实测 2026-10-13（Windows 11，Python 3.13，poll_s 0.05 / probe_s 0.1）：
启动到就绪 0.61 秒，发现退出 0.02 秒。改之前（_monitor 每轮都探活、
tcp_open 超时 1.2 秒）同一脚本量到的是：就绪 1.82 秒，发现退出 0.01 秒与
2.05 秒交替，上一轮量到 4.13 秒。

跑法：python neo/tests/probes/supervisor_events.py
"""
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]      # .../neo/tests/probes/此文件 -> 仓库根
sys.path.insert(0, str(REPO))

from neo.supervisor import RUNNING, Supervisor  # noqa: E402
from neo.tests.test_supervisor import FastEngine, free_port  # noqa: E402


def main() -> int:
    sup = Supervisor(FastEngine(REPO), free_port(), backoff=(0.2, 0.3, 0.4),
                     window_s=600.0, max_restarts=3, poll_s=0.05, probe_s=0.1)
    sup.start()
    t0 = time.time()
    while sup.status()["state"] != RUNNING and time.time() - t0 < 30:
        time.sleep(0.05)
    print(f"启动到就绪：{time.time() - t0:.2f} 秒")
    for k in (1, 2):
        base = time.time()
        n0 = len(sup.status()["events"])
        sup._proc.kill()
        while (sup.status()["state"] not in ("restarting", "starting")
               and time.time() - base < 30):
            time.sleep(0.02)
        print(f"\n杀第 {k} 次：{time.time() - base:.2f} 秒进入自动重启，新增事件：")
        for e in sup.status()["events"][n0:]:
            print(f"   +{e['at'] - base:6.2f}  [{e['kind']}] {e['text'][:60]}")
        while sup.status()["state"] != RUNNING and time.time() - base < 30:
            time.sleep(0.05)
        print(f"   重新就绪在第 {time.time() - base:.2f} 秒")
    sup.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
