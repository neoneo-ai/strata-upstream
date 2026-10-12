"""neo/console_server.py - Neo 的控制台服务：给桌面壳（或浏览器）看的页面与 JSON 接口。

只用标准库的 http.server：Strata 的 .venv 里没有 aiohttp（requirements.txt 只有 numpy /
psutil / sentencepiece / huggingface-hub / safetensors / jinja2），装新包会让用户的第一次
安装变慢，所以 Neo 自己的服务用标准库。

端口约定：Strata 端口 + 3（8080 → 8083）。被占用时依次试 +4..+8，与参考实现一致。
默认只绑 127.0.0.1；局域网开放与 token 是 M3 的事。

接口：
    GET  /                     控制台页面
    GET  /app.js /style.css    页面资源（一律 no-store：壳是本地文件，缓存会挡住新界面）
    GET  /neo/status           壳与服务的状态快照
    GET  /neo/models           可启动的模型列表
    POST /neo/start /stop /retry
    POST /neo/model            换模型（重启服务）
    GET  /neo/config  POST /neo/config
    GET  /neo/log?tail=N       服务日志尾部
    GET  /neo/events           监督的事件时间线
"""
from __future__ import annotations

import json
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .paths import safe_resolve

WEB_DIR = safe_resolve(Path(__file__).parent) / "web"
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


class Handler(BaseHTTPRequestHandler):
    server_version = "NeoStrata/0.1"
    protocol_version = "HTTP/1.1"

    # ---------------- 小工具 ----------------
    def _json(self, code: int, data) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("content-length") or 0)
        except ValueError:
            return {}
        if n <= 0 or n > 1_000_000:
            return {}
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _allowed(self) -> bool:
        """只接受本机来的请求；带 Origin 的（浏览器发的）必须指向本机，防跨站打 POST 接口。"""
        host = (self.headers.get("host") or "").split(":")[0].strip().strip("[]").lower()
        if host not in LOCAL_HOSTS and host not in getattr(self.server, "allowed_hosts", ()):
            self._json(403, {"error": "Neo 的控制台只接受本机访问"})
            return False
        origin = (self.headers.get("origin") or "").lower()
        if origin:
            port = str(self.server.server_address[1])
            if not any(origin.startswith(f"http://{h}:{port}") or origin == f"http://{h}"
                       for h in LOCAL_HOSTS):
                self._json(403, {"error": "来源不对"})
                return False
        return True

    def log_message(self, fmt, *args):          # 控制台请求不进日志，太吵
        pass

    # ---------------- 静态页面 ----------------
    def _static(self, name: str) -> None:
        f = WEB_DIR / name
        if "/" in name or "\\" in name or not f.is_file():
            self._json(404, {"error": "not found"})
            return
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", STATIC_TYPES.get(f.suffix, "application/octet-stream"))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ---------------- 路由 ----------------
    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if not self._allowed():
            return
        sup = self.server.supervisor
        if path in ("/", "/index.html"):
            self._static("index.html")
        elif path in ("/app.js", "/style.css", "/icon.svg"):
            self._static(path.lstrip("/"))
        elif path == "/neo/status":
            self._json(200, sup.status())
        elif path == "/neo/events":
            self._json(200, {"events": list(sup.events)[-80:]})
        elif path == "/neo/models":
            self._json(200, {"models": sup.adapter.discover(), "engine": sup.adapter.name,
                             "current": sup.model_id})
        elif path == "/neo/log":
            query = urlsplit(self.path).query
            tail = 120_000
            for part in query.split("&"):
                if part.startswith("tail="):
                    try:
                        tail = max(1000, min(60_000, int(part[5:])))
                    except ValueError:
                        pass
            # 日志页每 5 秒刷一次，一次最多 60 KB / 600 行，行内已经裁过（state.tail_text）
            self._json(200, {"path": sup.status()["log_path"], "text": sup.tail(tail, 600)})
        elif path == "/neo/config":
            self._json(200, self.server.public_config())
        else:
            self._json(404, {"error": "not found"})


    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = unquote(urlsplit(self.path).path)
        if not self._allowed():
            return
        sup = self.server.supervisor
        body = self._body()
        if path == "/neo/start":
            self._json(200, sup.start(model_id=body.get("model")))
        elif path == "/neo/stop":
            self._json(200, sup.stop())
        elif path == "/neo/retry":
            self._json(200, sup.retry())
        elif path == "/neo/model":
            model = str(body.get("model") or "")
            if not model:
                self._json(400, {"error": "要指定模型"})
                return
            self._json(200, sup.set_model(model))
        elif path == "/neo/config":
            self._json(200, self.server.apply_config(body))
        else:
            self._json(404, {"error": "not found"})


class ConsoleServer:
    """控制台服务的持有者。start() 起线程，url 给壳加载，stop() 收尾。"""

    def __init__(self, supervisor, host: str = "127.0.0.1", port: int | None = None,
                 strata_port: int | None = None, allowed_hosts=()):
        self.supervisor = supervisor
        self.host = host
        self.strata_port = strata_port
        self.allowed_hosts = tuple(allowed_hosts)
        self.httpd = None
        self.thread = None
        self.port = None
        self._pick = port if port is not None else self._default_port(strata_port)

    @staticmethod
    def _default_port(strata_port):
        return 8083 if strata_port is None else int(strata_port) + 3

    def start(self) -> str:
        """起服务。约定端口被占用时依次试 +1..+5（参考实现的做法）。"""
        tried = [self._pick] if self._pick is not None else [8083]
        if self.strata_port is not None:
            base = int(self.strata_port) + 3
            tried += [base + i for i in range(6) if base + i not in tried]
        last_error = None
        for port in tried:
            try:
                httpd = ThreadingHTTPServer((self.host, port), partial_handler(self))
                break
            except OSError as e:
                last_error = e
        else:
            raise RuntimeError(f"控制台端口起不来：{last_error}")
        httpd.supervisor = self.supervisor
        httpd.allowed_hosts = self.allowed_hosts
        httpd.public_config = self.public_config
        httpd.apply_config = self.apply_config
        self.httpd = httpd
        self.port = httpd.server_address[1]
        self.thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="neo-console")
        self.thread.start()
        host = "127.0.0.1" if self.host in ("0.0.0.0", "", "::") else self.host
        return f"http://{host}:{self.port}/"

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    # ---- 设置（M1 只存端口与 host；接线在 M3） ----
    def public_config(self) -> dict:
        from . import state
        cfg = state.launcher_config()
        cfg.update({"port": self.supervisor.port, "console_port": self.port,
                    "host": self.supervisor.host, "engine": self.supervisor.adapter.name,
                    "model": self.supervisor.model_id,
                    "has_api_key": bool(self.supervisor.api_key)})
        cfg["api_key"] = ""
        return cfg

    def apply_config(self, body: dict) -> dict:
        from . import state
        changes = {k: body[k] for k in ("port", "host", "model", "engine") if k in body}
        if "api_key" in body and str(body["api_key"] or "") != "":
            changes["api_key"] = str(body["api_key"])
            self.supervisor.api_key = changes["api_key"]
        state.save_launcher_config(**changes)
        if "port" in changes:
            try:
                self.supervisor.port = int(changes["port"])
            except (TypeError, ValueError):
                pass
        if "host" in changes:
            self.supervisor.host = str(changes["host"])
        return self.public_config()


def partial_handler(console: ConsoleServer):
    """handler 类本身不需要携带状态：所有状态挂在 httpd 上，handler 通过 self.server 取。"""
    return Handler


if __name__ == "__main__":                                      # 单独跑一个控制台（调试用）
    import sys

    from .engines import make_adapter
    from .supervisor import Supervisor
    root = safe_resolve(Path(__file__).parent).parent
    engine = sys.argv[1] if len(sys.argv) > 1 else "strata"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
    adapter = make_adapter(engine, root)
    sup = Supervisor(adapter, port)
    console = ConsoleServer(sup, strata_port=port)
    print("控制台:", console.start())
    sup.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sup.shutdown()
        console.stop()
