"""neo/deploy.py - 把开发树里的 Neo 放进部署树：先验后写，写完就地体检。

部署树（例如 D:\\Work\\Strata）是上游 Strata 的 git 检出，Neo 的文件在那儿全是 untracked
（AGENTS.md 那条：那边不提交，也不给 origin 加第二个 remote）。换版要换三样：
NeoStrata.exe、neo/ 源码、neo/web/ 页面 —— 只同步 .py 曾把部署树的页面留在坏掉的
t / applyLang 那一版（docs/NEO_DESKTOP.md 记的那次白屏）。

跑之前先过三道闸门，过不了什么都不写：

    1. 引擎闸门：部署树的 git HEAD == neo/engine_ref.json 的 pin，且 git status --porcelain
       为空 —— 上游树干净，Neo 的文件都是 untracked
    2. 路径闸门：要写的路径必须都在 Neo 自己的清单里（neo/**、NEO-START.bat、NeoStrata.exe），
       上游的文件一个都不写
    3. 进程闸门：部署树里的 NeoStrata.exe 在跑就拒绝（文件被锁着，写了也是半个）

写完就地跑 neo/webcheck（页面脚本当一个程序解析）与快测套件，并报告部署树现在的引擎 SHA。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import engine_setup, paths, state
from .paths import safe_resolve

EXE_NAME = "NeoStrata.exe"
BAT_NAME = "NEO-START.bat"
NEO_DIR_NAME = "neo"
#: neo/ 里不进部署树的东西：运行状态、缓存、打包产物
SKIP_DIRS = {"state", "__pycache__", "build", "dist"}
#: 部署树里 Neo 的落点（AGENTS.md：exe + neo/ + NEO-START.bat，全 untracked）
ALLOWED_TOP = (NEO_DIR_NAME, BAT_NAME, EXE_NAME)
#: 部署树里要让 git 看不见的东西：Neo 自己的三样 + 向导 clone 出来的引擎源码树 strata/。
#: 写进 .git/info/exclude（本地忽略，git 自己的机制），不改上游的 .gitignore ——
#: 上游的 .gitignore 是跟踪文件，改它就等于改了部署树的跟踪文件，换版闸门（只看跟踪文件）
#: 立刻永远不过，用户的 `git pull` 也会撞上它。
EXCLUDE_LINES = (f"/{NEO_DIR_NAME}/", f"/{BAT_NAME}", f"/{EXE_NAME}",
                 f"/{paths.ENGINE_DIR_NAME}/")
EXCLUDE_MARK = "# Neo（neo/deploy.py 写的）：Neo 的文件与引擎源码树，本地忽略，不动上游的 .gitignore"
#: 本地忽略文件在 git 里的固定位置（相对部署树根）
EXCLUDE_NAME = ".git/info/exclude"
QUICK_SUITE = ("test_start", "test_models", "test_ui", "test_tokens", "test_netprobe",
               "test_wizard", "test_connectors", "test_engines", "test_manager_cli",
               "test_upstream_api", "test_connect_cli", "test_logging",
               "test_model_settings", "test_paths", "test_engine_setup", "test_deploy")


def wide_stdout() -> None:
    """把 stdout 换成装得下中文与替换符的编码。

    实测 2026-10-13：`--apply` 跑完打印快套件的尾部时抛
    `UnicodeEncodeError: 'gbk' codec can't encode character '\\ufffd'` —— 子进程的输出按
    utf-8 读、读不到的字节变成 U+FFFD，而 Windows 控制台默认是 cp936，那个字符它装不下。
    体检已经跑完了，崩在打印上：换版脚本要说清结果，不能把结果弄丢。
    测试里 stdout 常被换成 `StringIO`（没有 reconfigure），那条路原样不动。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass



def sha256_file(path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(Path(path), "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def neo_source_files(root) -> list[str]:
    """开发树里该进部署树的 Neo 文件（相对路径，排序）。"""
    root = Path(root)
    out = []
    base = root / NEO_DIR_NAME
    if not base.is_dir():
        return out
    for item in base.rglob("*"):
        if not item.is_file():
            continue
        rel = item.relative_to(base)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if item.suffix in (".pyc", ".log", ".zip", ".tmp", ".bak"):
            continue
        out.append(f"{NEO_DIR_NAME}/{rel.as_posix()}")
    if (root / BAT_NAME).is_file():
        out.append(BAT_NAME)
    return sorted(out)


def engine_gate(target, ref=None) -> dict:
    """部署树的引擎闸门：HEAD 是不是 pin、git 干不干净。"""
    ref = ref or engine_setup.load_ref()
    target = Path(target)
    out = {"ok": False, "head": "", "clean": False, "pin": ref["sha"], "tag": ref["tag"],
           "notes": []}
    if not (target / ".git").exists():
        out["notes"].append(f"{target} 不是 git 检出：这一条闸门要的是上游树的 SHA")
        return out
    head = engine_setup.git_head(target)
    out["head"] = head
    dirty = engine_setup.git_dirty(target, tracked_only=True)
    out["clean"] = dirty is False
    if not head:
        out["notes"].append("问不到部署树的 HEAD（git 跑不起来）")
        return out
    if dirty:
        out["notes"].append("git status --porcelain --untracked-files=no 不空："
                            "部署树里有人改了上游的跟踪文件")
        return out
    if dirty is None:
        out["notes"].append("问不到 git 状态，按不干净处理")
        return out
    if head != ref["sha"]:
        out["notes"].append(f"部署树在 {head[:12]}，pin 是 {ref['sha'][:12]}（{ref['tag']}）："
                            "先在上游树 git pull / checkout 到 pin，再来换版")
        return out
    out["ok"] = True
    out["notes"].append(f"部署树就是钉住的 {ref['tag']}（{head[:12]}），git 干净")
    return out


def running_exe(target) -> list[str]:
    """部署树里的 NeoStrata.exe 在跑吗（在跑就锁着文件）。

    两条都问：neo/state/instance.json 记的那个进程，以及 Windows 上真在跑的
    NeoStrata 进程里路径落在这棵部署树里的那些。
    """
    target = Path(target)
    found = []
    info = state.read_json(target / NEO_DIR_NAME / "state" / "instance.json", {})
    if isinstance(info, dict) and state.instance_alive(info):
        found.append(f"instance.json 说进程 {info.get('pid')} 在跑（根目录 {info.get('root')}）")
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32

            class PE32(ctypes.Structure):
                _fields_ = [("dwSize", ctypes.c_uint32), ("th32ProcessID", ctypes.c_uint32),
                            ("th32ParentPid", ctypes.c_uint32), ("pcPriClassBase", ctypes.c_int32),
                            ("dwFlags", ctypes.c_uint32), ("szExeFile", ctypes.c_char * 260)]

            def image_path(pid: int) -> str:
                """进程的可执行文件全路径（QueryFullProcessImageW）。问不到回空串。"""
                handle = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
                if not handle:
                    return ""
                try:
                    buffer = ctypes.create_unicode_buffer(260)
                    size = ctypes.c_uint32(260)
                    if not kernel32.QueryFullProcessImageW(handle, 0, buffer, ctypes.byref(size)):
                        return ""
                    return str(buffer.value)
                finally:
                    kernel32.CloseHandle(handle)

            snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)     # TH32CS_SNAPPROCESS
            entry = PE32()
            entry.dwSize = ctypes.sizeof(PE32)
            root_text = str(target).rstrip("\\/").lower()
            if kernel32.Process32First(snapshot, ctypes.byref(entry)):
                while True:
                    if entry.szExeFile.decode("utf-8", "replace").lower() == "neostrata.exe":
                        where = image_path(int(entry.th32ProcessID))
                        if where and where.rstrip("\\/").lower().startswith(root_text):
                            found.append(f"进程 {entry.th32ProcessID} 在跑：{where}")
                    if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                        break
            kernel32.CloseHandle(snapshot)
        except (OSError, AttributeError, ValueError):
            pass
    return found


# ---------------- 本地忽略（.git/info/exclude，不动上游的 .gitignore） ----------------
def exclude_plan(target) -> dict:
    """部署树的 `.git/info/exclude` 里还缺哪几行（纯读，不写）。"""
    target = Path(target)
    path = target / ".git" / "info" / "exclude"
    lines = []
    if path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = [x.strip() for x in text.splitlines()]
    return {"root": str(target), "path": str(path), "git": (target / ".git").is_dir(),
            "have": [x for x in EXCLUDE_LINES if x in lines],
            "missing": [x for x in EXCLUDE_LINES if x not in lines]}


def apply_exclude(target, dry_run: bool = False) -> str:
    """把缺的忽略行补进 `.git/info/exclude`：只追加，已有一行不补第二遍。

    为什么是 `.git/info/exclude` 而不是 `.gitignore`：上游的 `.gitignore` 是跟踪文件，
    往里加一行就是改了部署树的跟踪文件 —— 换版闸门（只看跟踪文件）从此永远不过，
    用户的 `git pull` 也会撞上它。`.git/info/exclude` 是 git 给"本地忽略"准备的地方，
    它本身是 untracked 的 `.git/` 里的东西，`git status` 与 `git pull` 都不理它。
    """
    p = exclude_plan(target)
    if not p["missing"]:
        return ""
    if not p["git"]:
        return f"{p['root']} 不是 git 检出：忽略行没处补"
    if dry_run:
        return f"要补 {EXCLUDE_NAME}（{len(p['missing'])} 行：{'、'.join(p['missing'])}）"
    path = Path(p["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    head = ""
    if path.is_file():
        head = path.read_text(encoding="utf-8", errors="replace")
        if head and not head.endswith("\n"):
            head += "\n"
    tmp = path.with_name(path.name + ".neo-tmp")
    tmp.write_text(head + EXCLUDE_MARK + "\n" + "\n".join(p["missing"]) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return f"补了 {EXCLUDE_NAME}（{len(p['missing'])} 行：{'、'.join(p['missing'])}）"


# ---------------- 要写什么（纯读：算摘要、比现状，不写一个文件） ----------------
def plan(source, target, exe_path: str = "", ref=None) -> dict:
    source, target = Path(source), Path(target)
    files = neo_source_files(source)
    actions = []
    for rel in files:
        src = source / rel
        dst = target / rel
        digest = sha256_file(src)
        if not dst.is_file():
            kind = "new"
        elif sha256_file(dst) == digest:
            kind = "same"
        else:
            kind = "update"
        actions.append({"rel": rel, "kind": kind, "sha256": digest,
                        "bytes": src.stat().st_size})
    exe_src = Path(exe_path) if exe_path else source / EXE_NAME
    exe_action = {"rel": EXE_NAME, "kind": "missing", "sha256": "", "bytes": 0}
    if exe_src.is_file():
        digest = sha256_file(exe_src)
        dst = target / EXE_NAME
        exe_action = {"rel": EXE_NAME, "sha256": digest, "bytes": exe_src.stat().st_size,
                      "kind": "same" if dst.is_file() and sha256_file(dst) == digest else "update"}
    wanted = {x["rel"] for x in actions}
    extra = [rel for rel in neo_source_files(target) if rel not in wanted]
    gate = engine_gate(target, ref)
    busy = running_exe(target)
    exclude = exclude_plan(target)
    notes = list(gate["notes"])
    if not exe_src.is_file():
        notes.append(f"{exe_src} 不在：先 python -m neo.build_exe 打一个，或用 --exe 指一个")
    for line in busy:
        notes.append(line)
    if exclude["missing"]:
        notes.append(f"{target}/{EXCLUDE_NAME} 里还缺 {len(exclude['missing'])} 行"
                     f"（{'、'.join(exclude['missing'])}）：补上之后部署树的 git status "
                     "只看得到上游的东西")
    return {"ok": bool(gate["ok"]) and not busy and exe_src.is_file(),
            "source": str(source), "target": str(target), "actions": actions,
            "exe": exe_action, "extra": extra, "gate": gate, "busy": busy,
            "exclude": exclude, "notes": notes}


def _copy(src: Path, dst: Path) -> None:
    """整份复制（临时文件 + os.replace），复制完立刻比 SHA-256。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".neo-tmp")
    shutil.copyfile(src, tmp)
    if sha256_file(tmp) != sha256_file(src):
        tmp.unlink(missing_ok=True)
        raise OSError(f"{dst.name} 复制后的 SHA-256 与源文件不一致")
    os.replace(tmp, dst)


def apply(source, target, exe_path: str = "", ref=None, dry_run: bool = True,
          prune: bool = False, tests: bool = True) -> dict:
    """按清单写进部署树，写完就地体检。dry_run 时一个文件都不动。"""
    p = plan(source, target, exe_path=exe_path, ref=ref)
    if not p["ok"]:
        return {"ok": False, "wrote": [], "notes": p["notes"], "plan": p}
    if dry_run:
        dry_note = apply_exclude(target, dry_run=True)
        notes = p["notes"] + (["dry-run：一个文件都没写"] + ([dry_note] if dry_note else []))
        return {"ok": True, "wrote": [], "notes": notes, "plan": p}
    source, target = Path(p["source"]), Path(p["target"])
    wrote = []
    for item in p["actions"]:
        if item["kind"] == "same":
            continue
        _copy(source / item["rel"], target / item["rel"])
        wrote.append(item["rel"])
    if p["exe"]["kind"] != "same":
        _copy(Path(exe_path) if exe_path else source / EXE_NAME, target / EXE_NAME)
        wrote.append(EXE_NAME)
    if prune:
        for rel in p["extra"]:
            victim = target / rel
            if victim.is_file():
                victim.unlink()
                wrote.append(f"删除 {rel}")
    notes = list(p["notes"])
    exclude_note = apply_exclude(target)
    if exclude_note:
        wrote.append(EXCLUDE_NAME)
        notes.append(exclude_note)
    checks = post_checks(target) if tests else {}
    return {"ok": True, "wrote": wrote, "notes": notes, "plan": p, "checks": checks,
            "engine_head": engine_setup.git_head(target)}


def post_checks(target, python: str = "") -> dict:
    """部署完就地跑的两条体检：页面脚本（webcheck）与快测套件。"""
    python = python or sys.executable
    out = {}
    for name, argv in (("webcheck", [python, "-m", "neo.webcheck"]),
                       ("suite", [python, "-m", "unittest",
                                  *[f"neo.tests.{x}" for x in QUICK_SUITE]])):
        try:
            proc = subprocess.run(argv, cwd=str(target), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=1200)
            lines = (proc.stdout + proc.stderr).splitlines()
            out[name] = {"code": proc.returncode,
                         "tail": "\n".join(lines[-6:]) if lines else ""}
        except (OSError, subprocess.SubprocessError) as exc:
            out[name] = {"code": -1, "tail": f"{name} 没跑起来：{exc}"}
    return out


# ---------------- 命令行 ----------------
def parse_args(argv=None):
    import argparse

    p = argparse.ArgumentParser(
        description="把开发树里的 Neo 换进部署树（先过引擎/路径/进程三道闸门，写完就地体检）")
    p.add_argument("--to", required=True, help="部署树（上游 Strata 的 git 检出）")
    p.add_argument("--from", dest="source", default="", help="开发树（默认 Neo 的仓库根）")
    p.add_argument("--exe", default="", help=f"要换的 {EXE_NAME}（默认开发树根目录那一个）")
    p.add_argument("--ref", default="", help="pin 文件（默认 neo/engine_ref.json 钉的那一份）")
    p.add_argument("--apply", action="store_true", help="真写（默认 dry-run）")
    p.add_argument("--prune", action="store_true",
                   help="删掉部署树里开发树没有的 Neo 文件（只删 neo/ 与 NEO-START.bat 里的）")
    p.add_argument("--no-tests", action="store_true", help="写完不跑 webcheck 与快测套件")
    p.add_argument("--json", action="store_true", help="出一份 JSON")
    return p.parse_args(argv)


def main(argv=None) -> int:
    wide_stdout()
    args = parse_args(argv)
    source = safe_resolve(args.source) if args.source else paths.app_root()
    target = safe_resolve(args.to)
    ref = engine_setup.load_ref(args.ref) if args.ref else None
    result = apply(source, target, exe_path=args.exe, ref=ref, dry_run=not args.apply,
                   prune=args.prune, tests=not args.no_tests)
    if args.json:
        import json

        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0 if result.get("ok") else 1
    p = result.get("plan", {})
    gate = p.get("gate", {})
    print(f"== 部署树 {target}")
    print(f"   引擎闸门：{'OK' if gate.get('ok') else '不过'}"
          f"（HEAD {gate.get('head', '')[:12]} / pin {gate.get('pin', '')[:12]}）")
    for line in result.get("notes", []):
        print(f"   {line}")
    counts = {}
    for item in p.get("actions", []) + [p.get("exe", {})]:
        kind = item.get("kind", "?")
        counts[kind] = counts.get(kind, 0) + 1
    print("   要写的文件：" + "、".join(f"{k} {v}" for k, v in sorted(counts.items())))
    if p.get("extra"):
        extra = p["extra"]
        print(f"   部署树里多出来的 Neo 文件（{len(extra)} 个）："
              + "、".join(extra[:8]) + ("…" if len(extra) > 8 else ""))
    for name, data in (result.get("checks") or {}).items():
        print(f"== {name}：{'OK' if data['code'] == 0 else '不过（返回 ' + str(data['code']) + '）'}")
        print("  " + (data.get("tail") or "").replace("\n", "\n  "))
    if result.get("engine_head"):
        print(f"== 部署树现在的引擎：{result['engine_head'][:12]}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
