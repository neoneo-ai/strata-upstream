"""neo/tests/test_supervisor.py - 监督器：启动、就绪、崩溃自动重启、退避、熔断、停止不重启。

跑的是真实进程（serve/server.py --engine mock），所以这条测试就是 M1 的核心验收。
"""
from __future__ import annotations

import socket
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from neo import state                                          # noqa: E402
from neo.engines.mock import MockEngine                        # noqa: E402
from neo.supervisor import (HALTED, IDLE, RESTARTING, RUNNING, STARTING,  # noqa: E402
                            Supervisor, diagnose)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


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



class TestDiagnose(unittest.TestCase):
    def test_keywords(self):
        self.assertIn("页面文件", diagnose("CUDA error: out of memory"))
        self.assertIn("端口", diagnose("OSError: [Errno 98] port 8080 is already in use"))
        self.assertIn("驱动", diagnose("driver is too old: 550"))
        self.assertEqual(diagnose("一切正常"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
