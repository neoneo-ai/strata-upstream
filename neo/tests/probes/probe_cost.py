"""neo/tests/probes/probe_cost.py - 杀掉服务后，探活的每一层各花多少秒。

量法：起一个 neo/tests/mock_server.py，确认它活着，杀掉，然后逐层计时
`tcp_open` / `/health` / `/metrics`。

本机实测 2026-10-13（Windows 11，Python 3.13）：服务活着时 tcp_open 0.00 秒；杀掉之后
tcp_open 1.21 秒（超时 1.2 秒时）、/health 2.02 秒、/metrics 2.02 秒 —— 监听被杀掉后
Windows 把那个端口黑洞化（SYN 没人回，不是 RST），每一层都等到自己的超时。
一次 probe 因此是 5.25 秒，而监督器的 _monitor 每轮都要跑一次它：进程崩了之后
状态页要 5 秒才变。tcp_open 的默认超时因此从 1.2 秒收到 0.25 秒。

跑法：python neo/tests/probes/probe_cost.py
"""
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]      # .../neo/tests/probes/此文件 -> 仓库根
sys.path.insert(0, str(REPO))

from neo import state  # noqa: E402
from neo.engines.base import http_json, tcp_open  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> int:
    port = free_port()
    proc = subprocess.Popen([state.python_executable(REPO), str(REPO / "neo/tests/mock_server.py"),
                             "--port", str(port)], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    t = time.perf_counter()
    while time.perf_counter() - t < 20:
        if http_json(port, "/health", timeout=1.0):
            break
        time.sleep(0.05)
    print(f"活着时：tcp_open={tcp_open('127.0.0.1', port)}  /health 应答="
          f"{bool(http_json(port, '/health', 1.0))}")
    proc.kill()
    proc.wait()
    for i in range(6):
        t0 = time.perf_counter()
        lp = tcp_open("127.0.0.1", port)
        t1 = time.perf_counter()
        h = http_json(port, "/health", timeout=4.0)
        t2 = time.perf_counter()
        m = http_json(port, "/metrics", timeout=4.0)
        t3 = time.perf_counter()
        print(f"杀掉后第 {i} 轮：tcp_open {t1 - t0:5.2f}s(在听={lp})  /health {t2 - t1:5.2f}s"
              f"(应答={bool(h)})  /metrics {t3 - t2:5.2f}s")
        time.sleep(0.2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
