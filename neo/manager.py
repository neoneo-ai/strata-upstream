"""neo/manager.py - Neo 的桌面壳：一个窗口，里面是控制台页面。

    python neo/manager.py                    有 pywebview 就开原生窗口，没有就开浏览器
    python neo/manager.py --engine mock      用 mock 引擎跑（测试）
    python neo/manager.py --no-webview       强制用浏览器

窗口里做的事全走控制台服务的 HTTP 接口（neo/console_server.py），pywebview 只当容器：
壳缺了、WebView2 缺了，界面在浏览器里一模一样，功能不残。

单实例：命名互斥体 Local\\Neo.Strata.SingleInstance。第二个实例提示后退出，
避免两个壳抢同一个端口、互相把对方的服务当"别人的服务"接手。
窗口几何存 neo/state/window.json，恢复时丢掉不在任何屏幕上的坐标（拔掉副屏后窗口跑掉的
情况，与参考实现一致）。
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser
from pathlib import Path

if __package__ in (None, ""):
    # 被直接当脚本跑（python neo/manager.py，双击 NEO-START.bat 的旧写法）时没有父包，
    # 下面的相对导入会抛 ImportError 让窗口一闪就关。这里认下 neo 包，两种跑法都能起来。
    sys.path.insert(0, str(Path(__file__).parent.parent))
    __package__ = "neo"

from . import console_io
from . import state
from .console_server import ConsoleServer
from .engines import make_adapter
from .paths import safe_resolve
from .supervisor import Supervisor
from . import ui_watchdog

NEO_DIR = safe_resolve(Path(__file__).parent)
WEB_DIR = NEO_DIR / "web"
DEFAULT_STRATA_PORT = 8080
MIN_W, MIN_H = 900, 640
DEFAULT_W, DEFAULT_H = 1280, 860
MUTEX_NAME = "Local\\Neo.Strata.SingleInstance"

# 控制台闸门与有界 traceback 要在 import webview 之前装：logging 的 StreamHandler 抓的是
# 当时 sys.stderr 那个对象，装晚了就管不住 pywebview/pythonnet 往控制台打的内部日志
# （WinForms 属性链递归那一类，一条几十万字符）。
console_io.install()
state.install_excepthook()


# ---------------- Windows：单实例与屏幕范围 ----------------
def acquire_single_instance() -> int | None:
    """拿到互斥体回句柄；已经有实例在跑回 None；非 Windows 回 0（不管）。"""
    if sys.platform != "win32":
        return 0
    import ctypes

    handle = ctypes.c_void_p(ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME))
    if ctypes.windll.kernel32.GetLastError() == 183:            # ERROR_ALREADY_EXISTS
        return None
    return handle


def on_screen(x: int, y: int) -> bool:
    """坐标还落在某个屏幕上吗（拔掉副屏后保存的窗口位置可能在屏幕外）。"""
    if sys.platform != "win32":
        return True
    import ctypes
    u = ctypes.windll.user32
    vx, vy = u.GetSystemMetrics(79), u.GetSystemMetrics(80)    # 虚拟屏幕左上角
    vw, vh = u.GetSystemMetrics(81), u.GetSystemMetrics(82)
    return vx <= x < vx + vw and vy <= y < vy + vh


def load_geometry() -> tuple:
    cfg = state.read_json(state.WINDOW, {}) or {}
    if not isinstance(cfg, dict):
        cfg = {}
    w = int(cfg.get("w") or DEFAULT_W)
    h = int(cfg.get("h") or DEFAULT_H)
    x, y = int(cfg.get("x") or -1), int(cfg.get("y") or -1)
    w, h = max(MIN_W, w), max(MIN_H, h)
    if x >= 0 and y >= 0 and not on_screen(x, y):
        x = y = -1                                             # 屏幕外的位置丢掉，让系统摆
    return x, y, w, h


def save_geometry(x: int, y: int, w: int, h: int) -> None:
    state.write_json(state.WINDOW, {"x": int(x), "y": int(y), "w": int(w), "h": int(h)})


def webview2_installed() -> bool:
    """Edge WebView2 运行时在不在（pywebview 的 Windows 后端要用它）。"""
    if sys.platform != "win32":
        return True
    import winreg
    keys = (r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
            r"Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key in keys:
            try:
                with winreg.OpenKey(root, key) as h:
                    if str(winreg.QueryValueEx(h, "pv")[0]).strip():
                        return True
            except OSError:
                continue
    return False



# ---------------- 给页面调的接口（window.pywebview.api.xxx） ----------------
class Shell:
    """pywebview 的 js_api。页面里的按钮最终都落到监督器的方法上。"""

    def __init__(self, sup: Supervisor, console: ConsoleServer):
        self.sup = sup
        self.console = console
        self.window = None

    def status(self):
        return self.sup.status()

    def start(self):
        return self.sup.start()

    def stop(self):
        """页面里的"停止"。收尾最长等 0.5 秒就回，剩下的监督线程自己走完。

        js_api 的返回值要靠 evaluate_js 同步投递回窗口线程，这一条调用挂着不动，
        页面里 await 它的按钮就一直转；状态页每 2 秒轮询一次，停止的结果由轮询给出。
        """
        return self.sup.stop(join_s=0.5)

    def retry(self):
        return self.sup.retry()

    def set_model(self, model_id: str):
        return self.sup.set_model(str(model_id or ""))

    def save_config(self, changes: dict):
        return self.console.apply_config(dict(changes or {}))

    def config(self):
        return self.console.public_config()

    def open_log(self):
        """用系统默认方式打开最近一份服务日志。"""
        path = self.sup.status().get("log_path") or ""
        if not path:
            log = state.latest_server_log()
            path = str(log) if log else str(state.MANAGER_LOG)
        try:
            if sys.platform == "win32":
                import os
                os.startfile(path)
            else:
                webbrowser.open(f"file://{path}")
        except OSError:
            pass
        return {"ok": True, "path": path}

    def open_page(self, which: str):
        pages = self.sup.adapter.pages(self.sup.port, self.sup.host)
        url = pages.get(str(which), pages["chat"])
        webbrowser.open(url)
        return {"ok": True, "url": url}

    def quit(self):
        """页面里的"退出"。confirm_close=True 时 destroy() 会弹确认框并卡住（实测），
        所以程序化退出前先把确认关掉。"""
        if self.window is not None:
            try:
                self.window.confirm_close = False
            except (AttributeError, TypeError):
                pass
            try:
                self.window.destroy()
            except Exception:
                pass
        return {"ok": True}


# ---------------- 主流程 ----------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Neo Strata 桌面管理器")
    p.add_argument("--root", default=str(NEO_DIR.parent), help="Strata 的根目录")
    p.add_argument("--engine", default="strata", help="引擎适配器：strata（默认）或 mock（测试）")
    p.add_argument("--port", type=int, default=0, help="Strata 服务的端口（默认取配置里的，再默认 8080）")
    p.add_argument("--console-port", type=int, default=0, help="控制台端口（默认 Strata 端口 + 3）")
    p.add_argument("--host", default="127.0.0.1", help="服务绑定地址（局域网开放在 M3）")
    p.add_argument("--api-key", default="", help="服务的 API key（透传给 serve/server.py）")
    p.add_argument("--model", default="", help="启动哪个模型配置")
    p.add_argument("--no-start", action="store_true", help="只开壳，不自动启动服务")
    p.add_argument("--no-webview", action="store_true", help="用浏览器而不是原生窗口")
    p.add_argument("--debug", action="store_true", help="打开 pywebview 的调试面板")
    return p.parse_args(argv)


def pick_port(args, adapter) -> int:
    """端口优先级：命令行 --port > neo/state/launcher.json > 模型配置里的 port > 8080。"""
    if args.port:
        return int(args.port)
    cfg = state.launcher_config()
    if cfg.get("port"):
        try:
            return int(cfg["port"])
        except (TypeError, ValueError):
            pass
    items = adapter.discover()
    if items and items[0].get("port"):
        return int(items[0]["port"])
    return DEFAULT_STRATA_PORT


def run_in_browser(url: str, sup: Supervisor, console: ConsoleServer) -> None:
    """没有 pywebview（或没有 WebView2）时的路：同一个页面在浏览器里跑。"""
    state.log(f"用浏览器界面：{url}")
    print(f"Neo 控制台：{url}")
    print("（这个页面就是桌面壳的界面；按 Ctrl+C 结束 Neo）")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    stop_evt = threading.Event()
    try:
        while not stop_evt.wait(3600):
            pass
    except KeyboardInterrupt:
        pass
    sup.shutdown()
    console.stop()



def main(argv=None) -> int:
    args = parse_args(argv)
    root = safe_resolve(args.root)
    mutex = acquire_single_instance()
    if mutex is None:
        print("Neo 已经在运行了（用已经开着的那个窗口）。")
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, "Neo 已经在运行了。", "Neo Strata", 0x30)
            except OSError:
                pass
        return 0

    adapter = make_adapter(args.engine, root)
    port = pick_port(args, adapter)
    sup = Supervisor(adapter, port, host=args.host, api_key=args.api_key, model_id=args.model)
    console = ConsoleServer(sup, host="127.0.0.1", port=args.console_port or None, strata_port=port)
    url = console.start()
    state.log(f"控制台 {url} · 引擎 {adapter.name} · 端口 {port}")
    if not args.no_start and adapter.discover():
        sup.start()

    use_webview = not args.no_webview
    if use_webview and sys.platform == "win32" and not webview2_installed():
        state.log("没有 Edge WebView2 运行时，退回浏览器界面")
        use_webview = False
    if use_webview:
        try:
            import webview                                     # noqa: F401
        except (ImportError, ValueError) as e:
            state.log(f"pywebview 不可用（{e}），退回浏览器界面")
            use_webview = False
    if not use_webview:
        run_in_browser(url, sup, console)
        return 0

    console_io.quiet_gui_logging()          # pywebview / pythonnet 的内部日志不进控制台
    import webview

    x, y, w, h = load_geometry()
    shell = Shell(sup, console)
    profile = state.STATE / "webview-profile"
    profile.mkdir(parents=True, exist_ok=True)
    kwargs = {"title": "Neo Strata", "url": url, "js_api": shell, "width": w, "height": h,
              "min_size": (MIN_W, MIN_H), "confirm_close": True}
    if x >= 0 and y >= 0:
        kwargs.update({"x": x, "y": y})
    try:
        window = webview.create_window(**kwargs)
    except TypeError as e:                 # pywebview 各版本的窗口参数名不一样
        state.log(f"create_window 不认这些参数（{e}），用最少的参数再试")
        window = webview.create_window("Neo Strata", url, js_api=shell, width=w, height=h)
    shell.window = window
    closing = threading.Event()

    # 窗口几何：pywebview 6.2.1 没有 window.get_geometry()（实测 window.py 里搜不到
    # geometry），要实时几何只能听 resized / moved 事件。事件回调由 pywebview 起线程跑
    # （event.py 的 Event.set），这里只往字典里记两个数：不碰文件、不碰窗口线程。
    geometry = {"x": x, "y": y, "w": w, "h": h}

    def on_resized(new_w, new_h):
        geometry["w"], geometry["h"] = int(new_w), int(new_h)

    def on_moved(new_x, new_y):
        geometry["x"], geometry["y"] = int(new_x), int(new_y)

    def on_closed():
        closing.set()
        try:
            save_geometry(**geometry)
        except OSError:
            pass

    for event_name, handler in (("closed", on_closed), ("resized", on_resized),
                                ("moved", on_moved)):
        try:
            event = getattr(window.events, event_name)
            event += handler
        except (AttributeError, TypeError, KeyError):
            pass

    def keep_geometry():
        while not closing.wait(5.0):              # 每 5 秒存一次几何：窗口被直接关掉也留得住
            try:
                if geometry["w"] >= MIN_W:
                    save_geometry(**geometry)
            except OSError:
                pass

    threading.Thread(target=keep_geometry, daemon=True, name="neo-geometry").start()
    # 窗口"未响应"时把线程现场写进日志：探法就是 pywebview 自己那条 evaluate_js 路。
    ui_watchdog.start(window)
    # 不传 icon：Windows 后端要的是 .ico，给 SVG 会让 System.Drawing.Icon 抛异常（实测崩在窗口线程里）
    webview.start(private_mode=False, storage_path=str(profile), debug=args.debug)
    closing.set()
    sup.shutdown()
    console.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

