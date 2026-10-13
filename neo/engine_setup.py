"""neo/engine_setup.py - 引擎源码树：按 neo/engine_ref.json 钉住的那一个 tag 装、对齐。

Neo 从 Strata 里分出来之后，引擎源码不再与 Neo 在同一个仓库里。这一份按 pin 取：

    首选  git clone --branch <tag> --depth 1 --single-branch <url> strata/
    退路  下载 <url>/archive/refs/tags/<tag>.zip，本地算 SHA-256 与 pin 比，对上了才解压

取源码只认 GitHub 的 tag：ModelScope 那类镜像给的是一个仓库的当前文件，不是钉住的
revision（上游 setup.py 的 model_source() 里那句 #908 说的就是这件事），源码树不能按镜像装。
GitHub 的 archive zip 是它现打包的，没有官方摘要，所以 zip_sha256 是 Neo 这边算好钉进 pin 的
（bump 流程见 docs/NEO_DESKTOP.md）；下载回来的 zip 自己算一遍 SHA-256，与 pin 不一致就不解压。

拿到源码之后 Neo 不做任何编译、不装包、不下模型 —— venv、依赖、引擎二进制、模型全交给
上游的 setup.py（neo/wizard.py 那条路：setup.py --yes --no-start）。

引擎目录有两个形状，默认是前一个：

    自己装    树不在 → clone 到 <应用目录>/strata，装完就是 Neo 自己的那一份
    外置的    这台机器上别处一键装好的 Strata：python -m neo.engine_setup --set-root D:\\Work\\Strata
              记一次（neo/state/launcher.json 的 root）就长期用它，Neo 不把引擎复制一份过来

外置树 Neo 只检查不动：版本与 pin 不一样时报 external_tag，要切它的 HEAD 得显式 --align；
它的脏检查只看跟踪文件的改动（Neo 自己的文件在那棵树里按设计就是 untracked）。

plan() 是纯函数：不联网、不写文件，只说"现在该干什么"。状态：

    ready          树在、HEAD == pin、git 干净（按 zip 装的：摘要对上 + Neo 记过这一笔）
    clone          树不在，git 可用
    zip_copy       树不在，git 不可用，本地有 zip 且 SHA-256 == pin
    need_zip       树不在，git 不可用，本地没有可用的 zip
    checkout_pin   树在，HEAD 不是 pin
    dirty          树在，git 有改动 —— 部署闸门拒绝，Neo 不动用户的改动
    not_a_git_tree 树在，但不是 git 树，也不是 Neo 按 zip 装的那一份

浅克隆出来的是 detached HEAD，这正合用：pin 是一个 commit SHA，不是一条分支。
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

from . import netprobe, paths, state, wizard
from .cli import wide_stdout
from .paths import safe_resolve

REF_NAME = "engine_ref.json"
ENGINE_LOG_NAME = "engine.log"
ENGINE_STATE_NAME = "engine.json"
TAIL_BYTES = 60_000
GIT_TIMEOUT_S = 1800                              # clone 一份 20.8 MB 的源码树，慢网给足
SHA_HEX_LEN = 40
ZIP_SHA_HEX_LEN = 64
REQUIRED_KEYS = ("url", "tag", "sha")


#: wide_stdout() 在 neo/cli.py（打印中文的命令行共用），上面 import 进来。
#: `--ensure` 末尾打印 engine.log 尾部时踩的是同一处坑（cli.py 的模块串里记着）。


class EngineRefError(RuntimeError):
    """pin 读不出来或形状不对：宁可不装，也不装一个来路不明的引擎。"""


def ref_file() -> Path:
    """pin 文件在哪：源码跑是 neo/engine_ref.json，exe 里是解包目录下的那一份。"""
    return (paths.resource_dir() / REF_NAME) if paths.is_frozen() else (paths.app_dir() / REF_NAME)


def is_hex(value, length: int) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == length and all(c in "0123456789abcdef" for c in text)


def load_ref(path=None) -> dict:
    """读 neo/engine_ref.json。形状不对就抛 —— 不猜、不补默认值。"""
    path = Path(path) if path else ref_file()
    data = state.read_json(path, None)
    if not isinstance(data, dict):
        raise EngineRefError(f"{path} 读不出一份 JSON：Neo 不知道要装哪一份引擎")
    missing = [key for key in REQUIRED_KEYS if not str(data.get(key) or "").strip()]
    if missing:
        raise EngineRefError(f"{path} 里少了 {'、'.join(missing)}")
    sha = str(data["sha"]).strip().lower()
    if not is_hex(sha, SHA_HEX_LEN):
        raise EngineRefError(f"{path} 里的 sha 不是一个 {SHA_HEX_LEN} 位十六进制的 commit")
    zip_sha = str(data.get("zip_sha256") or "").strip().lower()
    if zip_sha and not is_hex(zip_sha, ZIP_SHA_HEX_LEN):
        raise EngineRefError(f"{path} 里的 zip_sha256 不是一个 {ZIP_SHA_HEX_LEN} 位十六进制的摘要")
    return {
        "url": str(data["url"]).strip().rstrip("/"),
        "tag": str(data["tag"]).strip(),
        "sha": sha,
        "zip_sha256": zip_sha,
        "zip_bytes": int(data.get("zip_bytes") or 0),
        "note": str(data.get("note") or ""),
    }


def archive_url(ref) -> str:
    """GitHub 现打包的那一份源码：<仓库>/archive/refs/tags/<tag>.zip。"""
    return f"{ref['url']}/archive/refs/tags/{ref['tag']}.zip"


def git_available() -> bool:
    return bool(shutil.which("git"))


def git_run(argv, cwd=None, timeout: float = GIT_TIMEOUT_S) -> tuple[int, str]:
    """跑一条完整的 git 命令（argv 第一个就是 git），回 (返回码, 输出)。"""
    if not git_available():
        return -1, "这台机器上没有 git"
    argv = [str(item) for item in argv]
    try:
        proc = subprocess.run(argv, cwd=str(cwd) if cwd else None, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, f"{' '.join(argv)} 没跑起来：{exc}"
    return proc.returncode, (proc.stdout + proc.stderr)


def git_head(dest) -> str:
    """这棵树现在的 commit（小写全 SHA）。问不到回空串。"""
    rc, out = git_run(["git", "rev-parse", "HEAD"], cwd=dest)
    lines = [x.strip().lower() for x in out.splitlines() if x.strip()]
    return lines[-1] if rc == 0 and lines else ""


def git_dirty(dest, tracked_only: bool = False) -> bool | None:
    """git status --porcelain 有内容吗。问不到回 None —— 不能据此说它干净。

    tracked_only=True 只看"改了的跟踪文件"，不算 untracked：部署树（D:\\Work\\Strata）
    里 Neo 的文件按设计就是 untracked（AGENTS.md：那边不提交），把它们算成脏会让
    换版闸门永远不过。
    """
    argv = ["git", "status", "--porcelain"]
    if tracked_only:
        argv.append("--untracked-files=no")
    rc, out = git_run(argv, cwd=dest)
    if rc != 0:
        return None
    return bool(out.strip())


def clone_argv(ref, dest) -> list[str]:
    """浅克隆钉住的那个 tag：--depth 1 只要那一个 commit（与 20.8 MB 的 zip 同量级）。"""
    return ["git", "clone", "--branch", ref["tag"], "--depth", "1", "--single-branch",
            ref["url"], str(Path(dest))]



def checkout_argv(ref) -> list[str]:
    """树在、HEAD 不是 pin：切到 pin 的那个 commit（detached —— pin 是一个 SHA，不是分支）。"""
    return ["git", "checkout", "--detach", ref["sha"]]


# ---------------- 现在该干什么（纯函数，不联网、不写文件） ----------------
STATE_READY = "ready"
STATE_CLONE = "clone"
STATE_ZIP = "zip_copy"
STATE_NEED_ZIP = "need_zip"
STATE_CHECKOUT = "checkout_pin"
STATE_DIRTY = "dirty"
STATE_NOT_GIT = "not_a_git_tree"
STATE_EXTERNAL = "external_tag"

#: 每个状态要做什么（页面与终端都印这一句）
STATE_ACTION = {
    STATE_READY: "什么都不用做：这一份就是钉住的引擎",
    STATE_CLONE: "git clone 钉住的那一份（浅克隆）",
    STATE_ZIP: "从本地 zip 解压（SHA-256 已与 pin 比对）",
    STATE_NEED_ZIP: "先下载 zip 存到本地，再用 --zip 指给 Neo",
    STATE_CHECKOUT: "git checkout --detach 到 pin 的 commit",
    STATE_DIRTY: "不动它：这里有人改过（部署闸门拒绝）",
    STATE_NOT_GIT: "不动它：这不是 git 树，也不是 Neo 按 zip 装的那一份",
    STATE_EXTERNAL: ("外置引擎目录：版本与 pin 不一样，Neo 不动它的 HEAD —— "
                     "要对齐就点「对齐到 pin」（命令行 --align）"),
}


def engine_state_path() -> Path:
    """Neo 记的那一笔：这台机器上的引擎树是哪来的（git 还是 zip）。"""
    return state.STATE / ENGINE_STATE_NAME


def read_engine_state() -> dict:
    data = state.read_json(engine_state_path(), {})
    return data if isinstance(data, dict) else {}


def write_engine_state(**changes) -> dict:
    """记下引擎树的来历（整份写入，旧内容留 .bak，与 launcher.json 一样）。"""
    data = read_engine_state()
    data.update({k: v for k, v in changes.items() if v is not None})
    data["at"] = time.time()
    state.write_json(engine_state_path(), data)
    return data


def zip_installed(dest, ref, installed=None) -> bool:
    """这棵树是 Neo 按 zip 装的那一份吗（没有 .git 时的唯一凭据）。

    zip 解出来没有 git 元数据，HEAD 查不了，所以"是不是钉住的那一份"只能看 Neo
    自己记的那一笔：装的时候摘要对上了 pin，才写得进去。
    """
    data = read_engine_state() if installed is None else installed
    return (str(data.get("dest") or "") == str(Path(dest))
            and str(data.get("sha") or "").lower() == ref["sha"]
            and data.get("source") == "zip")


def default_dest() -> Path:
    """引擎源码树默认落在哪：解析出来的那一份根目录；还没有引擎时 <应用目录>/strata。

    2026-10-13 之前这里只有 <应用目录>/strata 一个地方：Neo 住在用户一键装好的
    Strata 检出里时，`--ensure` 会把同一份源码再 clone 到 <根>/strata 里 —— 用户
    已经有引擎，Neo 又复制一份，两份还会各自漂移。现在默认就是 neo/paths.py 解析
    出来的那一份（记住的外置引擎目录 / exe 所在目录 / <应用目录>/strata 都算），
    谁在就用谁；只有哪份都没有时才 clone，clone 的落点仍是 <应用目录>/strata。
    """
    resolved, _why = paths.strata_root()
    return resolved if paths.is_strata_root(resolved) else paths.engine_checkout()


def is_neo_checkout(dest) -> bool:
    """这棵树是 Neo 自己装的那一份吗（装在 <应用目录>/strata，或 Neo 记过来历）。

    外置的引擎目录（用户一键装好的、别处的 Strata）不是：Neo 对它只做"看一眼"，
    要动它的 HEAD 得用户显式说（--align）。脏检查也跟着分：Neo 自己 clone 的树
    该是干净的，外置树里 Neo 的文件按设计就是 untracked（AGENTS.md：部署树不提交），
    把它们算成脏会让换版闸门永远不过。
    """
    dest = Path(dest)
    if dest == paths.engine_checkout():
        return True
    return str(read_engine_state().get("dest") or "") == str(dest)


def own_text(dest) -> str:
    """这一份引擎树是谁装的 —— 给页面与终端看的那一句话。"""
    return ("Neo 自己装的（按 pin clone 或解压 zip）" if is_neo_checkout(dest)
            else "外置引擎目录（Neo 起它、按 pin 检查它，版本要对齐得用户点头）")


def set_engine_root(dir_path: str) -> dict:
    """记住引擎目录：让 Neo 用这台机器上别处一键装好的 Strata，不复制目录。

    写 neo/state/launcher.json 的 root（与 --root、NEO_STRATA_ROOT 同一个意思，
    只是记下来长期用）。目录不存在就拒绝 —— 打错一个字母比记错一个目录难查得多。
    """
    if not str(dir_path or "").strip():
        return {"ok": False, "notes": ["引擎目录是空的：给一个目录，或用 --clear-root 忘掉"]}
    cand = paths.safe_resolve(dir_path, paths.app_root())
    if not cand.is_dir():
        return {"ok": False, "notes": [f"{cand} 不存在：Neo 不记一个找不到的目录"]}
    state.save_engine_root(str(cand))
    root, why = paths.strata_root()
    notes = [f"记住的引擎目录：{cand}", f"现在 Neo 用的根目录：{root}（{why}）"]
    if not paths.is_strata_root(cand):
        notes.append(f"{cand} 里没有 setup.py + serve/server.py：起服务会失败，"
                     "要换就再点一次，要 Neo 自己装就清除")
    return {"ok": True, "root": str(root), "reason": why, "notes": notes,
            "markers": paths.is_strata_root(cand)}


def clear_engine_root() -> dict:
    """忘掉记住的引擎目录，回到默认（Neo 自己按 pin 装）。"""
    state.save_engine_root("")
    root, why = paths.strata_root()
    return {"ok": True, "root": str(root), "reason": why,
            "notes": [f"已清除记住的引擎目录，现在用 {root}（{why}）"]}


def plan(dest=None, ref=None, git_ok=None, local_zip="", installed=None, own=None) -> dict:
    """现在该干什么。只读：读 git 状态、算本地文件摘要，不联网、不写任何文件。

    own 不给就是"这棵树 Neo 自己装的吗"现问（is_neo_checkout）；测试里直接给，
    省得为了造一棵"Neo 自己的树"去改真实的状态目录。
    """
    ref = ref or load_ref()
    dest = Path(dest) if dest else default_dest()
    own = is_neo_checkout(dest) if own is None else bool(own)
    git_ok = git_available() if git_ok is None else bool(git_ok)
    out = {"state": "", "action": "", "dest": str(dest), "tag": ref["tag"], "sha": ref["sha"],
           "url": ref["url"], "archive": archive_url(ref), "git": git_ok, "head": "",
           "own": own, "zip_ok": False, "notes": []}

    def done(state_name: str, note: str = "") -> dict:
        out["state"] = state_name
        out["action"] = STATE_ACTION[state_name]
        if note:
            out["notes"].append(note)
        return out

    if not dest.is_dir():
        if git_ok:
            return done(STATE_CLONE, f"{dest} 不在：git clone {ref['url']} 的 {ref['tag']}")
        ok, note = check_zip(local_zip, ref)
        out["zip_ok"] = ok
        if ok:
            return done(STATE_ZIP, note)
        return done(STATE_NEED_ZIP, note)
    if not (dest / ".git").exists():
        if zip_installed(dest, ref, installed):
            return done(STATE_READY, f"{dest} 是 Neo 按 zip 装的 {ref['tag']}"
                                     "（没有 git 元数据，闸门查的是 Neo 记的那一笔摘要）")
        return done(STATE_NOT_GIT, f"{dest} 里没有 .git，Neo 也没记过它是按 zip 装的"
                                   "（外置引擎目录照用：Neo 起它，只是不动它）")
    out["head"] = git_head(dest)
    dirty = git_dirty(dest, tracked_only=not own)
    if dirty:
        return done(STATE_DIRTY, "git status --porcelain 不空：这里有人改过，Neo 不动它"
                                 + ("" if own else "（外置目录只看跟踪文件的改动）"))
    if dirty is None:
        return done(STATE_DIRTY, "问不到 git 状态（git 跑不起来），按不干净处理")
    if out["head"] != ref["sha"]:
        if not own:
            return done(STATE_EXTERNAL, f"{dest} 是外置引擎目录：HEAD 是 {out['head'][:12]}，"
                                         f"pin 是 {ref['sha'][:12]}")
        return done(STATE_CHECKOUT, f"HEAD 是 {out['head'][:12]}，pin 是 {ref['sha'][:12]}")
    return done(STATE_READY, f"{dest} 就是钉住的 {ref['tag']}（{ref['sha'][:12]}），git 干净"
                             + ("" if own else "（外置引擎目录）"))


# ---------------- 本地 zip 那条路 ----------------
def sha256_file(path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(Path(path), "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def check_zip(zip_path, ref) -> tuple[bool, str]:
    """本地那份 zip 能不能用：摘要与 pin 一致才算。回 (能用, 给用户看的一句话)。"""
    if not zip_path:
        return False, "没有本地 zip：下载 " + archive_url(ref) + " 存到本地，再用 --zip 指给 Neo"
    path = Path(zip_path)
    if not path.is_file():
        return False, f"{path} 不在"
    if not ref["zip_sha256"]:
        return False, "pin 里没有 zip_sha256，Neo 不认任何本地 zip"
    digest = sha256_file(path)
    if digest != ref["zip_sha256"]:
        return False, (f"{path.name} 的 SHA-256 是 {digest[:16]}…，pin 要的是 "
                       f"{ref['zip_sha256'][:16]}…：这一份不是钉住的那一个")
    return True, f"{path.name} 的 SHA-256 与 pin 一致（{path.stat().st_size} 字节）"


def extract_zip(zip_path, dest, ref) -> dict:
    """摘要对上才解压：先解到临时目录，再整份挪进 dest —— 半途失败不留半个引擎树。"""
    ok, note = check_zip(zip_path, ref)
    if not ok:
        return {"ok": False, "notes": [note]}
    dest = Path(dest)
    if dest.exists():
        return {"ok": False, "notes": [f"{dest} 已经在了，Neo 不往里解压"]}
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.unpack"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(tmp)
        tops = list(tmp.iterdir())
        if len(tops) != 1 or not tops[0].is_dir():
            return {"ok": False,
                    "notes": [f"{Path(zip_path).name} 解出来不是一个顶层目录，Neo 不猜哪一个才是引擎"]}
        shutil.move(str(tops[0]), str(dest))
    except (OSError, zipfile.BadZipFile) as exc:
        return {"ok": False, "notes": [f"解压失败：{exc}"]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    write_engine_state(dest=str(dest), sha=ref["sha"], tag=ref["tag"], source="zip")
    return {"ok": True, "notes": [f"{dest} 就位（从 {Path(zip_path).name} 解出来；"
                                  "这一份没有 .git，闸门查 Neo 记的那一笔摘要）"]}

# ---------------- 跑（页面里点一下就是这条） ----------------
_LOCK = threading.Lock()
_RUN = {"running": False, "code": None, "started_at": 0.0, "state": "", "notes": [],
        "log": "", "setup_log": ""}


def _mark(**changes) -> None:
    with _LOCK:
        _RUN.update(changes)


def run_state() -> dict:
    with _LOCK:
        return dict(_RUN)


def engine_log_path() -> Path:
    return state.LOGS / ENGINE_LOG_NAME


def log_tail(max_bytes: int = TAIL_BYTES, max_lines: int = 200) -> str:
    return state.tail_text(engine_log_path(), max_bytes=max_bytes, max_lines=max_lines)


def _log_line(text: str) -> None:
    state.ensure()
    with open(engine_log_path(), "ab") as handle:
        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n".encode("utf-8", "replace"))


def github_reachable() -> tuple[bool, str]:
    """够得着 github.com 吗（取源码只认 GitHub，这一条要先说清楚）。"""
    rows = netprobe.probe_cached().get("targets", [])
    row = next((r for r in rows if r["host"] == "github.com"), None)
    if row is None:
        return True, "没量过 github.com，按能连上处理"
    return bool(row["ok"]), f"github.com {'连得上' if row['ok'] else '连不上'}（{row['ms']:.0f} ms）"


def steps(dest=None, ref=None, local_zip="") -> list[dict]:
    """给页面/终端看的清单：引擎目录是哪来的 + 引擎源码这一步 + 交给 setup.py 的那一步。不写不跑。"""
    ref = ref or load_ref()
    p = plan(dest=dest, ref=ref, local_zip=local_zip)
    dest = Path(p["dest"])
    net_ok, net_text = github_reachable()
    resolved, why = paths.strata_root()
    out = [{"name": "引擎目录", "ok": paths.is_strata_root(dest),
            "text": f"{dest}：{why}；{own_text(dest)}"},
           {"name": "引擎源码", "ok": p["state"] == STATE_READY,
            "text": f"{p['state']}：{p['action']}；" + "；".join(p["notes"])},
           {"name": "取源码的路", "ok": bool(net_ok) or p["state"] == STATE_READY,
            "text": f"{net_text}；git {'在' if p['git'] else '不在'}；zip 退路 {p['archive']}"}]
    has_setup = wizard.setup_path(dest).is_file()
    out.append({"name": "交给 setup.py", "ok": bool(has_setup),
                "text": (f"{wizard.setup_path(dest)} 在：venv、依赖、引擎二进制、模型都归它"
                         if has_setup else
                         f"{dest} 里没有 setup.py —— 源码没到位，跑不了安装")})
    return out


def status(dest=None, ref=None, local_zip="") -> dict:
    """给页面的一份现状（与 neo/wizard.py 的 status 同形状）。"""
    ref = ref or load_ref()
    p = plan(dest=dest, ref=ref, local_zip=local_zip)
    items = steps(dest=dest, ref=ref, local_zip=local_zip)
    resolved, why = paths.strata_root()
    return {"dest": p["dest"], "state": p["state"], "action": p["action"],
            "tag": ref["tag"], "sha": ref["sha"], "head": p["head"],
            "root": str(resolved), "root_reason": why,
            "remembered": paths.remembered_root(), "own": p["own"],
            "url": ref["url"], "archive": p["archive"], "git": p["git"],
            "clone_command": " ".join(clone_argv(ref, p["dest"])),
            "checkout_command": " ".join(checkout_argv(ref)),
            "steps": items, "ready": all(x["ok"] for x in items),
            "notes": p["notes"], "run": run_state(), "log_tail": log_tail(max_lines=40)}



def run_engine(dest=None, ref=None, local_zip="", port: int = 0, host: str = "127.0.0.1",
               api_key: str = "", family: str = "", model: str = "", source: str = "",
               setup: bool = True, wait: bool = True, align: bool = False) -> dict:
    """按 pin 把引擎源码树弄好，然后交给 setup.py。

    Neo 自己只做两件事：取源码（git clone 或解压摘要对上的 zip）、切到钉住的 commit。
    venv、依赖、引擎二进制、模型全在 setup.py 那边（neo/wizard.py 那条路）。
    dirty / not_a_git_tree 直接拒绝，不动用户的目录。
    外置引擎目录（external_tag）要动它的 HEAD 得显式 align=True —— 那是用户自己的
    检出，Neo 默认只检查不搬。
    """
    ref = ref or load_ref()
    p = plan(dest=dest, ref=ref, local_zip=local_zip)
    dest = Path(p["dest"])
    move_external = align and p["state"] == STATE_EXTERNAL

    def worker() -> None:
        _mark(running=True, code=None, started_at=time.time(), state=p["state"],
              notes=list(p["notes"]), log=str(engine_log_path()), setup_log=str(wizard.wizard_log_path()))
        if p["state"] in (STATE_DIRTY, STATE_NOT_GIT, STATE_NEED_ZIP) or (
                p["state"] == STATE_EXTERNAL and not align):
            _log_line(f"拒绝：{p['state']} —— {p['action']}；{'；'.join(p['notes'])}")
            _mark(running=False, code=1)
            return
        if p["state"] == STATE_CLONE:
            argv = clone_argv(ref, dest)
            _log_line(" ".join(argv))
            rc, out = git_run(argv, cwd=dest.parent, timeout=GIT_TIMEOUT_S)
            _log_line(f"返回 {rc}\n{out[-4000:]}")
            if rc != 0:
                _mark(running=False, code=rc, notes=[f"git clone 没成功（返回 {rc}）"])
                return
            write_engine_state(dest=str(dest), sha=ref["sha"], tag=ref["tag"], source="git")
        elif p["state"] == STATE_ZIP:
            result = extract_zip(local_zip, dest, ref)
            _log_line("；".join(result["notes"]))
            if not result["ok"]:
                _mark(running=False, code=1, notes=result["notes"])
                return
        elif p["state"] in (STATE_CHECKOUT, STATE_EXTERNAL):
            argv = checkout_argv(ref)
            _log_line(" ".join(argv) + ("" if not move_external else "（外置引擎目录，用户点了对齐）"))
            rc, out = git_run(argv, cwd=dest, timeout=GIT_TIMEOUT_S)
            _log_line(f"返回 {rc}\n{out[-4000:]}")
            if rc != 0:
                _mark(running=False, code=rc, notes=[f"git checkout 没成功（返回 {rc}）"])
                return
            if p["own"]:
                # 只给 Neo 自己装的树记来历：外置树记了就等于把它当自己的收进闸门，
                # 那里 Neo 的文件按设计是 untracked，脏检查会变严，闸门永远过不去。
                write_engine_state(dest=str(dest), sha=ref["sha"], tag=ref["tag"], source="git")

        if setup:
            _log_line(f"交给 setup.py：{wizard.setup_path(dest)} --yes --no-start")
            wizard.run_setup(dest, family=family, model=model, port=port, host=host,
                             api_key=api_key, source=source, wait=True)
        _mark(running=False, code=wizard.run_state().get("code") if setup else 0)

    if wait:
        worker()
        return {"started": True, "plan": p, **run_state()}
    _mark(running=True, code=None, started_at=time.time(), state=p["state"],
          notes=list(p["notes"]), log=str(engine_log_path()))
    threading.Thread(target=worker, daemon=True, name="neo-engine").start()
    return {"started": True, "running": True, "plan": p, "log": str(engine_log_path())}


# ---------------- 命令行 ----------------
def parse_args(argv=None):
    import argparse

    p = argparse.ArgumentParser(
        description="引擎源码树：按 neo/engine_ref.json 钉住的那一个 tag 装、对齐（然后交给 setup.py）")
    p.add_argument("--dest", default="",
                   help=f"引擎源码树放哪（默认按 neo/paths.py 解析出来的那一份根目录；"
                        f"没有引擎时 {paths.ENGINE_DIR_NAME}/）")
    p.add_argument("--ref", default="", help="pin 文件（默认 neo/engine_ref.json）")
    p.add_argument("--set-root", default="", metavar="DIR",
                   help="记住引擎目录（这台机器上别处一键装好的 Strata），写进 neo/state/launcher.json")
    p.add_argument("--clear-root", action="store_true", help="忘掉记住的引擎目录，回到 Neo 自己装")
    p.add_argument("--zip", dest="zip_path", default="",
                   help="离线 zip：git 不可用时用它，SHA-256 必须与 pin 一致")
    p.add_argument("--check", action="store_true", help="只看现状（默认就是看现状）")
    p.add_argument("--ensure", action="store_true", help="按 pin 装 / 对齐，然后交给 setup.py")
    p.add_argument("--no-setup", action="store_true", help="只弄源码树，不跑 setup.py")
    p.add_argument("--align", action="store_true",
                   help="外置引擎目录的版本不是 pin 时，允许 Neo 把它的 HEAD 切到 pin")
    p.add_argument("--dry-run", action="store_true", help="只把要跑的命令打出来")

    p.add_argument("--json", action="store_true", help="出一份 JSON")
    p.add_argument("--family", default="", help="setup.py 的 --family")
    p.add_argument("--model", default="", help="setup.py 的 --model")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--api-key", default="")
    p.add_argument("--source", default="", help="setup.py 的下载源（空 = 按探测建议）")
    return p.parse_args(argv)


def main(argv=None) -> int:
    wide_stdout()
    args = parse_args(argv)
    if args.set_root or args.clear_root:
        out = clear_engine_root() if args.clear_root else set_engine_root(args.set_root)
        for note in out["notes"]:
            print(f"  {note}")
        return 0 if out["ok"] else 1
    try:
        ref = load_ref(args.ref or None)
    except EngineRefError as exc:
        print(f"pin 读不出来：{exc}")
        return 1
    dest = safe_resolve(args.dest) if args.dest else default_dest()
    p = plan(dest=dest, ref=ref, local_zip=args.zip_path)
    if args.json:
        import json

        print(json.dumps(status(dest=dest, ref=ref, local_zip=args.zip_path),
                         ensure_ascii=False, indent=1))
    else:
        resolved, why = paths.strata_root()
        print(f"== 引擎目录 {resolved}（{why}）")
        print(f"   这一份是：{own_text(dest)}")
        print(f"== 引擎源码树 {dest}")
        print(f"   pin：{ref['tag']}（{ref['sha'][:12]}），{ref['url']}")
        print(f"   现在：{p['state']} —— {p['action']}")
        for note in p["notes"]:
            print(f"   {note}")
        print("== 清单")
        for item in steps(dest=dest, ref=ref, local_zip=args.zip_path):
            print(f"  [{'OK' if item['ok'] else '—'}] {item['name']}：{item['text']}")
        print("== 要跑的命令")
        print("  " + " ".join(clone_argv(ref, dest)))
        print("  " + " ".join(checkout_argv(ref)))
        print("  zip 退路：" + p["archive"] + f"（SHA-256 {ref['zip_sha256'][:16]}…）")
    if args.dry_run or args.check or not args.ensure:
        return 0
    result = run_engine(dest=dest, ref=ref, local_zip=args.zip_path, port=args.port,
                        host=args.host, api_key=args.api_key, family=args.family,
                        model=args.model, source=args.source, setup=not args.no_setup,
                        wait=True, align=args.align)

    code = int(result.get("code") or 0)
    print("== 结果")
    print(f"  {'成功' if code == 0 else '没成功（返回 ' + str(code) + '）'}")
    for note in result.get("notes", []):
        print(f"  {note}")
    print("== 输出（尾部）")
    print(log_tail(max_lines=30))
    return code


if __name__ == "__main__":
    sys.exit(main())



