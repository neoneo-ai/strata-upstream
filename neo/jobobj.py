"""neo/jobobj.py - Windows：Neo 退出时，它启动的 Strata 服务（和引擎、视觉编码器）跟着退出。

Neo 关掉窗口、被任务管理器结束、自己崩溃时都不会运行任何收尾代码，子进程就留在机器上继续占着
显存。contain(proc) 把子进程放进一个"最后一个句柄关闭时杀掉里面所有进程"的 Job Object，而那个
句柄只有 Neo 自己持有 —— 无论 Neo 怎么结束，系统都会把它关掉。子进程之后自己启动的进程
（Strata 的引擎、视觉编码器、MCP 服务）会一起进同一个 Job。

这是 serve/winjob.py 的同一套做法，Neo 自己实现一份：Neo 是这些进程的父，Job 归 Neo 所有，
上游文件不改。非 Windows、或 Job 建不出来时，contain 是无害的 no-op。
"""
from __future__ import annotations

import os
import threading

_job = None
_lock = threading.Lock()

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount",
                                                      "OtherOperationCount", "ReadTransferCount",
                                                      "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JobObjectExtendedLimitInformation = 9

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    _k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)

    class _PowerThrottling(ctypes.Structure):
        _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]

    _k32.SetProcessInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)

    def _make_job():
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(job, _JobObjectExtendedLimitInformation, ctypes.byref(info),
                                            ctypes.sizeof(info)):
            _k32.CloseHandle(job)
            return None
        return job

    def no_throttle(handle) -> bool:
        """让进程退出 Windows 的节能节流（EcoQoS）：窗口最小化时模型不该被挪到 E-core 上。"""
        for mask in (0x1 | 0x4, 0x1):
            info = _PowerThrottling(1, mask, 0)
            if _k32.SetProcessInformation(handle, 4, ctypes.byref(info), ctypes.sizeof(info)):
                return True
        return False
else:
    def no_throttle(handle) -> bool:
        return False


def contain(proc) -> bool:
    """把一个已启动的 subprocess.Popen 放进 kill-on-close 的 Job。进去了回 True，进不去回 False。"""
    global _job
    if os.name != "nt" or proc is None:
        return False
    with _lock:
        if _job is None:
            _job = _make_job() or 0
        if not _job:
            return False
    try:
        handle = int(proc._handle)
        no_throttle(handle)
        return bool(_k32.AssignProcessToJobObject(_job, handle))
    except (AttributeError, OSError, ValueError):
        return False


def close() -> None:
    """显式结束 Job：Job 里的进程（Strata 服务与它的引擎）全部被杀。Neo 正常停止时调用。"""
    global _job
    with _lock:
        if _job:
            try:
                _k32.TerminateJobObject(_job, 0)
            except OSError:
                pass
            _k32.CloseHandle(_job)
            _job = 0


def listener_pid(port: int) -> int | None:
    """这个端口上的监听进程号（Windows）。Neo 接手一个不是自己启动的 Strata 时用它认进程。"""
    if os.name != "nt":
        return None
    import ctypes.wintypes as wt
    _iphlp = ctypes.WinDLL("iphlpapi", use_last_error=True)

    class _Row(ctypes.Structure):
        _fields_ = [("addr", wt.ULONG), ("port", wt.ULONG), ("state", wt.ULONG),
                    ("owner", wt.ULONG)]

    size = wt.ULONG(0)
    _iphlp.GetExtendedTcpTable(None, ctypes.byref(size), True, 6, 4, 0)   # AF_INET, TCP_TABLE_OWNER_PID_ALL
    buf = ctypes.create_string_buffer(size.value)
    if _iphlp.GetExtendedTcpTable(buf, ctypes.byref(size), True, 6, 4, 0):
        return None
    count = int.from_bytes(buf[:4], "little")
    row = _Row()
    for i in range(count):
        ctypes.memmove(ctypes.byref(row), buf[4 + i * ctypes.sizeof(row):], ctypes.sizeof(row))
        if (row.port & 0xFFFF) == port and row.state == 2:               # MIB_TCP_STATE_LISTEN
            return int(row.owner) or None
    return None
