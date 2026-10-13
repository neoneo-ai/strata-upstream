"""neo/supervisor.py - 进程监督：启动 Strata 服务、探活、崩溃自动重启、熔断。

状态机：

    idle ──start()──> starting ──/health 应答──> running
                        │                          │
                        │ 加载超时                  │ 子进程非预期退出
                        ▼                          ▼
                   restarting（退避 5/15/45 秒）<──┘
                        │
                        └── 10 分钟内已经重启 3 次 ──> halted（熔断，等人工处理）

三条规则：
  1. 用户点"停止"（intent=stop）后，任何情况下都不会自动拉起。
  2. 上游自己会重启引擎（serve/server.py 的 StrataEngine.restart / #344），那只显示一条提示，
     Neo 不干预 —— 模型重载由上游负责，Neo 只管 serve/server.py 这个进程。
  3. 端口上已经有一个活着的服务（比如用户先用 START-HERE.bat 起来了），Neo 认下它、不重复启动。
     Neo 没有自己进程的那段时间也一直在盯这个端口：端口上有活着的服务，状态页就说
     "运行中（外部启动）"（external），日志页指向它的日志 —— 不能写"未启动"。

退避与熔断的默认值（5s / 15s / 45s，10 分钟窗口内最多 3 次）与参考实现
ComfyUI-Neo-Nodes 的桌面壳一致，测试里可以调小。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import deque

from . import jobobj, state

#: 状态取值
IDLE, STARTING, RUNNING, RESTARTING, STOPPING, HALTED, EXTERNAL = (
    "idle", "starting", "running", "restarting", "stopping", "halted", "external")

BACKOFF_S = (5.0, 15.0, 45.0)      # 第 1/2/3 次重启前等多久
WINDOW_S = 600.0                   # 数重启次数的窗口：10 分钟
MAX_RESTARTS = 3                   # 窗口内最多自动重启几次，超过就熔断
POLL_S = 1.0                       # 看子进程还活着没有
PROBE_S = 2.0                      # 问 /health 的间隔
WATCH_S = 2.0                      # 没有自己的进程时，多久看一眼端口上的外部服务

#: 日志里出现这些话时，给用户一句能照着做的解释（措辞对齐 docs/TROUBLESHOOTING.md）
DIAGNOSIS = (
    ("out of memory", "内存不够：把 Windows 的页面文件设成系统管理的大小，或换更小的模型"),
    ("failed to map", "显存不够：关掉占显存的程序，或换更小的模型"),
    ("page file", "页面文件被设成了固定大小：改成「系统管理的大小」，重启电脑"),
    ("already in use", "端口被占用：在设置里换一个端口，或先关掉另一个 Strata 窗口"),
    ("driver is too old", "显卡驱动太旧：装 NVIDIA 572 以上的驱动"),
    ("no device", "没找到显卡：确认机器有 NVIDIA 显卡并装了驱动"),
    ("not enough space", "磁盘空间不够：清出模型需要的大小"),
    ("no such file", "模型文件不在：重新运行 setup.py 下载模型"),
    ("Traceback", "服务自己报了错：把下面的日志贴到 GitHub 的 issue"),
)


def diagnose(text: str) -> str:
    """从一段日志里挑第一条对得上的解释。"""
    low = (text or "").lower()
    for needle, advice in DIAGNOSIS:
        if needle in low:
            return advice
    return ""


class Supervisor:
    """监督一个 serve/server.py 进程。所有公开方法都可以从 UI 线程调用。"""

    def __init__(self, adapter, port: int, host: str = "127.0.0.1", api_key: str = "",
                 model_id: str = "", backoff=BACKOFF_S, window_s: float = WINDOW_S,
                 max_restarts: int = MAX_RESTARTS, poll_s: float = POLL_S,
                 probe_s: float = PROBE_S, watch_s: float = WATCH_S, on_event=None):
        self.adapter = adapter
        self.port = int(port)
        self.host = host
        self.api_key = api_key
        self.model_id = model_id
        self.backoff = tuple(backoff)
        self.window_s = float(window_s)
        self.max_restarts = int(max_restarts)
        self.poll_s = float(poll_s)
        self.probe_s = float(probe_s)
        self.watch_s = float(watch_s)
        self.on_event = on_event
        #: Neo 控制台的端口，由 neo/manager.py 在 console.start() 之后填。
        #: 状态页的监控页地址要靠它（走 Neo 的只读通道，见 engines/base.py 的 pages()）。
        self.console_port: int | None = None

        self._lock = threading.RLock()
        self._wake = threading.Event()          # 让退避等待能被"停止"打断
        self._thread = None
        self._proc = None
        self._plan = None
        self._log_file = None
        self._log_path = ""
        self._last_probe = 0.0
        self._probe = {}
        self._external = {}                       # 端口上别人启动的那个服务（探到的）
        self._ready_deadline = 0.0

        self.state = IDLE
        self.intent = "stop"                    # "run" = 用户要它跑着，"stop" = 用户要它停
        self.restarts: list[float] = []
        self.restart_number = 0
        self.last_exit = None                   # {code, at, expected}
        self.diag = ""
        self.started_at = 0.0
        self.events: deque = deque(maxlen=200)
        self.adopted_pid = None                 # 接手别人启动的服务时的进程号

    # ---------------- 对外：启动 / 停止 / 重试 / 换模型 ----------------
    def start(self, model_id: str | None = None) -> dict:
        """用户点"启动"。intent=run，监督线程跑起来。"""
        with self._lock:
            self.intent = "run"
            if model_id is not None:
                self.model_id = model_id
            self.diag = ""
            if self._thread is None or not self._thread.is_alive():
                self.state = STARTING if self.adapter.discover() else IDLE
                self._thread = threading.Thread(target=self._loop, daemon=True, name="neo-supervisor")
                self._thread.start()
            self._wake.set()
        return self.status()

    def stop(self, join_s: float = 10.0) -> dict:
        """用户点"停止"。intent=stop，之后绝不自动拉起。

        join_s 是等监督线程收尾的时间。页面里点停止走的是 pywebview 的 js_api 线程，
        等满 10 秒期间页面每 2 秒一次的轮询也拿不到这一条调用的回音（结果要靠
        evaluate_js 同步投递回 UI 线程），按钮看起来就是按了没反应。桌面壳传一个短的，
        剩下的收尾由监督线程自己走完，状态页靠轮询更新。
        """
        with self._lock:
            self.intent = "stop"
            self.state = STOPPING
        self._kill_proc("用户停止")
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=join_s)
        with self._lock:
            self.state = IDLE
            self._note("已停止（不会自动重启）")
        return self.status()

    def retry(self) -> dict:
        """熔断后的人工"重试"：清空重启计数，重新走一遍启动。"""
        with self._lock:
            self.restarts.clear()
            self.restart_number = 0
            self.diag = ""
            self.last_exit = None
        return self.start()

    def set_model(self, model_id: str) -> dict:
        """换模型：停掉当前的，用新配置启动。"""
        with self._lock:
            self.model_id = model_id
        self.stop()
        return self.start()

    def shutdown(self) -> dict:
        """Neo 退出前收尾：杀掉子进程并结束 Job Object。"""
        out = self.stop()
        jobobj.close()
        return out

    # ---------------- 对外：状态与日志 ----------------
    def status(self) -> dict:
        with self._lock:
            proc_alive = self._proc is not None and self._proc.poll() is None
            probe = dict(self._probe)
            # 端口上有一个活着的服务、Neo 手里没有进程（脚本先起来的，或熔断后用户自己
            # 起来的）：状态页要说"运行中（外部启动）"，不能写"未启动"。
            external_live = bool(self._external.get("healthy")) and not proc_alive
            display = self.state
            if external_live and self.state in (IDLE, STOPPING, HALTED):
                display = EXTERNAL
            return {
                "state": self.state,
                "display_state": display,
                "external": external_live,
                "neo_pid": os.getpid(),
                "intent": self.intent,
                "engine": self.adapter.name,
                "model": (self._plan.model if self._plan else "") or "",
                "label": (self._plan.label if self._plan else "") or "",
                "port": self.port,
                "host": self.host,
                "pid": (self._proc.pid if proc_alive else self.adopted_pid),
                "adopted": bool(self.adopted_pid) and not proc_alive,
                "process_alive": proc_alive,
                "listening": bool(probe.get("listening")),
                "healthy": bool(probe.get("healthy")),
                "loaded": bool(probe.get("loaded")),
                "served_model": probe.get("model", ""),
                "model_id": self.model_id,
                "max_context": probe.get("max_context", 0),
                "metrics": probe.get("metrics", {}),
                "restarts": list(self.restarts),
                "restart_number": self.restart_number,
                "max_restarts": self.max_restarts,
                "backoff": list(self.backoff),
                "last_exit": self.last_exit,
                "diagnosis": self.diag,
                "log_path": self._log_path,
                "started_at": self.started_at,
                "events": list(self.events)[-40:],
                "pages": self.adapter.pages(self.port, self.host, self.console_port),
                "installed": bool(self.adapter.discover()),
                "ready_timeout_s": (self._plan.ready_timeout_s if self._plan else 0),
            }

    def tail(self, max_bytes: int = 120_000, max_lines: int = state.TAIL_LINES) -> str:
        with self._lock:
            path = self._log_path
        return state.tail_text(path, max_bytes, max_lines) if path else ""

    # ---------------- 内部 ----------------
    def _note(self, text: str, kind: str = "info") -> None:
        item = {"at": time.time(), "text": text, "kind": kind}
        self.events.append(item)
        state.log(f"[{kind}] {text}")
        if self.on_event:
            try:
                self.on_event(item)
            except Exception:
                pass

    def _sleep(self, seconds: float) -> bool:
        """可被"停止"打断的等待。被打断回 True。"""
        return self._wake.wait(timeout=seconds)

    def _close_log(self) -> None:
        """子进程结束后关掉它的日志句柄（反复重启不能攒下一堆没关的文件）。"""
        with self._lock:
            f = self._log_file
            self._log_file = None
        if f is not None:
            try:
                f.close()
            except OSError:
                pass

    def _kill_proc(self, reason: str) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is None or proc.poll() is not None:
            self._close_log()
            return
        self._note(f"结束服务进程 {proc.pid}（{reason}）")
        try:
            proc.terminate()
        except OSError:
            self._close_log()
            return
        for _ in range(80):
            if proc.poll() is not None:
                self._close_log()
                return
            time.sleep(0.1)
        self._note("进程没响应 terminate，直接 kill", "warn")
        try:
            proc.kill()
        except OSError:
            pass
        self._close_log()

    def _launch(self) -> bool:
        """按计划启动子进程。成功回 True。"""
        from . import state as _state
        try:
            plan = self.adapter.plan(self.port, self.host, self.api_key, self.model_id)
        except (FileNotFoundError, RuntimeError) as e:
            with self._lock:
                self.diag = str(e)
            self._note(str(e), "error")
            return False
        log_path = plan.log_path or str(_state.server_log_path(plan.model))
        try:
            head = (f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动"
                    f"（第 {self.restart_number} 次自动重启）=====\n$ {' '.join(plan.argv)}\n")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(head)
            log = open(log_path, "a", encoding="utf-8", buffering=1)
        except OSError as e:
            with self._lock:
                self.diag = f"日志文件写不了：{e}"
            self._note(self.diag, "error")
            return False
        flags = 0x08000000 if os.name == "nt" else 0        # CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(plan.argv, cwd=plan.cwd, env=plan.env, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
        except OSError as e:
            with self._lock:
                self.diag = f"起不来：{e}"
            self._note(self.diag, "error")
            return False
        in_job = jobobj.contain(proc)
        with self._lock:
            self._proc = proc
            self._plan = plan
            self._log_file = log
            self._log_path = log_path
            self._ready_deadline = time.time() + plan.ready_timeout_s
            self.started_at = time.time()
            self.state = STARTING
            self.adopted_pid = None
            self._last_probe = 0.0      # 刚起来的进程：第一探不排队，就绪要马上看得见
        self._note(f"启动 {plan.label}（端口 {plan.port}，进程 {proc.pid}"
                   f"{'' if in_job else '，Job Object 没建起来'}）")
        return True


    def _adopt(self) -> bool:
        """端口上已有活着的服务：认下它，不重复启动。"""
        probe = self.adapter.probe(self.port, self.api_key)
        if not probe.get("healthy"):
            return False
        with self._lock:
            self._probe = probe
            self._last_probe = time.time()   # 刚探过：下一探按 probe_s 的节奏，不排队
            self.state = RUNNING
            self.adopted_pid = jobobj.listener_pid(self.port)
            self.started_at = time.time()
            self._ready_deadline = 0.0
            try:
                self._plan = self.adapter.plan(self.port, self.host, self.api_key, self.model_id)
                self._log_path = self._plan.log_path
            except (FileNotFoundError, RuntimeError) as e:
                self._plan = None
                self.diag = str(e)
            # 接手的是脚本先起来的服务：它一直在写的是配置里 log 键那个文件（实测 2026-10-12
            # 在 D:\Work\Strata：strata-swift-iq3_xxs.json 的 log 指向
            # D:\Work\Strata\strata-swift-iq3_xxs.log，5.1 MB 还在长）。Neo 自己的
            # server-*.log 里一个字节都没有，日志页指到那儿就是空的（用户的原话是
            # "日志没有看到"）。
            external_log = self.adapter.log_path_for(self.model_id)
            if external_log:
                self._log_path = external_log
            self._external = {}
        self._note(f"接手已在运行的服务：{probe.get('model')}（端口 {self.port}）")
        return True

    def _exit_of(self, proc) -> dict:
        """子进程已经退出（poll() 非 None）时的退出信息。"""
        code = proc.returncode
        with self._lock:
            self._proc = None
        self._close_log()
        return {"code": code, "at": time.time(), "reason": "", "tail": self.tail(4000)}

    def _monitor(self):
        """盯到进程结束 / 接手的服务消失 / 加载超时。返回退出信息；None = intent 已变。

        探活按 `probe_s` 的节奏问，进程死活按 `poll_s` 的节奏看。之前是每一轮都探一次：
        一次探活最坏是 tcp_open 1.2 秒 + /health 4.0 秒 + /metrics 4.0 秒，而 `proc.poll()`
        只在两轮之间看，于是子进程死了要等这轮探活跑完才看得见。实测 2026-10-13
        （neo/state/tmp_events.py，杀掉服务后看监督自己的 events 时间戳）：发现退出
        0.01 秒与 2.05 秒交替出现，上一轮量到 4.13 秒——用户看到的就是"进程已经崩了，
        状态页还写着运行中，重启要等好几秒"。
        """
        misses = 0
        while True:
            with self._lock:
                if self.intent != "run":
                    return None
                proc = self._proc
                deadline = self._ready_deadline
                st = self.state
            if proc is not None and proc.poll() is not None:
                return self._exit_of(proc)
            with self._lock:
                due = time.time() - self._last_probe >= self.probe_s
                if due:
                    self._last_probe = time.time()
            if due:
                probe = self.adapter.probe(self.port, self.api_key)
                with self._lock:
                    self._probe = probe
                    if probe.get("healthy"):
                        misses = 0
                        self._ready_deadline = 0.0
                        if st in (STARTING, RESTARTING):
                            self.state = RUNNING
                            self._note(f"服务就绪：{probe.get('model')}"
                                       f" · 上下文 {probe.get('max_context')}")
                    elif st == RUNNING and proc is None:
                        # /health 一时不答不等于服务没了：服务正在生成时 GIL 被占满，
                        # 几秒不答是常态。只有端口真的不听了才算"接手的服务不见了"。
                        # 实测 2026-10-12 在 D:\Work\Strata：run-swift-iq3_xxs.bat 起的服务在
                        # 8080 上跑得好好的，6 秒里 3 次 /health 没答就被判成不见了，
                        # Neo 接着把它重启了一遍（用户看到的就是"状态不对"）。
                        misses = misses + 1 if not self.adapter.listening(self.port) else 0
                # 探活跑完的这几秒里子进程可能正好死了：马上看一眼，不要等下一轮
                if proc is not None and proc.poll() is not None:
                    return self._exit_of(proc)
            if proc is None and misses >= 3:
                return {"code": None, "at": time.time(), "reason": "接手的服务不见了",
                        "tail": self.tail(4000)}
            if st in (STARTING, RESTARTING) and deadline and time.time() > deadline:
                tail = self.tail(4000)
                self._kill_proc("加载超时")
                return {"code": None, "at": time.time(), "reason": "加载超时", "tail": tail}
            self._sleep(self.poll_s)

    def _schedule_restart(self, exit_info) -> bool:
        """这次非预期退出该不该自动重启。要重启回 True（退避时间已等完）。"""
        now = time.time()
        with self._lock:
            self.restarts = [t for t in self.restarts if now - t < self.window_s]
            self.restarts.append(now)
            n = len(self.restarts)
            self.restart_number = n
            self.last_exit = {"code": exit_info.get("code"), "at": now,
                              "reason": exit_info.get("reason", "")}
        self._note(f"服务退出（{exit_info.get('reason') or '退出码 ' + str(exit_info.get('code'))}）",
                   "warn")
        advice = diagnose(exit_info.get("tail", ""))
        with self._lock:
            if n > self.max_restarts:
                self.state = HALTED
                self.diag = advice or "服务连续崩溃：看日志找原因，或换更小的模型"
                self._note(f"{self.window_s / 60:.0f} 分钟内自动重启已达 {self.max_restarts} 次，"
                           f"停止自动重启", "error")
                return False
            delay = self.backoff[min(n - 1, len(self.backoff) - 1)]
            self.state = RESTARTING
            if advice:
                self.diag = advice
                self._note(advice, "hint")
            self._note(f"{delay:.0f} 秒后自动重启（第 {n}/{self.max_restarts} 次）", "warn")
        self._sleep(delay)                       # 退避等待；期间用户点"停止"会立刻打断
        with self._lock:
            if self.intent != "run":
                self.state = IDLE
                return False
        return True

    def _watch_external(self) -> bool:
        """Neo 手里没有进程时盯端口：端口上有一个活着的服务（脚本先起来的），状态页
        就要说"运行中（外部启动）"，日志页指向它的日志，点启动时认下它。

        实测 2026-10-12 在 D:\\Work\\Strata：run-swift-iq3_xxs.bat 起的服务在 8080 上
        正常应答 /health，Neo 熔断之后状态页写"未启动"、日志页是空的 —— 用户的话是
        "我感觉已经启动了，只是日志没有看到，状态也不对"。

        回 True：该回主循环（intent 要 run）；回 False：用户要它停着，线程收尾。
        """
        while True:
            with self._lock:
                if self.intent == "stop":
                    self._external = {}
                    return False
                if self._proc is not None and self._proc.poll() is None:
                    return True
            probe = self.adapter.probe(self.port, self.api_key)
            with self._lock:
                self._probe = probe
                self._external = probe if probe.get("healthy") else {}
                if self._external:
                    self.state = EXTERNAL
                    self.adopted_pid = self.adopted_pid or jobobj.listener_pid(self.port)
                    external_log = self.adapter.log_path_for(self.model_id)
                    if external_log:
                        self._log_path = external_log
                    if self.intent == "run":
                        # 用户要它跑着、端口上有一个活着的服务：交回主循环去 `_adopt` 认下它。
                        # 接手的动作（记 adopted_pid、把日志页指到服务的日志、进 running）在
                        # `_adopt` 里，盯端口这一层替它做不了。之前这一条没有，Neo 就永远停在
                        # "运行中（外部启动）"，点启动不会接手（实测 2026-10-13：把 tcp_open 的
                        # 超时从 1.2 秒收紧到 0.25 秒后 test_adopt_shows_config_log 就输了 ——
                        # 那 1.2 秒正好是给外部服务的启动窗口，窗口没了这条路径就断了）。
                        return True
            self._sleep(self.watch_s)

    def _loop(self) -> None:
        while True:
            with self._lock:
                intent = self.intent
            if intent != "run":
                with self._lock:
                    self.state = IDLE
                if not self._watch_external():
                    return
            if not self._adopt():
                if not self._launch():
                    with self._lock:
                        self.state = HALTED
                    # 起不来（配置缺东西）也要继续盯端口：端口上可能有一个脚本起来的
                    # 服务在跑，说"未启动"是骗人的
                    if not self._watch_external():
                        return
                    continue
            exit_info = self._monitor()
            if exit_info is None:
                continue
            with self._lock:
                if self.intent != "run":
                    self.state = IDLE
                    return
            if not self._schedule_restart(exit_info):
                return

