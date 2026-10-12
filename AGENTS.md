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
Strata root directory and uses that directory as its root. Its tests need no GPU: `python -m unittest
neo.tests.test_supervisor neo.tests.test_engines neo.tests.test_upstream_api neo.tests.test_console
neo.tests.test_logging neo.tests.test_ui` (118 tests, 150.1 s on Windows 11 / Python 3.13, using
`serve/server.py --engine mock`). Neo's own packages live in `neo/requirements-neo.txt`, not in `requirements.txt`.

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
