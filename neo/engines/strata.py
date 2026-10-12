"""neo/engines/strata.py - Strata 引擎适配器：发现模型配置、拼启动命令。

发现方式与 setup.py / serve/server.py 一致：根目录下的 strata-*.json 就是"装好的模型"，
文件里的 "path" 是 GGUF，"port" 是它上次用的端口，"args" 是引擎参数。setup.py 每次启动会把
配置文件的写入时间刷新一次（setup.py 的 touch()），所以"最近写入的那个"就是上次用的模型，
Neo 按这个顺序排列表。

启动只用 serve/server.py 已有的命令行参数（--engine/--config/--port/--host/--api-key/--gpu），
不改上游一行代码。
"""
from __future__ import annotations

import sys
from pathlib import Path

from .base import EngineAdapter, LaunchPlan
from ..paths import safe_resolve

CONFIG_GLOB = "strata-*.json"
DEFAULT_PORT = 8080                             # setup.py 的 --port 默认值


def list_configs(root) -> list[dict]:
    """根目录下的模型配置，最近用过的排最前（与 setup.py 的 touch() 语义一致）。"""
    root = Path(root)
    out = []
    for f in sorted(root.glob(CONFIG_GLOB), key=lambda p: p.stat().st_mtime, reverse=True):
        import json
        try:
            cfg = json.loads(f.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(cfg, dict):
            continue
        args = cfg.get("args") if isinstance(cfg.get("args"), dict) else {}
        out.append({
            "id": f.name,
            "name": str(cfg.get("model_name") or f.stem),
            "path": str(cfg.get("path") or ""),
            "file": str(f),
            "mtime": f.stat().st_mtime,
            "port": int(cfg.get("port") or DEFAULT_PORT),
            "context": int(args.get("--context") or 0),
            "vision": bool(cfg.get("vision")),
            "backend": cfg.get("backend") or "",
            "installed": bool(cfg.get("path")) and Path(str(cfg.get("path"))).exists(),
        })
    return out


class StrataEngine(EngineAdapter):
    name = "strata"
    title = "Strata"
    entry = "serve/server.py"

    def discover(self) -> list[dict]:
        return list_configs(self.root)

    def installed(self) -> bool:
        return any(x["installed"] for x in self.discover())

    def ready_timeout_s(self, context: int = 0) -> int:
        """模型加载要多久：上下文越大越久，8GB 显存上的 27B 要几分钟，给足余量。"""
        if context >= 200_000:
            return 1800
        if context >= 64_000:
            return 1200
        return 900

    def plan(self, port: int, host: str = "127.0.0.1", api_key: str = "",
             model_id: str = "", gpu=None) -> LaunchPlan:
        from .. import state

        cfg = None
        items = self.discover()
        if model_id:
            cfg = next((x for x in items if x["id"] == model_id), None)
        cfg = cfg or (self.default_config())
        if cfg is None:
            raise FileNotFoundError("这台机器上还没有装好的模型（根目录没有 strata-*.json）")
        if not cfg["installed"]:
            raise FileNotFoundError(f"模型文件不在：{cfg['path']}")

        python = state.python_executable(self.root)
        argv = [python, str(self.root / "serve" / "server.py"),
                "--engine", "strata", "--config", cfg["file"], "--port", str(port)]
        if host and host != "127.0.0.1":
            argv += ["--host", host]
        if api_key:
            argv += ["--api-key", api_key]
        if gpu not in (None, "", []):
            argv += ["--gpu", gpu if isinstance(gpu, str) else ",".join(str(g) for g in gpu)]
        return LaunchPlan(argv=argv, cwd=str(self.root), env=None,
                          log_path=str(state.server_log_path(cfg["name"])),
                          port=port, host=host, model=cfg["name"], engine=self.name,
                          label=f"{cfg['name']} · {cfg['context']} 上下文" if cfg["context"] else cfg["name"],
                          ready_timeout_s=self.ready_timeout_s(cfg["context"]),
                          extra={"config": cfg["file"], "vision": cfg["vision"],
                                 "backend": cfg["backend"]})


if __name__ == "__main__":                                      # 快速看一眼这台机器装了什么模型
    for item in list_configs(Path(sys.argv[1]) if len(sys.argv) > 1
                             else safe_resolve(Path(__file__).parent).parents[1]):
        print(f"{item['name']:<28} {item['context']:>7}  端口 {item['port']}  "
              f"{'视觉' if item['vision'] else '文本'}  {'缺文件' if not item['installed'] else '在'}")
