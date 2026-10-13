# Neo - Strata 的桌面管理层

Neo 给 Strata 加一层桌面外壳：窗口、进程监督、崩溃自动重启、状态/日志/聊天/监控页面，
外加"装模型"和"接编码 agent"两件事。它只通过上游已有的命令行参数、环境变量和 HTTP 端点
和 Strata 说话，**不改上游任何一个文件**。

上游是 [Niko1221/Strata](https://github.com/Niko1221/Strata)（本仓库的 remote `upstream`）。
引擎源码只从 GitHub 的 tag 来：钉在 `neo/engine_ref.json` 里（今天 `v0.1.43.1` =
`a0116b2782a5b82622358c04a06496485d12b2d6`）。

## 上手（在这个仓库里开发）

```bash
python -m neo.engine_setup --ensure      # 按 pin 把引擎源码浅克隆进 strata/（已 gitignore）
python -m neo.wizard --check             # 这台 PC 的情况 + 该装哪个模型（只读，不装）
python -m neo.tests.run_all --quick --skip-network
python -m neo.webcheck
```

`strata/` 是 Neo 真会用的那个引擎根：`neo/paths.py::strata_root()` 一条规则算出它
（`--root` > `NEO_STRATA_ROOT` > 目录里有 `setup.py` + `serve/server.py` > `<app>/strata` >
应用目录本身），测试走的是同一条规则（`neo/tests/_bootstrap.py` 里的 `ENGINE`）。
命令行（`neo.wizard` / `neo.models` / `neo.model_settings` / `neo.connect`）不给 `--root` 时
走的也是这一条（`neo/paths.py::resolve_root()`），所以在这个仓库里不用点名指目录。
对着一个完整的 Strata 检出跑测试就给它：`NEO_STRATA_ROOT=D:\Work\Strata`。

## 测试

| 跑法 | 实测（2026-10-13，同一台机器） |
|---|---|
| `python -m neo.tests.run_all --quick --skip-network` | 10.2 s，401 测试 |
| `python -m neo.tests.run_all --skip-network` | 18.8 s，488 测试 |
| `python -m neo.tests.run_all`（含真克隆那一条） | 29.2 s，496 测试（墙钟跟着网络走） |

`run_all` 把每个模块拆成测试类并行跑（`--jobs`，默认 `min(4, 核数)`），失败的类单独重跑一次，
所以端口/负载引起的偶发失败会和真失败分开报。测量用的探针在 `neo/tests/probes/`
（`probe_cost.py`：进程启动与就绪的代价；`supervisor_events.py`：监督器事件时间线）。

## 部署到一台装了 Strata 的机器

```bash
python -m neo.deploy --to D:\Work\Strata            # 先看要写什么（dry-run）
python -m neo.deploy --to D:\Work\Strata --apply    # 写：只写 neo/** + NEO-START.bat + NeoStrata.exe
python -m neo.build_exe                             # 打窗口版 exe（先跑 webcheck，缺包会一次报全）
```

部署树是上游 Strata 的 git 检出，Neo 的文件在那儿全是 untracked；闸门：那边 HEAD 等于 pin、
跟踪文件干净、没有该树的 `NeoStrata.exe` 在跑。细节见 [docs/NEO_DESKTOP.md](docs/NEO_DESKTOP.md)。

## 页面

`python -m neo.webcheck` 把 `neo/web/` 里两个 `<script>` 当一个程序解析（约 1 s），
`build_exe` 打包前会先跑它——两个文件共用一个全局作用域，同名声明会让后一个文件永不执行。
