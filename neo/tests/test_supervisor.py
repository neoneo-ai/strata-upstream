"""neo/tests/test_supervisor.py - 监督器：启动、就绪、崩溃自动重启、退避、熔断、停止不重启。

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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from neo import jobobj, state                                          # noqa: E402
from neo.engines.mock import MockEngine                        # noqa: E402
from neo.supervisor import (EXTERNAL, HALTED, IDLE, RESTARTING, RUNNING, STARTING,  # noqa: E402
                            Supervisor, diagnose)


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
    def make(self, **kw):
        kw.setdefault("backoff", (0.5, 0.8, 1.0))
        kw.setdefault("window_s", 600.0)
        kw.setdefault("max_restarts", 3)
        kw.setdefault("poll_s", 0.25)
        kw.setdefault("probe_s", 0.4)
        self.sup = Supervisor(MockEngine(ROOT), free_port(), **kw)
        return self.sup

    def tearDown(self):
        try:
            self.sup.shutdown()
        except Exception:
            pass
        time.sleep(0.4)

    def wait(self, pred, timeout=90.0, why=""):
        t0 = time.time()
        while time.time() - t0 < timeout:
            s = self.sup.status()
            if pred(s):
                return s
            time.sleep(0.25)
        self.fail(f"等不到 {why}；最后状态 {self.sup.status()}")


class TestStartAndReady(SupervisorBase):
    def test_start_to_running(self):
        sup = self.make()
        sup.start()
        s = self.wait(lambda x: x["state"] == RUNNING and x["healthy"], why="服务就绪")
        self.assertTrue(s["process_alive"], "子进程应该在跑")
        self.assertTrue(s["pid"] > 0)
        self.assertEqual(s["engine"], "mock")
        self.assertTrue(s["max_context"] > 0, "/health 应该报上下文大小")
        self.assertTrue(Path(s["log_path"]).exists())
        self.assertIn("ready", state.tail_text(s["log_path"], 20000))   # 上游的就绪行进了日志

    def test_events_recorded(self):
        sup = self.make()
        sup.start()
        self.wait(lambda x: x["state"] == RUNNING, why="服务就绪")
        texts = " ".join(e["text"] for e in sup.status()["events"])
        self.assertIn("启动", texts)
        self.assertIn("就绪", texts)


class TestCrashRestart(SupervisorBase):
    def kill(self):
        s = self.sup.status()
        proc = self.sup._proc
        self.assertIsNotNone(proc, "应该有一个由 Neo 启动的子进程")
        pid = proc.pid
        proc.kill()
        return pid

    def test_unexpected_exit_restarts(self):
        sup = self.make()
        sup.start()
        first = self.wait(lambda x: x["state"] == RUNNING, why="第一次就绪")
        pid = self.kill()
        self.wait(lambda x: x["state"] in (RESTARTING, STARTING), why="进入自动重启")
        again = self.wait(lambda x: x["state"] == RUNNING and x["healthy"], why="重启后就绪")
        self.assertNotEqual(first["pid"], again["pid"], "重启后应该是新的进程")
        self.assertGreaterEqual(again["restart_number"], 1)
        self.assertEqual(len(again["restarts"]), 1, "10 分钟窗口里记 1 次重启")

    def test_backoff_sequence_and_breaker(self):
        sup = self.make()
        sup.start()
        self.wait(lambda x: x["state"] == RUNNING, why="就绪")
        delays = []
        for expected in (1, 2, 3):
            self.kill()
            self.wait(lambda x, e=expected: x["restart_number"] >= e, why=f"第 {expected} 次重启计数")
            delays.append(self.sup.status()["restart_number"])
            self.wait(lambda x: x["state"] == RUNNING and x["healthy"], why="重启后就绪")
        s = sup.status()
        self.assertEqual(delays, [1, 2, 3], "退避按第 1/2/3 次走")
        self.kill()
        self.wait(lambda x: x["state"] == HALTED, why="熔断")
        s = sup.status()
        self.assertGreater(len(s["restarts"]), s["max_restarts"], "超过上限才熔断")
        self.assertIsNone(sup._proc, "熔断后不该再有子进程")
        self.assertTrue(s["diagnosis"], "熔断要给一句诊断")
        time.sleep(2.0)
        self.assertEqual(sup.status()["state"], HALTED, "熔断后保持停着，等人工处理")

    def test_stop_never_autorestarts(self):
        sup = self.make()
        sup.start()
        self.wait(lambda x: x["state"] == RUNNING, why="就绪")
        sup.stop()
        s = sup.status()
        self.assertEqual(s["state"], IDLE)
        self.assertEqual(s["intent"], "stop")
        self.assertIsNone(sup._proc)
        time.sleep(2.5)
        self.assertEqual(sup.status()["state"], IDLE, "停止后 2.5 秒内不能被自动拉起")
        self.assertEqual(sup.status()["restarts"], [], "用户停止不计入重启次数")


class TestAdopt(SupervisorBase):
    def test_adopts_a_live_server(self):
        """端口上已有活着的服务（用户先用 START-HERE.bat 起来了）：认下它，不重复启动。"""
        import subprocess
        port = free_port()
        proc = subprocess.Popen([state.python_executable(ROOT), str(ROOT / "serve" / "server.py"),
                                 "--engine", "mock", "--port", str(port)], cwd=str(ROOT),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self.addCleanup(proc.kill)
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
                f"g = subprocess.Popen([{exe!r}, '-c', 'import time;time.sleep(20)'])\n"
                "print(g.pid, flush=True)\n"      # 不 flush 的话 readline 要等它 18 秒后退出才返回
                "time.sleep(18)\n")
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
    应答 /health，Neo 熔断后状态页写"未启动"、日志页是空的（用户的话是"我感觉已经
    启动了，只是日志没有看到，状态也不对"）。
    """

    def make(self, adapter, port, **kw):
        kw.setdefault("poll_s", 0.2)
        kw.setdefault("probe_s", 0.3)
        kw.setdefault("watch_s", 0.3)
        self.sup = Supervisor(adapter, port, **kw)
        return self.sup

    def wait_for(self, sup, pred, timeout=40.0, why=""):
        t0 = time.time()
        while time.time() - t0 < timeout:
            s = sup.status()
            if pred(s):
                return s
            time.sleep(0.25)
        self.fail(f"等不到 {why}；最后状态 {sup.status()}")

    def start_server(self, port):
        """脚本先起来的那个服务：Neo 不认识它，Neo 也没启动它。"""
        import subprocess
        proc = subprocess.Popen([state.python_executable(ROOT), str(ROOT / "serve" / "server.py"),
                                 "--engine", "mock", "--port", str(port)], cwd=str(ROOT),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self.addCleanup(proc.kill)
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

    def test_halted_neo_reports_external_as_running(self):
        """Neo 自己起不来（熔断）时，端口上有一个活着的服务：状态是"运行中（外部启动）"。"""
        tmp = self.temp_root("neo-ext-halt-")
        port = free_port()
        sup = self.make(NoPlanEngine(tmp), port)
        sup.start()
        s = self.wait_for(sup, lambda x: x["state"] == HALTED, why="起不来要熔断")
        self.assertEqual(s["display_state"], HALTED, "端口上没服务时就是停着")
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
        time.sleep(1.5)
        self.assertIsNone(out["info"], "端口在听的时候不能判成不见了")
        sup.port = free_port()                      # 端口上的服务真没了
        sup.adapter.live = False
        t0 = time.time()
        while out["info"] is None and time.time() - t0 < 5.0:
            time.sleep(0.1)
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
