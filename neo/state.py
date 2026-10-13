"""neo/state.py - Neo 自己的运行状态放在哪里，以及读写它们。

全部落在 neo/state/（neo/.gitignore 忽略这个目录），上游的文件一个都不改：

    launcher.json   记住的启动设置：root / port / host / api_key / model / engine
    window.json     桌面壳的窗口几何（x, y, w, h）
    source.json     装模型时选的下载源（probe / huggingface / modelscope / mirror）
    logs/manager.log        壳与监督的日志（写入前裁到尾部 LOG_MAX）
    logs/server-*.log       监督启动的 Strata 服务的完整输出（stdout 与 stderr 都在里面）

写 JSON 一律"临时文件 + os.replace"，并把旧内容留一份 .bak，与 setup.py 写配置的做法一致。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import traceback
from pathlib import Path

from . import paths
from .console_io import clean_line
from .paths import safe_resolve

NEO_DIR = safe_resolve(Path(__file__).parent)
ROOT = NEO_DIR.parent                       # Strata 的根目录
STATE = NEO_DIR / "state"
LOGS = STATE / "logs"
LAUNCHER = STATE / "launcher.json"
WINDOW = STATE / "window.json"
INSTANCE = STATE / "instance.json"
SOURCE = STATE / "source.json"

MANAGER_LOG = LOGS / "manager.log"
LOG_MAX = 200_000                           # 反复开关机只增不减：写入前裁到尾部
TAIL_LINES = 400                            # 送出去看的日志最多多少行
LOG_LOCK = threading.Lock()

# 打包成 exe 后（M4）exe 可能被放在只读目录里，状态退到 %APPDATA%\Neo Strata。
APP_NAME = "Neo Strata"


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        mark = path / ".write-test"
        mark.write_text("", encoding="utf-8")
        mark.unlink()
        return True
    except OSError:
        return False


def pick_state_dir(exe_dir: Path, appdata: str = "") -> Path:
    """exe 旁的 neo/state 优先；exe 在不可写目录里时退到 %APPDATA%\\Neo Strata。"""
    cand = exe_dir / "neo" / "state"
    if _writable(cand):
        return cand
    base = Path(appdata or str(exe_dir)) / APP_NAME
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return base


def _init_state() -> Path:
    if getattr(sys, "frozen", False):       # PyInstaller 的 exe
        return pick_state_dir(safe_resolve(sys.executable).parent,
                              os.environ.get("APPDATA", ""))
    return NEO_DIR / "state"


STATE = _init_state()
LOGS = STATE / "logs"
LAUNCHER = STATE / "launcher.json"
WINDOW = STATE / "window.json"
INSTANCE = STATE / "instance.json"
SOURCE = STATE / "source.json"
MANAGER_LOG = LOGS / "manager.log"


def ensure(path: Path | None = None) -> Path:
    path = path or LOGS
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: str | Path, default=None):
    """读一个 JSON 文件；不存在或坏了就回 default（Neo 的状态文件坏了不该让程序起不来）。"""
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def write_json(path: str | Path, data) -> Path:
    """整份写入（临时文件覆盖旧文件），旧内容留一份 <name>.bak。"""
    path = Path(path)
    ensure(path.parent)
    if path.exists():
        try:
            shutil.copyfile(path, path.with_name(path.name + ".bak"))
        except OSError:
            pass
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


def tail_text(path: str | Path, max_bytes: int = LOG_MAX, max_lines: int = TAIL_LINES) -> str:
    """一个日志文件的尾部（整行起，不切半行），逐行清洗后再交出去。

    清洗三件事：换行统一成 \n（Windows 的 \r\n 读回来再写会变成 \r\r\n，越写越多）；
    控制字符换成空格（控制台里 \r 把光标送回行首，后面的文字从第 0 列重画）；
    单行裁到 LINE_MAX（一条 RecursionError 的消息有几十万字符，原样送出去就是刷满屏）。
    max_lines=0 表示不限行数（log() 自己回写日志时用，保留的条数不受影响）。
    """
    path = Path(path)
    if not path.exists():
        return ""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            data = f.read(max_bytes)
    except OSError as e:
        return f"(读不到 {path.name}: {e})"
    text = data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if max_lines and len(lines) > max_lines:
        lines = [f"…[前面 {len(lines) - max_lines} 行已省略]"] + lines[-max_lines:]
    return "\n".join(clean_line(ln) for ln in lines)


def log(message: str) -> None:
    """壳与监督的日志：加时间戳追加，超过 LOG_MAX 才裁前面。

    pywebview 把每个 js_api 调用丢进自己的线程，读尾部再写回必须串行（LOG_LOCK）。
    写文件用二进制：文本模式会把每个 \n 变成 \r\n，而 tail_text 读回来的是 \r\n，
    下一次写就成了 \r\r\n —— 实测每写一次，前面每一行多一个 \r（6 次写入后第一行
    带 6 个 \r）。这些 \r 进了控制台就是满屏重画。

    以前每条日志都把整个尾部读出来清洗一遍再整份回写：一次最多 20 万字符的纯 Python
    字符串运算，全程握着 GIL。窗口线程（pywebview 的 STA 线程）跑任何委托都要抢 GIL，
    监督一密集写日志，窗口就收不到消息，标题栏出现"(未响应)"。现在常态只追加一行，
    超过 LOG_MAX 才裁一次。
    """
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = clean_line(f"{stamp} {message}")
    with LOG_LOCK:
        try:
            ensure(LOGS)
            with open(MANAGER_LOG, "ab") as f:
                f.write((line + "\n").encode("utf-8"))
                size = f.tell()
            if size > LOG_MAX:
                keep_bytes = max(1024, LOG_MAX - 4096)
                keep = tail_text(MANAGER_LOG, keep_bytes, max_lines=0)
                with open(MANAGER_LOG, "wb") as f:
                    f.write((keep + "\n").encode("utf-8"))
        except OSError:
            pass


def server_log_path(tag: str = "") -> Path:
    """本次启动的 Strata 服务日志的路径（监督在启动子进程前问一次，之后往里写）。"""
    ensure(LOGS)
    name = "server-" + time.strftime("%Y%m%d-%H%M%S") + (f"-{tag}" if tag else "") + ".log"
    return LOGS / name


def latest_server_log() -> Path | None:
    ensure(LOGS)
    files = []
    for p in LOGS.glob("server-*.log"):
        try:
            files.append((p.stat().st_mtime, p))
        except OSError:                      # 目录里多一个读不了的条目不该让日志页起不来
            continue
    files.sort(key=lambda x: x[0])
    return files[-1][1] if files else None


def python_executable(root: Path | None = None) -> str:
    """跑 Strata 用的 Python：setup.py 建的 .venv 优先，没有就用当前解释器。

    exe 里 sys.executable 就是 NeoStrata.exe 自己，拿它当"Python"等于让 Neo
    一遍遍启动 Neo 自己，所以 exe 跑时只认 .venv 与系统里的 python，找不到就把话说清。
    """
    root = Path(root) if root else ROOT
    for cand in (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"):
        if cand.exists():
            return str(cand)
    if not paths.is_frozen():
        return sys.executable
    import shutil
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            return str(found)
    raise FileNotFoundError(
        "没找到跑 Strata 用的 Python：这个 exe 需要 Strata 目录里的 .venv"
        f"（{root / '.venv'}）—— 在 Strata 目录跑一次 START-HERE.bat 就有了")


def launcher_config() -> dict:
    cfg = read_json(LAUNCHER, {}) or {}
    return cfg if isinstance(cfg, dict) else {}


# ---------------- 正在跑的这一个实例（第二个实例要说清它在哪儿） ----------------
def record_instance(**changes) -> dict:
    """记下"现在有一个 Neo 在跑"：pid / root / 端口 / 界面地址 / 是窗口还是浏览器。

    第二个实例撞上单实例互斥体时，靠这份文件告诉用户"在跑的那个"的进程号、
    界面地址、以及它到底有没有窗口 —— 只说"已经在运行了"是不够的，
    浏览器界面那条路根本没有窗口（实测：用户看不到窗口，只看到这句话）。
    """
    info = instance_info()
    info.update({k: v for k, v in changes.items() if v is not None})
    info["pid"] = os.getpid()
    try:
        write_json(INSTANCE, info)
    except OSError:
        pass                                # 状态目录在只读介质里：不影响 Neo 跑
    return info


def instance_info() -> dict:
    cfg = read_json(INSTANCE, {}) or {}
    return cfg if isinstance(cfg, dict) else {}


def clear_instance() -> None:
    """退出时把这份记录抹掉，别留下一个"看着还在跑"的假现场。"""
    try:
        INSTANCE.unlink()
    except OSError:
        pass


def instance_alive(info: dict | None = None) -> bool:
    """记录里那个进程还活着吗（Windows 用 OpenProcess 问，不发消息、不碰它）。"""
    info = instance_info() if info is None else info
    try:
        pid = int(info.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform != "win32":
        return True                        # 非 Windows 不做进程存在性检查
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    ctypes.windll.kernel32.CloseHandle(h)
    return True


def save_launcher_config(**changes) -> dict:
    cfg = launcher_config()
    cfg.update({k: v for k, v in changes.items() if v is not None})
    write_json(LAUNCHER, cfg)
    return cfg


def source_choice() -> str:
    """装模型时选的那个下载源（"probe" / "huggingface" / "modelscope" / "mirror"）。

    只记 Neo 用户的偏好，认不认得由 neo/models.py 判断（它认得 setup.py 的 --source
    有哪几个值）；文件坏了回空串，等于"没选过，按探测建议"。
    """
    cfg = read_json(SOURCE, {}) or {}
    if not isinstance(cfg, dict):
        return ""
    return str(cfg.get("source") or "")


def save_source_choice(source: str) -> str:
    """记下选中的下载源（整份写入，旧内容留 .bak，与 launcher.json 一样）。"""
    write_json(SOURCE, {"source": str(source or ""), "at": time.time()})
    return str(source or "")

# ---------------- 未捕获异常：一条有界的 traceback ----------------
TRACE_MAX = 4000                            # traceback 总长上限（字符）
TRACE_LINES = 40                            # traceback 最多多少行

RECURSION_NOTE = (
    "递归到顶（RecursionError）。Neo 的窗口里这类输出有两个已知来源："
    "一是 pywebview 的 WinForms 后端经 pythonnet 打的属性链内部日志"
    "（ActiveControl...SyncRoot...GenericSansSerif）；"
    "二是 Neo 或 Strata 放在 OneDrive 同步目录里，Path.resolve() 跟着同步根的"
    "重解析点一路拼 .SyncRoot。把目录移出同步目录，或用 --root 指一个普通目录。"
    "窗口里的输出已经裁过，完整日志在 neo/state/logs/manager.log。"
)


def format_exception_bounded(kind, exc, tb, limit: int = TRACE_MAX) -> str:
    """一条不会刷满屏幕的 traceback：逐行裁，行数与总长度封顶。

    RecursionError 不格式化 traceback：它的 traceback 上千帧、消息本身几十万字符，
    格式化它自己要再递归一层。
    """
    name = getattr(kind, "__name__", str(kind))
    if isinstance(exc, RecursionError):
        return f"{RECURSION_NOTE}\n{name}: {clean_line(exc, 300)}"
    try:
        text = "".join(traceback.format_exception(kind, exc, tb))
    except (RecursionError, RuntimeError, OSError, ValueError):
        return f"{name}: {clean_line(exc, 300)}"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [clean_line(ln, 300) for ln in text.split("\n")][-TRACE_LINES:]
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[:limit] + f"…[截断，原长 {len(text)} 字符]"
    return text


def install_excepthook() -> None:
    """主线程与监督线程的未捕获异常：只打一条有界的，别让一条巨行刷满控制台。"""
    def hook(kind, exc, tb):
        try:
            text = format_exception_bounded(kind, exc, tb)
            sys.stderr.write(text + "\n")
            sys.stderr.flush()
            log("未捕获异常：" + text.replace("\n", " | "))
        except Exception:
            pass

    sys.excepthook = hook
    threading.excepthook = lambda args: hook(args.exc_type, args.exc_value, args.exc_traceback)
