"""neo/tests/mock_server.py - 测试用的最小"服务"：只答 /health 与 /metrics。

为什么要有：监督的测试要真进程（真端口、真 HTTP、真 kill、真重启），但 MockEngine 起的是
serve/server.py --engine mock —— 那要 import 整个上游服务（numpy 等），实测 2026-10-13 在
这台机器上（Defender 扫磁盘）一次 spawn 约 3.9 秒，test_supervisor 里 11 次 spawn 就是 43 秒。
这个文件只用标准库，一次 spawn 约 0.3 秒，测的链路一个字都不少：
启动 → 就绪 → 被杀 → 停着并给出诊断 → 点启动再起。

上游真行为（就绪行进日志、/health 的真字段）由 neo/tests/test_upstream_api.py 和
test_supervisor 里那条用真 serve/server.py 的用例守着，不靠这个文件。

应答的形状照 serve/server.py:4925 的 /health 与 neo/engines/base.py 的 flatten_metrics：
少了键，监督的探活就会判成"不健康"，测试会以另一种方式失败 —— 这是有意的。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_CONTEXT = 32768
HEALTH = {"status": "ok", "max_context": MAX_CONTEXT, "model": "mock",
          "loaded": True, "images": False, "api_key": False}
METRICS = {"live": {"state": "idle", "running": 0, "queued": 0, "phase": "",
                    "generated": 0, "elapsed_s": 0.0, "prompt_tokens": 0},
           "totals": {"requests": 0, "output_tokens": 0, "decode_ms": 0},
           "hardware": {}, "hardware_static": {}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):      # 不要往 stdout 写访问日志
        pass

    def _json(self, code: int, data) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/health", "/api/health"):
            self._json(200, HEALTH)
        elif path == "/metrics":
            self._json(200, METRICS)
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        self._json(405, {"error": {"message": "mock server: no writes"}})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Neo 测试用的最小服务")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--engine", default="mock")      # 与 MockEngine 的参数兼容，忽略
    ap.add_argument("--script", default="")          # 同上
    ap.add_argument("--api-key", default="")
    ns = ap.parse_args(argv)
    httpd = ThreadingHTTPServer((ns.host, ns.port), Handler)

    def serve():
        httpd.serve_forever(poll_interval=0.05)

    threading.Thread(target=serve, daemon=True).start()
    # 就绪行：监督的测试要看到"ready"进了服务日志
    print(f"mock server ready on {ns.host}:{ns.port}", flush=True)
    sys.stdout.flush()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
