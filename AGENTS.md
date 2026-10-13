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

`neo/` is an additive desktop layer (window shell, process supervisor, crash auto-restart, status/logs/chat/monitor
pages). It talks to Strata only through existing CLI flags, env vars and HTTP endpoints, and keeps all its own state
under `neo/state/` - it changes no upstream file. Details, the restart/backoff rules, the single-instance rules and
its tests: **[docs/NEO_DESKTOP.md](docs/NEO_DESKTOP.md)**. Package it as one windowed `NeoStrata.exe` with
`python -m neo.build_exe` (spec `neo/NeoStrata.spec`), with pywebview installed in the build env - the build refuses
without it, because the exe would silently lose its window and open only a browser page; the exe is meant to sit in a
Strata root directory and uses that directory as its root. Its tests need no GPU, no network and never touch a real
user's agent configs: `python -m unittest
neo.tests.test_supervisor neo.tests.test_engines neo.tests.test_upstream_api neo.tests.test_console
neo.tests.test_logging neo.tests.test_ui neo.tests.test_netprobe neo.tests.test_tokens
neo.tests.test_connectors neo.tests.test_models neo.tests.test_wizard neo.tests.test_connect_api
neo.tests.test_connect_cli neo.tests.test_manager_cli neo.tests.test_start` (329 tests, 243.4 s on Windows 11 /
Python 3.13, using `serve/server.py --engine mock`; 288.6 s on the earlier 312-test run). The three modules that
start real processes are the whole cost - measured on their own: `test_supervisor` 56 s, `test_console` 9.1 s,
`test_connect_api` 6.7 s; the other twelve together are 14 s. For a quick pass while developing, run those twelve
and leave the three out:
`python -m unittest neo.tests.test_start neo.tests.test_models neo.tests.test_ui neo.tests.test_tokens
neo.tests.test_netprobe neo.tests.test_wizard neo.tests.test_connectors neo.tests.test_engines
neo.tests.test_manager_cli neo.tests.test_upstream_api neo.tests.test_connect_cli
neo.tests.test_logging` (13.6 s). Neo's own
packages live in `neo/requirements-neo.txt`, not in `requirements.txt`.

The 接入 page (`neo/web/`, tab "接入") installs models and wires coding agents: `neo/netprobe.py` probes
`huggingface.co` / `hf-mirror.com` / `www.modelscope.cn` over TCP 443 and only suggests a source,
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
