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
    POST /neo/open             在浏览器里打开聊天页/监控页（页面里点"打开"就是这条）
    POST /neo/open-log         用系统默认方式打开服务日志
    GET  /neo/config  POST /neo/config
    GET  /neo/log?tail=N       服务日志尾部
    GET  /neo/events           监督的事件时间线

M2/M3 加的（默认没人点就什么都不发生，读接口不写任何文件）：

    GET  /neo/net              下载源探测（huggingface / 镜子 / ModelScope 的连通与时延）
    GET  /neo/lan              局域网与 token 现状（地址、有没有 key、防火墙命令）
    POST /neo/token            生成一把 token 并交给服务（写 launcher.json）
    GET  /neo/connect          四个 agent 的接入配方（env / 要清空的变量 / 命令）
    POST /neo/connect          把一个 provider 写进 agent 的配置文件（先备份 .neo-bak）
    GET  /neo/wizard           安装向导现状（缺什么、建议的源、命令、日志尾部）
    POST /neo/wizard           跑 setup.py（后台线程，输出进 neo/state/logs/wizard.log）
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import paths

WEB_DIR = paths.web_dir()
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def neo_console_info(port: int, timeout: float = 0.8) -> dict | None:
    """这个端口上有没有一个 Neo 控制台在应答。有就回它的状态快照，没有回 None。

    实测 2026-10-12（D:\\Work\\Strata）：18:39 起的旧 Neo（pythonw -m neo.manager，跑的是
    修好前的代码）与 19:34 起的 NeoStrata.exe 同时在听 127.0.0.1:8083，页面每 2 秒一次的
    轮询全落在旧的那个上 —— 窗口里是"未启动 / 文件不在 / 没有日志"，而真正在跑的服务
    在另一个进程里（45372 在听 8080，日志 10 KB 还在写）。
    """
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/neo/status",
                                     headers={"User-Agent": "NeoStrata/0.1",
                                              "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "state" in data else None


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """绑控制台端口时用 SO_EXCLUSIVEADDRUSE（Windows）：别人抢不走，我们也不抢别人的。

    ThreadingHTTPServer 默认 allow_reuse_address=True，绑之前会设 SO_REUSEADDR；
    Windows 上这个标志等于"允许重复绑定同一个端口"，于是两个 Neo 都能监听 8083，
    新连接落在先绑的那个上（实测见 neo_console_info 的注释）。清掉它、改设独占标志，
    端口被占时 bind 直接失败，start() 就换下一个端口。
    """

    allow_reuse_address = False

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


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
        elif path == "/neo/net":
            self._json(200, self.server.net_info(fresh="fresh=1" in urlsplit(self.path).query))
        elif path == "/neo/lan":
            self._json(200, self.server.lan_info())
        elif path == "/neo/connect":
            self._json(200, self.server.connect_recipes())
        elif path == "/neo/wizard":
            self._json(200, self.server.wizard_status())
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
        elif path == "/neo/token":
            self._json(200, self.server.make_token())
        elif path == "/neo/connect":
            self._json(200, self.server.connect_write(str(body.get("agent") or "")))
        elif path == "/neo/wizard":
            self._json(200, self.server.wizard_run(body))
        elif path in ("/neo/open", "/neo/open-log"):
            self._shell_action("open_page" if path == "/neo/open" else "open_log", body)
        else:
            self._json(404, {"error": "not found"})

    def _shell_action(self, name: str, body: dict) -> None:
        """"打开聊天页 / 监控页 / 日志文件"这类动作：由壳（manager.Shell）执行。

        页面不再经 pywebview 的 js_api 调这些 —— 那条路一次点击要窗口线程两次收消息，
        窗口线程一停，第一次点击的 promise 永不返回，后面每次点击都排在它后面
        （实测"一点就卡死"）。放在服务上，窗口就只是个容器，浏览器里也是同一条路。
        """
        shell = getattr(self.server, "shell", None)
        if shell is None:
            self._json(503, {"error": "Neo 壳没在跑，打不开页面"})
            return
        call = getattr(shell, name, None)
        if call is None:
            self._json(503, {"error": f"壳不支持 {name}"})
            return
        try:
            if name == "open_page":
                self._json(200, call(str(body.get("page") or "chat")))
            else:
                self._json(200, call())
        except Exception as e:                              # 打开失败要回话，不能让页面干等
            self._json(500, {"error": f"打不开：{e}"})


class ConsoleServer:
    """控制台服务的持有者。start() 起线程，url 给壳加载，stop() 收尾。"""

    def __init__(self, supervisor, host: str = "127.0.0.1", port: int | None = None,
                 strata_port: int | None = None, allowed_hosts=(), shell=None):
        self.supervisor = supervisor
        self.host = host
        self.strata_port = strata_port
        self.allowed_hosts = tuple(allowed_hosts)
        self.shell = shell                                # 页面里"打开页面/日志"由它执行
        self.httpd = None
        self.thread = None
        self.port = None
        self._pick = port if port is not None else self._default_port(strata_port)

    @staticmethod
    def _default_port(strata_port):
        return 8083 if strata_port is None else int(strata_port) + 3

    def start(self) -> str:
        """起服务。约定端口被占用时依次试 +1..+5（参考实现的做法）。

        两条规则（实测 2026-10-12，见 neo_console_info 的注释）：
        端口上已有 Neo 在应答就跳过它，绑定时独占 —— 绝不与另一个 Neo 共用一个端口，
        共用时页面显示的是那个 Neo 的状态，不是这个窗口的。
        """
        tried = [self._pick] if self._pick is not None else [8083]
        if self.strata_port is not None:
            base = int(self.strata_port) + 3
            tried += [base + i for i in range(6) if base + i not in tried]
        last_error = None
        taken = []
        for port in tried:
            other = neo_console_info(port)
            if other is not None:
                taken.append((port, other.get("neo_pid") or "?"))
                continue
            try:
                httpd = _ExclusiveHTTPServer((self.host, port), partial_handler(self))
                break
            except OSError as e:
                last_error = e
        else:
            hint = ""
            if taken:
                hint = "；这些端口上已有别的 Neo 在应答：" + \
                    "、".join(f"{p}（进程 {pid}）" for p, pid in taken) + \
                    " —— 任务管理器里结束它，或把本机的控制台端口改成别的"
            raise RuntimeError(f"控制台端口起不来：{last_error}{hint}")
        httpd.supervisor = self.supervisor
        httpd.allowed_hosts = self.allowed_hosts
        httpd.public_config = self.public_config
        httpd.apply_config = self.apply_config
        httpd.net_info = self.net_info
        httpd.lan_info = self.lan_info
        httpd.make_token = self.make_token
        httpd.connect_recipes = self.connect_recipes
        httpd.connect_write = self.connect_write
        httpd.wizard_status = self.wizard_status
        httpd.wizard_run = self.wizard_run
        httpd.shell = self.shell
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

    # ---- M2/M3：探测、局域网、token、接入、向导 ----
    # 读接口（net / lan / connect / wizard）不写任何文件；写动作只有
    # POST /neo/token（launcher.json）、POST /neo/connect（agent 的配置，先备份）、
    # POST /neo/wizard（跑 setup.py，输出进 wizard.log）。
    def root_dir(self):
        return getattr(self.supervisor.adapter, "root", None) or paths.app_dir().parent

    def net_info(self, fresh: bool = False) -> dict:
        from . import netprobe
        return netprobe.probe_cached(force=bool(fresh))

    def lan_info(self) -> dict:
        from . import tokens
        return tokens.token_status(self.supervisor.port, self.supervisor.host,
                                   self.supervisor.api_key)

    def make_token(self) -> dict:
        """生成一把 token，写进 launcher.json 并交给监督（下一次启动服务带上）。"""
        from . import state, tokens
        key = tokens.new_key()
        base = tokens.base_for(self.supervisor.host, self.supervisor.port)
        tokens.remember_key(base, key, "minted")
        state.save_launcher_config(api_key=key)
        self.supervisor.api_key = key
        return {"ok": True, "api_key": key, "api_key_masked": tokens.mask_of(key),
                "note": "已写进 neo/state/launcher.json 并交给监督：正在跑的那个服务"
                        "还按启动时的设置认，点一次\"停止\"再\"启动\"就按这把 key 认"}

    def _model_for_connect(self):
        from . import connectors
        adapter = self.supervisor.adapter
        items = []
        try:
            items = adapter.discover()
        except OSError:
            pass
        wanted = str(self.supervisor.model_id or "")
        cfg = next((x for x in items if x["id"] == wanted), None) if wanted else None
        cfg = cfg or (items[0] if items else None)
        if cfg is None:
            return connectors.Model(wanted or "qwen3.8-flash-next", 0)
        return connectors.Model.from_config(cfg)

    def connect_recipes(self, agent: str = "") -> dict:
        """四个 agent 的接入配方。key 用服务现在那把；没设 key 就回空串并说清楚。"""
        from . import connectors, tokens
        base = tokens.base_for(self.supervisor.host, self.supervisor.port)
        key = str(self.supervisor.api_key or "")
        model = self._model_for_connect()
        names = [agent] if agent in connectors.AGENTS else list(connectors.AGENTS)
        recipes = [connectors.recipe(name, base, key, model) for name in names]
        return {"base": base, "model": model.id, "context": model.context,
                "has_api_key": bool(key), "lan": self.lan_info(), "recipes": recipes}

    def connect_write(self, agent: str) -> dict:
        from . import connectors, tokens
        if agent not in connectors.AGENTS:
            return {"ok": False, "error": f"不认识的 agent：{agent}"}
        base = tokens.base_for(self.supervisor.host, self.supervisor.port)
        key = str(self.supervisor.api_key or "")
        result = connectors.write_config(agent, base, key, self._model_for_connect())
        return {"ok": bool(result.get("written")), "agent": agent, **result}

    def wizard_status(self) -> dict:
        from . import wizard
        return wizard.status(self.root_dir(), port=self.supervisor.port,
                             host=self.supervisor.host, api_key=self.supervisor.api_key)

    def wizard_run(self, body: dict) -> dict:
        from . import wizard
        return wizard.run_setup(self.root_dir(),
                                model=str(body.get("model") or ""),
                                port=self.supervisor.port,
                                host=self.supervisor.host,
                                api_key=self.supervisor.api_key,
                                source=str(body.get("source") or ""),
                                wait=False)


def partial_handler(console: ConsoleServer):
    """handler 类本身不需要携带状态：所有状态挂在 httpd 上，handler 通过 self.server 取。"""
    return Handler


if __name__ == "__main__":                                      # 单独跑一个控制台（调试用）
    import sys

    from .engines import make_adapter
    from .supervisor import Supervisor
    root = paths.app_dir().parent                  # 源码跑是仓库根；exe 跑是 exe 所在目录
    engine = sys.argv[1] if len(sys.argv) > 1 else "strata"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
    adapter = make_adapter(engine, root)
    sup = Supervisor(adapter, port)
    console = ConsoleServer(sup, strata_port=port)
    from .manager import Shell                                # 只在单独跑调试时用，不绕开循环导入
    console.shell = Shell(sup, console)
    print("控制台:", console.start())
    sup.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sup.shutdown()
        console.stop()
