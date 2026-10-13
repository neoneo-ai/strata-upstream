# Neo Strata 桌面管理器

Neo 是 Strata 的桌面管理层：一个窗口，管着模型的启动、停止、日志和接入（服务崩了 Neo 不自动
重启，只记下退出码并给一句诊断，2026-10-13）。
它住在 `neo/` 里，只通过上游已有的命令行参数、环境变量和 HTTP 端点与 Strata 打交道，
**不修改仓库里的任何现有文件**，所以上游更新时 Neo 不会与之冲突。

当前进度：**M1 完成**（桌面壳 + 进程监督 + 状态/日志/聊天/监控页；监督器不自动重启，见下面
「监督器的行为」）。
M2（安装向导与国内网络自动判断）、M3（局域网访问、token 与 Claude Code / Codex 接入）、
M4（llama.cpp 引擎适配与打包）还没开始。

## 怎么起来

```
python neo/install_neo.py          # 装 pywebview（装进 Strata 的 .venv，没有 .venv 就装当前 Python）
NEO-START.bat                      # Windows 双击/命令行启动；没有控制台用 neo\NEO-START.vbs
python neo/manager.py              # 直接跑；--engine mock 用 mock 引擎，--no-webview 用浏览器
NeoStrata.exe                      # 打好的单文件 exe，放进 Strata 的根目录双击就跑（见"打成 exe"）
```

pywebview 用 Edge (WebView2) 后端；机器上没有 WebView2 运行时、或没装 pywebview 时，
`neo/manager.py` 自动退回浏览器界面 —— 控制台服务是标准库写的，功能一样，只是没有原生窗口。
这条路上进程在后台挂着、屏幕上什么都没有，所以"第二个实例被挡住了"那句话必须说清
在跑的是谁（见下面"第二个实例"）。

**控制台被 QuickEdit 卡住**：用控制台方式跑（`.bat` / `python -m neo.manager`）时，在窗口里
点一下就会进入选择状态，控制台缓冲区被选中后写 stdout 的线程会停在那里 —— Neo 的
输出走这条线程，窗口就"未响应"了。三个办法：在控制台属性里关掉 QuickEdit；用
`neo\NEO-START.vbs`（没有控制台）；用 `NeoStrata.exe`（窗口版，根本没有控制台）。

### 双击闪退（已修）

双击 `NEO-START.bat` 窗口一闪就没，实测有两个来源：

- 启动器写的是 `python neo\manager.py`。`neo/manager.py` 顶部是包内相对导入，直接当脚本跑会抛
  `ImportError: attempted relative import with no known parent package`，进程立刻退出。现在启动器
  统一走 `-m neo.manager`（`.bat` / `.vbs` / `.ps1` 都改了），`manager.py` 自己也认下 `neo` 包，
  两种跑法都能起来（实测 `python neo\manager.py --help` 与 `python -m neo.manager --help` 都退 0）。
- 退出码非 0 时 `.bat` 以前直接关窗口，看不到原因。现在窗口留着，打印退出码和
  `neo\state\logs\manager.log` 的最后 15 行，按任意键才关；`.bat` 开头加了 `chcp 65001`，
  中文日志在控制台里不再成乱码。

**第二个实例**（比如上一次没退干净）弹"Neo 已经在运行了"模态提示后退出 —— 这个框要点掉，
不是闪退。端口被上一次留下的服务占着时，新实例会接手它（状态页显示"接手已在运行的服务"）。

互斥体按**根目录**分开（`neo/manager.py` 的 `mutex_name_for(root)`）：两个不同 Strata 目录
各跑一个 Neo 不互相挡；同一个目录跑第二个才被挡。第二个实例要说清"在跑的是谁"，靠的是
`neo/state/instance.json`（`state.record_instance()` 记的 pid / root / 端口 / 界面地址 /
窗口还是浏览器，`atexit` 在退出时抹掉）：

```
已经有一个 Neo 在跑：进程 47308，根目录 D:\Work\Strata。
它的界面在 http://127.0.0.1:8083/
要关掉它：任务管理器里结束进程 47308。要绕过这个检查：加 --force。
```

浏览器界面那条路把 `mode="browser"` 记进去，第二句就换成"它没有窗口 —— 界面在浏览器里：…"，
并给出装 pywebview 的命令。记录里那个进程已经没了（上次被任务管理器结束、断电）时说的是
另一句："…但它已经没了 —— 这条记录已清掉"，并顺手把过期记录抹掉（`state.instance_alive()`
用 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` 问，不发消息、不碰那个进程）。
实测 2026-10-12 在 Windows 11：`NeoStrata.exe` 起一个后，第二个实例的日志里就是上面那段话。

## 文件

| 文件 | 作用 |
| --- | --- |
| `neo/manager.py` | 桌面壳：单实例互斥体（按根目录）、窗口几何持久化、pywebview 容器（**不挂 js_api**）、浏览器退回、`--force` |
| `neo/supervisor.py` | 进程监督：启动、探活、退出诊断、事件时间线（**不自动重启**，2026-10-13 删了退避与熔断） |
| `neo/console_server.py` | 控制台服务（标准库 `http.server`）：页面 + JSON 接口；页面里所有动作的落点，含 `POST /neo/open` / `/neo/open-log` |
| `neo/state.py` | 状态落点 `neo/state/`（启动设置、窗口几何、日志、`instance.json`），原子写 + `.bak`；exe 里状态落点见 `pick_state_dir()` |
| `neo/paths.py` | 路径解析：`safe_resolve()` 不跟 Windows 重解析点（OneDrive 同步根的 `.SyncRoot`），深度封顶 64 段；`app_dir()` / `web_dir()` / `is_frozen()` 认 exe 与源码两种落点；`strata_root()` 一条规则认引擎根目录（见"根目录怎么认"）、`app_root()` / `engine_checkout()` |
| `neo/console_io.py` | 控制台输出闸门：单行 400 字符、每秒 30 行、5 秒内同样的行折叠、控制字符清掉；只包 `isatty()` 为真的流，一次 `write()` 里 2–60 行算一整块放行（`BURST_LINES` 行/秒）；`quiet_gui_logging()` 压 pywebview/pythonnet 的内部日志；真流写不动时丢掉而不排队；`NullStream` 接窗口版里 `sys.stdout` 是 `None` 的情况 |
| `neo/ui_watchdog.py` | 窗口线程看门狗：`evaluate_js` 探窗口线程，3 秒没回音就把各线程当前所在帧写进日志 |
| `neo/jobobj.py` | Windows Job Object：Neo 结束时子进程（含引擎、视觉编码器）跟着结束 |
| `neo/engines/` | 引擎适配层：`strata.py`（发现配置、拼命令）、`base.py`、`mock.py`（测试） |
| `neo/netprobe.py` | 下载源探测：TCP 443 连 `huggingface.co` / `hf-mirror.com` / `www.modelscope.cn` / `github.com` 并计时，结果缓存；前三样给"模型从哪个源下"的建议，`github.com` 只给引擎源码树用（不改上游的源选择） |
| `neo/tokens.py` | key 与绑法：`sk-neo-<32 hex>`、掩码、按 base 缓存的 key、本机局域网地址、防火墙提示命令（只给不执行） |
| `neo/wizard.py` | 安装向导：四步现状 + 拼 `setup.py --yes --no-start [--family X --model Y]` 命令 + 后台跑并写 `logs/wizard.log`，`run_state()` 说现在有没有在跑 |
| `neo/engine_setup.py` | 引擎源码树向导：按 `engine_ref.json` 的 pin `git clone --depth 1` 或解压摘要对上的 zip 到 `strata/`，`plan()` 回七个状态之一，之后交给 `setup.py`；命令行 `python -m neo.engine_setup --check \| --ensure [--zip 文件] [--json]` |
| `neo/engine_ref.json` | 钉住的那一份引擎：`url` / `tag` / `sha` / `zip_sha256`（+ 字节数与一句话说明）。bump 就是改这一份 |
| `neo/deploy.py` | 换版脚本：把开发树的 `neo/` + `NEO-START.bat` + `NeoStrata.exe` 写进部署树，先过引擎 / 路径 / 进程三道闸门，写完核对 SHA-256 并就地跑 `webcheck` 与快套件；默认 dry-run，`--apply` 才写 |
| `neo/models.py` | 安装清单：ast 读 `setup.py` 的 `MODELS` / `FAMILIES` 两张表（不 import、不跑它）+ 这台机器（内存 / 显卡 / 盘剩）+ 按内存的推荐 + 装没装（fits / tight / no）；命令行 `python -m neo.models [--json]`（根目录不给就走 `neo/paths.py::resolve_root()`） |
| `neo/connectors.py` | 四个 agent（Claude Code / Codex CLI / OpenCode / OpenClaw）的配方与合并写入：先备份 `.neo-bak`，只动 Neo 自己那一段，解析不了就不动 |
| `neo/connect.py` | 接线命令行：`python -m neo.connect [agent] [--persist] [--lan] [--json]` |
| `neo/web/` | 控制台页面：状态 / 模型 / 聊天 / 监控 / 日志 / 设置 / 接入 |
| `neo/webcheck.py` | 页面脚本体检：按 `index.html` 的加载顺序把页面当一个程序查（跨文件顶层重名、花括号配平、接线在不在顶层），`build_exe` 出包前的闸门；命令行 `python -m neo.webcheck` |
| `neo/NeoStrata.spec`、`neo/build_exe.py`、`neo/neo_entry.py` | 打成一个 `NeoStrata.exe`（见"打成 exe"） |
| `neo/tests/` | 523 个测试（见下）：`_bootstrap.py` 给出 `REPO` 与 `ENGINE`（引擎树 = `paths.strata_root()` 算的那个）与 `server_ready`（引擎的真服务起不动时，用真 `serve/server.py` 的测试跳过并说清缺哪个包），`test_paths.py` 守根目录规则，`test_engine_setup.py` 守 pin / clone / zip 摘要 / dirty / 外置引擎目录，`test_deploy.py` 守换版闸门，`test_harness.py` 守"30 秒时间标准"与那条跳过规则 |
| `neo/requirements-neo.txt`、`neo/install_neo.py` | Neo 自己的依赖，不进上游 `requirements.txt` |

Neo 的运行状态全在 `neo/state/`，被 `neo/.gitignore` 忽略。

## 监督器的行为

```
idle ──启动──> starting ──/health 应答──> running
                  │                        │
                  │ 加载超时                │ 子进程非预期退出
                  ▼                        ▼
             idle（记下退出码与一句诊断，等用户点「启动」）
                  │
                  └── 端口上出现活着的服务 ──> external（接手）
```

- **Neo 任何时候都不自动拉起**：用户点「停止」后不会（`intent=stop` 是硬开关）；服务崩了、
  加载超时后也不会。**2026-10-13 删掉了退避重启与熔断**（原来是"退避 5 / 15 / 45 秒、
  10 分钟内 3 次后熔断 halted、页面上给一个「重试」按钮"）。删它的理由是收益太小、麻烦太大：
  模型重载本来归上游（下面那条「引擎重载不归 Neo」），Neo 这一层重启是重复劳动；麻烦都量过 ——
  这条链路是全套件最慢的一格（`TestCrashRestart` 14.7 秒，全跑 18.3 秒里的八成），引擎依赖缺失时
  它把整张退避表跑完（全跑从 18.3 秒变 5 分钟），熔断还让状态页在端口上有活着的服务时写"未启动"。
  现在崩了只做三件事：记下退出码（状态页「上次退出」那一行）、给一句诊断、在事件里写
  "已停止，点「启动」重新起"。顺带修了一个旧 bug：监督线程停在盯端口那一层（`_watch_external`）
  出不去，熔断后点「重试」等于没点；现在那一层认得"用户又点了一次启动"（`_start_seq`），
  崩了之后点启动真的会再起（`TestCrashStops.test_start_after_a_crash_starts_again`）。
- **探活分三层**：端口在听（TCP）→ `/health` 应答（服务活着）→ `loaded`（模型已加载）。
  模型加载要几分钟，这段时间 Neo 显示"启动中"，不算失败。
- **探活的节奏：进程死活按 `poll_s` 看，`/health` 按 `probe_s` 问**（2026-10-13 改）。之前是
  每一轮都探一次，而一次探活最坏是 TCP 1.2 秒 + `/health` 4 秒 + `/metrics` 4 秒，`proc.poll()`
  只在两轮之间看一眼 —— 子进程崩了要等这一轮探活跑完才看得见。实测 2026-10-13 本机
  （`neo/tests/probes/probe_cost.py`，杀掉服务后逐层计时）：监听被杀掉后 Windows 把那个端口黑洞化
  （SYN 没人回，不是 RST），一次探活 1.21 + 2.02 + 2.02 = 5.25 秒，用户看到的就是"进程已经崩了，
  状态页还写着运行中，自动重启晚 5 秒才开始"（那时还有自动重启；现在崩了不重启，这条计时量的
  是"多久看得见它死了"）。TCP 那一层的超时从 1.2 秒收到 0.25 秒（回环上连
  一个活着的监听是 0.00 秒，同一脚本实测），发现退出从 2.06 秒到 **0.02 秒**、启动到就绪从
  1.82 秒到 **0.61 秒**（`neo/tests/probes/supervisor_events.py`：读监督器自己记的事件时间戳）。
- **引擎重载不归 Neo**：`serve/server.py` 自己会重启引擎（`StrataEngine.restart`，issue #344），
  Neo 只在事件里显示一条提示，不干预。
- **端口上已有活着的服务就接手**：用户先用 `START-HERE.bat` 起来了，Neo 认下它，不重复启动。
  Neo 自己起不来（配置缺东西）时也一样：盯端口那一层探到活着的服务就交回主循环去接手
  （`_watch_external` 在 `intent=run` 时回 True）。之前只有"运行中（外部启动）"这个显示，没有交回
  这一步，Neo 就永远停在"外部启动"、点启动不会接手 —— 2026-10-13 把 TCP 超时从 1.2 秒收到
  0.25 秒后 `test_adopt_shows_config_log` 才暴露这件事：那 1.2 秒正好是给外部服务的启动窗口，
  窗口没了这条路径就断了。
- **认服务是自家的还是外人的，走进程树，不能只比 pid**：启动服务的那个 python 自己会再拉
  一个进程去跑脚本，所以端口上的监听者通常是 Neo 孩子的孩子。实测 2026-10-12 在
  `D:\Work\Strata`：NeoStrata 40856 → NeoStrata 46380 → `.venv\Scripts\python.exe`（跳板）
  46984 → 绑上 8080 的 45868，后两条命令行一模一样；单独启动那个 venv 的 python.exe，
  启动它回 42016、真正跑脚本的是它的孩子 42388。`jobobj.is_descendant` 就是干这件事的：
  按"监听者不是我孩子就是外人的服务"写，会把 Neo 自己正在应答的服务判成外人的并杀掉它。
- **退出给一句能照着做的提示**：从日志尾部认 `out of memory` / `failed to map` /
  `page file` / `already in use` / `driver is too old` 等，措辞对齐 `docs/TROUBLESHOOTING.md`；
  认不出就给一句"服务停了：看日志找原因，或点「启动」重新起"。

## 控制台接口

默认绑 `127.0.0.1`，端口 = Strata 端口 + 3（8080 → 8083），被占用时依次试 +4..+8。
带 `Origin` 的请求必须指向本机，`Host` 不是本机地址的一律 403。

```
GET  /                      控制台页面（静态资源一律 no-store）
GET  /neo/status            状态快照：state / pid / listening / healthy / loaded / metrics / events
GET  /neo/models            可启动的模型（根目录的 strata-*.json，最近用过的排最前）
GET  /neo/log?tail=N        服务日志尾部
GET  /neo/config            POST /neo/config     启动设置（存 neo/state/launcher.json，不回显 key）
POST /neo/start /stop /retry /model
POST /neo/open               {"page":"chat|monitor"} 在浏览器里打开那一页（由壳执行）
POST /neo/open-log           用系统默认方式打开服务日志
GET  /neo/net?fresh=1        下载源连通性（TCP 443）与下载源建议；结果缓存在 neo/state/netprobe.json
GET  /neo/lan                本机局域网地址、可访问的界面地址、防火墙提示、当前 token 状态
POST /neo/token              {"action":"new|reveal|unset|forget"}：new 生成一把 key 写
                             neo/state/launcher.json；reveal 把当前这把原样交回页面（给"复制
                             token"用，只读，且只认对端地址是本机的请求，别处只给掩码）；
                             unset 清掉服务上的 key（缓存留着）；forget 从缓存里删掉这把。
                             new 与 reveal 的应答带完整 key，其余只带掩码
GET  /neo/connect            四个 agent 的接线配方（读配置，不写）
POST /neo/connect            {"agent":"claude|codex|opencode|openclaw"} 写配置文件（先备份）
GET  /neo/wizard             安装向导：现状四步 + 将要执行的 setup.py 命令 + 日志尾部
POST /neo/wizard             后台执行 setup.py --yes --no-start（带 family/model 时加 --family X --model Y）
GET  /neo/catalog            可安装清单：ast 读 setup.py 的 MODELS / FAMILIES 两张表 + 这台机器
                             （内存 / 显卡 / 盘剩）+ 按内存的推荐 + 装没装 + 正在装的那一个 +
                             下载源那一排（探测结论、四个选项的耗时与命令前后缀、记住的那一个）
GET  /neo/catalog?fresh=1    同上，并在后台线程里重新探测一次（页面读缓存，不被慢站点拖住）
POST /neo/install            {"family":"qwen","model":"IQ3_S","source":"modelscope"} 后台跑
                             setup.py --yes --no-start --family qwen --model IQ3_S，输出进
                             neo/state/logs/wizard.log；菜单里没有这一项、或已经有一个在跑，
                             都挡回来并说明原因
POST /neo/source             {"source":"probe|huggingface|modelscope|mirror"} 记住下载源，
                             存 neo/state/source.json，回同一份清单；认不得的源回一条说明，
                             什么都不写、不起子进程
GET  /neo/engine             引擎源码树的现状（接入页的「引擎源码树」卡片读这一条）：pin 的
                             tag / sha / archive 地址、这台机器有没有 git、`github.com` 通不通
                             （吃探测缓存，不联网）、`strata/` 在哪、HEAD 与 pin 对不对、
                             `plan()` 的七个状态与下一步的动作、将要执行的那条 clone 命令、
                             三步的现状、`engine.log` 的尾部、有没有一个在跑
POST /neo/engine             {"action":"install","zip":"D:/x.zip"}：按 pin 取源码，后台线程跑
                             （`wait=False`，一次 clone 要几十分钟，不能把请求线程堵住），
                             取完交给 `setup.py`（`setup=True`，Neo 不自己装 venv / 依赖 / 引擎 /
                             模型），host 与 key 从监督器传下去；输出进 neo/state/logs/engine.log；
                             认不得的 action 回一条说明，什么都不跑；脏树 / 普通目录在动手前拒绝
GET  /i18n.js                中英切换与明暗主题的脚本（与 /app.js 一样 no-store）
GET  /neo/upstream?what=…    上游面板的三块（models / status / metrics），由 Neo 代读：浏览器
                             在 8083 的页面上直连 8080 会被 CORS 挡住，API key 也不出 Neo
GET  /neo/remote             远程接入那一行命令：地址、模型、上下文算好了给，key 只给占位符，
                             复制那一下才由 /neo/token 的 reveal 填上
GET  /neo/model/settings     一个模型的运行参数（上下文 / KV / 图片 / 显存保留）+ 每栏的可选项
     ?model=ID              + 显存估算 + 有没有覆盖配置 + 要不要重启；没点名就用第一个配置，
                             点了名找不到就把清单列出来，不悄悄换成别的模型
POST /neo/model/settings     写覆盖配置 neo/state/configs/strata-<id>.neo.json（上游那份一个
                             字节都不动）；越界与认不得的键挡回来并说明；{"action":"reset"}
                             删掉覆盖配置，下一次启动回到 setup.py 写的那份
```

## 接入页（M2/M3）：装模型、开局域网、接四个 agent

页面在窗口顶栏的「接入」，命令行是 `python -m neo.wizard` 和 `python -m neo.connect`。
**默认路径不变**：不写任何用户文件、服务绑 `127.0.0.1`、不带 key；这一页的每一项都要用户点名才动。

### 模型页的「下载安装模型」（`neo/models.py`）

清单不在 Neo 里抄一份：`neo/models.py` 用 `ast` 读 `setup.py` 的 `MODELS` / `FAMILIES` 两张表
（不 import、不跑它，文件改了缓存自动失效），装没装是扫根目录的 `strata-*.json`（配置在、文件不在
的记「装好一半」），机器是问 `GlobalMemoryStatusEx`（与 `setup.py` 的 `ram_gb()` 同一算法）、
`nvidia-smi`（60 秒内不重复问，AMD 卡问不到就说问不到）、`shutil.disk_usage` 看盘剩多少。

推荐按内存分档，档位是 `docs/AI_SETUP.md` 第 3 步与 `docs/MODELS.md` 的 Pick by RAM：
32 GB → Coder IQ1_M，48 与 64 GB → IQ2_XS，96 GB 以上 → IQ3_S。装不装得下按 `setup.py --check`
的口径：需要内存不超过本机内存 = 装得下，超出 8 GB 以内 = 勉强，再多 = 装不下；unsloth 那两档
是流式加载，按它自己的下限（48 GB）算。

点「安装」= `POST /neo/install` = 后台跑 `setup.py --yes --no-start --family X --model Y`，
输出进 `neo/state/logs/wizard.log`，页面在模型页时每 5 秒刷一次清单与日志尾部。Neo 不自己下载：
下载、校验、写 `strata-*.json` 全是 `setup.py` 的事；菜单里没有这一项、或已经有一个安装在跑，
接口都挡回来并说明原因，不起子进程。命令行同一条路：`python -m neo.models`（根目录不给就走
`neo/paths.py::resolve_root()`；`--json` 出机器可读的那份）看清单，
`python -m neo.wizard --family coder --model IQ1_M` 装。

清单下面那一排是**下载源**：`自动（按探测）` / `Hugging Face 官方` / `ModelScope 国内` /
`hf-mirror 镜像`，选了就 `POST /neo/source` 记住（`neo/state/source.json`），命令预览立刻跟着变。
四个选项全走上游本来就有的口子，Neo 不改 `setup.py` 一行：官方与 ModelScope 是 `--source` 的值，
镜像是子进程环境里的 `HF_ENDPOINT=https://hf-mirror.com`（`setup.py` 自己读它，`--source` 里没有
镜像这一项，所以镜像不带 `--source`）。「重新探测」按钮 = `GET /neo/catalog?fresh=1`，在后台线程里
测，页面读缓存。实测 2026-10-13 这台 PC：`自动` 建议 ModelScope 时命令是
`python setup.py --yes --no-start --source modelscope`，选镜像时是
`HF_ENDPOINT=https://hf-mirror.com python setup.py --yes --no-start`，两行都写进 `wizard.log` 头部。

实测 2026-10-13 这台 PC（Windows 11，Python 3.13，94 GB 内存，RTX 5090 D v2 24 GB 显存，
D 盘剩 487 GB）：清单 10 项，推荐 `--family qwen --model IQ3_S`（83.6 GB 下载，需要约 62 GB 内存），
已装的 Swift 1.5 IQ3_XXS 在清单里标「已装」、按钮是「已装好」。

### 下载源探测（`neo/netprobe.py`）

对 `huggingface.co` / `hf-mirror.com` / `www.modelscope.cn` 做 TCP 443 连接计时，结果缓存进
`neo/state/netprobe.json`（`GET /neo/net?fresh=1` 重测）。实测 2026-10-12 这台 PC（Windows 11，
家庭网络）：`huggingface.co` 196 ms、`hf-mirror.com` 4052 ms、`www.modelscope.cn` 36 ms，三个都连得上，
建议保持默认源，`setup.py` 命令里不加 `--source`。

Neo **不改上游的下载源选择**，只把用户点的那一个翻译成上游本来就认的参数：`--source modelscope` /
`--source huggingface`，镜像走 `HF_ENDPOINT`（见上）。探测的结论只用于「自动」这一档：
`huggingface.co` 连不上而 ModelScope 或镜像连得上时，建议那一个并说清为什么；三个都连得上就
不加 `--source`，用 `setup.py` 的默认源。用户随时可以越过建议自己点。

### 安装向导（`neo/wizard.py`）

四步：环境（根目录与 `setup.py`）→ 模型（`strata-*.json`）→ 网络（探测结论）→ 安全（绑法与 key）。
"开始安装"跑的是 `[HF_ENDPOINT=https://hf-mirror.com] python setup.py --yes --no-start [--source ...]
[--host ...] [--api-key ...]`（方括号里那截环境是选镜像时才加的，见上），
输出进 `neo/state/logs/wizard.log`，页面每 5 秒读尾部。`--no-start` 是必须的：服务归监督器起，
不能让 `setup.py` 自己起一个占住端口。实测 2026-10-12：跑一个打印参数的桩 `setup.py`，
子进程 4.4 秒（venv 的 python 冷启动），退出码 0，命令行与输出都落在 `wizard.log` 里。
`--check` 只探测不安装，`--dry-run` 只打印将要执行的命令。

### 局域网与 token（`neo/tokens.py`）

- key 形状 `sk-neo-<32 位 hex>`（`secrets.token_hex(16)`），界面上平时只显示掩码
  `sk-neo-xxxxxxxx…（39 字符）`。
- **一键复制**：接入页的「复制 token」把当前生效的那把完整 key 送进剪贴板（`navigator.clipboard`，
  缺权限时退回 `execCommand("copy")`，与「复制服务地址」「复制命令」同一条路）。完整 key 只出现在
  token 那一行的只读输入框里；掩码一变（换了 key、清了 key）页面立刻把它清掉，复制到的是当前这把。
  取 key 走 `POST /neo/token {"action":"reveal"}`：只读、不写任何文件，且只认**对端地址**是本机
  （127.0.0.1/::1）的请求 —— 控制台绑 `0.0.0.0` 时，从局域网来的请求只拿得到掩码。
- 每个 base 用过的 key 存 `neo/state/agent_keys.json`（分 saved / minted 两桶），切绑法时不用重新生成。
- 绑 `0.0.0.0` 时列出本机局域网地址与 `http://<ip>:<port>` 界面地址，并给一条**只提示不执行**的
  Windows 防火墙命令（`netsh advfirewall firewall add rule ... port=<端口>`）。
  绑局域网而不带 key 会被 `wizard.py` 判成"未就绪"，`neo/manager.py` 也会打一行警告。

### token 管到哪，监控页怎么读到数据（`neo/console_server.py` 的 `/strata/`）

token 只管模型：所有 `POST`（`/v1/*`、`/settings`、`/config`、`/load`、`/unload`、`/slots/*`）与模型清单
（`/models`、`/v1/models`）在任何地址都只认 key。监控的只读端点（`/metrics`、`/metrics/prometheus`、
`/status`、`/slots`、`/props`、`/config`、`/settings`、`/mcp`、`/api/requests`、`/v1/status`）设了 key 之后
**同样要 key** —— 上游对这些端点没有本机豁免。

Neo 不动上游这一条：先前打在 `serve/server.py` 上的本机豁免补丁已在 2026-10-13 撤掉，部署树
（`D:\Work\Strata`，上游仓库的一个 checkout）里的 `serve/server.py` 恢复成上游原样（391,688 字节，
`git status` 里不再有已修改的跟踪文件）。监控页要读数据走 Neo 自己的只读通道：控制台把上游的页面与
面板端点从自己的端口转发出去，key 由 Neo 在服务端附上，页面看不到 key。状态页给的监控地址因此是
`http://127.0.0.1:8083/strata/#monitor`；聊天页照旧直连上游（`http://127.0.0.1:8080/`）—— 聊天要 `POST`，
Neo 不替聊天页花用户的 token。

通道的规矩（`neo/console_server.py` 的 `STRATA_READ_PATHS` / `STRATA_READ_PREFIXES`）：

| 规矩 | 具体 |
| --- | --- |
| 只转 GET 与 HEAD | `POST /strata/*` 一律 405，写与推理转不出去 |
| 白名单 | `""`、`/api-monitor`、`/health`、`/metrics`、`/metrics/prometheus`、`/status`、`/slots`、`/props`、`/config`、`/settings`、`/mcp`、`/api/requests`、`/models`、`/v1/models`、`/v1/status`；前缀 `/web/`、`/fonts/`、`/slots/` |
| 推理不转 | `/v1/*` 里除 `/v1/models`、`/v1/status` 之外一律 405（`/v1/chat/completions` GET 也 405） |
| 上游没起来 | 502，说清是上游没应答 |
| 一次的上限 | 应答最多 8 MB、上游请求 5 秒超时 |
| 不遮 Neo 自己的路 | `/strata` 与 `/strata/` 都归到上游根（`""`），Neo 的 `/neo/*` 路由照常 |

上游的页面用相对路径发请求，所以 `/strata/` 这个前缀天然把页面、它的 js/css、它的数据请求一起带过来。
`/api-monitor` 与 `/api/requests` 要上游配置里 `api_monitor` 开着才发，关着时通道原样转回 404。

实测 2026-10-13（mock 引擎设了 key，`tools/_strata_channel_check.py`，23 条全过）：直连上游 `/metrics`
无 key 401；经通道 200，`live`/`totals`/`hardware`/`hardware_static` 四节齐，应答里不带 key；上游页面与
`web/app.js`、`web/tokens.css` 都 200；`POST /strata/settings` 405、`POST /strata/v1/chat/completions` 405、
`GET /strata/v1/chat/completions` 405、`POST /strata/load` 405；上游带 key 的 `/v1/models` 200、无 key 401。
浏览器层（`tools/_strata_page_check.py`，headless Edge，10 条全过）：同一个上游页面直连时 DOM 里写着
"API key needed"，经通道时没有这句、监控标签是激活的、`renderMonitor` 跑到位、状态徽标点亮。

回归测试：`neo/tests/test_console.py` 的 `TestStrataChannel`（假上游，覆盖页面与资源、服务端附 key、
查询串、写操作、推理、路径拒绝、上游没起来、不遮 Neo 自己的路、路径常量一致）和
`neo/tests/test_engines.py` 的 `test_pages_monitor_goes_through_the_console`；
`neo/tests/test_upstream_api.py` 反过来盯"Neo 不依赖上游补丁"——上游文件里不许出现豁免补丁的标记，
而监控页与那些只读端点必须还在。

加一个控制台端点的规矩：`Handler` 里写 `self.server.X(...)`，`ConsoleServer.start()` 就得有一行
`httpd.X = self.X` —— httpd 才是 `self.server`，控制台的方法是一个一个挂上去的。挂漏了不会崩在页面上：
AttributeError 被 socketserver 的 `handle_error` 吞掉，连接直接关掉，`curl` 只看到
`Empty reply from server`，页面那边就是一直转圈（2026-10-13 的引擎卡：`/neo/engine` 两条都挂漏了）。
`test_console.py` 现在钉两条：源码里 `self.server.X(` 用到的名字必须都在 `start()` 的挂载清单里，
以及真走一次 HTTP 的 `GET /neo/engine`（只直接调方法测不到这件事）。

### 四个 agent 的配方（`neo/connectors.py`）

| agent | 落点 | Neo 写的那一块 |
| --- | --- | --- |
| Claude Code | `~/.claude/settings.json` | `env` 里的 `ANTHROPIC_BASE_URL`（**不带 `/v1`**）、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_MODEL` 与四个关掉遥测/实验特性的变量 |
| Codex CLI | `~/.codex/config.toml` + `neo.config.toml` | `[model_providers.neo]`（`base_url` 带 `/v1`、`wire_api = "responses"`、`requires_openai_auth = false`、`stream_idle_timeout_ms = 1200000`）+ `[profiles.neo]` |
| OpenCode | `~/.config/opencode/opencode.json` | `provider.neo` 用 `@ai-sdk/openai-compatible`，模型 id 带 `neo/` 前缀，输出上限取 `window // 4`（封顶 32000），compaction 预留 `min(max(0.3×window, 4000), 15000)` |
| OpenClaw | `~/.openclaw/openclaw.json` | `models.providers.neo`（`api = "openai-completions"`）+ `agents.defaults.model.primary = neo/<id>`；顺带把 `memory.search.provider` 设成 `none`、`env.shellEnv.enabled` 设成 `false`，否则它会自己去找真 OpenAI key |

写文件的规矩（`test_connectors.py` 逐条盯着）：

- **合并，不覆盖**：只动 Neo 自己的那一段，用户其余的键原样留着。
- **写之前先备份**成 `<文件>.neo-bak`；JSON 解析不了的 TOML/JSON 一律**不动文件**，回一条警告。
- **幂等**：重跑替换 Neo 自己那一段，不叠加重复块；`GET /neo/connect` 只读不写。
- 目录认 `CLAUDE_CONFIG_DIR` / `CODEX_HOME` / `XDG_CONFIG_HOME` / `OPENCLAW_HOME`，测试就是靠这四个
  变量把四个 agent 的配置全落进临时目录，不碰真实用户配置。
- Claude Code 只认 `--settings` 传进来的 env，`--settings` 不读 `~/.claude`，所以配方里同时给一份
  PowerShell / POSIX 的 `$env:` / `export` 块。带 key 时要 unset 的 11 个变量列在配方里
  （`ANTHROPIC_API_KEY`、`CLAUDE_CODE_OAUTH_TOKEN`、`ANTHROPIC_UNIX_SOCKET`、Foundry/Bedrock/Vertex/
  AWS/Google Cloud/Mantle 那六个开关及其 base_url/resource）——留着任何一个都会把请求带去真 Anthropic。

命令行示例（实测 2026-10-12，`--key` 显式给出时不读用户配置）：

```
python -m neo.connect list
python -m neo.connect claude --port 8080 --key sk-neo-... --model qwen3.8-flash-next
python -m neo.connect codex --persist --lan
python -m neo.wizard --check
```

## 引擎源码树：按 pin 装（`neo/engine_setup.py`、`neo/engine_ref.json`）

Neo 与 Strata 分家之后，引擎源码不在 Neo 的仓库里，Neo 按一个**钉住的 tag** 取它。
`neo/engine_ref.json` 是一份 JSON，四样东西：仓库地址、tag、commit SHA、archive zip 的
SHA-256。今天钉的是 `v0.1.43.1` = `a0116b2782a5b82622358c04a06496485d12b2d6`
（部署树 `D:\Work\Strata` 的 HEAD 就是它）。

**取源码只认 GitHub 的 tag。** ModelScope 那类镜像给的是一个仓库的当前文件，不是钉住的
revision —— 上游 `setup.py` 的 `model_source()` 里那句 `#908` 写的就是这件事。镜像只用于
下模型（见"下载源探测"），不用于取源码。

两条路：

```
git clone --branch v0.1.43.1 --depth 1 --single-branch <url> strata/
<url>/archive/refs/tags/v0.1.43.1.zip          （这台机器没有 git 时的退路）
```

浅克隆出来是 detached HEAD，正合用：pin 是一个 commit SHA，不是一条分支。
zip 是 GitHub 现打包的，**没有官方摘要**，所以 `zip_sha256` 是 Neo 这边算好钉进 pin 的：
2026-10-13 下载 `v0.1.43.1` 的 archive zip，21,786,355 字节、3415 个条目，本地算出
SHA-256 `9e37f07e87473210ac98ee6041e6651c02e1c473bf75eae65191840fa2b80768`，这一串写进
`engine_ref.json`；解压前 Neo 自己再算一遍，对不上就一个文件都不解。

**目录叫 `strata/`，不叫 `engine/`。** 上游 `setup.py` 的 `engine_dir()` 里 `engine/` 是
**引擎二进制**的目录（根 `.gitignore` 里 `/engine/` 就是它），源码树不能占这个名字。
`strata/` 在根 `.gitignore` 里：它是 clone 出来的，不进 Neo 的仓库。部署树里这一行不在
`.gitignore`（那是上游的跟踪文件），在 `.git/info/exclude`，由 `neo/deploy.py` 补上。

`plan()` 是纯函数（不联网、不写文件），状态七个：`ready`（HEAD == pin 且 git 干净）、
`clone`、`zip_copy`、`need_zip`、`checkout_pin`（HEAD 不是 pin）、
`dirty`（有人改过 → 拒绝，Neo 不动用户的改动）、`not_a_git_tree`。zip 解出来没有 `.git`，
HEAD 查不了，所以 Neo 在 `neo/state/engine.json` 里记一笔来历（装的时候摘要对上了 pin
才写得进去），`ready` 认这一笔。

拿到源码之后 Neo 什么都不装：venv、依赖、引擎二进制、模型全交给上游 `setup.py`
（`neo/wizard.py` 那条 `--yes --no-start`）。页面上是「接入」页的「引擎源码树」卡片
（`GET /neo/engine`、`POST /neo/engine {"action":"install"}`），命令行是：

```
python -m neo.engine_setup --check
python -m neo.engine_setup --ensure --no-setup
python -m neo.engine_setup --ensure --zip D:\Downloads\Strata-0.1.43.1.zip
```

`netprobe.py` 因此多量一个 `github.com`（443，与模型源一样只测连不连得上）。它不参与
"模型从哪个源下"的建议 —— 那条建议只看 huggingface / hf-mirror / modelscope。

### 根目录怎么认（`neo/paths.py` 的 `strata_root()`）

一条规则，程序与测试共用（`neo/tests/_bootstrap.py` 里的 `ENGINE` 就是它算出来的）：

```
--root  >  NEO_STRATA_ROOT  >  应用目录本身  >  <应用目录>/strata  >  应用目录（理由里写"这里还没有引擎"）
```

第 3、4 条要目录里 `setup.py` 与 `serve/server.py` 都在才认；前两条是用户点名，照用不检查
（起不来由启动那一步明说，Neo 不偷偷换目录）。exe 放进 Strata 根目录时（今天所有用户的
装法）第 3 条先命中，解析结果与改动前逐字节一致。

### bump：换一版引擎要动什么

1. 上游发新 tag → 改 `neo/engine_ref.json` 的 `tag` 与 `sha`；
2. 下载 `archive/refs/tags/<tag>.zip`，本地算 SHA-256 填进 `zip_sha256`（顺手记下字节数）；
3. 部署树 `git checkout <tag>`（或 `git pull --ff-only`），再 `python -m neo.deploy --to <部署树>`；
4. 全量测试重跑一遍，这一节与"测试"一节的数字按实测更新。

## 模型设置页与语言/主题（`neo/model_settings.py`、`neo/gguf_meta.py`、`neo/web/i18n.js`）

模型设置页改的是**一个模型的启动参数**，四栏：上下文长度（`--max-context`）、KV 类型（`--kv`）、
KV 常驻单元（`--kv-resident`）、显存保留（`--vram-reserve-mib`），外加图片开关（`--vision` 与 `vision` 段）。
它是「设置」页里的第一块，左边栏不再有独立页签（2026-10-13 并入）：控件 id 一个没动，
`showPage("settings")` 一次把「模型设置」与「启动设置」两块都拉出来（`if (name === "settings") { loadConfig(); loadModelSettings(); }`）。
写法是**覆盖配置**：`setup.py` 写的那份 `strata-*.json` 只读，改动写进 `neo/state/configs/strata-<id>.neo.json`，
启动时 `config_path_for()` 指到哪份就用哪份；删掉覆盖配置就是原样。四个数怎么来的都摆在页面上：
KV 显存 = 常驻单元 × 每单元每层字节 × 层数，每单元每层的字节数出自 `bench/results/2026-09-27-kv-q4/README.md`
（q4_0 576 B、int8 1,056 B），层数读 GGUF 头部（`neo/gguf_meta.py`，只读头部，不加载模型）。

三个坑，测试守着：

- **关图片要两件一起做**：`serve/server.py:2348`（#1322）看见配置里 `vision` 段还在就把 `--vision` 加回去，
  所以只删 flag 等于没关。Neo 删 flag 的同时把段置空，原段存进 `neo_vision_saved`，再开图片时还原回去
  （mmproj 的路径 Neo 不猜，只还给人家原来那份）。
- **`--kv-resident` 带值**：只删 flag 会把那个数变成一个孤零零的位置参数，引擎会把它当模型文件。删就连值一起删。
- **页面传的是 "on"/"off" 字符串**：`bool("off")` 是 True，所以字符串逐字认（`on/true/yes/1/开`、
  `off/false/no/0/关`），认不得的报错，不猜。

覆盖配置 = 上游那份 + 这次的改动，所以跟上游一样的键不写进去；这次要的跟上游一样时，
已有的覆盖配置会被删掉（回到原样），而不是留着一份没用的。点了名却认不得的 id 不悄悄换成别的模型。

语言与主题只在页面里，Neo 与上游都不记：`neo/web/i18n.js` 认 `data-i18n` 键（静态文案整块换）与
`app.js` 里的 `t("中文")`（动态文案），记忆键 `neo.lang` / `neo.theme` 存在 localStorage 里。
**没有记忆时页面与以前逐字一样**（中文 + 暗色），i18n.js 不改一处文字。整个 i18n.js 包在一个 IIFE 里，
只往外递一个 `window.NEO_I18N`（app.js 取的就是这个命名空间）：`<script src="/i18n.js">` 与
`<script src="/app.js">` 是同一个全局作用域的两段经典脚本，顶层留名字就会与 app.js 撞名，浏览器解析
app.js 时整段不跑（见「窗口上任何点击都没反应、状态永远「未启动」」）。主题只换 CSS 变量：
`style.css` 里 `html[data-theme="light"]` 覆盖 `:root` 那一批变量，组件一律用 `var(--…)`，
所以换主题不改任何一条组件规则（`neo/tests/test_ui.py` 里有一条量这个：`:root` 与
`html[data-theme="light"]` 之外的规则里不许出现硬编码颜色）。

## 模型配置的形状（`strata-*.json`）

配置是 `setup.py` 写的，Neo 只读它、拼成 `serve/server.py` 的命令行参数，不改上游一行。
实测 2026-10-12 在 `D:\Work\Strata`（4 个配置全部识别，`installed=True`）：

```
{"exe": "engine/strata.exe", "cwd": "D:\\Work\\Strata",
 "args": ["--native", "D:\\Work\\Strata-data\\models\\swift-IQ3_XXS\\...gguf",
          "--pack", "D:\\Work\\Strata-data\\packs\\swift-iq3_xxs",
          "--max-context", 202072, "--mtp", "...", "--gpu", "0", ...],
 "model_name": "swift-1.5-iq3_xxs", "port": 8080, "tokenizer": ..., "log": ..., "vision": {...}}
```

- **模型文件在 `args` 的 `--native` 里**（其次 `--pack`、`vision.model`），**没有 `path` 键**；
  上下文在 `--max-context` 里（其次 `--context`）。`neo/engines/strata.py` 的
  `model_file_of()` / `context_of()` / `arg_value()` 两种形状都认（扁平列表与老字典）。
- 早期只认 `path` + 字典 `args` 的版本在真实配置上永远算不出模型文件，启动时报的是
  **空消息"模型文件不在："**（`cfg["path"]` 本来就是空的）—— 实测 2026-10-12 的 `manager.log`。
- 引擎程序：配置里的 `exe` 键优先（相对路径按 `cwd` 展开，与 `serve/server.py` 里那句注释一样），
  没有就看根目录的 `strata.exe` / `strata-hip.exe` / `strata-sycl.exe`。
- 缺什么就一条一句写清（`missing_parts()`），启动被拒时原样给用户，不再报空消息。
- 模型目录默认在 Strata 的 `<根目录>-data`（实测 `D:\Work\Strata-data`：`models` / `mtp` / `packs`）。
- 没点名模型时挑一个**能启动的**配置（`base.py` 的 `default_config()`：点名的 → 第一个已安装的 →
  第一个），全都不行才回第一个，让报错说的是它。

## 日志与控制台输出：刷屏的四个来源，逐个实测过

窗口被刷满这件事有四个独立的来源，各自修各自的，措辞与数字都来自实测
（复现脚本 `neo/state/repro/crlf.py`、`neo/state/repro/flood.py`）。

**1. `manager.log` 里的 `\r` 会随写入次数长出来（已修）。**
`state.log()` 以前用文本模式回写日志尾部做清洗：`\n` 写出去变 `\r\n`，读回来再写就变
`\r\r\n` —— 每写一次，前面每一行多一个 `\r`（实测连续 6 次写入后第一行带 6 个 `\r`）。
控制台里每个 `\r` 把光标送回行首，一行就重画一遍，看起来像同一句话反复刷。
现在写入走二进制、清洗只去 `\r` 不限行数（历史不丢）。实测连续写 8 条后文件里 0 个 `\r`。

**2. 一条几十万字符的行，以前原样穿过每一层（已修）。**
pywebview/pythonnet 的属性链递归日志、`RecursionError` 的消息都是这种行。现在每一层都有界：

| 层 | 上限 | 实测 |
| --- | --- | --- |
| 控制台 stdout/stderr（`neo/console_io.py`） | 单行 400 字符、每秒 30 行、5 秒内同样的行折叠成一条计数、控制字符（`\r`、BEL、ANSI 序列）换空格；只包 `isatty()` 为真的流，一次 `write()` 里 2–60 行算一整块放行 | 5000 条 36 万字符的 `.SyncRoot` 行（共 18 亿字符）灌进闸门 → 出来 441 字符、2 行、最长行 419 字符、0 个 `\r` |
| `neo/state/logs/manager.log` | 单行裁到 400 字符并标"原长 N 字符"；写入前裁到尾部 200 KB | 同样 5000 条巨行 → 文件 196 KB、454 行、最长行 419 字符、0 个 `\r` |
| `GET /neo/log` | 一次最多 60 KB / 600 行，行内已裁 | 灌过后应答 539 字符 |
| 日志页（`neo/web/app.js`） | 一次取 60000 字符、最多渲染 400 行、内容没变不重画 | 页面不再被一条巨行卡死 |
| `NEO-START.bat` 打印的尾部 | 15 行、控制字符换空格、单行 200 字符 | 实测 15 行、最长 77 字符、中文正常 |
| 未捕获异常 | traceback 最多 40 行 / 4000 字符；`RecursionError` 不格式化 traceback（上千帧、消息本身几十万字符，格式化它自己要再递归一层），只给一句指向 pywebview 与同步目录的提示 | 90019 字符的异常消息进日志后单行 416 字符 |

**5. 闸门把机器要读的整块裁掉了（已修）。** 闸门按行裁（30 行/秒），`neo/manager.py`
在 import 时就装它，于是两条实测到的副作用：`python -m neo.connect --json` 的 30 行 JSON
被丢掉 26 行（页面拿到半截 JSON，`json.loads` 报 "Expecting value: line 31 column 1"）；
测试里 `redirect_stdout` 换上的 `StringIO` 也被当成控制台包了闸门，unittest 的 traceback
被折叠成"同样的行重复 N 次"，看不出是哪个测试出的错。现在两条规矩：`install()` 只包
`isatty()` 为真的流（管道、文件、`StringIO` 一律不包 —— 刷屏只在控制台发生）；一次
`write()` 里 2–60 行算一整块放行，从"整块额度"（`BURST_LINES` = 60 行/秒）里扣，超了照样丢。
实测：一次写 40 行全部通过；一秒内两块 40 行，第二块超出额度被丢；500 行一次一行的洪水
仍压到 120 行以内（`test_logging.py` 的 `test_one_print_block_passes_whole`、
`test_burst_allowance_is_also_capped`、`test_rate_cap_drops_excess`、
`test_install_leaves_a_pipe_alone`）。

`.bat` 里读日志用 `Get-Content -Encoding UTF8`：Windows PowerShell 5.1 默认按系统 ANSI
码页解文件，UTF-8 的中文日志会被解成乱码（实测加 `-Encoding UTF8` 前后对比：
`default` 出来是乱码字节，`utf8` 出来是"已停止（不会自动重启）"）。

**3. `.SyncRoot` 是外部噪音，不在仓库里。** 两条独立的路径都会打出这个名字：

- pywebview 的 WinForms 后端经 pythonnet 打的属性链内部日志
  （`window.native.ActiveControl...SyncRoot...GenericSansSerif`，那个 `SyncRoot` 是 .NET 的
  `ISynchronizeInvoke.SyncRoot`）。闸门必须在 `import webview` **之前**装：logging 的
  `StreamHandler` 抓的是当时那个 `sys.stderr` 对象，装晚了就管不住它。
  `neo/manager.py` 里顺序是 `console_io.install()` → `state.install_excepthook()` → 才 import webview。
- Windows 的重解析点：`Path.resolve()` 会跟着 OneDrive 同步根的 reparse point 走，
  `os.readlink()` 对这种点回 `.SyncRoot`，realpath 一路拼回去直到 `maximum recursion depth exceeded`。
  现在 `neo/paths.py` 的 `safe_resolve()` 不做文件系统遍历、深度封顶 64 段，`is_reparse()` 负责识别；
  `state.py`、`manager.py`、`console_server.py`、`install_neo.py`、`engines/strata.py` 都改用它。
  实测 5000 段 `.SyncRoot` 拼出来的路径被截到 64 段以内，不递归。

**4. 给用户的建议**：Neo 或 Strata（含模型目录）放在 OneDrive / 其他同步目录里时，
把目录移出同步范围，或用 `--root` 指一个普通目录。窗口里的输出已经裁过，完整日志在
`neo/state/logs/manager.log`。

## 窗口点了就"未响应"（已修，pywebview 6.2.1 / WinForms + WebView2）

标题栏上的"(未响应)"就是窗口线程没在收消息。这条线程是 pywebview 自己起的 STA 线程
（`platforms/winforms.py` 的 `create_window`：`ThreadStart(create)` + `ApartmentState.STA`），
Python 主线程只在 `thread.Join(500)` 里转。页面里一次点击经 pywebview 的 js_api 桥的完整路子：

1. 点按钮 → WebView2 `PostWebMessageAsJson` → `platforms/edgechromium.py:225` 的
   `on_script_notify` **在窗口线程上**执行，它里面的 `print` 也在这条线程上写 stdout；
2. `util.py:247` 的 `js_bridge_call` 把 `Shell` 的方法丢到新线程跑（这一段不挡窗口）；
3. 方法返回后 `window.evaluate_js(...)` 把结果送回页面 → `platforms/edgechromium.py:137` 里
   `self.webview.Invoke(...)` **同步投递给窗口线程**，再 `semaphore.acquire()` 等它跑完；
4. 窗口线程跑这个委托要抢 GIL。它一旦长时间抢不到 GIL，或者被 `print` 卡在控制台上，
   消息循环就停住：所有 js_api 调用都不返回，窗口点什么都没反应。

**主修：摘掉这座桥，动作全走服务。** `neo/manager.py` 的 `window_kwargs()` 里**没有 `js_api`**，
窗口只是容器；页面（`neo/web/app.js`）的每个动作只发 HTTP 到控制台服务，连"打开聊天页 /
监控页"（`POST /neo/open`）和"打开日志文件"（`POST /neo/open-log`）也一样，由挂在服务上的
`Shell` 执行（`neo/console_server.py` 的 `_shell_action`）。点击路径上一行 Python 都不跑，
窗口线程只做一件事：收消息。浏览器界面走的是同一批接口，两条路从此完全一样。
`neo/tests/test_ui.py` 的 `TestClickPathIsHttpOnly` 盯着这条 —— 窗口参数里出现 `js_api`、
页面代码里出现 `window.pywebview`，测试就红。

实测 2026-10-12（Windows 11 / pywebview 6.2.1 / 打包的 `NeoStrata.exe`，mock 引擎，端口 8780）：
窗口开着跑 95 秒（页面每 2 秒一次 HTTP 轮询），`Responding=True`，`manager.log` 里 0 次"没应答"；
`GET /neo/status` 8.7 ms、`POST /neo/start` 4.5 ms、`POST /neo/open` 65.3 ms（真开了浏览器）、
`POST /neo/open-log` 146.7 ms（真打开了日志文件）。同一个 exe 放进 `D:\Work\Strata` 跑
`--no-start`：窗口 `Responding=True`、模型页列出 4 个模型、`POST /neo/open` 回 200、日志 0 次"没应答"。

桥摘掉之前喂这个坑的三处留着 —— 它们挡的是"窗口线程上的 print 与 GIL"，看门狗探针和
pywebview 自己的线程仍然走那条路：

| 处 | 原来 | 现在 | 实测 |
| --- | --- | --- | --- |
| `console_io.BoundedStream.write` | 排队等 `_lock`。有线程卡在真流的写上（控制台被 QuickEdit 选中、管道满）时，窗口线程跟着挂住 | 拿不到锁立刻返回，这一行丢掉并计数 | 别的线程占住锁时 `write()` 立刻返回（测试断言 < 0.5 秒），行计入"控制台写不动，丢弃 N 行" |
| `state.log()` | 每条日志把整个尾部（最多 200 KB）读出来逐行清洗再整份回写，全程握着 GIL | 常态只追加一行，超过 `LOG_MAX` 才裁一次 | 20 条日志 = 20 次追加写、文件里 0 个 `\r`；6 个线程并发 90 条，不丢不咬 |
| `Shell.stop()` | `sup.stop()` 里 `thread.join(timeout=10.0)`，这条调用挂 10 秒，页面里 `await` 它的按钮一直转 | `join_s=0.5`，剩下的收尾由监督线程自己走完，状态页靠 2 秒一次的轮询更新 | 点停止 0.5 秒内回话 |

顺带修掉一件事：pywebview 6.2.1 **没有** `window.get_geometry()`（实测 `window.py` 里搜不到
`geometry`），`neo/manager.py` 以前每 5 秒调一次、每次抛 `AttributeError` 被 `except Exception`
吃掉 —— 窗口几何从来没存下来过。现在几何从 `events.resized` / `events.moved` 事件里取
（`event.py` 的 `Event.set` 起线程跑回调，回调里只往字典记两个数：不碰文件、不碰窗口线程），
后台线程每 5 秒存一次。实测带窗口起来一次后 `neo/state/window.json` 里是真实坐标
（`x=20, y=261, w=1280, h=860`）。

**看门狗 `neo/ui_watchdog.py`**：每 2 秒用 pywebview 自己那条路探一次窗口线程 ——
`window.evaluate_js("1")` 必须等窗口线程收消息才回得来。超过 3 秒没回音就认定卡住，把每个
Python 线程当前停在哪一帧（`sys._current_frames()`）逐行写进 `neo/state/logs/manager.log`，
两次现场之间至少隔 30 秒。探针只在后台线程上跑，在飞的最多 4 个。实测带窗口跑 1 分钟
（页面每 2 秒一次 HTTP 轮询）：日志里 0 次"没应答"，进程 `Responding=True`；测试里把
`evaluate_js` 挂住，日志里立刻出现"没应答"和 `neo-ui-probe` 停在哪一帧。

## 聊天页与监控页是空白的（已修）

窗口里点"聊天"/"监控"，内嵌框是一块 1062x677 的空白；状态页的胶囊写着"未启动"，
而 8080 上的服务在正常应答（`/health` 200、`/neo/status` 200）。

实测 2026-10-12 在真开的窗口里（pywebview 6.2.1 / WebView2，页面 `http://127.0.0.1:8083/`）：
页面的资源列表里只有 `style.css`、`icon.svg`、`app.js` 三条，**一次 `/neo/status` 都没有** ——
页面根本没开始轮询。原因在 `neo/web/app.js`：`num()` 的函数体只写了开头就断了，从 `render`
到所有按钮接线全落进它的身体里，文件末尾那句 `poll()` 成了 `ReferenceError`，`last` 永远是
null，`ensureFrame()` 直接 return，两个内嵌框的 `src` 就停在 `about:blank`。行首缩进看不出
这种损坏（被吞掉的代码本来就顶格写），花括号深度能看出来。

修法是补上 `num()` 的闭合，接线与轮询回到顶层。修完在同一个探针下重测（源码跑
`--engine strata --no-start`，控制台 8084，服务是 8080 上那个在跑的）：`/neo/status` 每 2 秒
一次（200，2 ms）、状态胶囊"运行中"、聊天框 `src=http://127.0.0.1:8080/`、监控框
`src=http://127.0.0.1:8080/#monitor`，5 秒后两个框都是 1062x677、内页是跨源文档。
同一个 iframe 在 Edge 无头截图里渲染出完整的 Strata 监控页（Speed / VRAM / PCIe 卡片与
请求表都在）；`127.0.0.1:8080` 不发 `X-Frame-Options` 也不发 CSP，内嵌不被挡。

回归测试：`neo/tests/test_ui.py` 的 `TestAppJsTopLevelWiring` 用花括号深度扫描
（`js_depth_lines`，跳过注释、字符串、模板串与正则字面量）盯 `render`、`poll`、`ensureFrame`、
`num` 在顶层，文件末尾的 `poll();` 与 `setInterval(poll, 2000);` 在顶层，花括号配平。
把 `num()` 的闭合去掉，这几条立刻红。

## 状态页的卡片全是"—"、请求栏写成 [object Object]（已修）

用户 2026-10-12 的窗口截图：服务在跑（状态胶囊"运行中"、上下文 202,072），六张卡片里
**生成速度 / 解码步长 / 显存 / 内存 / 在跑的请求**五张是"—"，"上下文"那张的副标题写的是
"请求 [object Object],[object Object]"。

实测 2026-10-12 在本机（RTX 5090 D v2 / Windows 11 / Python 3.13.15，
`serve/server.py --engine mock --port 28181`，跑过一次 `/v1/chat/completions` 后取）：
`/metrics` 的顶层只有 `engine`、`live`、`requests`、`requests_kept`、`totals`、`hardware`、
`hardware_static`、`history`、`time` 九个键，卡片要的数字全在节里 ——

| 节 | 里面有什么 |
| --- | --- |
| `live` | 当前请求：`state`、`queued`、`phase`、`prompt_tokens`、`generated`、`tok_s`、`tok_s_mean`（`--batch` 时另有 `running`、`slots`、`waiting`） |
| `totals` | 累计：`requests`、`prompt_tokens`、`reused`、`output_tokens`、`prompt_ms`、`decode_ms` |
| `hardware` | 每秒一采：`gpu_util`、`gpu_mem_used`、`gpu_mem_total`（字节）、`gpu_temp`、`gpu_power`、`cpu`（百分比）、`ram_used`、`ram_total`（字节）、`disk_read_mb`、`disk_write_mb` |
| `hardware_static` | `gpu_name`、`gpu_count`、`gpu_note`、`cpu_name`、`cores`、`threads` |

而 `neo/engines/base.py` 以前按 41 个扁平键（`tokens_per_s`、`decode_step_ms`、`gpu_used_mib`、
`ram_used_gb` …）在**顶层**挑：这些名字上游一个都没发过，卡片要的键里只有 `requests` 在，
而它是最近 12 条请求的**记录列表** —— 列表被模板串写进文字就是 `[object Object],[object Object]`。

修法两层：`base.py` 的 `flatten_metrics()` 按真实的节取值并换算单位（字节 → MiB / GB、
`decode_ms / output_tokens` → ms/步、`live.running` 或 `live.state` → 在跑的请求数），
上游没有的键就不出现，页面显示"—"；`app.js` 的卡片一律经过 `num()` / `count()`，
`count()` 对列表与对象回"—"，页面这一层也写不出 `[object Object]`。

修完实测（`python -m neo.console_server mock 28190`，Edge 无头截图 1500x420）：显存
**24079 / 24455 MiB · NVIDIA GeForce RTX 5090 D v2**、内存 **70.4 / 93.6 GB**、在跑的请求
**0**（排队 0）、上下文 **32,768**、请求 **0**；生成速度与解码步长在空闲时是"—" ——
没有请求在生成，不编一个 0 出来。

回归测试：`test_engines.py` 的 `TestMetricsFlatten`（8 个，吃的是上面那次真应答的载荷）、
`test_supervisor.py` 的 `test_status_metrics_are_numbers`（真进程，值里不许有列表/对象）、
`test_upstream_api.py` 的 `test_metrics_sections_neo_flattens_still_exist`（上游改名先响）、
`test_ui.py` 的 `TestStatusPageMetricCards`（卡片只经过 `num()` / `count()`，`m.requests`
这类不许直接嵌进模板串）。

## 窗口上任何点击都没反应、状态永远「未启动」（已修）

用户 2026-10-13 的新 exe：窗口出得来、进程在跑、8080 的服务与 8083 的控制台都在正常应答，
但状态胶囊一直是「未启动」、六张卡片全是「—」、「发生了什么」是空的，**任何点击都没反应**。

服务端没问题：窗口取到的 `/app.js` 与源码逐字节一致，`/neo/status` 200，HTML 里的 id 与
`app.js` 里引用的 id 全对得上，也没有全屏遮罩挡点击。问题在页面的 JavaScript。根因是
**两个 `<script>` 共享同一个全局作用域**：`index.html` 里 `<script src="/i18n.js">` 与
`<script src="/app.js">` 是两段经典脚本，i18n.js 顶层有 `function t` 与 `function applyLang`，
app.js 顶层有 `const t` 与 `const applyLang`。浏览器在**解析** app.js 时抛
`SyntaxError: Identifier 't' has already been declared` —— app.js 一行都不执行：`poll()` 没跑起来
（状态与卡片停在初始值），`document.querySelectorAll(".tab")` 那一批 onclick 一个都没挂
（点什么都没反应）。i18n.js 是这次新加的，所以只有新 exe 出现。

为什么 385 条测试全绿、exe 也照打成功：逐文件的语法检查看不见跨文件重名 ——
`node --check neo/web/i18n.js` 与 `node --check neo/web/app.js` 各自都合法。最小重现
`function t(){}; const t = 1;` 拼在一个文件里，`node --check` 报的就是浏览器那句原话。

修法：`neo/web/i18n.js` 整个文件包进一个 IIFE，只往外递 `window.NEO_I18N`。app.js 一直是
`const I18N = window.NEO_I18N || {}` 再 `I18N.t(...)`，取的就是这个命名空间，所以包起来
不改任何一处文案。实测包之前 i18n.js 顶层 14 个声明，包之后 0 个。

防回归是**打包前的闸门**：`neo/webcheck.py` 按 `index.html` 的加载顺序把页面**当一个程序**查 ——
花括号配平、跨文件顶层重名、`poll` / `render` / `ensureFrame` 与末尾的 `poll();` 在不在顶层、
i18n.js 有没有把 `NEO_I18N` 挂到 window；机器上有 node 时把拼接后的整段交给 V8 解析一遍
（`node --check`），没 node 就跳过这一关并说明跳过。命令行：

```
python -m neo.webcheck           # 体检 neo/web/，不干净非零退出
```

`neo/build_exe.py` 在调 PyInstaller 之前先跑它（`web_gate()`），不干净拒绝出包。实测把 i18n.js
的 IIFE 拆掉再跑闸门，报的是：

```
- t 在 i18n.js:57 与 app.js:14 的顶层都声明了：同一全局作用域里这是解析期 SyntaxError，app.js 整段不跑
- applyLang 在 i18n.js:71 与 app.js:15 的顶层都声明了：同一全局作用域里这是解析期 SyntaxError，app.js 整段不跑
- 页面脚本按加载顺序拼起来 V8 解析不过：SyntaxError: Identifier 't' has already been declared
```

回归测试：`neo/tests/test_ui.py` 的 `TestWebCheck`（8 个）—— 真树体检干净、闸门放行；拆掉 IIFE
后闸门拒绝出包并点名撞的两个名字；有 node 时 V8 报的是浏览器那句原话、没 node 时不许假装跑过；
花括号漏一个要报；顶层的 `poll();` 与 `setInterval(poll` 都没了要报；`<script>` 指的文件不在要报；
顶层名字要抓到 40 个以上（不然这条检查等于没跑）。

## 服务在跑，右上角亮的却是「启动」（已修）

用户的话：模型已经启动了，应该是停止。截图里状态胶囊是"运行中"、服务表写"运行中·模型已就绪"，
右上角三个按钮里 **启动是亮蓝色主按钮、停止是灰的**。

实测 2026-10-12（`python -m neo.console_server mock 28210`，Edge 无头截图 1500x120，
`/neo/status` 回 `state=running`、`healthy=true`、`loaded=true`）：

- `neo/web/style.css` 里**一条 `:disabled` 样式都没有** —— 按钮被禁用后长得跟能按时一模一样，
  而 `button.primary` 的蓝色写死在"启动"上（`index.html` 里带的 class，页面从不换），
  于是运行中的页面读起来就是"该点启动"。
- 禁用判断本身是对的（`btn-start` 在 running/starting 时禁用，`btn-stop` 只在 idle 时禁用），
  纯样式问题。

修法：主按钮的蓝色跟着"此刻能按的那个"走 —— `app.js` 的 `render()` 里
`running / starting / stopping / external` 时点亮**停止**，停着时点亮**启动**；
`style.css` 补 `button:disabled`（`opacity .42` + `not-allowed`）与
`button.primary:disabled`（退回灰底、灰字），禁用一眼看得出来。

修完实测（同一探针、同一截图尺寸）：`state=running` 时右上角是**停止**亮蓝、**启动**发灰。

回归测试：`test_ui.py` 的 `TestHeaderActionButtons`（3 个）—— CSS 里要有 `button:disabled` 与
`button.primary:disabled`；`btn-start` / `btn-stop` 两个都要 `toggle("primary")`；
`btn-stop` 的禁用条件里不许出现 `running` / `external` / `stopping`（在跑就该能停）。

## 模型页的"启动它"：跑着的时候点它会发生什么

用户问：模型列表里的"启动它"，是不是要等服务停止了才能按？

**不用先停。** `set_model`（`neo/supervisor.py:156`）就是"记下新模型 + `stop()` + `start()`"——
Neo 自己把当前服务停掉、用新模型重新起。实测 2026-10-12（mock 真进程，
`neo/tests/test_supervisor.py` 的 `TestSetModel`）：running 时调用它，进程号换了、状态回到
running，`last_exit` 是空的 —— 用户主动换模型不算一次非预期退出，状态页的「上次退出」那一行
不会因此亮起。

代价是**正在跑的请求会被切断**（`stop()` 给完优雅退出窗口就杀整个子进程树）。以前按钮只写
"启动它"，跑着和停着长得一模一样，看不出点下去会发生什么。现在写清楚：

- 当前模型：按钮"正在用"，禁用；
- 服务在跑（running / starting / stopping / external）：按钮"换过去"，
  鼠标上去是"会先停掉当前服务（正在跑的请求会被切断），再用「模型名」重新起"；
- 停着：按钮"启动它"，提示"用「模型名」起服务"；模型文件不在的一律禁用。

回归测试：`test_ui.py` 的 `TestModelPageSwitch`（4 个）与 `test_supervisor.py` 的 `TestSetModel`（1 个）。

## 打成 exe（`NeoStrata.exe`）

```
python -m pip install pyinstaller      # 只在打包时用，用户那边不用装
python -m neo.build_exe                # 打出来放在仓库根：NeoStrata.exe
python -m neo.build_exe --dest D:\Work\Strata
```

按 `neo/NeoStrata.spec` 打一个单文件 exe，**设计成放进 Strata 的根目录双击就跑**。
实测 2026-10-12 在 Windows 11 / Python 3.13.15 / PyInstaller 6.22.3：**16.8 MB**；双击后
出窗口（标题 Neo Strata）、`Responding=True`、`/neo/status` 到 `state=running`
（`served_model=qwen3.8-flash-next`）、`neo/state/instance.json` 里 `mode="window"`。

- **exe 的"根目录"就是 exe 自己所在的目录**（`neo/manager.py` 的 `default_root()`；源码跑时是
  `neo/` 的上一层）。状态、日志、窗口几何都落在 `<根目录>\neo\state\`
  （exe 在不可写目录里时退到 `%APPDATA%\Neo Strata`，`state.py` 的 `pick_state_dir()`）。
- **exe 里不打模型、不打引擎、不打 torch / numpy**：Neo 只是用命令行参数启动
  `serve/server.py`（`neo/engines/strata.py`），模型、引擎、`.venv` 都在 Strata 那边。
  打进来只会让 exe 变大、版本对不上。
- **exe 里打 pywebview 与它的 WinForms 后端**（spec 里的 `collect_submodules("webview")`：
  pywebview 按平台在运行时挑后端，这些导入 PyInstaller 看不见）和控制台页面 `neo/web/`
  （运行时从解包目录读，`paths.web_dir()`）。
- **打包环境缺包就拒绝打包，缺的一次报全**（`build_exe.missing_build_deps()`）：缺 pywebview 时
  `collect_submodules("webview")` 是空列表，PyInstaller 一声不响地打成功，出来的 exe 没有窗口。
  实测 2026-10-12 19:58 部署的 exe 自己的日志：`pywebview 不可用（No module named 'webview'），退回浏览器界面`
  —— 用户那边看不到任何界面。以前一关一关拦，在两个包都没装的环境里（部署树的 `.venv` 就是这样）
  报的那句话里连 pywebview 都不出现，装完 pyinstaller 还要再撞一次；现在一条话里列齐缺的包和各自的装法
  （`python -m pip install -r neo/requirements-neo.txt`）；确实只要浏览器界面就加 `--no-webview`。
  实测两种打包：装了 pywebview **13.2 MB**（`webview.*` 73 个模块进包，含
  `webview.platforms.edgechromium` 与 `winforms`），没装 **8.8 MB**。
- **页面脚本体检不过就拒绝出包**（`build_exe.web_gate()` → `neo/webcheck.py`）：页面是这个 exe
  的全部界面，而逐文件的语法检查看不见跨文件重名。实测 2026-10-13 部署的 exe（13,981,433 字节）
  就是打成功、装出去、界面是死的（见「窗口上任何点击都没反应、状态永远「未启动」」）。现在
  `python -m neo.build_exe` 在调 PyInstaller 之前先体检 `neo/web/`，不干净就报出是哪两个文件的
  哪两行撞了哪个名字，并让人先跑 `python -m neo.webcheck`。
- **exe 是窗口版（`console=False`），没有控制台**：QuickEdit 那条路直接绕开（见"怎么起来"）。
  窗口版里 `sys.stdout` / `sys.stderr` 是 `None`，`console_io.NullStream` 接住它们，
  所有输出走 `neo/state/logs/manager.log`。
- **exe 需要 Strata 的 `.venv`**：打包后 `sys.executable` 就是 `NeoStrata.exe` 自己，拿它当
  "Python"等于让 Neo 一遍遍启动 Neo 自己。所以 `state.py` 的 `python_executable()` 在 exe 里
  只认 `<根目录>\.venv\Scripts\python.exe` 和系统里的 `python`，都没有就报一句
  "没找到跑 Strata 用的 Python：这个 exe 需要 Strata 目录里的 .venv —— 在 Strata 目录跑一次
  START-HERE.bat 就有了"。
- 入口是 `neo/neo_entry.py`：`neo/manager.py` 顶部是包内相对导入，PyInstaller 把包里的模块当
  顶层脚本分析时认不出父包，那些导入会被标成"找不到模块"，所以给一个显式 import 包的脚本，
  `pathex` 指到仓库根。
- **结束进程时它是两个进程**：PyInstaller 的单文件 exe 先跑引导进程，解包后再启动应用进程。
  实测 2026-10-12：`Stop-Process` 结束引导进程（pid 42780）后应用进程（pid 26312）还在，
  服务端口 28180 与控制台 28183 都还开着；结束应用进程后两个端口都关了。Neo 的 Job Object
  管的是 Neo 起的 Strata 服务，管不到这对父子。关窗口（或在窗口里点"停止"再关）走 `atexit`，
  两个进程都会退；在任务管理器里结束，请把两个 `NeoStrata.exe` 都结束。
- 打包产物（`NeoStrata.exe`、`neo/dist/`、`neo/build/`）不进仓库（`neo/.gitignore`）。

## 部署树：Neo 不改上游（`D:\Work\Strata`）

部署的那棵树是**上游 Strata 仓库的一个 checkout**（`origin` = `github.com/Niko1221/Strata`；
2026-10-13 在 v0.1.43 / `7695859a`），不是开发仓库。规矩：

- **不在部署树里提交任何东西**，也不给它的 `origin` 加第二个远端 —— 上游更新要能直接 `git pull`。
- **Neo 不改上游文件**。Neo 带去的东西全是未跟踪的：`NeoStrata.exe`、`neo/`、`NEO-START.bat`。
  部署时 `neo/deploy.py` 把这三条加上 `strata/`（引擎源码树）和 `strata-*.json.bak`（上游
  `setup.py` 在用户机器上留下的模型配置备份 —— 上游的 `.gitignore` 只写到 `/strata-*.json`，
  以 `.json` 结尾的模式匹配不到 `.json.bak`）写进部署树的
  `.git/info/exclude` —— 那是 git 的本地忽略文件，不在任何提交里，所以 `git status` 只看得到上游的
  东西，而 Neo 一个跟踪文件都没动（上游的 `.gitignore` 是跟踪文件，不碰）。
  2026-10-13 撤掉了先前打在 `serve/server.py` 上的本机豁免补丁（旧文件备份在
  `neo/state/backups/server.py.v0.1.43.neo-patched.bak`，393,602 字节），上游文件恢复原样
  （391,688 字节）。撤补丁留下的缺口由 Neo 自己补：监控页改走 `/strata/` 只读通道（见"token 管到哪"）。
- 判据：`git -C D:\Work\Strata status --short --untracked-files=no` 里**没有已修改的跟踪文件**
  （Neo 的文件按设计就是 untracked，不能算成"脏"，`neo/engine_setup.py` 的
  `git_dirty(..., tracked_only=True)` 就是为这一条加的）；
  `git rev-list --count HEAD..origin/main` 与 `origin/main..HEAD` 是 **33 与 0** —— 拉取是纯快进。
- **换版用 `python -m neo.deploy --to D:\Work\Strata`**（默认 dry-run，加 `--apply` 才写）。
  三道闸门，过不了什么都不写：① 引擎 —— 部署树的 HEAD 要等于 `neo/engine_ref.json` 的 pin
  且跟踪文件干净；② 路径 —— 只写 Neo 清单里的东西（`neo/**`、`NEO-START.bat`、`NeoStrata.exe`），
  `neo/state/`、`__pycache__/`、`build/`、`dist/` 不带走；③ 进程 —— 部署树里的 `NeoStrata.exe`
  在跑就拒绝（文件被锁着，写了也是半个）。写完逐文件核对 SHA-256，再就地跑
  `python -m neo.webcheck` 与快套件。2026-10-13 第一次真跑（dry-run，`--no-tests`）：
  引擎闸门 OK（HEAD `a0116b2782a5` == pin），进程闸门被 `instance.json` 记的 pid 7276 挡住，
  清单是 new 7、same 26、update 30，并当场报出部署树里多出来的一个 `neo/test_ui.py`
  （旧同步留下的，`--prune` 会删）—— 只同步 `.py` 漏掉 `neo/web/` 那一类坑就是这么露出来的。
- **控制台打印**：打印中文的命令行 `main()` 先调 `wide_stdout()`（共用那一份在 `neo/cli.py`），
  把 stdout 改成 UTF-8（`errors="replace"`）。2026-10-13 实测：`python -m neo.engine_setup --ensure`
  在 cp936 控制台上崩在打印那一步（`UnicodeEncodeError: 'gbk' codec can't encode character '\ufffd'`）——
  git 的输出按 UTF-8 解，解出来的替换字符 `U+FFFD` 是 GBK 编码不了的。现在这两条 CLI 在 cp936 控制台上
  把那一行打成 `git : fatal: ...`，退出码 1，不再崩。
  同一天量到第二种：输出进管道（`| Select-Object`、重定向进文件、测试里捕获）时 `sys.stdout.encoding`
  从控制台的 65001 回落到 locale 的 gbk，写出去的是 GBK 字节，按 UTF-8 读的那一头整片是乱码
  （`python -m neo.wizard --check` 直接跑在控制台里是好的，加管道就花；同一条命令 `-X utf8` 是好的）。
  2026-10-13 起 `wizard` / `models` / `model_settings` / `connect` / `start` 的 `main()` 也调它，
  `neo/tests/test_deploy.py::TestWideStdoutShared` 钉"七个命令行是同一个函数、`cli.py` 里只有一份
  `reconfigure`"，`neo/tests/test_model_settings.py::TestCliRootAndConsole` 在 cp936 的字节控制台里
  跑一遍 `main()`，读回来必须是 UTF-8 的中文。
- **部署 = 换 `NeoStrata.exe` + 同步 `neo/` 的源码与页面**。exe 里打的是开发树的 `neo/`；
  在部署树里跑源码（`python -m neo.manager`、`python -m neo.models`、跑测试）用的是部署树的 `neo/`，
  两边都要跟上。2026-10-13 实测踩过这个坑：只同步 `.py`、没同步 `neo/web/`，部署树的源码版页面
  就还是 `t` / `applyLang` 撞名那个死界面 —— `python -m neo.webcheck` 在部署树当场报出来。
  同步后部署树 `python -m neo.webcheck` 干净、快套件 326 个测试全过（用它自己的 `.venv`）。
- 打包中间产物（`neo/build/`、`neo/dist/`）不进部署树。

### 换版怎么走（`neo/deploy.py` 的命令行）

```
python -m neo.deploy --to D:\Work\Strata                      # 只看计划，一个文件都不写
python -m neo.deploy --to D:\Work\Strata --json               # 同一份计划，给机器读
python -m neo.deploy --to D:\Work\Strata --no-tests           # 计划里不算跑测试那一步
python -m neo.deploy --to D:\Work\Strata --apply              # 真写
python -m neo.deploy --to D:\Work\Strata --apply --prune      # 真写，并删掉目标里源里没有的 Neo 文件
# 别的开关：--from <开发树>（默认脚本所在的仓库根）、--exe <NeoStrata.exe>、--ref <engine_ref.json>
```

顺序是固定的四步，每一步都要看清输出再走下一步：

1. **dry-run**：`--to <部署树>`。看三道闸门过没过、清单里 new / update / same 各多少、
   目标里多出来的文件是哪些、`.git/info/exclude` 里还缺哪几行。这一步不写任何东西，
   退出码非 0 就是闸门没过。
2. **停掉在跑的 exe**：进程闸门认两样 —— `neo/state/instance.json` 里记的 pid，和 Windows
   进程表里镜像路径落在这棵部署树里的 `NeoStrata.exe`。文件被进程锁着的时候写进去就是半个 exe，
   所以这一条不能绕。关掉窗口（或 `taskkill /PID <pid> /T /F`）再走下一步。
3. **`--apply`**：每个文件先写同目录的临时文件、核对 SHA-256、再 `os.replace` 换上去；
   缺的忽略行追加进 `.git/info/exclude`（只追加、不重复，dry-run 只报不写）；
   写完把整份清单再核一遍摘要，并**在部署树里**跑 `python -m neo.webcheck` 与快套件
   （`--no-tests` 只跳过测试那一步，webcheck 不跳）。
4. **事后检查**：部署树里 `python -m neo.webcheck`、快套件、`git -C <部署树> rev-parse HEAD`
   等于 pin、打开窗口看「接入」页的引擎卡。

`--apply` 之前闸门必须全绿，脚本不看"用户说没事"。目标树没有 git、HEAD 不是 pin、
跟踪文件被改过、exe 在跑 —— 四种情况都是**一个字节都不写**并说清是哪一道。

## 测试

```
python -m neo.tests.run_all --quick --skip-network   # 开发时：十七个模块，9.6 秒
python -m neo.tests.run_all                          # 换版前：二十个模块，含克隆那条
python -m neo.tests.run_all --no-split --jobs 1      # 老用法：按模块串行
```

523 个测试（Windows 11，Python 3.13.15，起真实进程，不需要 GPU、不需要装好的模型、不碰真实用户配置；
唯一真联网的是 `test_engine_setup.TestRunAndPage` 那条克隆路径）。跑法是 `python -m neo.tests.run_all`：
它把每个模块切成测试类、按类并行（默认 4 个并发）、每个类报秒数，并把失败的类单独重跑一次——并发下的
端口/负载抖动要和真失败分开。**时间标准（2026-10-13）：跑测试必须报时间，单元与套件的墙上时间都按 30 秒
封顶**——`run_all.py` 里的 `BUDGET_S` 就是这条标准，`--timeout` 的默认值是它，超预算的单元报
`OK(超预算)`、整跑判失败；超过 30 秒要优化，不是把超时调大。2026-10-13 本机实测（删掉退避重启之后，
同一台机器）：**`--quick --skip-network` 墙上 9.6 秒 / 435 个用例 / 86 个单元**，**`--skip-network`
墙上 17.1 秒 / 523 个用例 / 101 个单元**（最慢的两个单元 5.7 秒 `test_models.TestCatalog` 与
5.1 秒 `test_supervisor.TestCrashStops`；删退避之前最慢的是 `TestCrashRestart` 14.7 秒 —— 杀四次进程、
走完整条退避，全跑 18.3 秒）。同样的单元串行相加是 66.1 秒，而 19 个模块一条 `python -m unittest`
跑是 1067.1 秒 = 17.8 分钟（492 个测试，
这是并行之前的数）。`--skip-network` 摘掉的那条克隆路径在同一台机器同一天量到 6.8 秒与 145.3 秒——它跟着
链路走，30 秒的标准下全跑就摘掉它，要看那条就单独跑那一类并放宽 `--timeout`。

引擎的真服务起不动时（引擎根目录的 `.venv` 没装上游的包），用真 `serve/server.py` 的那几条自己跳过并
说清缺哪个包：`_bootstrap.py::server_ready` 用 ast 读它的 import，毫秒级、不起进程。实测 2026-10-13：
缺 `jinja2` 时那个进程 import 就死，监督器按退避一遍遍重启、测试把等待跑满，全跑从 18.3 秒变成 5 分钟；
`test_harness.py` 钉着这条跳过规则与上面的时间标准。

监督器的测试驱动 `neo/tests/mock_server.py`（stdlib 小服务，按 Neo 读的形状答 `/health` 与 `/metrics`），
作为真子进程跑在真端口上：启动、就绪、杀掉、停着并给出诊断、点启动再起全是真的。起进程 0.27 秒，
`serve/server.py --engine mock` 也是 0.27 秒，小服务省掉的是"第一次答 `/health`"要的那 3.5 秒。
`TestStartAndReady.test_start_to_running` 用真的 `serve/server.py --engine mock`：要验的是"上游的就绪行
真的进了日志"，那是 Neo 与上游的接缝，不能让假服务替它作证。

开销全在起真进程与建真 git 树的模块上。合起来比各自相加慢，慢在哪也量了（2026-10-13，同一台机器）：
`test_engine_setup` 与 `test_deploy` 各自单跑是 8.4 与 5.0 秒，在全量套件里是十几分钟 —— 它们每个用例在
临时目录里 `git init` + 一次提交造真树，Windows Defender 扫临时目录时一次 `git init` 要几十秒。
所以这两条在开发时走快套件，全量套件留给换版前跑一次。两处实测改掉的等法（2026-10-13）：
`neo_console_info` 先摸一次 TCP（0.05 秒）再发那个 HTTP 请求，没人听的端口不再等满 0.8 秒超时；
控制台线程 `serve_forever(poll_interval=0.05)`，`stop()` 从 0.51 秒到 0.06 秒。监督器测试里
`poll_s` 0.25→0.05、`probe_s` 0.4→0.1、`wait()` 每 0.05 秒问一次，等的是条件不是秒数：
`test_connect_api` 39.6→6.7 秒、`test_console` 34.7→9.1 秒、`test_supervisor` 70.3→56 秒。
再往下是这一轮的两件事（2026-10-13，见上面「监督器的行为」）：探活按 `probe_s` 限速 + TCP 超时
1.2→0.25 秒，被监督的进程换成 `neo/tests/mock_server.py`。按类并行后最慢的两个类：
`TestCrashRestart` 29.3→**7.0** 秒、`TestExternalService` 42 秒（并发下偶发失败）→**2.6** 秒——
删掉一个用例都没用，慢的是"发现进程死了"要等一轮探活跑完。
（这一格后来改成了 `TestCrashStops`：退避删了之后 5.1 秒。慢的原因也顺手修了——`_wake` 是
set 一次就一直在的，盯端口那一层的 `_sleep` 于是立刻返回，那一层变成一边忙等一边反复探活，
监督线程占着 GIL，测试里状态页的轮询都被拖慢；`_sleep` 醒来把标记收回去，同一格从 12.2 秒到
5.0 秒，2026-10-13 本机实测。）

- `test_supervisor`：启动到就绪、事件记录、非预期退出后**不自动重启**（记下退出码 + 一句诊断 +
  事件里写"已停止，点「启动」重新起"）、崩了停着之后点「启动」真的再起一个进程、用户停止后不被
  拉起、接手已在运行的服务、诊断关键词、
  **状态页的 metrics 真是数字**（真进程：`requests` 是累计条数，值里不许有列表/对象）。
- `test_engines`：配置发现与排序（`setup.py` 的 `touch()` 语义）、启动命令只用上游已有参数、
  模型文件缺失拒绝启动、没有配置时的引导态、mock 适配器；**真实配置的形状**（扁平 `args`、模型在
  `--native`、上下文在 `--max-context`、没有 `path` 键）、**模型在而引擎 exe 不在要标出来并拒绝启动**、
  没点名时挑一个能启动的配置；**`/metrics` 的四节压成卡片键**（`TestMetricsFlatten` 吃真应答载荷：
  显存 24028 / 24455 MiB、内存 70.6 / 93.6 GB、`requests` 是 1 不是列表、空闲时不编一个生成速度）；
  **监控页地址走控制台**（`test_pages_monitor_goes_through_the_console`：给了 `console_port` 时
  `pages["monitor"]` 是 `http://<host>:<console>/strata/#monitor`，没给时直连上游；聊天页永远直连上游）。
- `test_upstream_api`：Neo 用到的 `serve/server.py` 参数（`--engine/--config/--port/--host/
  --api-key/--gpu`）、`/health` 的字段、`/metrics` 的**四节与里面的键**（`live` / `totals` /
  `hardware` / `hardware_static`，以及 `serve/telemetry.py` 的 `cpu` / `ram_used` / `mem_used`）、
  聊天页与监控页文件、`setup.py` 的 M2 参数、
  上游 `requirements.txt` 未被改动。上游改名时这条测试会先响，而不是等用户的 Neo 起不来。
  **`test_the_monitor_channel_needs_no_upstream_change` 盯的是反过来的事**：`serve/server.py` 里不许出现
  本机豁免补丁的标记（`loopback_peer`、`_authorized(self, monitor: bool = False)`、`_authorized(monitor=True)`），
  而监控页与那些只读端点必须还在 —— 监控页靠 Neo 的 `/strata/` 通道读数据，不靠改上游。
- `test_console`：状态接口、静态页 no-store、起不来的引擎停着并给出原因（不自动重启）、外站 Origin 与外站 Host 403、
  设置落盘且不回显 API key；**页面点"打开"走的是服务**（`POST /neo/open` 带 page、`POST /neo/open-log`、
  没壳时回 503、外站 Origin 403 且壳没被调用）；**第二个实例那句话**（窗口模式说 pid 与界面地址、
  浏览器模式说"没有窗口"并给出装 pywebview 的命令、记录里的进程没了说"已经没了"并清掉过期记录）；
  **模型设置那组**（`TestModelSettingsEndpoint`，10 个）：`GET /neo/model/settings` 的表单形状、
  `?model=` 认 id 也认文件名、没点名用第一个配置、点了名找不到就列清单且**不写盘**、
  `POST` 按 `app.js` 发的那份表单写覆盖配置（`--max-context` 与 `--vision` 成、`--kv-resident` 不成对时
  被删）、越界回 `ok:false` 加说明且**配置字节未动**、`action:"reset"` 删掉覆盖配置且上游那份未动、
  改了要重启的东西回 `restart_required` 并带 pid；**`/strata/` 只读通道那组**（`TestStrataChannel`，9 个，
  用一台假上游）：页面与它的 js/css 都转得到、面板端点靠 Neo 在服务端附的 key 答上且应答里不带 key、
  查询串原样带过去、`POST` 一律 405、`/v1/chat/completions` 不转、白名单外的路径不转、上游没起来回 502、
  `/strata` 不遮 Neo 自己的 `/neo/*`、通道用的路径常量与 `engines/base.py` 给的监控页地址一致。
- `test_ui`（65 个）：闸门在别的线程占住锁时 `write()` 立刻返回并把行计入丢弃数、丢弃数要说出来、
  没人抢锁时输出不被改；`state.log()` 常态只追加（20 条 = 20 行、0 个 `\r`）、超过 `LOG_MAX`
  才裁且只留尾部、6 线程并发 90 条不丢不咬；看门狗无窗口不起、线程现场有界且带线程名、
  `evaluate_js` 挂住时日志出现"没应答"与 `neo-ui-probe` 那一帧、窗口正常应答时不写日志；
  `window_kwargs()` 里没有 `js_api`（有坐标才写 `x`/`y`）、`app.js` 代码里不出现
  `window.pywebview` 且带 `/neo/open`、`/neo/open-log` 两条路由；**`app.js` 的花括号配平且
  `render`/`poll`/`ensureFrame`/`num`/`count` 在顶层、末尾的 `poll();` 与 `setInterval(poll, 2000);`
  在顶层**（内嵌框停在 `about:blank` 的那次实测就是被吞进函数体）；**状态页六张卡片只经过
  `num()` / `count()`**（`m.requests` 这类不许直接嵌进模板串，`count()` 对列表与对象回"—"）；
  **打包环境缺包时
  `build_exe` 拒绝打包**并给出装包的命令，`--no-webview` 真的传到 `build(allow_no_webview=True)`，
  两个包都缺时一句话列齐（`test_build_names_every_missing_dep_at_once` —— 部署树的 `.venv` 就是这种环境）；
  **语言与主题那一组**（`TestLanguageAndTheme`，9 个）：`i18n.js` 语法配平、`BLOCKS`/`PHRASES`/`ATTRS`
  齐且 `applyLang`/`applyTheme`/`init` 在顶层、`index.html` 里每一个 `data-i18n` 键在 `BLOCKS` 里
  （漏一个键那块文字就永远不换）、`data-i18n` 不包着控件（包着就把下拉框与按钮抹了）、
  `app.js` 里每个 `t("中文")` 字面量在 `PHRASES` 里、HTML 里的中文 `placeholder`/`title` 在 `ATTRS` 里、
  顶栏两个按钮与 `.nav-prefs` 在 HTML 与 CSS 里、**`:root` 与 `html[data-theme="light"]` 之外的规则里
  不许出现硬编码颜色**（有一条组件规则写死了 `#fff`，浅色主题下它就是一块白斑）；**两个
  `<script>` 不许在顶层声明同一个名字**（按 `index.html` 的加载顺序把顶层名字串起来查，
  状态永远「未启动」、任何点击都没反应的那次事故就是 `t` 与 `applyLang` 撞名）。
- `test_ui` 里的 `TestWebCheck`（8 个）：`neo/webcheck.py` 的体检与打包闸门 —— 真树体检干净时
  `build_exe.web_gate()` 放行；拆掉 `i18n.js` 的 IIFE 后闸门拒绝出包，并点名撞的是哪两个文件的
  哪两行；机器上有 node 时拼接后的整段交给 V8 解析、报的是浏览器那句原话（`already been declared`），
  没 node 时不许假装跑过这一关；花括号漏一个要报、顶层的 `poll();` 与 `setInterval(poll` 都没了要报、
  `<script>` 指的文件不在要报；顶层名字要抓到 40 个以上（不然这条检查等于没跑）。
- `test_logging`（25 个）：连续写 8 条后日志里 0 个 `\r`（CRLF 放大的回归）、20 万字符的单行被裁并标出
  原长、清洗尾部不丢历史；`tail_text` 去 `\r` 与控制字符、行数封顶并标"已省略"；闸门在 18 亿字符灌入下
  输出有界、说明折叠与丢弃了多少、正常输出不被改；**一次 print 的 40 行整块不被裁**（CLI 的 JSON
  不被切成半截）、**一秒内两块 40 行第二块超出额度要丢**、500 行一次一行的洪水仍压到 120 行以内、
  **`install()` 只包 `isatty()` 为真的流**（管道与 `StringIO` 不包）且幂等；
  `clean_line` 行为、
  `RecursionError` 不格式化 traceback、普通 traceback 行数与总长封顶、excepthook 装得上；
  `RecursionError` 不格式化 traceback、普通 traceback 行数与总长封顶、excepthook 装得上；
  `safe_resolve` 把 5000 段 `.SyncRoot` 截到 64 段以内、相对路径与 `..` 正常、`is_reparse` 对普通目录回 False；
  `instance.json` 记下/读回/清掉、只补 `mode` 不抹 `root`、进程没了算过期。
- `test_model_settings`（38 个）：上面那三个坑各有一条（关图片要同时删段、`--kv-resident` 连值一起删、
  `"off"` 不算真）；外加 `view` 的表单形状（`model_id`/`config_name`/`fields`/`options`/`estimate`/
  `overridden`/`restart_required`）、覆盖配置落在 `neo/state/configs/` 且上游那份字节未动、`clear` 后
  回到原样、跟上游一样的键不写进覆盖配置（一样的那份被删掉）、`config_path_for` 指哪份用哪份、
  显存估算（q4_0 16 单元 = 128 MiB、int8 同形状 = 234 MiB、关掉 KV 常驻 = 0）、GGUF 头部
  （合成文件与真 `qwen3.8-flash-mlx-32b-ikd-q4.p1.gguf` 的 48 层、坏文件与截断文件不抛、
  读头部不超过 1 MiB）。
- `test_netprobe`（12 个）：注入的 `connect` 记录地址族/端口/超时，四个目标都走 443；全通建议默认源、
  HF 断 + ModelScope 通才建议 `--source modelscope`、只有镜像通时只说 `HF_ENDPOINT` 而不换源、
  全断时不推荐任何源；`github.com` 只进"引擎源码树通不通"那一栏，不参与模型源建议；
  缓存读写与坏文件回空、`probe_cached` 不联网、`describe` 说得出建议。
- `test_tokens`（16 个）：key 形状 `sk-neo-` + 32 位 hex 且 100 个不重复、掩码不含完整 key、
  按 base 缓存命中同一个 key 且文件里存的是明文（界面只回显掩码）、`forget_key` 删了缓存、
  环回不写防火墙提示而 `0.0.0.0` 写且带端口、`base_for` 认 `0.0.0.0`→本机地址、`looks_like_key`
  认 `sk-neo-` 与 `sk-` 两种形状。
- `test_connectors`（26 个）：四个 agent 的落点认 `CLAUDE_CONFIG_DIR`/`CODEX_HOME`/`XDG_CONFIG_HOME`/
  `OPENCLAW_HOME`；**合并**（用户的 `permissions` / `[model_providers.other]` / `provider.other` /
  `models.providers.other` 在 Neo 接线后原样在）；**幂等**（跑两遍 provider 只有一个、块里的值跟着
  新端口变）；**先备份**（`.neo-bak` 是接线前的内容）；**坏文件不动**（截断的 JSON 报"没动你的文件"、
  字节一模一样）；Claude 的 base **不带 `/v1`** 且 unset 列表含 `ANTHROPIC_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN`、
  Codex 的 `wire_api = "responses"` 与 `requires_openai_auth = false` 与 profile 层、
  OpenCode 的 `neo/` 前缀与 `output_limit = 32000`（window 131072）与 compaction 预留、
  OpenClaw 的 `api = "openai-completions"` 与 `primary = neo/<id>` 与关掉 `memory.search` 和 `shellEnv`；
  没有 key 时 unset 列表为空并给出"先建 key"的提示；PowerShell 与 POSIX 两块命令都在。
- `test_wizard`（26 个）：命令是 `setup.py --yes --no-start` 且端口跟着传、`--no-start` 必须在
  （服务归监督器起）、`--host 0.0.0.0` 才带 `--host` 且带 key、探测到 modelscope 才加 `--source`、
  用户点名 hf 时不改、源在 `--yes` 之前；四步里"局域网没 key"判成未就绪、没有 `setup.py` 要说出来；
  真跑一次（临时目录里的桩 `setup.py`）命令行与输出都落进 `logs/wizard.log`、缺 `setup.py` 时不跑。
- `test_connect_api`（28 个）：`GET /neo/net` 不联网（吃缓存）、`GET /neo/lan` 报出绑法与掩码且
  环回时没有防火墙提示、绑 `0.0.0.0` 时给出局域网地址与带端口的防火墙命令；`POST /neo/token`
  生成的 key 落进 `launcher.json`、进 key 缓存、交给监督，应答里带完整 key 与掩码；
  `{"action":"reveal"}` 原样交回**同一把**且 `launcher.json` 与 key 缓存的字节一字未动、
  没有 key 时明说"没有 token"、对端不是本机时只给掩码（`api_key` 回空串）、
  `unset` 清掉服务上的 key 而缓存留着、`forget` 删了缓存、坏 action 不写任何东西也不生成 key；
  `GET /neo/connect` 四个配方齐且**不写文件**、`POST /neo/connect` 写进临时目录并留备份、
  坏 agent 回一条说明；`GET /neo/wizard` 四步齐且带上探测建议的源；`GET /neo/catalog` 列出
  菜单与这台机器、`POST /neo/install` 跑临时目录里那份只会打印的假 `setup.py`（命令行与输出
  进 `wizard.log`）、菜单里没有的/盘算里跑不起来的/已经有一个在跑的都挡回来且不起子进程；
  下载源那一排：四个选项齐、记住的源进安装命令、镜子只加 `HF_ENDPOINT` 不加 `--source`、
  认不得的源挡回来且不写 `source.json`；`?fresh=1` 不拖住探测；**五条读接口一个文件都不写**。
- `test_connect_cli`（8 个）：`list` 说出四个 agent 与各自的配置文件、不认识的 agent 回 2 并列出可选；
  `--json` 出整份配方（base、env、unset 列表、命令、PowerShell 与 POSIX 两块）且**不建任何目录**、
  给了 `--key` 就不写 key 缓存；`--base` 压过 `--host`/`--port`（写进 `config.toml` 的 `base_url`
  用的是它）；key 来源要说出来、服务没设 key 时要提醒；`--persist` 只写临时目录里那两个文件、
  每个都留 `.neo-bak` 且备份是用户原来那份、跑两遍 `[model_providers.neo]` 只有一块。
- `test_paths`（30 个）：根目录那一条规则逐条钉住 —— 两个标记（`setup.py` + `serve/server.py`）都要在、
  标记表就是 Neo 真跑的那两个文件、**引擎树不许叫 `engine/`**（那是上游放引擎二进制的目录名）、
  `--root` 压过一切且不做标记检查、`NEO_STRATA_ROOT` 压过应用目录、应用目录有标记时压过 `<app>/strata`、
  应用目录是空的才用 `<app>/strata`、哪都没有时回应用目录并在理由里写"这里还没有引擎"且监督器的默认值
  照样能用、`NEO_STRATA_ROOT` 原样照用、相对路径按应用目录解析；`test_real_tree_today` 与
  `test_manager_default_root_matches` / `test_state_root_matches` 钉住今天这台机器：新规则算出的根
  与 `manager` / `state` 原来各自算的**逐字节相同**（exe 放进 Strata 根目录时第 3 条先命中）。
- `test_engine_setup`（48 个）：`engine_ref.json` 的形状（缺键 / 坏 SHA / 坏 zip 摘要 / 不是字典都抛，
  archive 地址就是那个 tag 的 zip）；`clone_argv` 是 `--depth 1 --single-branch --branch <tag>`
  且落到 `strata/`、`checkout` 是 detach 到钉住的 SHA；`plan()` 七个状态各钉一条
  （`clone` / `zip_copy` / `need_zip` / `not_a_git_tree` / `checkout_pin` / `dirty` / `ready`），
  摘要对不上的 zip 算"不可用"、zip 装的树靠 Neo 自己记的那一笔算 `ready`、记的 SHA 不是 pin 就不算；
  摘要是唯一的进门路（pin 里没有摘要时任何 zip 都不解）、解压只搬那个顶层目录并记下来历、
  两个顶层目录的 zip 不解、目标已存在不解；`run_engine` 在脏树 / 普通目录前拒绝且**一个文件都不动**，
  `--zip` 那条路装完并记下来历；`status()` 的形状就是页面读的；命令行 `--dry-run` 只打印命令不写文件、
  `--json` 是 JSON、pin 读不出来说人话；`--ensure` 在 cp936 控制台上打印带 `U+FFFD` 的 git 输出不崩
  （`wide_stdout()`）。要 git 的用例在没 git 的机器上带理由跳过。
- `test_deploy`（29 个）：三道闸门各钉一条 —— 引擎（HEAD == pin 且干净才过；HEAD 不是 pin 挡；
  跟踪文件脏挡；**没有 git 不算干净**；**部署树里 Neo 自己的 untracked 文件不算脏**）、
  路径（`neo/state/`、`__pycache__/`、`build/`、`dist/` 不带走；exe 不在源码清单里；
  每个文件分 new / update / same；目标里多出来的文件要报出来）、进程（缺 exe 挡、在跑的实例挡）；
  dry-run 一个文件都不写；`--apply` 写完逐文件核对 SHA-256、闸门不过的树拒绝写、
  `--prune` 只删"源里没有的 Neo 文件"；`--json` 是 JSON 且说得出闸门过没过；报告里点名闸门与计数；
  `--to` 不给就报错；pin 与目标树对不上时挡住报告。本地忽略那一笔（`.git/info/exclude`）钉六条：
  缺的行一次补齐、第二次一个字节都不动（不重复）、dry-run 只报不写、目标不是 git 检出就说"没有可写的地方"、
  补完的行让 `git check-ignore -v` 真把 `strata/` 与 `strata-q2_0.json.bak` 认成忽略（备份那条是上游
  `setup.py` 写的文件，上游的 `.gitignore` 只到 `/strata-*.json`）；`--apply` 那一条还核对了上游的
  `.gitignore` 逐字节不变。控制台打印一条：把 stdout 换成 cp936 的字节流（`BytesIO` + `TextIOWrapper`，
  和真控制台一样能 `reconfigure`），打印带 `U+FFFD` 的行 —— 不宽化就抛 `UnicodeEncodeError`，
  宽化后按 UTF-8 读回来是 `git : fatal: ...`。
- `test_harness`（7 个）：测试自己的两条规矩 —— 时间标准（`run_all.BUDGET_S` 就是 30 秒、`--timeout`
  的默认值就是它、跑完必报秒数与"超预算"那一行）与那条跳过规则（引擎的真服务 import 不动时
  `server_ready` 说清缺哪个包并给出 `requirements.txt`，纯标准库的 import 不算缺，没有引擎的目录
  说"没有引擎"，当前引擎根要么起得来要么给原因，不许"起不来也没话说"）。
- `test_console` 里的 `TestEngineEndpoint`（6 个）：`GET /neo/engine` 回页面要读的 13 个键、
  三步的名字、两条路由接在 `console_server.py` 里、认不得的 action 挡回来、
  `install` 走后台线程（`wait=False`，点一下不能把请求线程堵几十分钟）且 `setup=True`
  （取完源码交给 `setup.py`，Neo 不自己装）、host 与 key 从监督器传下去。netprobe 与
  `run_engine` 都换成假的 —— 一次 443 都不连、一次 clone 都不跑。
  2026-10-13 补的两条钉住"接线"本身：`start()` 必须把 Handler 里 `self.server.X(...)` 用到的
  每个名字都挂到 httpd 上（少挂一个，socketserver 把异常吞在 `handle_error` 里，连接直接关掉，
  页面只看到 fetch 失败 —— 引擎卡当时就是转圈不动，`curl` 报 `Empty reply from server`），
  以及 **真走一次 HTTP** 的 `GET /neo/engine`（以前只直接调 `console.engine_status()`，绕开了
  httpd，所以挂漏没人响）。
- `test_manager_cli`（12 个）：三个开关都不给 = 老默认（`127.0.0.1`、无 key、不打印）；`--lan` 绑
  `0.0.0.0`、`--host` 压过 `--lan`、命令行压过 `launcher.json`；`--token` 现生成一把并打印全串、
  落进 `launcher.json`、**不进 `manager.log`**，已有 key 时不再生成也不打印；端口优先级
  `--port` > `launcher.json` > 发现的第一条 > 8095；`--connect` 打印的那块含 base、key、
  `claude --model …`，且**不创建任何 agent 目录**，不认识的 agent 与没有 key 各说一句人话。
- `test_ui` 里的接入页检查：「接入」页与顶栏 tab 在、页面里写的 `/neo/net` `/neo/lan` `/neo/token`
  `/neo/connect` `/neo/wizard` `/neo/engine` 六条路由服务里都有、`app.js` 里每一个 `$("id")` 在 `index.html` 里都有
  对应的 `id="..."`（这类错在窗口里是静默的：`getElementById` 回 null，取 `.textContent` 抛异常，
  整段渲染停住，页面看着像没数据）、新样式块 `.agents`/`.agent`/`.cmd`/`.steps`/`.keyval` 在
  `style.css` 里、`loadConnect` 在顶层；**「复制 token」那条**：按钮 `lan-copy-key` 与只读输入框
  `lan-key-value` 在 HTML 里、输入框默认是藏着的（`class="path keyval hidden"`）、`app.js` 里
  接了 `{"action":"reveal"}` 且生成后把完整 key 留在页面里（省得再生成一把）、
  `.keyval.hidden` 那条补在 `.keyval` 之后（`.keyval` 的 `display:block` 定义在 `.hidden` 之后，
  不补这一条那个框会一直露着）。

exe 那条路是手工验的（2026-10-12，Windows 11）：`NeoStrata.exe --engine mock` 起窗口 →
`/neo/status` 到 `running`、`Responding=True`、`neo/state/instance.json` 里 `mode="window"`；
再开第二个实例，日志里出现带 pid / 根目录 / 界面地址 / `--force` 的那段话；关窗口后
`instance.json` 被 `atexit` 抹掉。摘掉 js_api 桥之后重验一遍：窗口开着跑 95 秒
`Responding=True`、日志 0 次"没应答"，`POST /neo/open` 与 `/neo/open-log` 都回 200 并真打开了
页面 / 文件；同一个 exe 放进 `D:\Work\Strata` 跑 `--no-start`，模型页列出 4 个模型。

接入页那五条接口在 exe 里也逐条验过（2026-10-12，`NeoStrata.exe --engine mock --port 28180`，
控制台 28183）：`/neo/status` `state=running` `healthy=True` `pid=6912`；`/neo/lan` 说"只绑本机
（127.0.0.1）"、`api_key_masked` 为空、`lan_bases` 给出 `http://172.18.240.1:28180` 与
`http://192.168.0.165:28180`；`/neo/net` 三个目标都通（huggingface.co 228 ms / hf-mirror.com
382 ms / www.modelscope.cn 67 ms），建议源 `huggingface`；`/neo/connect` 四个配方齐、
`base=http://127.0.0.1:28180`、模型 `mock`、上下文 32768；`/neo/wizard` 四步全 OK，命令是
`...\python.exe ...\setup.py --port 28180 --yes --no-start`。命令行侧 `python -m neo.wizard --check`
与 `python -m neo.connect list` 在同一台机器上打印完整（闸门不再裁整块，见"日志与控制台输出"第 5 条）。

「复制 token」这条在 mock 壳上逐条验过（2026-10-13，Windows 11 / Python 3.13，
`ConsoleServer` + `MockEngine`，Neo 的 state 目录指到临时目录）：开局 `/neo/lan` 说没有 key；
`POST /neo/token` 出 `sk-neo-23fb810c80e7629c12f0d26dd16c7f6e`（39 字符），`/neo/lan` 跟着只报
`sk-neo-23fb810c…（39 字符）`；`{"action":"reveal"}` 交回同一把，连点两次都是它；
对端不是本机时（直接调 `make_token(..., loopback=False)`）`api_key` 回空串、只给掩码；
`unset` 后 `launcher.json` 里是空串而 key 缓存里还有 1 条；页面 `/`、`/app.js`、`/style.css`
里 `lan-copy-key`、`lan-key-value`、`{"action":"reveal"}`、`.keyval` 都在。

token 管到哪这条在 exe 里也验过（2026-10-13，Windows 11，`NeoStrata.exe --engine mock
--port 28190 --console-port 28193`；exe 13.3 MB，`instance.json` 里 `mode="window"`，
窗口真起来了）：`POST /neo/token` 出 `sk-neo-cb2cc50c…`（39 字符）→ `POST /neo/stop` →
`POST /neo/start`，服务日志里的命令行变成 `... serve/server.py --engine mock ... --port 28190
--api-key sk-neo-cb2cc50c606104ccabdaec5a7da32588`。此后本机 `GET /metrics` 无 key 回 **200**，
`live`/`totals`/`hardware`/`hardware_static`/`requests` 五节齐（就是监控页那几张卡片要的），
`/status`、`/slots` 无 key 200，`Host: evil.example.com` **401**，`POST /v1/chat/completions`
无 key **401** / 带 key **200**，`GET /v1/models` 无 key **401**，`POST /settings` 无 key **401**，
`/health` 报 `api_key=true`；`{"action":"unset"}` 之后 `/neo/lan` 的 `api_key` 为空。
`/api/requests` 在这台机器上是 404 —— 这条路由要 `svc.api_monitor` 开着才存在，与鉴权无关。

强杀 exe（`Stop-Process -Force`）不走 `atexit`：`neo/state/instance.json` 会留下，监督进程与
服务进程也各自还活着（实测留下 pid 16536 听 28193、pid 42336 听 28190），要连着收端口与
`instance.json`；正常关窗口时这三样自己就清了。


## 已知边界

- 端口改了要重启 Neo 才生效（M1 不做热切换）。
- 聊天页与监控页是内嵌上游页面。**监控页在本机不要 key**：`GET /metrics` 那批只读端点对本机对端
  放行（见上节「token 管到哪」），设了 token 之后窗口里的卡片照旧出数。聊天页要 key ——「接入」页的
  "生成 token"就是给这件事用的，key 只在生成那一次完整显示，之后界面上只有掩码（「复制 token」一键
  拿当前这把）。
- 局域网开放、token 生成、四个 agent 的接线在「接入」页（见上节）；**默认仍只绑 `127.0.0.1`、不带 key**。
- 防火墙那条命令**只给不执行**，要用户用管理员权限自己跑；不跑它，局域网里的别的机器连不上。
- 接线配方写的是 agent 的配置文件，Neo **不看 agent 装没装**：agent 不在时配置文件照样写对，
  装上就能用；agent 自己改了配置格式（Claude / Codex / OpenCode / OpenClaw 都在快速变动），
  Neo 认的是参考实现那一份形状，格式变了配方就要跟着改（`test_connectors.py` 盯的就是这一份形状）。
- 向导跑 `setup.py` 时不流式转发输出，页面每 5 秒读一次 `logs/wizard.log` 尾部；
  一次只允许一个安装在跑。
- 未安装模型时显示引导页（根目录没有 `strata-*.json`），「接入」页的安装向导会把 `setup.py` 的
  检查和安装过程搬进这个窗口。
- `neo/state/`（运行数据）与 `neo/state.py`（模块）同名：在 `neo/state/` 目录里直接跑脚本时，
  `import neo.state` 会命中那个目录而不是模块（实测 `python neo\state\repro\hook.py` 报
  `AttributeError: module 'neo.state' has no attribute 'install_excepthook'`）。跑 Neo 的代码要在仓库根，
  `neo/state/repro/` 下的复现脚本自己把根插进 `sys.path`。

## pywebview 6.2.1 上的实测坑

- `create_window()` 不认 `min_width` / `min_height` / `confirm`：窗口参数是 `min_size=(w, h)` 和
  `confirm_close`；`private_mode` 与 `storage_path` 属于 `webview.start()`。参数给错会在启动时抛
  `TypeError`，`neo/manager.py` 认这个异常并退回最小参数集。
- `webview.start(icon=...)` 给 SVG 会让 Windows 后端的 `System.Drawing.Icon.Initialize` 抛
  `ArgumentException`，崩在窗口线程里（Neo 的图标是 SVG，所以不传 icon）。
- `confirm_close=True` 时 `window.destroy()` 会弹确认框并卡住：页面里的"退出"先把
  `confirm_close` 关掉再 destroy。
- 打开真实页面时 pywebview 会往 stderr 打一串 WinForms 字体递归的内部日志
  （`window.native.ActiveControl...GenericSansSerif`），窗口功能不受影响，是噪音。

## 端到端检查

`python neo/tests/manual_end_to_end.py` 真起一次桌面壳（mock 引擎，服务端口 28080，控制台 28083）：
2 秒到就绪（`state=running`、`healthy=true`、`max_context=32768`），状态/模型/日志接口都有应答；
然后把桌面壳进程杀掉模拟被任务管理器结束，3 秒后服务端口 28080 已关闭 —— Job Object 把
Strata 服务带走了，没有残留进程。浏览器退回路径同样验过（`--no-webview`，控制台 28093，
`state=running healthy=true`，杀壳后端口关闭）。

