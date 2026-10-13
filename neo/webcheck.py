"""neo/webcheck.py - 页面脚本的静态体检：窗口在打包前就知道页面跑不跑得起来。

为什么需要它（实测 2026-10-13 部署的 NeoStrata.exe，13,981,433 字节）：
`neo/web/index.html` 里 `<script src="/i18n.js">` 与 `<script src="/app.js">` 是**同一个全局
作用域**的两段经典脚本。i18n.js 顶层的 `function t` / `function applyLang` 与 app.js 顶层的
`const t` / `const applyLang` 撞名，浏览器在**解析** app.js 时抛
`SyntaxError: Identifier 't' has already been declared` —— app.js 一行都不执行：`poll()` 没跑起来
（状态胶囊停在"未启动"、卡片全是"—"、"发生了什么"是空的），`document.querySelectorAll(".tab")`
那一批 onclick 一个都没挂（任何点击都没反应）。exe 打成功、进程在跑、8080 与 8083 都在应答、
385 条测试全绿，用户看到的是"全功能不可用"。

逐文件的 `node --check` 看不见跨文件重名（每个文件自己都是合法语法），所以这里按 index.html 的
加载顺序把页面**当一个程序**查：花括号配平、跨文件顶层重名、接线在不在顶层。有 node 时再把拼接
后的整段交给 V8 解析一遍（`node --check`），与浏览器解析经典脚本的语义一致 —— 实测最小重现
`function t(){}; const t = 1;` 报的就是同一句话。

用法：
    python -m neo.webcheck                      体检 neo/web/，不干净非零退出
    from neo.webcheck import problems           回问题清单，空表 = 干净
`neo/build_exe.py` 在调 PyInstaller 之前先跑它，不干净拒绝出包。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import paths

WEB_DIR = "neo/web"
PAGE = "index.html"
SCRIPT_RE = re.compile(r'<script src="/([^"]+)"\s*>\s*</script>')


def js_depth_lines(text: str) -> list[int]:
    """页面脚本每一行开头的花括号深度。跳过注释、字符串、模板串与正则字面量。

    返回的第 k 项就是第 k 行（从 0 数）行首的深度。用它盯"顶层"这件事：
    一个函数体的花括号漏了，后面整段顶层代码就落进它的身体里，行首缩进看不出来，
    深度能看出来。
    """
    depths = [0]
    depth = 0
    i, n = 0, len(text)
    prev = ""                                   # 上一个代码字符（判断这里是正则还是除法）
    REGEX_AFTER = "=(,:[!&|?{};+-*%~^<>"

    def nl(from_i: int, to_i: int) -> None:     # 跳过多行字面量时保持行号对齐
        for k in range(from_i, to_i):
            if text[k] == "\n":
                depths.append(depth)

    while i < n:
        ch = text[i]
        if ch == "\n":
            depths.append(depth)
            i += 1
        elif ch == "/" and text[i + 1:i + 2] == "/":
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif ch == "/" and text[i + 1:i + 2] == "*":
            j = text.find("*/", i + 2)
            end = n if j < 0 else j + 2
            nl(i, end)
            i = end
        elif ch in "'\"":
            j = i + 1
            while j < n and text[j] != ch:
                j += 2 if text[j] == "\\" else 1
            nl(i, min(j + 1, n))
            prev = ch
            i = min(j + 1, n)
        elif ch == "`":
            j = i + 1
            while j < n and text[j] != "`":
                if text[j] == "\\":
                    j += 2
                elif text[j] == "$" and text[j + 1:j + 2] == "{":
                    inner, k = 1, j + 2
                    while k < n and inner:
                        if text[k] == "{":
                            inner += 1
                        elif text[k] == "}":
                            inner -= 1
                        k += 1
                    nl(j, k)
                    j = k
                else:
                    j += 1
            nl(i, min(j + 1, n))
            prev = "`"
            i = min(j + 1, n)
        elif ch == "/" and (not prev or prev in REGEX_AFTER):
            j = i + 1
            while j < n and text[j] != "/":
                j += 2 if text[j] == "\\" else 1
            j += 1
            while j < n and text[j].isalpha():
                j += 1                            # 正则的 flags：/x/gi
            nl(i, min(j, n))
            prev = "/"
            i = min(j, n)
        elif ch in "{}":
            depth += 1 if ch == "{" else -1
            prev = ch
            i += 1
        else:
            if not ch.isspace():
                prev = ch
            i += 1
    depths.append(depth)
    return depths


def js_top_level_names(text: str) -> dict:
    """这个文件在顶层声明的名字 -> 第一次出现的行号（从 1 数）。

    认 const / let / var / function / class 五种，只看行首深度为 0 的行（顶层）。
    注释与字符串里的假声明不算：深度表已经跳过了注释与字面量，这里只把行尾注释切掉。
    """
    depths = js_depth_lines(text)
    names: dict[str, int] = {}
    for k, line in enumerate(text.splitlines()):
        if depths[k] != 0:
            continue
        code = re.sub(r"//.*$", "", line.strip())
        m = re.match(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)", code)
        if not m:
            m = re.match(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", code)
        if not m:
            m = re.match(r"class\s+([A-Za-z_$][\w$]*)", code)
        if m:
            names.setdefault(m.group(1), k + 1)
    return names


def page_scripts(root: Path) -> list[tuple[str, str]]:
    """index.html 里 <script src> 的加载顺序与文件内容：[(名字, 源码)]。"""
    html = (Path(root) / WEB_DIR / PAGE).read_text(encoding="utf-8")
    out = []
    for name in SCRIPT_RE.findall(html):
        path = Path(root) / WEB_DIR / name
        if path.exists():
            out.append((name, path.read_text(encoding="utf-8")))
    return out


def problems(root: Path, use_node: bool = True) -> list[str]:
    """页面脚本的问题清单，按 index.html 的加载顺序把页面当一个程序查。空表 = 干净。"""
    root = Path(root)
    bad: list[str] = []
    page = root / WEB_DIR / PAGE
    if not page.exists():
        return [f"{page} 不在"]
    html = page.read_text(encoding="utf-8")
    order = SCRIPT_RE.findall(html)
    if not order:
        return [f"{page} 里一个 <script src> 都没有 —— 页面没有脚本，按钮与轮询都不存在"]

    scripts: list[tuple[str, str]] = []
    for name in order:
        path = root / WEB_DIR / name
        if not path.exists():
            bad.append(f'{path} 不在，页面里写着 <script src="/{name}">')
            continue
        scripts.append((name, path.read_text(encoding="utf-8")))

    seen: dict[str, tuple[str, int]] = {}
    for name, src in scripts:
        depths = js_depth_lines(src)
        if depths[-1] != 0:
            bad.append(f"{name} 花括号不配平（末尾深度 {depths[-1]}）：后面的代码落进别人的身体里")
        for ident, line in js_top_level_names(src).items():
            if ident in seen:
                where, at = seen[ident]
                bad.append(f"{ident} 在 {where}:{at} 与 {name}:{line} 的顶层都声明了："
                           f"同一全局作用域里这是解析期 SyntaxError，{name} 整段不跑")
            seen[ident] = (name, line)

    app = dict(scripts).get("app.js", "")
    if app:
        depths = js_depth_lines(app)
        top = js_top_level_names(app)
        for fn in ("poll", "render", "ensureFrame"):
            if fn not in top:
                bad.append(f"app.js 顶层没有 {fn}()：页面不刷新或按钮不接线")
        if not any(depths[k] == 0 and re.match(r"^(poll\(\);|setInterval\(poll)", ln.strip())
                   for k, ln in enumerate(app.splitlines())):
            bad.append("app.js 顶层没有 poll(); 也没有 setInterval(poll —— 页面不轮询，"
                       "状态永远停在初始值（未启动、卡片全是\"—\"）")

    i18n = dict(scripts).get("i18n.js", "")
    if i18n and app:
        if "window.NEO_I18N" not in i18n:
            bad.append("i18n.js 没把 NEO_I18N 挂到 window：app.js 的中英切换与主题没有来源")
        if "NEO_I18N" not in app:
            bad.append("app.js 不读 window.NEO_I18N：i18n.js 白加载")

    if use_node and scripts:
        node = shutil.which("node")
        if node:
            joined = "\n".join(src for _, src in scripts)
            with tempfile.TemporaryDirectory() as td:
                probe = Path(td) / "page-combined.js"
                probe.write_text(joined, encoding="utf-8")
                proc = subprocess.run([node, "--check", str(probe)], capture_output=True,
                                      text=True, encoding="utf-8", errors="replace")
            if proc.returncode != 0:
                lines = (proc.stderr + proc.stdout).splitlines()
                detail = next((ln.strip() for ln in lines if "Error" in ln), "解析失败")
                bad.append(f"页面脚本按加载顺序拼起来 V8 解析不过：{detail}")
    return bad


def main(argv=None) -> int:
    """命令行：python -m neo.webcheck [根目录]。不干净回 1。"""
    root = paths.app_root()
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        root = Path(args[0])
    bad = problems(root)
    if bad:
        print("页面脚本体检有问题：")
        for item in bad:
            print(f"  - {item}")
        return 1
    print(f"{root / WEB_DIR} 体检干净："
          f"{'、'.join(n for n, _ in page_scripts(root))} 按加载顺序当一个程序解析通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())





