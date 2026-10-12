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

    _k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    _k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    _k32.Process32FirstW.restype = wintypes.BOOL
    _k32.Process32FirstW.argtypes = (ctypes.c_void_p, wintypes.LPVOID)
    _k32.Process32NextW.restype = wintypes.BOOL
    _k32.Process32NextW.argtypes = (ctypes.c_void_p, wintypes.LPVOID)

    class _ProcessEntry(ctypes.Structure):        # PROCESSENTRY32W
        _fields_ = [("dwSize", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD), ("th32CntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("th32ClsId", wintypes.DWORD),
                    ("th32PriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260),      # WCHAR[MAX_PATH]
                    ("_tail", ctypes.c_byte * 8)]             # 补齐到 568

    _PE_SIZE = ctypes.sizeof(_ProcessEntry)      # Windows 要 dwSize 等于它（实测 568；560 报 ERROR_BAD_LENGTH）

    def parent_map() -> dict | None:
        """所有进程的 {进程号: 父进程号}（Windows 快照）。查不到回 None。

        进程号取在偏移 8、父进程号取在偏移 32：实测 2026-10-12 在这台机器上，
        Process32FirstW/NextW 写进缓冲区的不是头文件里那套偏移（按 4/24 取会全读到 0）。
        取到的表里没有自己这个进程就当没查到 —— 一张不可信的表会把自家服务判成外人的。
        """
        snap = _k32.CreateToolhelp32Snapshot(0x2, 0)      # TH32CS_SNAPPROCESS
        if snap is None or snap in (-1, ctypes.c_void_p(-1).value):
            return None
        try:
            entry = ctypes.create_string_buffer(_PE_SIZE)
            ctypes.cast(entry, ctypes.POINTER(wintypes.DWORD))[0] = _PE_SIZE
            out = {}
            ok = _k32.Process32FirstW(snap, entry)
            while ok:
                raw = bytes(entry.raw)
                out[int.from_bytes(raw[8:16], "little")] = int.from_bytes(raw[32:36], "little")
                ok = _k32.Process32NextW(snap, entry)
            return out if len(out) >= 20 and os.getpid() in out else None
        finally:
            _k32.CloseHandle(ctypes.c_void_p(snap))
else:
    def no_throttle(handle) -> bool:
        return False

    def parent_map() -> dict | None:
        return None


def is_descendant(pid: int, root: int, max_depth: int = 24) -> bool | None:
    """pid 是不是 root 的后代（进程树往上走）。查不到回 None —— 不能据此下结论。

    判断"端口上的服务是不是 Neo 自己起的"必须用它，不能只比"监听者 == Neo 的孩子"：
    启动服务的那个 python 自己会再拉一个进程去跑脚本。实测 2026-10-12 在
    D:\\Work\\Strata：venv 的 .venv\\Scripts\\python.exe 是跳板，启动它回的是它自己
    （42016），真正跑脚本的是它的孩子（42388，base 解释器）。Neo 那台跑着的服务
    进程树就是 NeoStrata 40856 → NeoStrata 46380 → 跳板 python 46984 → 绑上 8080 的
    45868（46984 的孩子，命令行一模一样）。谁要按"监听者不是我孩子就是别人的服务"
    写代码，就会把 Neo 自己正在应答的服务判成外人的、然后把它杀掉。
    """
    if int(pid) == int(root):
        return True
    parents = parent_map()
    if parents is None:
        return None
    cur, depth = int(pid), 0
    while cur in parents and depth < max_depth:
        cur = int(parents[cur])
        if cur == int(root):
            return True
        if cur == 0:
            return False
        depth += 1
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
    """这个端口上的监听进程号（Windows）。Neo 接手一个不是自己启动的 Strata 时用它认进程。

    实测 2026-10-12：原来那次调用参数全错了——ulAf 传了 6（AF_INET 是 2）、表类传了 4
    （TCP_TABLE_OWNER_PID_CONNECTIONS，里面根本没有 LISTEN 行，要的是 5 =
    TCP_TABLE_OWNER_PID_ALL）、行结构按 4 个字段读（MIB_TCPROW_OWNER_PID 是 6 个，
    state 在最前）、端口直接比对（表里的端口是网络字节序）。结果这个函数在 Windows 上
    一直返回 None，接手的服务在状态页上一直没有进程号。
    """
    if os.name != "nt":
        return None
    import ctypes.wintypes as wt
    _iphlp = ctypes.WinDLL("iphlpapi", use_last_error=True)

    class _Row4(ctypes.Structure):        # MIB_TCPROW_OWNER_PID
        _fields_ = [("state", wt.ULONG), ("local_addr", wt.ULONG), ("local_port", wt.ULONG),
                    ("remote_addr", wt.ULONG), ("remote_port", wt.ULONG), ("owner", wt.ULONG)]

    class _Row6(ctypes.Structure):        # MIB_TCP6ROW_OWNER_PID
        _fields_ = [("local_addr", wt.BYTE * 16), ("local_port", wt.USHORT), ("state", wt.LONG),
                    ("remote_addr", wt.BYTE * 16), ("remote_port", wt.USHORT), ("owner", wt.LONG)]

    def scan(cls, af: int):
        size = wt.ULONG(0)
        _iphlp.GetExtendedTcpTable(None, ctypes.byref(size), True, af, 5, 0)   # 122=缓冲区不够
        if size.value < 4:
            return []
        buf = ctypes.create_string_buffer(size.value)
        if _iphlp.GetExtendedTcpTable(buf, ctypes.byref(size), True, af, 5, 0):
            return []
        step = ctypes.sizeof(cls)
        count = min(int.from_bytes(buf[:4], "little"), (len(buf) - 4) // step)
        row, out = cls(), []
        for i in range(count):
            ctypes.memmove(ctypes.byref(row), buf[4 + i * step:4 + (i + 1) * step], step)
            out.append((int(row.state), int(row.local_port) & 0xFFFF, int(row.owner)))
        return out

    want = ((port & 0xFF) << 8) | ((port >> 8) & 0xFF)      # 表里的端口是网络字节序
    for state, local, owner in scan(_Row4, 2) + scan(_Row6, 23):   # AF_INET, AF_INET6
        if state == 2 and local == want:                     # 2 = MIB_TCP_STATE_LISTEN
            return owner or None
    return None
