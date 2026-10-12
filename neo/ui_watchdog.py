"""neo/ui_watchdog.py - 窗口"未响应"时，把每个线程卡在哪一行写进日志。

pywebview 6.2.1 的 Windows 后端把窗口开在一个专用 STA 线程上跑 app.Run()
（platforms/winforms.py 的 create_window：ThreadStart(create) + ApartmentState.STA），
Python 主线程只在 thread.Join(500) 里转。页面里一次 js_api 调用的完整路子是：

    点按钮 -> WebView2 PostWebMessageAsJson
           -> on_script_notify（在 UI 线程上，platforms/edgechromium.py:225）
           -> js_bridge_call 起线程跑 Shell 的方法（util.py:247）
           -> 方法返回后 window.evaluate_js(...) 把结果送回页面
           -> evaluate_js 里 self.webview.Invoke(...) 同步投递给 UI 线程，
              然后 semaphore.acquire() 等它跑完（platforms/edgechromium.py:137）

Invoke 要 UI 线程在消息循环里取；取到之后跑的是 Python 委托，还要拿到 GIL。UI 线程
长时间拿不到 GIL（别的线程在跑一大段纯 Python）、或者被 print 卡在控制台上，消息循环
就停住 —— 标题栏出现"(未响应)"，窗口点什么都没了。

本模块的探法就是上面那条路本身：window.evaluate_js("1") 必须等 UI 线程应答。
探到超时，就把每个 Python 线程当前停在哪一帧逐行写进 neo/state/logs/manager.log，
"卡死"从一句描述变成日志里的一个位置。探针只在后台线程上跑，不影响窗口。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from . import console_io, state

#: 探一次用的脚本：越短越好，UI 线程要跑的就是它。
PROBE_SCRIPT = "1"
#: 多久探一次。
INTERVAL_S = 2.0
#: 探出去多久没回音算 UI 线程卡住。
STALL_S = 3.0
#: 同时在飞的探针上限：UI 线程一直不应答时，探针会卡在 Invoke 里，不能让它堆。
MAX_INFLIGHT = 4
#: 两次线程现场之间的最短间隔，卡住时不把日志刷满。
DUMP_COOLDOWN_S = 30.0
#: 每个线程记几帧。state.log 单行裁到 LINE_MAX，帧数多了会被裁掉。
FRAME_DEPTH = 6


def thread_frames(limit: int = FRAME_DEPTH) -> list[str]:
    """每个 Python 线程现在停在哪一帧（sys._current_frames()），一条线程一行、都裁过。"""
    frames = sys._current_frames()
    out = []
    for t in threading.enumerate():
        parts = [t.name]
        frame = frames.get(t.ident)
        depth = 0
        while frame is not None and depth < limit:
            code = frame.f_code
            parts.append(f"{Path(code.co_filename).name}:{frame.f_lineno} {code.co_name}")
            frame = frame.f_back
            depth += 1
        out.append(console_io.clean_line(" <- ".join(parts)))
    return out


def start(window, interval_s: float = INTERVAL_S, stall_s: float = STALL_S):
    """起看门狗，回用来叫停的 Event；window 是 None 就不起。

    探针线程调 window.evaluate_js("1")：这条调用会同步投递给 UI 线程并等它跑完，
    所以它不回来说明 UI 线程没在收消息。等超过 stall_s 就认定卡住，记一次线程现场。
    """
    if window is None:
        return None

    stop = threading.Event()
    inflight = [0]
    stalls = [0]
    last_dump = [0.0]

    def loop():
        while not stop.wait(interval_s):
            if inflight[0] >= MAX_INFLIGHT:
                continue                                  # 探针已经卡在 UI 线程上，不再堆
            done = threading.Event()
            inflight[0] += 1

            def probe():
                try:
                    window.evaluate_js(PROBE_SCRIPT)
                except Exception:
                    pass
                finally:
                    inflight[0] -= 1
                    done.set()

            threading.Thread(target=probe, daemon=True, name="neo-ui-probe").start()
            if done.wait(stall_s):
                continue

            stalls[0] += 1
            now = time.monotonic()
            if now - last_dump[0] < DUMP_COOLDOWN_S:
                continue
            last_dump[0] = now
            state.log(f"UI 线程 {stall_s:.0f} 秒没应答（累计 {stalls[0]} 次）。"
                      f"探针在 window.evaluate_js 里等 UI 线程收消息。线程现场：")
            for line in thread_frames():
                state.log(f"  {line}")

    threading.Thread(target=loop, daemon=True, name="neo-ui-watchdog").start()
    return stop
