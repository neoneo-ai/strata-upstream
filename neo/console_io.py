"""neo/console_io.py - 控制台输出闸门：太长、太重复、太快的输出不让它刷满窗口。

Neo 的窗口是 NEO-START.bat 开的控制台，进程写 stdout/stderr 的一切都在里面，包括
第三方库写的。实测两类会把窗口刷满：

1. pywebview 6.2.1 的 Windows(WinForms) 后端在打开真实页面时，经 pythonnet 往 stderr
   打一条 WinForms 属性链递归的内部日志（ActiveControl...SyncRoot...GenericSansSerif），
   结尾是 maximum recursion depth exceeded —— 一条几十万字符的行（见
   docs/NEO_DESKTOP.md「pywebview 6.2.1 上的实测坑」）。".SyncRoot" 是 .NET 的
   ISynchronizeInvoke.SyncRoot，pythonnet 取属性链时把它拼成点号串。
2. 日志行里成串的 \r（neo/state.py 里修掉的那条）：控制台里每个 \r 把光标送回行首，
   后面的文字从第 0 列重画，一屏全是重叠。

闸门做四件事，正常输出不受影响：单行裁到 LINE_MAX；控制字符换成空格（\r 当换行）；
DUP_WINDOW_S 内连续同样的行只打一次并计数；每秒超过 RATE_PER_S 行就丢弃并说明丢了多少。

install() 要在 import webview 之前调用：logging 的 StreamHandler 抓的是当时
sys.stderr 的那个对象，装晚了就管不住它的输出。
"""
from __future__ import annotations

import re
import sys
import threading
import time

#: 单行最长。一条 RecursionError 的消息有几十万字符，裁到这一屏看得完。
LINE_MAX = 400
#: 每秒最多写出的行数。
RATE_PER_S = 30
#: 多久之内连续同样的行算重复。
DUP_WINDOW_S = 5.0

#: 控制字符：\r(0x0d) 单独处理（当换行），其余换成空格。
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_line(text, line_max: int = LINE_MAX) -> str:
    """一行能安全地打到控制台的样子：换行与控制字符换成空格，裁到 line_max。

    \r 单独处理：控制台里它把光标送回行首，后面的文字从第 0 列重画，满屏重叠。
    """
    text = str(text).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    text = _CONTROL.sub(" ", text)
    if len(text) > line_max:
        return text[:line_max] + f" …[截断，原长 {len(text)} 字符]"
    return text


class BoundedStream:
    """包一层 stdout / stderr。write() 按行走闸门，其余属性透传给真流。"""

    def __init__(self, stream, line_max: int = LINE_MAX, rate_per_s: float = RATE_PER_S,
                 dup_window_s: float = DUP_WINDOW_S):
        self._stream = stream
        self._line_max = int(line_max)
        self._rate = float(rate_per_s)
        self._dup_window = float(dup_window_s)
        self._lock = threading.RLock()
        self._buf = ""
        self._last = ""
        self._last_at = 0.0
        self._dup = 0
        self._dropped = 0
        self._busy = 0
        self._stamp = time.monotonic()
        self._budget = float(rate_per_s)

    def __getattr__(self, name):                      # flush / fileno / encoding / ...
        return getattr(self._stream, name)

    def writable(self) -> bool:
        return True

    def write(self, text) -> int:
        if not isinstance(text, str):
            text = str(text)
        if not self._lock.acquire(blocking=False):
            # 另一个线程正卡在真流的写上（控制台被 QuickEdit 选中、管道满、杀软拦句柄）。
            # 排队等锁会把调用方一起挂住 —— pywebview 的 on_script_notify 就是在窗口
            # 线程上跑的（platforms/edgechromium.py:225，里面的 print 走的就是这里），
            # 一挂住消息循环就停，标题栏变成"(未响应)"。这一行丢掉，窗口不能陪它等。
            self._busy += 1
            return len(text)
        try:
            self._buf += text.replace("\r\n", "\n").replace("\r", "\n")
            lines = self._buf.split("\n")
            self._buf = lines.pop()                   # 末尾半行留着，等下一段
            for line in lines:
                self._emit(line)
        finally:
            self._lock.release()
        return len(text)

    def flush(self) -> None:
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self._buf:
                line, self._buf = self._buf, ""
                self._emit(line)
            self._flush_counters()
            self._stream.flush()
        finally:
            self._lock.release()

    # ---------------- 内部 ----------------
    def _refill(self) -> None:
        now = time.monotonic()
        self._budget = min(self._rate, self._budget + (now - self._stamp) * self._rate)
        self._stamp = now

    def _emit(self, line: str) -> None:
        line = clean_line(line, self._line_max)
        now = time.monotonic()
        if line == self._last and (now - self._last_at) <= self._dup_window:
            self._dup += 1
            self._last_at = now
            return
        self._refill()
        if self._budget < 1.0:
            self._dropped += 1
            self._last, self._last_at, self._dup = line, now, 0
            return
        self._flush_counters()          # 只在真要写一行之前把攒下的计数说明一次
        self._budget -= 1.0
        self._stream.write(line + "\n")
        self._last, self._last_at, self._dup = line, now, 0

    def _flush_counters(self) -> None:
        if self._dup:
            self._stream.write(f"…[同样的行重复 {self._dup} 次，已折叠]\n")
            self._dup = 0
        if self._dropped:
            self._stream.write(f"…[输出过快，丢弃 {self._dropped} 行]\n")
            self._dropped = 0
        if self._busy:
            self._stream.write(f"…[控制台写不动，丢弃 {self._busy} 行]\n")
            self._busy = 0


class NullStream:
    """窗口版 exe（PyInstaller 的 --noconsole）里 sys.stdout / sys.stderr 是 None。

    不包一层的话，print() 直接抛 AttributeError（None 没有 write）。Neo 的输出本来
    就该进 neo/state/logs/manager.log，控制台没有就没进去，这里把它安静地吞掉。
    """

    encoding = "utf-8"

    def write(self, text):
        return len(text) if isinstance(text, str) else len(str(text))

    def writelines(self, lines):
        return None

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("Neo 的 exe 没有控制台")


def install(line_max: int = LINE_MAX, rate_per_s: float = RATE_PER_S) -> None:
    """把 sys.stdout / sys.stderr 换成闸门。已经装过就不重复包。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:                     # 窗口版 exe：没有控制台，吞掉
            setattr(sys, name, NullStream())
        elif not isinstance(stream, BoundedStream):
            setattr(sys, name, BoundedStream(stream, line_max=line_max, rate_per_s=rate_per_s))


#: pywebview 的 logger 名（webview.frontend 等）与 pythonnet 的 logger 名。
QUIET_LOGGERS = ("pywebview", "webview", "webview.frontend", "pythonnet", "clr")


def quiet_gui_logging() -> None:
    """桌面壳那边（pywebview / pythonnet）的内部日志不打到控制台。"""
    import logging
    logging.getLogger("pywebview").setLevel(logging.CRITICAL)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.CRITICAL)
