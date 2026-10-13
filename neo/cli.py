"""neo/cli.py - 命令行打印共用的一层：把 stdout 换成装得下中文的编码。

wide_stdout() 原来写在 neo/deploy.py 里（engine_setup.py 抄了一份），
2026-10-13 打印中文的命令行越来越多，就挪到这里共用：deploy、engine_setup、
wizard、models、model_settings、connect、start 的 main 第一件事都是它。

两条实测（2026-10-13，同一台机器，PowerShell 7，控制台代码页 65001，locale cp936）：

    python -m neo.wizard --check        直接跑在控制台里：sys.stdout.encoding = utf-8
                                        （Python 认控制台的 65001），中文是好的
    python -m neo.wizard --check | ...  输出进管道：sys.stdout.encoding = gbk
                                        （回落到 locale），写出去的是 GBK 字节，
                                        读的那一头按 UTF-8 解 —— 中文全成乱码

同一条命令加 `-X utf8` 就是好的，所以差的只有这一层 reconfigure。
`neo/deploy.py --apply` 那次崩的是更硬的一种：子进程输出按 utf-8 读、坏字节变成
U+FFFD，cp936 连那个字符都装不下，直接 UnicodeEncodeError。

测试里 stdout 常被换成 `StringIO`（没有 reconfigure），那条路原样不动。
"""
from __future__ import annotations

import sys


def wide_stdout() -> None:
    """把 stdout 换成 UTF-8，装不下的一律变替换符，不抛。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
