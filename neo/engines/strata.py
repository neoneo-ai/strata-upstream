"""neo/engines/strata.py - Strata 引擎适配器：发现模型配置、拼启动命令。

发现方式与 setup.py / serve/server.py 一致：根目录下的 strata-*.json 就是"装好的模型"。
配置文件是 setup.py 写的，形状与 serve/server.py 读的一样（实测 D:\\Work\\Strata 的
strata-swift-iq3_xxs.json）：

    {"exe": "...strata.exe", "args": ["--pack", 目录, "--native", gguf, "--max-context", "202072", ...],
     "cwd": ..., "tokenizer": ..., "model_name": ..., "log": ..., "port": 8080, "vision": {...}}

**没有 "path" 键**，模型文件在 args 的 --native 里，上下文在 --max-context 里，args 是扁平
列表不是字典。早期按 "path" + 字典 args 写的版本在真实配置上永远算不出模型文件，
启动时报的是空消息"模型文件不在："（实测 2026-10-12 的 manager.log）。这里两种形状都认。

setup.py 每次启动会把配置文件的写入时间刷新一次（setup.py 的 touch()），所以"最近写入的那个"
就是上次用的模型，Neo 按这个顺序排列表。

启动只用 serve/server.py 已有的命令行参数（--engine/--config/--port/--host/--api-key/--gpu），
不改上游一行代码。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .base import EngineAdapter, LaunchPlan
from ..paths import safe_resolve

CONFIG_GLOB = "strata-*.json"
DEFAULT_PORT = 8080                             # setup.py 的 --port 默认值
# 根目录里可能出现的引擎程序名（setup.py 按后端装成这几个之一）
EXE_NAMES = ("strata.exe", "strata-hip.exe", "strata-sycl.exe", "strata")


def arg_value(args, flag: str) -> str:
    """从配置的 args 里取一个参数的值。args 是扁平列表（setup.py 写的），也认字典（老形状）。"""
    if isinstance(args, dict):
        v = args.get(flag)
        return "" if v in (None, False, "") else str(v)
    if not isinstance(args, list):
        return ""
    for i, item in enumerate(args):
        text = str(item)
        if text == flag and i + 1 < len(args):
            return str(args[i + 1])
        if text.startswith(flag + "="):
            return text.split("=", 1)[1]
    return ""


def model_file_of(cfg: dict) -> str:
    """这个配置要加载的那个模型文件：path 键（老形状）→ args 里的 --native / --pack → vision.model。"""
    for source in (str(cfg.get("path") or ""),
                   arg_value(cfg.get("args"), "--native"),
                   arg_value(cfg.get("args"), "--pack"),
                   str((cfg.get("vision") or {}).get("model") or "")):
        if source.strip():
            return source.strip()
    return ""


def context_of(cfg: dict) -> int:
    """上下文长度：--max-context（setup.py 写的）优先，再 --context（老形状）。"""
    for flag in ("--max-context", "--context"):
        raw = arg_value(cfg.get("args"), flag)
        try:
            value = int(str(raw).replace(",", ""))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def root_exe(root) -> str:
    """根目录里的引擎程序：配置没写 exe 键时用它（setup.py 把引擎放在根目录）。"""
    for name in EXE_NAMES:
        cand = Path(root) / name
        if cand.exists():
            return str(cand)
    return ""


def exe_of(cfg: dict, root=None) -> str:
    """引擎程序：配置里的 exe 键优先（相对路径按 cwd 展开，与 serve/server.py 里那句注释一样），
    没有就看根目录里的 strata.exe / strata-hip.exe。"""
    raw = str(cfg.get("exe") or "").strip()
    if not raw:
        return root_exe(root)
    if Path(raw).is_absolute():
        return raw
    return str(safe_resolve(Path(raw), root=str(cfg.get("cwd") or (root or "."))))


def missing_parts(cfg: dict, root=None) -> list[str]:
    """这个配置缺了哪些东西（给用户看的中文，一条一句）。

    实测 2026-10-12 在 D:\\Work\\Strata：只认 path 键时真实配置（模型在 args 的
    --native 后面）全被判成"没装"，报错是"模型文件不在："——后面是空的，
    因为 cfg["path"] 本来就是空的。
    """
    out = []
    exe = exe_of(cfg, root)
    if not exe:
        out.append("引擎程序不在（strata.exe / strata-hip.exe）")
    elif not Path(exe).exists():
        out.append(f"引擎程序不在：{exe}")
    model = model_file_of(cfg)
    if not model:
        out.append("配置里没有模型文件（path 键或 args 里的 --native / --pack）")
    elif not Path(model).exists():
        out.append(f"模型文件不在：{model}")
    return out


def list_configs(root) -> list[dict]:
    """根目录下的模型配置，最近用过的排最前（与 setup.py 的 touch() 语义一致）。"""
    root = Path(root)
    out = []
    for f in sorted(root.glob(CONFIG_GLOB), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            cfg = json.loads(f.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(cfg, dict):
            continue
        missing = missing_parts(cfg, root)
        out.append({
            "id": f.name,
            "name": str(cfg.get("model_name") or f.stem),
            "path": model_file_of(cfg),
            "file": str(f),
            "mtime": f.stat().st_mtime,
            "port": int(cfg.get("port") or DEFAULT_PORT),
            "context": context_of(cfg),
            "vision": bool(cfg.get("vision")),
            "backend": cfg.get("backend") or "",
            "exe": exe_of(cfg, root),
            "args": cfg.get("args") or [],
            "missing": missing,
            "installed": not missing,
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

    def log_path_for(self, model_id: str = "") -> str:
        """这个模型的日志文件在哪 —— 配置里的 log 键（setup.py 给 run-*.bat 写的就是它）。

        脚本先起来的服务一直在往这个文件写。Neo 接手它之后日志页要指向同一份，
        否则用户看到的就是"服务在跑，日志页是空的"（实测 2026-10-12，D:\\Work\\Strata
        的 strata-swift-iq3_xxs.json 里 "log": "strata-swift-iq3_xxs.log"）。
        """
        items = self.discover()
        cfg = next((x for x in items if x["id"] == model_id), None) if model_id else \
            (self.default_config() or (items[0] if items else None))
        if cfg is None:
            return ""
        try:
            raw = json.loads(Path(cfg["file"]).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return ""
        log = str((raw or {}).get("log") or "").strip()
        return str(safe_resolve(log, root=self.root)) if log else ""

    def ready_timeout_s(self, context: int = 0) -> int:
        """模型加载要多久：上下文越大越久，8GB 显存上的 27B 要几分钟，给足余量。"""
        if context >= 200_000:
            return 1800
        if context >= 64_000:
            return 1200
        return 900

    def plan(self, port: int, host: str = "127.0.0.1", api_key: str = "",
             model_id: str = "", gpu=None) -> LaunchPlan:
        from .. import model_settings, state

        cfg = None
        items = self.discover()
        if model_id:
            cfg = next((x for x in items if x["id"] == model_id), None)
        cfg = cfg or (self.default_config())
        if cfg is None:
            raise FileNotFoundError("这台机器上还没有装好的模型（根目录没有 strata-*.json）")
        if not cfg["installed"]:
            raise FileNotFoundError(f"{cfg['id']} 起不来：{'；'.join(cfg['missing'])}"
                                    "（跑 START-HERE.bat 装模型，或用 --model 换一个配置）")

        python = state.python_executable(self.root)
        # 有覆盖配置（模型设置页改过上下文/KV/图片/显存保留）就指过去：--config 是
        # serve/server.py 已有的参数，Neo 不给它加参数，也不动上游那份 strata-*.json
        cfg_file = model_settings.config_path_for(cfg["file"])
        context = cfg["context"]
        if cfg_file != str(cfg["file"]):
            context = model_settings.settings_of(
                model_settings.read_config(cfg_file))["context"] or context
        argv = [python, str(self.root / "serve" / "server.py"),
                "--engine", "strata", "--config", cfg_file, "--port", str(port)]
        if host and host != "127.0.0.1":
            argv += ["--host", host]
        if api_key:
            argv += ["--api-key", api_key]
        if gpu not in (None, "", []):
            argv += ["--gpu", gpu if isinstance(gpu, str) else ",".join(str(g) for g in gpu)]
        return LaunchPlan(argv=argv, cwd=str(self.root), env=None,
                          log_path=str(state.server_log_path(cfg["name"])),
                          port=port, host=host, model=cfg["name"], engine=self.name,
                          label=f"{cfg['name']} · {context} 上下文" if context else cfg["name"],
                          ready_timeout_s=self.ready_timeout_s(context),
                          extra={"config": cfg_file, "vision": cfg["vision"],
                                 "backend": cfg["backend"],
                                 "overlay": cfg_file != str(cfg["file"])})


if __name__ == "__main__":                                      # 快速看一眼这台机器装了什么模型
    from ..paths import strata_root

    for item in list_configs(Path(sys.argv[1]) if len(sys.argv) > 1
                             else strata_root()[0]):
        print(f"{item['name']:<28} {item['context']:>7}  端口 {item['port']}  "
              f"{'视觉' if item['vision'] else '文本'}  "
              f"{'；'.join(item['missing']) if item['missing'] else '可启动'}")
