"""neo/engines/mock.py - 测试用的引擎适配器：跑 serve/server.py --engine mock。

mock 引擎不需要模型文件，几秒就就绪，所以监督的"启动→就绪→被杀→自动重启→熔断"整条链路
都能在 CI 里用真实进程跑一遍。它不是给用户用的。
"""
from __future__ import annotations

from .base import EngineAdapter, LaunchPlan

SCRIPT = "Neo 的测试脚本说：你好。"


class MockEngine(EngineAdapter):
    name = "mock"
    title = "Mock（测试用）"
    entry = "serve/server.py"

    def discover(self) -> list[dict]:
        return [{"id": "mock", "name": "mock", "path": "", "file": "", "mtime": 0,
                 "port": 8095, "context": 32768, "vision": False, "backend": "",
                 "installed": (self.root / "serve" / "server.py").exists()}]

    def plan(self, port: int, host: str = "127.0.0.1", api_key: str = "",
             model_id: str = "", gpu=None) -> LaunchPlan:
        from .. import state
        argv = [state.python_executable(self.root), str(self.root / "serve" / "server.py"),
                "--engine", "mock", "--script", SCRIPT, "--port", str(port)]
        if api_key:
            argv += ["--api-key", api_key]
        return LaunchPlan(argv=argv, cwd=str(self.root), env=None,
                          log_path=str(state.server_log_path("mock")), port=port, host=host,
                          model="mock", engine=self.name, label="mock 引擎（测试）",
                          ready_timeout_s=90)
