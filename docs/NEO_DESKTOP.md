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
| `neo/supervisor.py` | 进程监督：启动、探活、退避重启、熔断、诊断、事件时间线 |
| `neo/console_server.py` | 控制台服务（标准库 `http.server`）：页面 + JSON 接口；页面里所有动作的落点，含 `POST /neo/open` / `/neo/open-log` |
| `neo/state.py` | 状态落点 `neo/state/`（启动设置、窗口几何、日志、`instance.json`），原子写 + `.bak`；exe 里状态落点见 `pick_state_dir()` |
| `neo/paths.py` | 路径解析：`safe_resolve()` 不跟 Windows 重解析点（OneDrive 同步根的 `.SyncRoot`），深度封顶 64 段；`app_dir()` / `web_dir()` / `is_frozen()` 认 exe 与源码两种落点 |
| `neo/console_io.py` | 控制台输出闸门：单行 400 字符、每秒 30 行、5 秒内同样的行折叠、控制字符清掉；`quiet_gui_logging()` 压 pywebview/pythonnet 的内部日志；真流写不动时丢掉而不排队；`NullStream` 接窗口版里 `sys.stdout` 是 `None` 的情况 |
| `neo/ui_watchdog.py` | 窗口线程看门狗：`evaluate_js` 探窗口线程，3 秒没回音就把各线程当前所在帧写进日志 |
| `neo/jobobj.py` | Windows Job Object：Neo 结束时子进程（含引擎、视觉编码器）跟着结束 |
| `neo/engines/` | 引擎适配层：`strata.py`（发现配置、拼命令）、`base.py`、`mock.py`（测试） |
| `neo/web/` | 控制台页面：状态 / 模型 / 聊天 / 监控 / 日志 / 设置 |
| `neo/NeoStrata.spec`、`neo/build_exe.py`、`neo/neo_entry.py` | 打成一个 `NeoStrata.exe`（见"打成 exe"） |
| `neo/tests/` | 118 个测试（见下） |
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
- **认服务是自家的还是外人的，走进程树，不能只比 pid**：启动服务的那个 python 自己会再拉
  一个进程去跑脚本，所以端口上的监听者通常是 Neo 孩子的孩子。实测 2026-10-12 在
  `D:\Work\Strata`：NeoStrata 40856 → NeoStrata 46380 → `.venv\Scripts\python.exe`（跳板）
  46984 → 绑上 8080 的 45868，后两条命令行一模一样；单独启动那个 venv 的 python.exe，
  启动它回 42016、真正跑脚本的是它的孩子 42388。`jobobj.is_descendant` 就是干这件事的：
  按"监听者不是我孩子就是外人的服务"写，会把 Neo 自己正在应答的服务判成外人的并杀掉它。
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
POST /neo/open               {"page":"chat|monitor"} 在浏览器里打开那一页（由壳执行）
POST /neo/open-log           用系统默认方式打开服务日志
```

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

## 服务在跑，右上角亮的却是「启动」（已修）

用户的话：模型已经启动了，应该是停止。截图里状态胶囊是"运行中"、服务表写"运行中·模型已就绪"，
右上角三个按钮里 **启动是亮蓝色主按钮、停止是灰的**。

实测 2026-10-12（`python -m neo.console_server mock 28210`，Edge 无头截图 1500x120，
`/neo/status` 回 `state=running`、`healthy=true`、`loaded=true`）：

- `neo/web/style.css` 里**一条 `:disabled` 样式都没有** —— 按钮被禁用后长得跟能按时一模一样，
  而 `button.primary` 的蓝色写死在"启动"上（`index.html` 里带的 class，页面从不换），
  于是运行中的页面读起来就是"该点启动"。
- 禁用判断本身是对的（`btn-start` 在 running/starting 时禁用，`btn-stop` 只在 idle 与 halted 时禁用），
  纯样式问题。

修法：主按钮的蓝色跟着"此刻能按的那个"走 —— `app.js` 的 `render()` 里
`running / starting / stopping / restarting / external` 时点亮**停止**，停着时点亮**启动**，
熔断时点亮**重试**；`style.css` 补 `button:disabled`（`opacity .42` + `not-allowed`）与
`button.primary:disabled`（退回灰底、灰字），禁用一眼看得出来。

修完实测（同一探针、同一截图尺寸）：`state=running` 时右上角是**停止**亮蓝、**启动**发灰。

回归测试：`test_ui.py` 的 `TestHeaderActionButtons`（3 个）—— CSS 里要有 `button:disabled` 与
`button.primary:disabled`；`btn-start` / `btn-stop` / `btn-retry` 三个都要 `toggle("primary")`；
`btn-stop` 的禁用条件里不许出现 `running` / `external` / `restarting` / `stopping`（在跑就该能停）。

## 模型页的"启动它"：跑着的时候点它会发生什么

用户问：模型列表里的"启动它"，是不是要等服务停止了才能按？

**不用先停。** `set_model`（`neo/supervisor.py:156`）就是"记下新模型 + `stop()` + `start()`"——
Neo 自己把当前服务停掉、用新模型重新起。实测 2026-10-12（mock 真进程，
`neo/tests/test_supervisor.py` 的 `TestSetModel`）：running 时调用它，进程号换了、状态回到
running，`restart_number` 仍是 0 —— 用户主动换模型不占退避次数、不推熔断。

代价是**正在跑的请求会被切断**（`stop()` 给完优雅退出窗口就杀整个子进程树）。以前按钮只写
"启动它"，跑着和停着长得一模一样，看不出点下去会发生什么。现在写清楚：

- 当前模型：按钮"正在用"，禁用；
- 服务在跑（running / starting / stopping / restarting / external）：按钮"换过去"，
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
- **打包环境里没装 pywebview 就拒绝打包**：缺包时 `collect_submodules("webview")` 是空列表，
  PyInstaller 一声不响地打成功，出来的 exe 没有窗口。实测 2026-10-12 19:58 部署的 exe
  自己的日志：`pywebview 不可用（No module named 'webview'），退回浏览器界面` —— 用户那边
  看不到任何界面。`neo/build_exe.py` 现在先查这一关，缺了就报"先装上：
  `python -m pip install -r neo/requirements-neo.txt`"；确实只要浏览器界面就加 `--no-webview`。
  实测两种打包：装了 pywebview **13.2 MB**（`webview.*` 73 个模块进包，含
  `webview.platforms.edgechromium` 与 `winforms`），没装 **8.8 MB**。
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
- 打包产物（`NeoStrata.exe`、`neo/dist/`、`neo/build/`）不进仓库（`neo/.gitignore`）。

## 测试

```
python -m unittest neo.tests.test_logging neo.tests.test_ui neo.tests.test_supervisor \
                   neo.tests.test_engines neo.tests.test_upstream_api neo.tests.test_console
```

118 个测试，150.1 秒（Windows 11，Python 3.13.15，`serve/server.py --engine mock` 起真实进程，
不需要 GPU、不需要装好的模型；`test_logging` 22 个、`test_ui` 29 个共 6.1 秒，其余 67 个 144.0 秒）：

- `test_supervisor`：启动到就绪、事件记录、非预期退出后自动重启（新进程）、退避按 1/2/3 次走、
  10 分钟内 3 次后熔断并停在 halted、用户停止后不被拉起、接手已在运行的服务、诊断关键词、
  **状态页的 metrics 真是数字**（真进程：`requests` 是累计条数，值里不许有列表/对象）。
- `test_engines`：配置发现与排序（`setup.py` 的 `touch()` 语义）、启动命令只用上游已有参数、
  模型文件缺失拒绝启动、没有配置时的引导态、mock 适配器；**真实配置的形状**（扁平 `args`、模型在
  `--native`、上下文在 `--max-context`、没有 `path` 键）、**模型在而引擎 exe 不在要标出来并拒绝启动**、
  没点名时挑一个能启动的配置；**`/metrics` 的四节压成卡片键**（`TestMetricsFlatten` 吃真应答载荷：
  显存 24028 / 24455 MiB、内存 70.6 / 93.6 GB、`requests` 是 1 不是列表、空闲时不编一个生成速度）。
- `test_upstream_api`：Neo 用到的 `serve/server.py` 参数（`--engine/--config/--port/--host/
  --api-key/--gpu`）、`/health` 的字段、`/metrics` 的**四节与里面的键**（`live` / `totals` /
  `hardware` / `hardware_static`，以及 `serve/telemetry.py` 的 `cpu` / `ram_used` / `mem_used`）、
  聊天页与监控页文件、`setup.py` 的 M2 参数、
  上游 `requirements.txt` 未被改动。上游改名时这条测试会先响，而不是等用户的 Neo 起不来。
- `test_console`：状态接口、静态页 no-store、起不来的引擎停在 halted、外站 Origin 与外站 Host 403、
  设置落盘且不回显 API key；**页面点"打开"走的是服务**（`POST /neo/open` 带 page、`POST /neo/open-log`、
  没壳时回 503、外站 Origin 403 且壳没被调用）；**第二个实例那句话**（窗口模式说 pid 与界面地址、
  浏览器模式说"没有窗口"并给出装 pywebview 的命令、记录里的进程没了说"已经没了"并清掉过期记录）。
- `test_ui`（22 个）：闸门在别的线程占住锁时 `write()` 立刻返回并把行计入丢弃数、丢弃数要说出来、
  没人抢锁时输出不被改；`state.log()` 常态只追加（20 条 = 20 行、0 个 `\r`）、超过 `LOG_MAX`
  才裁且只留尾部、6 线程并发 90 条不丢不咬；看门狗无窗口不起、线程现场有界且带线程名、
  `evaluate_js` 挂住时日志出现"没应答"与 `neo-ui-probe` 那一帧、窗口正常应答时不写日志；
  `window_kwargs()` 里没有 `js_api`（有坐标才写 `x`/`y`）、`app.js` 代码里不出现
  `window.pywebview` 且带 `/neo/open`、`/neo/open-log` 两条路由；**`app.js` 的花括号配平且
  `render`/`poll`/`ensureFrame`/`num`/`count` 在顶层、末尾的 `poll();` 与 `setInterval(poll, 2000);`
  在顶层**（内嵌框停在 `about:blank` 的那次实测就是被吞进函数体）；**状态页六张卡片只经过
  `num()` / `count()`**（`m.requests` 这类不许直接嵌进模板串，`count()` 对列表与对象回"—"）；
  **打包环境缺 pywebview 时
  `build_exe` 拒绝打包**并给出装包的命令，`--no-webview` 真的传到 `build(allow_no_webview=True)`。
- `test_logging`（22 个）：连续写 8 条后日志里 0 个 `\r`（CRLF 放大的回归）、20 万字符的单行被裁并标出
  原长、清洗尾部不丢历史；`tail_text` 去 `\r` 与控制字符、行数封顶并标"已省略"；闸门在 18 亿字符灌入下
  输出有界、说明折叠与丢弃了多少、正常输出不被改；`clean_line` 行为、`install()` 幂等；
  `RecursionError` 不格式化 traceback、普通 traceback 行数与总长封顶、excepthook 装得上；
  `safe_resolve` 把 5000 段 `.SyncRoot` 截到 64 段以内、相对路径与 `..` 正常、`is_reparse` 对普通目录回 False；
  `instance.json` 记下/读回/清掉、只补 `mode` 不抹 `root`、进程没了算过期。

exe 那条路是手工验的（2026-10-12，Windows 11）：`NeoStrata.exe --engine mock` 起窗口 →
`/neo/status` 到 `running`、`Responding=True`、`neo/state/instance.json` 里 `mode="window"`；
再开第二个实例，日志里出现带 pid / 根目录 / 界面地址 / `--force` 的那段话；关窗口后
`instance.json` 被 `atexit` 抹掉。摘掉 js_api 桥之后重验一遍：窗口开着跑 95 秒
`Responding=True`、日志 0 次"没应答"，`POST /neo/open` 与 `/neo/open-log` 都回 200 并真打开了
页面 / 文件；同一个 exe 放进 `D:\Work\Strata` 跑 `--no-start`，模型页列出 4 个模型。


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

