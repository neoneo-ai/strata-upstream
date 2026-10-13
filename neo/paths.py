"""neo/paths.py - 取绝对路径，不跟着 Windows 的重解析点（reparse point）走。

Path.resolve() 与 os.path.realpath() 会跟着重解析点一层层往下解。Windows 的同步根
（OneDrive、桌面/文档这些已知文件夹）就是重解析点，而 CPython 的 os.readlink() 对
非符号链接、非挂载点的重解析点回的是**标签名** ".SyncRoot"。realpath 把它当成下一段
路径拼回去，一路 ".SyncRoot.SyncRoot..." 拼到递归上限，就是
    RecursionError: maximum recursion depth exceeded
报错消息本身是几十万字符的一行，落进控制台就是刷不到底的墙。Neo 只要一个绝对路径，
不需要跟着链接走，所以这里用 normpath（纯字符串运算，不碰文件系统，不会递归）。

neo/state/launcher.json 里的 root 是用户能改的，--root 也是用户能给的，所以这两条
路径一律走 safe_resolve，不再用 Path.resolve()。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

#: 路径段数上限。正常路径到不了这个数；到得了的就是 ".SyncRoot" 递归链拼出来的。
MAX_DEPTH = 64


def is_reparse(path: str | Path) -> bool:
    """这个路径自己是不是重解析点（符号链接、挂载点、OneDrive 同步根、云占位文件）。"""
    try:
        return bool(getattr(os.lstat(path), "st_reparse_tag", 0))
    except OSError:
        return False


def safe_resolve(path: str | Path, root: str | Path | None = None) -> Path:
    """绝对路径，不跟重解析点走。不访问文件系统，所以不会递归。

    相对路径按 root（默认当前目录）展开；段数超过 MAX_DEPTH 就截断。
    与 Path.resolve() 的差别：不解析符号链接、不解析云占位文件、不检查路径存在。
    """
    base = Path(str(root)) if root is not None else Path(os.getcwd())
    p = Path(str(path))
    if not p.is_absolute():
        p = base / p
    parts = [seg for seg in p.parts if seg != "."]
    if len(parts) > MAX_DEPTH:
        parts = parts[:MAX_DEPTH]
    if not parts:
        return Path(os.path.normpath(str(base)))
    return Path(os.path.normpath(str(Path(*parts))))


# ---------------- 源码跑 / 打包成 exe 跑：Neo 自己的目录与只读资源 ----------------
def is_frozen() -> bool:
    """PyInstaller 的 exe 里跑（sys.frozen）。"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Neo 自己的目录：源码跑是 neo/，exe 跑是 exe 所在的那个目录。

    exe 是设计成放在 Strata 根目录里的（D:\\Work\\Strata\\NeoStrata.exe），
    所以 exe 跑时"根目录"就是这个目录本身，见 manager.default_root()。
    """
    if is_frozen():
        return safe_resolve(Path(sys.executable).parent)
    return safe_resolve(Path(__file__).parent)


def resource_dir() -> Path:
    """打包进 exe 的只读资源在哪：PyInstaller 的解包目录；源码跑就是 neo/。"""
    bundle = getattr(sys, "_MEIPASS", "")
    if bundle:
        return safe_resolve(Path(str(bundle)))
    return safe_resolve(Path(__file__).parent)


def web_dir() -> Path:
    """控制台页面（app.js / style.css / *.html）在哪。exe 里是解包目录下的 web/。"""
    return (resource_dir() / "web") if is_frozen() else (app_dir() / "web")


# ---------------- Strata 引擎的根目录 ----------------
#: 认一个目录是 Strata 根目录的依据：这两样都在才算。setup.py 是 Neo 跑安装要用的
#: （neo/wizard.py），serve/server.py 是 Neo 起服务与代发监控页要用的（neo/engines/strata.py、
#: neo/console_server.py）。
ENGINE_MARKERS = ("setup.py", "serve/server.py")
#: Neo 的向导把引擎源码树放在应用目录下的这个名字里（neo/engine_setup.py）。
#: 不叫 engine/：上游 setup.py 的 engine_dir() 里 engine/ 是**引擎二进制**的目录
#: （根 .gitignore 里 /engine/ 就是它），源码树不能占这个名字。
ENGINE_DIR_NAME = "strata"
#: 用户/脚本指一个引擎根目录的口子（与 --root 同一个意思，给 exe 双击时用）。
ENGINE_ENV = "NEO_STRATA_ROOT"
#: 引擎目录记在 neo/state/launcher.json 里的键名（用户在接入页点名，重启还在）。
ENGINE_ROOT_KEY = "root"


def app_root() -> Path:
    """Neo 的"应用目录"：exe 跑是 exe 所在目录，源码跑是 neo/ 的上一层。

    exe 放进 Strata 根目录时（D:\\Work\\Strata\\NeoStrata.exe）它就是 Strata 根目录，
    这是今天的行为；Neo 自己装了引擎后（D:\\Neo\\NeoStrata.exe）它是 Neo 的目录，
    引擎在它的 strata/ 里。
    """
    return app_dir() if is_frozen() else app_dir().parent


def engine_checkout() -> Path:
    """向导 clone 出来的引擎源码树在哪：<应用目录>/strata。"""
    return app_root() / ENGINE_DIR_NAME


def is_strata_root(path) -> bool:
    """这个目录像不像 Strata 的根目录：ENGINE_MARKERS 那两样都在。"""
    p = Path(str(path))
    return all((p / marker).is_file() for marker in ENGINE_MARKERS)


def remembered_root() -> str:
    """用户在 neo/state/launcher.json 里记住的引擎目录（外置的、一键装好的 Strata）。

    为什么在这里延迟 import state：state.py 在 import 时要用 paths，而这一条要读
    state 的状态目录（exe 在只读目录里时它退到 %APPDATA%，那条判断只有 state 知道）。
    state.py 里那一条 import 时算的 ROOT 已经改成 root() 现算，两边不会在 import
    的半路上撞见对方。
    """
    from . import state
    return str(state.engine_root() or "")


def strata_root(explicit: str = "", env=None, app=None, remembered=None) -> tuple[Path, str]:
    """Strata 的根目录，加上"为什么是它"（这一句给页面、日志和向导看）。

    顺序（4、5 都要目录里两样标记都在才认；1、2、3 是用户点名，照用不检查）：

        1. 显式给的 --root
        2. 环境变量 NEO_STRATA_ROOT
        3. 记住的引擎目录       neo/state/launcher.json 的 root（接入页「引擎目录」那一步写的）
        4. 应用目录本身        exe 放进 Strata 根目录时就是它（今天的行为，逐字节不变）
        5. 应用目录/strata     Neo 的向导 clone 出来的那一份
        6. 都没有             回应用目录，理由里写"这里还没有引擎"，向导会接手去装

    第 4 条排在第 5 条前面是刻意的：exe 在 Strata 根目录里时，第 4 条先命中，
    老用户的路径与今天完全一样；只有 exe 单独放在一个空目录里才会走到第 5 条。
    1、2、3 不检查标记：用户点名指哪儿就起哪儿，起不来由启动那一步明说（neo/engines/strata.py
    找不到 serve/server.py 时报的是它自己的话），Neo 不偷偷换目录 —— 偷偷换目录会让用户
    以为自己跑的是那一台机器上的引擎。
    第 3 条是 2026-10-13 加的外置引擎口子：一键装好的 Strata 在任意目录里，
    记一次就长期用它，Neo 不再需要把引擎复制 / clone 到自己旁边（neo/engine_setup.py
    的默认目标跟着这一条走）。
    """
    env = os.environ if env is None else env
    app = app_root() if app is None else safe_resolve(app)
    if explicit:
        return safe_resolve(explicit, app), f"--root 指定的 {explicit}"
    from_env = str(env.get(ENGINE_ENV) or "")
    if from_env:
        return safe_resolve(from_env, app), f"环境变量 {ENGINE_ENV}"
    remembered = remembered_root() if remembered is None else str(remembered or "")
    if remembered:
        return safe_resolve(remembered, app), f"记住的引擎目录（neo/state/launcher.json 的 {ENGINE_ROOT_KEY}）"
    if is_strata_root(app):
        return app, "Neo 就放在 Strata 的根目录里"
    checkout = app / ENGINE_DIR_NAME
    if is_strata_root(checkout):
        return checkout, f"Neo 的向导装在这里（{app.name}/{ENGINE_DIR_NAME}/）"
    return app, (f"{app} 和 {checkout} 里都没有 setup.py + serve/server.py —— "
                 "这里还没有引擎，接入页的「引擎」那一步可以去装")



def resolve_root(explicit: str = "") -> Path:
    """命令行 `--root` 的默认值：给了就照用，没给就走 strata_root() 那一条规则。

    与 strata_root() 的分工：那条是"程序运行时的根"（连"为什么是它"一起回），
    这一条是命令行用的同一规则（显式值优先、不检查标记，与 --root 的语义一致）。
    没给 --root 时，认的顺序就是 strata_root() 那六条，其中第 3 条是
    neo/state/launcher.json 里记住的引擎目录 —— 外置的、一键装好的 Strata 记一次就长期用。
    2026-10-13 拆仓库前，neo/wizard.py 与 neo/models.py 的默认根是硬写的
    "neo/ 的上一层"——Neo 住在 Strata 检出里时那层就是 Strata 根，拆开后就变成
    没有 setup.py 的仓库根（`neo.wizard --check` 因此报"这里不是 Strata 的根目录"，
    还给出一条指向不存在的 <仓库根>/setup.py 的命令）。默认值只留这一条规则。
    """
    if explicit:
        return safe_resolve(explicit)
    return safe_resolve(strata_root()[0])
