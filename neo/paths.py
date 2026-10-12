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
