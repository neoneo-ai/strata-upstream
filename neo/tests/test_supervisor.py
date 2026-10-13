"""neo/tests/test_supervisor.py - 监督器：启动、就绪、崩溃后不自动重启（给诊断）、停止不重启、接手外部服务。

跑的是真实进程（serve/server.py --engine mock），所以这条测试就是 M1 的核心验收。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

if __package__ in (None, ""):                       # 直接跑 python neo/tests/test_supervisor.py 时没有父包
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "neo.tests"

from ._bootstrap import ENGINE, REPO, server_ready              # noqa: E402

ROOT = REPO                                         # 老写法留下的名字：Neo 仓库的根（Neo 自己的文件）

#: 引擎的真服务（serve/server.py --engine mock）在这台机器上起得来吗。
#: 起不来时，用真的 serve/server.py 的那几条跳过并说明原因（原因见 _bootstrap.server_ready）。
SERVER_OK, SERVER_WHY = server_ready(ENGINE)

from neo import jobobj, state                                          # noqa: E402
from neo.engines.mock import MockEngine                        # noqa: E402
from neo.supervisor import (EXTERNAL, IDLE, RUNNING, STARTING,          # noqa: E402
                            Supervisor, diagnose)


class FastEngine(MockEngine):
    """起 neo/tests/mock_server.py 的适配器：真进程、真端口、真 HTTP、真 kill，
    但不 import 整个上游服务。

    实测 2026-10-13（这台机器，Windows 11，Defender 在扫磁盘）：起
    `serve/server.py --engine mock` 一次约 3.9 秒，起那个 40 行标准库服务一次约 0.3 秒；
    test_supervisor 里 11 次 spawn 就是 43 秒，占这个模块 51 秒里的绝大部分。
    监督的链路（启动 → 就绪 → 被杀 → 停着并给出诊断 → 点启动再起 → 停止不重启）一个字都不少，
    换的只是"被监督的那个进程里装了什么"。上游真行为（就绪行进日志、/health 的真字段）
    由 test_upstream_api.py 和 TestStartAndReady 里那条用真 serve/server.py 的用例守着。
    """
    name = "mock"                       # 状态页与断言里的引擎名不变
    entry = "neo/tests/mock_server.py"
    #: 这个适配器跑的是 Neo 自己的文件（neo/tests/mock_server.py），所以它的根是 Neo 仓库的根；
    #: 真的 MockEngine 跑的是上游的 serve/server.py，根是引擎根目录（见 SupervisorBase.make）。
    neo_root = True

    def plan(self, port: int, host: str = "127.0.0.1", api_key: str = "",
             model_id: str = "", gpu=None):
        from neo import state
        from neo.engines.base import LaunchPlan
        argv = [state.python_executable(self.root),
                str(self.root / "neo" / "tests" / "mock_server.py"), "--port", str(port)]
        if api_key:
            argv += ["--api-key", api_key]
        return LaunchPlan(argv=argv, cwd=str(self.root), env=None,
                          log_path=str(state.server_log_path("mock")), port=port, host=host,
                          model=model_id or "mock", engine=self.name,
                          label="mock 服务（测试）", ready_timeout_s=60)


class NoPlanEngine(MockEngine):
    """plan() 起不来的适配器（真实场景：配置里模型文件不在），日志路径指向配置的 log。

    用来测"Neo 自己起不动服务，端口上却有一个脚本先起来的服务"这一种状态。
    """
    name = "noplan"

    def plan(self, port, host="127.0.0.1", api_key="", model_id="", gpu=None):
        raise FileNotFoundError("strata-x.json 起不来：模型文件不在")

    def log_path_for(self, model_id=""):
        return str(self.root / "external.log")


class PlanOkEngine(MockEngine):
    """plan() 拼得出来、配置里有 log 键的适配器：接手服务时日志页要指到服务在写的那个 log。"""
    name = "planok"

    def log_path_for(self, model_id=""):
        return str(self.root / "external.log")


class BusyEngine(MockEngine):
    """端口在听、/health 一直不答（服务正在生成）：接手之后不能被判成"不见了"。"""
    name = "busy"

    def __init__(self, root, live: bool = True):
        super().__init__(root)
        self.live = live          # 测试中途改成 False，模拟端口上的服务真没了

    def probe(self, port, api_key=""):
        return {"engine": self.name, "port": port, "listening": self.live, "healthy": False,
                "loaded": False, "model": "", "max_context": 0, "metrics": {}}

    def listening(self, port):
        return self.live



def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_listening(port: int, timeout: float = 15.0) -> bool:
    """等端口上真的在听了，再去监督它。

    接手类的测试起的是上游的 serve/server.py。在 Neo 单独的仓库里那一棵树是刚按 pin
    clone 出来的（<repo>/strata），第一次 import 要过 Windows 杀毒扫描：实测 2026-10-13
    同一条 TestAdopt 在 4 路并发下 0.6 秒就判失败、单独重跑就过——不是监督器接不住，
    是端口那时候还没开始听。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


def dead_pid() -> int:
    """一个"确实开不出句柄"的进程号：跑一个立刻退出的子进程，把它的 pid 交出去。

    实测 2026-10-12（neo/state/repro/openprocess.py）：子进程退出后，只要父进程还握着
    那个进程句柄（subprocess.Popen 握着，收尾对象 Close(self._handle) 也握着），
    OpenProcess 对这个 pid 照样成功；句柄放掉后才失败（GetLastError 87）。
    所以 del + gc + 0.3 秒（那个收尾对象自己先睡 0.1 秒）才是"进程真的没了"的现场。
    """
    import gc
    import subprocess
    with open(os.devnull, "wb") as null:
        proc = subprocess.Popen([sys.executable, "-c", "pass"], stdout=null, stderr=null)
        proc.wait()
        pid = proc.pid
    del proc
    gc.collect()
    time.sleep(0.3)
    return pid


class SupervisorBase(unittest.TestCase):
    #: 默认被监督的进程：neo/tests/mock_server.py（原因见 FastEngine 的注释）
    engine_cls = FastEngine

    def make(self, engine=None, **kw):
        # 轮询间隔按"测试要跑得快"给：状态机每 0.05 秒看一次子进程、每 0.1 秒问一次
        # /health，就绪与崩溃都是立刻能看见的事，等的时间由条件决定（见 wait）。
        kw.setdefault("poll_s", 0.05)
        kw.setdefault("probe_s", 0.1)
        self.sup = Supervisor((engine or self.engine_cls)(
            ROOT if getattr(engine or self.engine_cls, "neo_root", False) else ENGINE),
            free_port(), **kw)
        return self.sup

    def tearDown(self):
        try:
            self.sup.shutdown()
        except Exception:
            pass
        # shutdown() 自己等子进程退出；这里只留一点时间给 Windows 放掉进程句柄
        time.sleep(0.1)

    def wait(self, pred, timeout=30.0, why=""):
        t0 = time.time()
        while time.time() - t0 < timeout:
            s = self.sup.status()
            if pred(s):
                return s
            time.sleep(0.05)
        self.fail(f"等不到 {why}；最后状态 {self.sup.status()}")


class TestStartAndReady(SupervisorBase):
    def test_start_to_running(self):
        # 这一条用真的 serve/server.py --engine mock：要验的就是"上游的就绪行真的进了日志"，
        # 那是 Neo 与上游的接缝，不能由 neo/tests/mock_server.py 替它作证（原因见 FastEngine）。
        if not SERVER_OK:
            self.skipTest(SERVER_WHY)
        sup = self.make(engine=MockEngine)
        sup.start()
        s = self.wait(lambda x: x["state"] == RUNNING and x["healthy"], why="服务就绪")
        self.assertTrue(s["process_alive"], "子进程应该在跑")
        self.assertTrue(s["pid"] > 0)
        self.assertEqual(s["engine"], "mock")
        self.assertTrue(s["max_context"] > 0, "/health 应该报上下文大小")
        self.assertTrue(Path(s["log_path"]).exists())
        self.assertIn("ready", state.tail_text(s["log_path"], 20000))   # 上游的就绪行进了日志

    def test_status_metrics_are_numbers(self):
        """状态页卡片读到的 metrics 要真是数字（真进程，mock 引擎）。

        实测 2026-10-12 用户的截图：五张卡片"—"、"请求 [object Object],[object Object]"。
        这条盯的就是那一层：值里不许有列表/对象，requests 是累计条数。
        """
        sup = self.make()
        sup.start()
        s = self.wait(lambda x: x["state"] == RUNNING and x["healthy"], why="服务就绪")
        m = s["metrics"]
        self.assertIsInstance(m, dict)
        self.assertEqual(m.get("active"), 0, "没有请求在跑")
        self.assertEqual(m.get("queued"), 0)
        self.assertIsInstance(m.get("requests"), int, "requests 是累计条数，不是请求记录列表")
        for key, value in m.items():
            self.assertNotIsInstance(value, (list, dict),
                                     f"卡片会把它写成 [object Object]：{key}={value!r}")
        if "gpu_used_mib" in m:
            self.assertGreater(m["gpu_used_mib"], 0)
        if "ram_total_gb" in m:
            self.assertGreater(m["ram_total_gb"], 0.0)
        self.assertNotIn("tokens_per_s", m, "空闲时不编一个生成速度出来")

    def test_events_recorded(self):
        sup = self.make()
        sup.start()
        self.wait(lambda x: x["state"] == RUNNING, why="服务就绪")
        texts = " ".join(e["text"] for e in sup.status()["events"])
        self.assertIn("启动", texts)
        self.assertIn("就绪", texts)


class TestSetModel(SupervisorBase):
    """模型页的"换过去"：服务在跑时直接换模型，用户不用先按停止。

    `set_model`（neo/supervisor.py）就是"记下新模型 + `stop()` + `start()`"。
    实测 2026-10-12（mock 真进程）：running 时点它，Neo 自己把服务停掉、用新模型重新起，
    进程号换了、状态回到 running，而且这一条走的是用户主动的启动，不记成非预期退出。
    """

    def test_set_model_while_running_restarts_on_its_own(self):
        sup = self.make()
        sup.start()
        first = self.wait(lambda x: x["state"] == RUNNING and x["healthy"], why="先跑起来")
        out = sup.set_model("mock-b")
        self.assertEqual(sup.model_id, "mock-b", "新模型要记下来")
        self.assertIn(out["state"], (STARTING, RUNNING), "点下去就进了换模型的流程")
        again = self.wait(lambda x: x["state"] == RUNNING and x["healthy"], why="换完模型重新就绪")
        self.assertNotEqual(first["pid"], again["pid"], "服务真的换了一个进程")
        self.assertIsNone(again["last_exit"], "用户主动换模型不该被记成一次非预期退出")


class TestCrashStops(SupervisorBase):
    """服务非预期退出后 Neo 不自动拉起：记下退出码与一句诊断就停着，要用户点"启动"。

    （2026-10-13 删了退避重启与熔断，理由见 neo/supervisor.py 开头第 2 条。这一格以前是
    全套件最慢的：14.7 秒 —— 四次杀掉进程加整张退避表；现在三条用例合起来约 2 秒。）
    """

    def kill(self):
        s = self.sup.status()
        proc = self.sup._proc
        self.assertIsNotNone(proc, "应该有一个由 Neo 启动的子进程")
        pid = proc.pid
        proc.kill()
        return pid

    def test_unexpected_exit_stops_and_is_reported(self):
        sup = self.make()
        sup.start()
        self.wait(lambda x: x["state"] == RUNNING, why="第一次就绪")
        self.kill()
        s = self.wait(lambda x: x["state"] == IDLE and x["last_exit"],
                      why="退出后要停着并记下退出码")
        self.assertIsNone(sup._proc, "不该有自动重启出来的新进程")
        self.assertIsNotNone(s["last_exit"]["code"], "要记下退出码")
        self.assertTrue(s["diagnosis"], "要给一句能照着做的提示")
        texts = " ".join(e["text"] for e in s["events"])
        self.assertIn("已停止，点「启动」重新起", texts, "事件里要说清下一步要用户点「启动」")
        # 0.6 秒是 poll_s 0.05 的十几轮：这段时间里会被自动拉起就一定看得见
        time.sleep(0.6)
        self.assertEqual(sup.status()["state"], IDLE, "崩了之后保持停着，不自动拉起")
        self.assertIsNone(sup._proc, "崩了之后不该有子进程")

    def test_start_after_a_crash_starts_again(self):
        """崩了停着之后，用户点"启动"要真的再起一个进程。

        旧代码做不到这一条：监督线程停在盯端口那一层（_watch_external）出不去，
        熔断后的"重试"点了等于没点。2026-10-13 删退避重启时一起修（_start_seq）。
        """
        sup = self.make()
        sup.start()
        first = self.wait(lambda x: x["state"] == RUNNING, why="第一次就绪")
        self.kill()
        self.wait(lambda x: x["state"] == IDLE and x["last_exit"], why="退出后要停着")
        sup.start()
        again = self.wait(lambda x: x["state"] == RUNNING and x["healthy"], why="点启动后要重新就绪")
        self.assertNotEqual(first["pid"], again["pid"], "重新起来的是新进程")
        self.assertIsNone(again["last_exit"], "重新就绪后不该还挂着上次退出的提示")

    def test_stop_never_autorestarts(self):
        sup = self.make()
        sup.start()
        self.wait(lambda x: x["state"] == RUNNING, why="就绪")
        sup.stop()
        s = sup.status()
        self.assertEqual(s["state"], IDLE)
        self.assertEqual(s["intent"], "stop")
        self.assertIsNone(sup._proc)
        # 1.0 秒是 poll_s 0.05 的二十几轮：这段时间里要是会被自动拉起，就一定看得见
        time.sleep(1.0)
        self.assertEqual(sup.status()["state"], IDLE, "停止后不能被自动拉起")


class TestAdopt(SupervisorBase):
    def test_adopts_a_live_server(self):
        """端口上已有活着的服务（用户先用 START-HERE.bat 起来了）：认下它，不重复启动。"""
        import subprocess
        if not SERVER_OK:
            self.skipTest(SERVER_WHY)
        port = free_port()
        proc = subprocess.Popen([state.python_executable(ENGINE), str(ENGINE / "serve" / "server.py"),
                                 "--engine", "mock", "--port", str(port)], cwd=str(ENGINE),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self.addCleanup(proc.kill)
        self.assertTrue(wait_listening(port), f"{port} 上该有一个在听的服务")
        sup = self.make()
        sup.port = port
        sup.start()
        s = self.wait(lambda x: x["state"] == RUNNING and x["healthy"], why="接手已在运行的服务")
        self.assertIsNone(sup._proc, "接手的进程不是 Neo 生的，Neo 不持有它")
        texts = " ".join(e["text"] for e in s["events"])
        self.assertIn("接手", texts, "要在事件里说明这是接手的服务")



class TestServiceOwnership(SupervisorBase):
    """Neo 自己的服务必须永远被判成"自家"的。

    实测 2026-10-12 在 D:\\Work\\Strata：Neo 的孩子 46984 是 venv 的跳板 python
    （.venv\\Scripts\\python.exe 启动它自己后，真正跑脚本的是它的孩子），8080 的监听者
    是 46984 的孩子 45868，命令行一模一样。谁要按"监听者不是我孩子就是外人的服务"写
    代码，就会把 Neo 自己正在应答的服务判成外人的、然后把它杀掉。
    """

    def test_own_listener_is_kin_of_my_proc(self):
        """Neo 起的服务：端口上的监听者（往往是我孩子的孩子）必须算自家。"""
        if os.name != "nt":
            self.skipTest("监听者的进程号只在 Windows 上查得到")
        sup = self.make()
        sup.start()
        s = self.wait(lambda x: x["state"] == RUNNING and x["healthy"], why="服务就绪")
        mine = sup._proc.pid
        owner = jobobj.listener_pid(sup.port)
        self.assertIsNotNone(owner, "端口上的监听进程要查得到")
        self.assertIsNot(jobobj.is_descendant(owner, mine), False,
                         "Neo 自己起的服务不能被判成外人的（监听者常是我孩子的孩子）")
        self.assertFalse(s["adopted"], "自己起的服务不算接手")
        self.assertTrue(s["process_alive"], "Neo 得留着它")

    def test_descendant_walks_the_real_tree(self):
        """进程树往上走：孙子算后代，祖先不算，自己算自己。

        用 sys._base_executable 起进程：venv 的 python.exe 是跳板，跳板进程跑完就没了，
        父链会断在它身上（serve/test_winjob.py 里也是这么绕的）。
        """
        import subprocess
        exe = getattr(sys, "_base_executable", None) or sys.executable
        code = ("import subprocess, sys, time\n"
                f"g = subprocess.Popen([{exe!r}, '-c', 'import time;time.sleep(12)'])\n"
                "print(g.pid, flush=True)\n"      # 不 flush 的话 readline 要等它退出才返回
                "time.sleep(6)\n")                # 断言要在父链还在的时候做完
        proc = subprocess.Popen([exe, "-c", code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        line = proc.stdout.readline().strip()
        self.assertTrue(line, "孙子进程要起得来")
        grandchild = int(line)
        self.addCleanup(lambda: subprocess.run(["taskkill", "/F", "/PID", str(grandchild)],
                                               capture_output=True))
        self.assertIs(jobobj.is_descendant(grandchild, os.getpid()), True, "孙子是我的后代")
        self.assertIs(jobobj.is_descendant(os.getpid(), grandchild), False, "祖先不是我的后代")
        self.assertIs(jobobj.is_descendant(os.getpid(), os.getpid()), True, "自己算自己")


class TestExternalService(unittest.TestCase):
    """端口上有一个脚本先起来的服务：Neo 要说"运行中（外部启动）"，日志页要有它的日志。

    实测 2026-10-12 在 D:\\Work\\Strata：run-swift-iq3_xxs.bat 起的服务在 8080 上正常
    应答 /health，Neo 停着的时候状态页写"未启动"、日志页是空的（用户的话是"我感觉已经
    启动了，只是日志没有看到，状态也不对"）。
    """

    @classmethod
    def setUpClass(cls):
        # 这三条起的是引擎的真 serve/server.py：起不动就整类跳过（原因见 _bootstrap.server_ready），
        # 不能让监督器一遍遍重启一个 import 就死的进程、把每条的等待跑满。
        if not SERVER_OK:
            raise unittest.SkipTest(SERVER_WHY)

    def make(self, adapter, port, **kw):
        kw.setdefault("poll_s", 0.05)
        kw.setdefault("probe_s", 0.1)
        kw.setdefault("watch_s", 0.15)
        self.sup = Supervisor(adapter, port, **kw)
        return self.sup

    def wait_for(self, sup, pred, timeout=20.0, why=""):
        t0 = time.time()
        while time.time() - t0 < timeout:
            s = sup.status()
            if pred(s):
                return s
            time.sleep(0.05)
        self.fail(f"等不到 {why}；最后状态 {sup.status()}")

    def start_server(self, port):
        """脚本先起来的那个服务：Neo 不认识它，Neo 也没启动它。"""
        import subprocess
        proc = subprocess.Popen([state.python_executable(ENGINE), str(ENGINE / "serve" / "server.py"),
                                 "--engine", "mock", "--port", str(port)], cwd=str(ENGINE),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self.addCleanup(proc.kill)
        wait_listening(port)          # 端口在听了再让 Neo 去接手（原因见 wait_listening）
        return proc

    def temp_root(self, prefix):
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix=prefix))
        (tmp / "external.log").write_text("脚本写的日志：服务在跑\n", encoding="utf-8")
        return tmp

    def test_adopt_shows_config_log(self):
        """接手脚本起的服务：日志页要指向配置里的那个日志文件，不能是空的。"""
        tmp = self.temp_root("neo-ext-adopt-")
        port = free_port()
        self.start_server(port)
        sup = self.make(NoPlanEngine(tmp), port)
        sup.start()
        s = self.wait_for(sup, lambda x: x["state"] == RUNNING and x["adopted"],
                          why="接手已在运行的服务")
        self.assertEqual(s["log_path"], str(tmp / "external.log"),
                         "接手的服务要把日志交给用户看")
        self.assertTrue(s["healthy"])

    def test_adopt_prefers_the_service_log(self):
        """接手服务时日志页指向服务在写的那个 log，不是 Neo 自己那个空的 server-*.log。

        实测 2026-10-12：接手成功了、状态是运行中，日志页还是空的——log_path 指的是
        Neo 自己写的 server-*.log，脚本起的服务一个字都没往里写。
        """
        tmp = self.temp_root("neo-ext-plan-")
        port = free_port()
        self.start_server(port)
        sup = self.make(PlanOkEngine(tmp), port)
        sup.start()
        s = self.wait_for(sup, lambda x: x["state"] == RUNNING and x["adopted"],
                          why="接手已在运行的服务")
        self.assertEqual(s["log_path"], str(tmp / "external.log"),
                         "日志页要指向服务在写的那个文件")

    def test_stopped_neo_reports_external_as_running(self):
        """Neo 自己起不来（停着）时，端口上有一个活着的服务：状态是"运行中（外部启动）"。"""
        tmp = self.temp_root("neo-ext-stop-")
        port = free_port()
        sup = self.make(NoPlanEngine(tmp), port)
        sup.start()
        s = self.wait_for(sup, lambda x: x["state"] == IDLE and x["diagnosis"],
                          why="起不来要停着并给出原因")
        self.assertEqual(s["display_state"], IDLE, "端口上没服务时就是停着")
        self.assertFalse(s["external"])
        self.start_server(port)
        s = self.wait_for(sup, lambda x: x["display_state"] == EXTERNAL,
                          why="端口上出现活着的服务要算运行中")
        self.assertTrue(s["external"])
        self.assertTrue(s["healthy"])
        self.assertEqual(s["log_path"], str(tmp / "external.log"))
        self.assertTrue(s["pid"], "要把那个服务的进程号报出来")

    def test_busy_adopted_service_is_not_declared_gone(self):
        """接手的服务只是忙着不答 /health（端口还在听）：不能判成"不见了"把它重启掉。"""
        sup = self.make(BusyEngine(ROOT), free_port())
        sup.state, sup.intent, sup._proc = RUNNING, "run", None
        out = {"info": None}
        th = threading.Thread(target=lambda: out.update(
            {"info": sup._monitor()}), daemon=True, name="neo-monitor-test")
        th.start()
        self.addCleanup(lambda: (setattr(sup, "intent", "stop"), sup._wake.set()))
        time.sleep(0.6)                     # watch_s 0.15 的 4 轮：足够看出"没有判成不见了"
        self.assertIsNone(out["info"], "端口在听的时候不能判成不见了")
        sup.port = free_port()                      # 端口上的服务真没了
        sup.adapter.live = False
        t0 = time.time()
        while out["info"] is None and time.time() - t0 < 5.0:
            time.sleep(0.05)
        th.join(5.0)
        self.assertIsNotNone(out["info"], "端口不听了要判成不见了")
        self.assertIn("接手的服务不见了", out["info"]["reason"])


class TestDiagnose(unittest.TestCase):
    def test_keywords(self):
        self.assertIn("页面文件", diagnose("CUDA error: out of memory"))
        self.assertIn("端口", diagnose("OSError: [Errno 98] port 8080 is already in use"))
        self.assertIn("驱动", diagnose("driver is too old: 550"))
        self.assertEqual(diagnose("一切正常"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
