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
