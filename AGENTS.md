# AGENTS.md

Strata runs the Qwen3.8-Flash-Next mixture-of-experts model (and its Coder, Swift 1.5 and Unsloth variants) on a
normal PC: one NVIDIA or AMD graphics card plus system RAM, on Windows or Linux. It has a C++/CUDA/HIP engine
(`src/`, `include/`), a Python server with an OpenAI- and Anthropic-compatible API and a web app (`serve/`), and a
one-click installer (`setup.py`, started by `START-HERE.bat` / `setup.sh`).

## Installing Strata for a user

Follow **[docs/AI_SETUP.md](docs/AI_SETUP.md)**: check the PC, pick the model by RAM, run setup non-interactively,
start and verify the server, and connect the user's apps. Never expose the server beyond `127.0.0.1` without
`--api-key`. As an alternative to shell commands, Strata's MCP server ([docs/MCP_SERVER.md](docs/MCP_SERVER.md))
offers the same steps as tools.

## Working on the code

- How the engine works, every measured number, the API and all settings: [docs/DETAILS.md](docs/DETAILS.md) and
  the [paper](docs/paper/Strata-Paper.pdf).
- AMD (HIP) build and validation: [docs/AMD_HIP.md](docs/AMD_HIP.md); multi-GPU: [docs/MULTI_GPU.md](docs/MULTI_GPU.md).
- Setup's own tests run without a GPU or downloads: `python tools/test_setup_<name>.py` (for example
  `tools/test_setup_amd.py`, `tools/test_setup_choices.py`).
- Keep the docs' style: plain words, measured numbers with what they were measured on, no claims without a
  measurement.

## Neo desktop manager (`neo/`)

> **Neo's own development repo is split out (2026-10-13): `neoneo-ai/neo-strata`** (local
> `D:\Work\projects\neostrata`), carrying the 19 commits that touched `neo/`, `docs/NEO_DESKTOP.md` and
> `NEO-START.bat` (replayed with `git am`, messages/authors/dates kept; the tree is byte-identical -
> compared by blob hash). This fork is the upstream-tracking and PR base (renamed to
> `neoneo-ai/strata-upstream` the same day, so the old `neoneo-ai/Neo-Strata` URL is now 404 and
> `origin` here is `https://github.com/neoneo-ai/strata-upstream`): upstream-side changes
> (`serve/`, `setup.py`) stay here, and Neo development happens in the other repo. `neo/` here stays
> deployable, so the suite must stay green in both shapes: in a Neo-only repo the engine root is
> `<repo>/strata` (`python -m neo.engine_setup --ensure` clones the pinned tag there), and
> `neo/tests/_bootstrap.py` is what separates `REPO` from `ENGINE`.

`neo/` is an additive desktop layer (window shell, process supervisor, crash auto-restart, status/logs/chat/monitor
pages). It talks to Strata only through existing CLI flags, env vars and HTTP endpoints, and keeps all its own state
under `neo/state/` - it changes no upstream file. Details, the restart/backoff rules, the single-instance rules and
its tests: **[docs/NEO_DESKTOP.md](docs/NEO_DESKTOP.md)**. Package it as one windowed `NeoStrata.exe` with
`python -m neo.build_exe` (spec `neo/NeoStrata.spec`), with pywebview installed in the build env - the build refuses
when a build package is missing and names every missing one in one message (pywebview included, because the exe
would silently lose its window and open only a browser page), and it refuses when
`neo/webcheck.py` reports a page script that would not run in the browser (two `<script>` tags share one global
scope, so a top-level name declared in both files means the later file never executes - the window opens and every
click is dead); the exe is meant to sit in a
Strata root directory and uses that directory as its root. The root is resolved by one rule
(`neo/paths.py::strata_root()`: `--root` > `NEO_STRATA_ROOT` > the app dir when it holds `setup.py` and
`serve/server.py` > `<app>/strata` > the app dir with a stated reason), and `neo/tests/_bootstrap.py` exposes
the same value as `ENGINE` - a green suite means it ran against the tree Neo itself would use. The engine
source is pinned in `neo/engine_ref.json` (`url` / `tag` / `sha` / `zip_sha256`; today `v0.1.43.1` =
`a0116b2782a5b82622358c04a06496485d12b2d6`): `neo/engine_setup.py` clones that tag shallowly into the
gitignored `strata/`, or unpacks `archive/refs/tags/<tag>.zip` only after recomputing its SHA-256 and matching
the pin. Source comes from GitHub tags only - mirrors serve a repository's current files, not the pinned
revision (upstream's own reason in `setup.py`'s `model_source()`, `#908`). Never name the source tree
`engine/`: upstream's `engine/` is the engine *binary* folder. Once the tree is in place Neo installs nothing
itself - venv, dependencies, engine binaries and models all go through upstream `setup.py` (`neo/wizard.py`).
Deploying into a Strata checkout goes through `python -m neo.deploy --to <tree>` (dry-run by default; gates:
engine HEAD equals the pin with tracked files clean - Neo's own files there are untracked by design, so the
dirty check passes `--untracked-files=no`; only Neo's listed paths are written; a running `NeoStrata.exe`
blocks the write), then `webcheck` and the quick suite run in the target tree. `--apply` also appends Neo's four
paths (`/neo/`, `/NEO-START.bat`, `/NeoStrata.exe`, `/strata/`, `/strata-*.json.bak` - the last one covers
upstream `setup.py`'s own `strata-<model>.json.bak` config backups, which upstream's `.gitignore` misses because
a pattern ending in `.json` cannot match `.json.bak`) to the target's `.git/info/exclude` - append-only,
never to the tracked upstream `.gitignore`, dry-run only reports - so `git status` there shows only upstream
files. `neo/deploy.py` and `neo/engine_setup.py` widen stdout to UTF-8 (`wide_stdout()`) before printing: git
output decoded as UTF-8 carries `U+FFFD`, which a cp936 console cannot encode (the `--ensure` CLI crashed on it
before 2026-10-13). Its tests need no GPU, no network and never touch a real
user's agent configs: `python -m unittest
neo.tests.test_supervisor neo.tests.test_engines neo.tests.test_upstream_api neo.tests.test_console
neo.tests.test_logging neo.tests.test_ui neo.tests.test_netprobe neo.tests.test_tokens
neo.tests.test_connectors neo.tests.test_models neo.tests.test_wizard neo.tests.test_connect_api
neo.tests.test_connect_cli neo.tests.test_manager_cli neo.tests.test_start neo.tests.test_model_settings
neo.tests.test_paths neo.tests.test_engine_setup neo.tests.test_deploy`
Run them through `python -m neo.tests.run_all`: it splits every module into its test classes and runs the
classes in parallel (`--jobs`, default `min(4, cores)`), prints seconds per class, and re-runs a failed class
alone once so a port/load flake is reported apart from a real failure. Measured 2026-10-13 on the same
machine: **18.8 s wall for 488 tests with `--skip-network`, 29.2 s with the clone case in** (the same units
summed serially are 71.1 s; the 19 modules as one `python -m unittest` line were 1067.1 s before this, and
492 tests / 1067.1 s is the number that paragraph used to carry). `--quick` takes the sixteen modules that
start no processes: **10.2 s wall, 401 tests**. `--skip-network` drops the one unit that really touches the
network - `test_engine_setup.TestRunAndPage` clones the pinned tag, and it measured 6.8 s and 145.3 s on the
same machine the same day, so the wall clock of a full run follows the link.
`test_engine_setup` and `test_deploy` build real git trees in temp dirs (git init + one commit per case),
which is why they were 8.4 s and 5.0 s alone and minutes inside a serial full run: Windows Defender scans
each temp dir. Run the quick pass while developing, the full pass before a deploy.
Supervisor tests drive `neo/tests/mock_server.py` - a stdlib service that answers `/health` and `/metrics`
in the shapes Neo reads - as a real subprocess on a real port, so start, ready, kill, restart, backoff and
breaker all stay real: spawn is 0.27 s against `serve/server.py --engine mock`'s 0.27 s, and what the mock
skips is the 3.5 s it takes the full server stack to answer `/health` the first time.
`TestStartAndReady.test_start_to_running` keeps the real `serve/server.py --engine mock`, because "the
upstream readiness line reaches the log" is the Neo/upstream seam and the mock must not vouch for it.

`python -m neo.webcheck` checks the page
scripts (the two `<script>` files as one program) in about a second, and `build_exe` runs it before packaging.
Neo's own packages live in `neo/requirements-neo.txt`, not in `requirements.txt`.

The deployed tree (`D:\Work\Strata`) is an upstream Strata checkout, not the dev repo: never commit there and never
add a second remote to its `origin`, so upstream updates stay a plain `git pull` (measured 2026-10-13: 33 commits
behind, 0 ahead, no modified tracked files). Neo's files there are untracked - `NeoStrata.exe`, `neo/`,
`NEO-START.bat`. Deploy with `python -m neo.deploy --to D:\Work\Strata` (dry-run first, `--apply` to write): it
refuses when the tree's HEAD is not the pinned tag, when tracked files are modified, when a `NeoStrata.exe` from
that tree is running, and writes only Neo's listed paths - a `.py`-only
sync once left the deployed source-tree page on the dead `t` / `applyLang` build, and `python -m neo.webcheck` run in
that tree reported it. Build artifacts (`neo/build/`, `neo/dist/`) do not go into the deployed tree.

The embedded monitor page reads Strata through Neo, never through a patched upstream: `neo/console_server.py` proxies
the upstream page, its assets and its read-only panel endpoints under `/strata/` from the console port - GET and HEAD
only, a fixed path menu, `POST` refused with 405, inference routes not forwarded, 502 when the upstream is down, 8 MB
and 5 s caps. Neo attaches its own API key server-side, so the page never sees the key. `engines/base.py`'s `pages()`
returns `http://127.0.0.1:<console>/strata/#monitor` and keeps the chat page direct to the upstream port (chat needs
`POST`; Neo does not spend the user's token on its behalf). `serve/server.py` carries no Neo patch - the loopback
exemption patch was reverted 2026-10-13 - and `neo/tests/test_upstream_api.py` asserts the patch markers stay absent
while the monitor page and those read endpoints stay present.

The 接入 page (`neo/web/`, tab "接入") installs models and wires coding agents: `neo/netprobe.py` probes
`huggingface.co` / `hf-mirror.com` / `www.modelscope.cn` / `github.com` over TCP 443 and only suggests a source,
`neo/wizard.py` runs `setup.py --yes --no-start` in the background, `neo/tokens.py` mints `sk-neo-<32 hex>` keys
and prints LAN addresses (the page's 「复制 token」 puts the live key on the clipboard in one click:
`POST /neo/token {"action":"reveal"}` returns the full key only to requests whose peer address is loopback and
writes nothing, and the page shows the mask until asked), and `neo/connectors.py` merges Neo's block into Claude Code / Codex CLI / OpenCode /
OpenClaw config files (backup to `.neo-bak`, merge never overwrite, unparseable file left untouched). Default stays
`127.0.0.1` with no API key; every one of these is opt-in and writes nothing until the user asks. The page shows one
line per agent - `NeoStrata.exe start claude` (or `python -m neo.start claude` from the source tree, `neo/start.py`) -
which resolves the base URL, key, model id and context window, clears the cloud-account env vars, and launches the
agent; `--no-launch` prints the paste block instead, `NEO_SERVER_URL` + `NEO_API_KEY` point it at a remote Strata.
CLI: `python -m neo.wizard --check`, `python -m neo.connect list`, `python -m neo.start list`.
Console endpoints are attached one by one: a `self.server.X(...)` call in `neo/console_server.py`'s Handler
needs a matching `httpd.X = self.X` in `ConsoleServer.start()`, or the request dies inside socketserver's
`handle_error` and the page only sees a dead fetch (`/neo/engine` was missing both 2026-10-13;
`neo/tests/test_console.py` now guards the wiring list and does one real HTTP round trip).

The server's key guards inference only: `serve/server.py`'s `_authorized(monitor=True)` lets the Monitor's
read-only endpoints (`/metrics`, `/status`, `/slots`, `/props`, `/config`, `/settings`, `/mcp`, `/api/requests`,
`/v1/status`) answer to a request whose peer address is loopback and whose `Host` the server answers to (the Host
check is re-applied there, so a rebinding page gets the 401), while every `POST`, `/v1/*` and `/models` stays
key-gated from any address. The embedded Monitor page therefore keeps showing data after a token is minted;
`neo/tests/test_upstream_api.py` guards that shape.

The 模型 page also carries a "下载安装模型" area: `neo/models.py` reads `setup.py`'s `MODELS` / `FAMILIES`
tables with `ast` (never imports or runs it), reads this machine's RAM / GPU / free disk, recommends by RAM tier
from the docs, and marks each entry fits / tight / no. `GET /neo/catalog` serves it, `POST /neo/install` runs
`setup.py --yes --no-start --family X --model Y` in the background into `neo/state/logs/wizard.log` - Neo never
downloads a model itself, and refuses anything not on that menu. CLI: `python -m neo.models --root .` (`--json`).
Below the menu is a download-source row (auto-by-probe / Hugging Face / ModelScope / hf-mirror), saved in
`neo/state/source.json` and set with `POST /neo/source`: the two direct sources become `--source` values, the
mirror is `HF_ENDPOINT=https://hf-mirror.com` in the child's environment (`setup.py` reads that env var itself,
`--source` has no mirror value), so the selector uses only knobs the upstream already has and changes no upstream
file. CLI: `python -m neo.models --root . --source modelscope`, `python -m neo.wizard ... --source mirror`.

## Contributing a change or report

- Search the open issues and pull requests first, and add to a thread that already covers your point.
- Open an issue with the form that fits (bug report, feature request or question).
- One change per pull request. Say what it changes and what it leaves alone.
- A new feature is opt-in, and the default path stays byte-identical to the last release. Say how you checked.
- Build every backend a file touches (CUDA, HIP, SYCL) before asking for review.
- Change a default only where you measured it faster, and show the numbers with what they were measured on.
- A report from hardware the maintainers do not have is welcome. Follow
  [docs/COMMUNITY_BENCHMARKS.md](docs/COMMUNITY_BENCHMARKS.md), compare against a same-day run of the build you are
  testing, and say what you did not test.
- Open test requests and the hardware that is wanted are listed in [docs/TEST_REQUESTS.md](docs/TEST_REQUESTS.md).
