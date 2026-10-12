# Neo Strata 桌面管理器

Neo 是 Strata 的桌面管理层：一个窗口，管着模型的启动、停止、崩溃自动重启、日志和接入。
它住在 `neo/` 里，只通过上游已有的命令行参数、环境变量和 HTTP 端点与 Strata 打交道，
**不修改仓库里的任何现有文件**，所以上游更新时 Neo 不会与之冲突。

当前进度：**M1 完成**（桌面壳 + 进程监督 + 崩溃自动重启 + 状态/日志/聊天/监控页）。
M2（安装向导与国内网络自动判断）、M3（局域网访问、token 与 Claude Code / Codex 接入）、
M4（llama.cpp 引擎适配与打包）还没开始。

## 怎么起来

```
python neo/install_neo.py          # 装 pywebview（装进 Strata 的 .venv，没有 .venv 就装当前 Python）
NEO-START.bat                      # Windows 双击/命令行启动；没有控制台用 neo\NEO-START.vbs
python neo/manager.py              # 直接跑；--engine mock 用 mock 引擎，--no-webview 用浏览器
```

pywebview 用 Edge (WebView2) 后端；机器上没有 WebView2 运行时、或没装 pywebview 时，
`neo/manager.py` 自动退回浏览器界面 —— 控制台服务是标准库写的，功能一样，只是没有原生窗口。

### 双击闪退（已修）

双击 `NEO-START.bat` 窗口一闪就没，实测有两个来源：

- 启动器写的是 `python neo\manager.py`。`neo/manager.py` 顶部是包内相对导入，直接当脚本跑会抛
  `ImportError: attempted relative import with no known parent package`，进程立刻退出。现在启动器
  统一走 `-m neo.manager`（`.bat` / `.vbs` / `.ps1` 都改了），`manager.py` 自己也认下 `neo` 包，
  两种跑法都能起来（实测 `python neo\manager.py --help` 与 `python -m neo.manager --help` 都退 0）。
- 退出码非 0 时 `.bat` 以前直接关窗口，看不到原因。现在窗口留着，打印退出码和
  `neo\state\logs\manager.log` 的最后 15 行，按任意键才关；`.bat` 开头加了 `chcp 65001`，
  中文日志在控制台里不再成乱码。

第二个实例（比如上一次没退干净）弹"Neo 已经在运行了"模态提示后退出 —— 这个框要点掉，
不是闪退。端口被上一次留下的服务占着时，新实例会接手它（状态页显示"接手已在运行的服务"）。

## 文件

| 文件 | 作用 |
| --- | --- |
| `neo/manager.py` | 桌面壳：单实例互斥体、窗口几何持久化、pywebview 容器、浏览器退回 |
| `neo/supervisor.py` | 进程监督：启动、探活、退避重启、熔断、诊断、事件时间线 |
| `neo/console_server.py` | 控制台服务（标准库 `http.server`）：页面 + JSON 接口 |
| `neo/state.py` | 状态落点 `neo/state/`（启动设置、窗口几何、日志），原子写 + `.bak` |
| `neo/paths.py` | 路径解析：`safe_resolve()` 不跟 Windows 重解析点（OneDrive 同步根的 `.SyncRoot`），深度封顶 64 段 |
| `neo/console_io.py` | 控制台输出闸门：单行 400 字符、每秒 30 行、5 秒内同样的行折叠、控制字符清掉；`quiet_gui_logging()` 压 pywebview/pythonnet 的内部日志；真流写不动时丢掉而不排队 |
| `neo/ui_watchdog.py` | 窗口线程看门狗：`evaluate_js` 探窗口线程，3 秒没回音就把各线程当前所在帧写进日志 |
| `neo/jobobj.py` | Windows Job Object：Neo 结束时子进程（含引擎、视觉编码器）跟着结束 |
| `neo/engines/` | 引擎适配层：`strata.py`（发现配置、拼命令）、`base.py`、`mock.py`（测试） |
| `neo/web/` | 控制台页面：状态 / 模型 / 聊天 / 监控 / 日志 / 设置 |
| `neo/tests/` | 33 个测试（见下） |
| `neo/requirements-neo.txt`、`neo/install_neo.py` | Neo 自己的依赖，不进上游 `requirements.txt` |

Neo 的运行状态全在 `neo/state/`，被 `neo/.gitignore` 忽略。

## 监督器的行为

```
idle ──启动──> starting ──/health 应答──> running
                  │                        │
                  │ 加载超时                │ 子进程非预期退出
                  ▼                        ▼
             restarting（退避 5 / 15 / 45 秒）
                  │
                  └── 10 分钟内已重启 3 次 ──> halted（熔断，等人工处理）
```

- **用户点"停止"后绝不自动拉起**：`intent=stop` 是硬开关，退避等待会被它立刻打断。
- **探活分三层**：端口在听（TCP）→ `/health` 应答（服务活着）→ `loaded`（模型已加载）。
  模型加载要几分钟，这段时间 Neo 显示"启动中"，不算失败。
- **引擎重载不归 Neo**：`serve/server.py` 自己会重启引擎（`StrataEngine.restart`，issue #344），
  Neo 只在事件里显示一条提示，不干预。
- **端口上已有活着的服务就接手**：用户先用 `START-HERE.bat` 起来了，Neo 认下它，不重复启动。
- **熔断给一句能照着做的提示**：从日志尾部认 `out of memory` / `failed to map` /
  `page file` / `already in use` / `driver is too old` 等，措辞对齐 `docs/TROUBLESHOOTING.md`。

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
```

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
| 控制台 stdout/stderr（`neo/console_io.py`） | 单行 400 字符、每秒 30 行、5 秒内同样的行折叠成一条计数、控制字符（`\r`、BEL、ANSI 序列）换空格 | 5000 条 36 万字符的 `.SyncRoot` 行（共 18 亿字符）灌进闸门 → 出来 441 字符、2 行、最长行 419 字符、0 个 `\r` |
| `neo/state/logs/manager.log` | 单行裁到 400 字符并标"原长 N 字符"；写入前裁到尾部 200 KB | 同样 5000 条巨行 → 文件 196 KB、454 行、最长行 419 字符、0 个 `\r` |
| `GET /neo/log` | 一次最多 60 KB / 600 行，行内已裁 | 灌过后应答 539 字符 |
| 日志页（`neo/web/app.js`） | 一次取 60000 字符、最多渲染 400 行、内容没变不重画 | 页面不再被一条巨行卡死 |
| `NEO-START.bat` 打印的尾部 | 15 行、控制字符换空格、单行 200 字符 | 实测 15 行、最长 77 字符、中文正常 |
| 未捕获异常 | traceback 最多 40 行 / 4000 字符；`RecursionError` 不格式化 traceback（上千帧、消息本身几十万字符，格式化它自己要再递归一层），只给一句指向 pywebview 与同步目录的提示 | 90019 字符的异常消息进日志后单行 416 字符 |

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
Python 主线程只在 `thread.Join(500)` 里转。页面里一次点击的完整路子：

1. 点按钮 → WebView2 `PostWebMessageAsJson` → `platforms/edgechromium.py:225` 的
   `on_script_notify` **在窗口线程上**执行，它里面的 `print` 也在这条线程上写 stdout；
2. `util.py:247` 的 `js_bridge_call` 把 `Shell` 的方法丢到新线程跑（这一段不挡窗口）；
3. 方法返回后 `window.evaluate_js(...)` 把结果送回页面 → `platforms/edgechromium.py:137` 里
   `self.webview.Invoke(...)` **同步投递给窗口线程**，再 `semaphore.acquire()` 等它跑完；
4. 窗口线程跑这个委托要抢 GIL。它一旦长时间抢不到 GIL，或者被 `print` 卡在控制台上，
   消息循环就停住：所有 js_api 调用都不返回，窗口点什么都没反应。

Neo 这边喂这个坑的三处，逐个堵掉：

| 处 | 原来 | 现在 | 实测 |
| --- | --- | --- | --- |
| `console_io.BoundedStream.write` | 排队等 `_lock`。有线程卡在真流的写上（控制台被 QuickEdit 选中、管道满）时，窗口线程跟着挂住 | 拿不到锁立刻返回，这一行丢掉并计数 | 别的线程占住锁时 `write()` 立刻返回（测试断言 < 0.5 秒），行计入"控制台写不动，丢弃 N 行" |
| `state.log()` | 每条日志把整个尾部（最多 200 KB）读出来逐行清洗再整份回写，全程握着 GIL | 常态只追加一行，超过 `LOG_MAX` 才裁一次 | 20 条日志 = 20 次追加写、文件里 0 个 `\r`；6 个线程并发 90 条，不丢不咬 |
| `Shell.stop()` | `sup.stop()` 里 `thread.join(timeout=10.0)`，这条 js_api 调用挂 10 秒，页面里 `await` 它的按钮一直转 | `join_s=0.5`，剩下的收尾由监督线程自己走完，状态页靠 2 秒一次的轮询更新 | 点停止 0.5 秒内回话 |

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
（页面每 2 秒一次 js_api 轮询）：日志里 0 次"没应答"，进程 `Responding=True`；测试里把
`evaluate_js` 挂住，日志里立刻出现"没应答"和 `neo-ui-probe` 停在哪一帧。

## 测试

```
python -m unittest neo.tests.test_logging neo.tests.test_ui neo.tests.test_supervisor \
                   neo.tests.test_engines neo.tests.test_upstream_api neo.tests.test_console
```

61 个测试，36.9 秒（Windows，Python 3.13.15，`serve/server.py --engine mock` 起真实进程，
不需要 GPU、不需要装好的模型；`test_logging` 18 个、`test_ui` 10 个共 4.3 秒，其余 33 个 32.6 秒）：

- `test_supervisor`：启动到就绪、事件记录、非预期退出后自动重启（新进程）、退避按 1/2/3 次走、
  10 分钟内 3 次后熔断并停在 halted、用户停止后不被拉起、接手已在运行的服务、诊断关键词。
- `test_engines`：配置发现与排序（`setup.py` 的 `touch()` 语义）、启动命令只用上游已有参数、
  模型文件缺失拒绝启动、没有配置时的引导态、mock 适配器。
- `test_upstream_api`：Neo 用到的 `serve/server.py` 参数（`--engine/--config/--port/--host/
  --api-key/--gpu`）、`/health` 的字段、`/metrics`、聊天页与监控页文件、`setup.py` 的 M2 参数、
  上游 `requirements.txt` 未被改动。上游改名时这条测试会先响，而不是等用户的 Neo 起不来。
- `test_console`：状态接口、静态页 no-store、起不来的引擎停在 halted、外站 Origin 与外站 Host 403、
  设置落盘且不回显 API key。
- `test_ui`（10 个）：闸门在别的线程占住锁时 `write()` 立刻返回并把行计入丢弃数、丢弃数要说出来、
  没人抢锁时输出不被改；`state.log()` 常态只追加（20 条 = 20 行、0 个 `\r`）、超过 `LOG_MAX`
  才裁且只留尾部、6 线程并发 90 条不丢不咬；看门狗无窗口不起、线程现场有界且带线程名、
  `evaluate_js` 挂住时日志出现"没应答"与 `neo-ui-probe` 那一帧、窗口正常应答时不写日志。
- `test_logging`（18 个）：连续写 8 条后日志里 0 个 `\r`（CRLF 放大的回归）、20 万字符的单行被裁并标出
  原长、清洗尾部不丢历史；`tail_text` 去 `\r` 与控制字符、行数封顶并标"已省略"；闸门在 18 亿字符灌入下
  输出有界、说明折叠与丢弃了多少、正常输出不被改；`clean_line` 行为、`install()` 幂等；
  `RecursionError` 不格式化 traceback、普通 traceback 行数与总长封顶、excepthook 装得上；
  `safe_resolve` 把 5000 段 `.SyncRoot` 截到 64 段以内、相对路径与 `..` 正常、`is_reparse` 对普通目录回 False。


## 已知边界

- 端口改了要重启 Neo 才生效（M1 不做热切换）。
- 聊天页与监控页是内嵌上游页面：服务需要 API key 时，iframe 里会要 key（M3 的 token 处理这件事）。
- 局域网开放、token 生成、Claude Code / Codex 接线在 M3；M1 只绑 127.0.0.1。
- 未安装模型时显示引导页（根目录没有 `strata-*.json`），M2 会把 `setup.py` 的检查和安装
  过程搬进这个窗口。
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

