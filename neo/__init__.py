"""Neo Strata - Strata 的桌面管理器。

这个目录是新增的：它只通过 setup.py / serve/server.py 的命令行参数、环境变量和 HTTP 端点
与 Strata 打交道，不修改仓库里的任何现有文件，所以上游频繁更新时 Neo 不会与之冲突。

M1（当前）：桌面壳 + 进程监督 + 崩溃自动重启 + 状态 / 日志 / 聊天 / 监控页。
M2：安装向导与国内网络自动判断。  M3：局域网访问、token 与 Claude Code / Codex 接入。
M4：llama.cpp 引擎适配与打包。
"""

__version__ = "0.1.0"
