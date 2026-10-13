"""neo/tests/run_all.py - 并行跑 Neo 的测试模块，报每个模块的用时。

为什么并行：19 个模块串行跑，模块用时相加约 117 秒（实测 2026-10-13，Windows 11 /
Python 3.13，`serve/server.py --engine mock`）。串行跑的是"一个进程排队"，
而模块之间互不相干：各自开自己的端口、各自的临时目录。并行跑之后墙上时间约等于
最慢的那个 worker（test_supervisor），不是它们的和。

用法：
    python -m neo.tests.run_all                      # 全部 19 个模块
    python -m neo.tests.run_all --quick              # 不起真进程的 16 个（开发时跑这个）
    python -m neo.tests.run_all --jobs 2             # 限并发（机器忙的时候用）
    python -m neo.tests.run_all -m neo.tests.test_console   # 只跑点名的模块
每个模块是一个 `python -m unittest <module>` 子进程：模块内共享的服务（见各模块的
setUpClass）留在一个进程里，模块之间才并行。失败的模块会打出它输出的末尾几十行。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from neo import state  # noqa: E402
from neo.deploy import wide_stdout  # noqa: E402

#: 全量：部署前跑这个（AGENTS.md 的"full pass"）
FULL_MODULES = [
    "neo.tests.test_supervisor",
    "neo.tests.test_engines",
    "neo.tests.test_upstream_api",
    "neo.tests.test_console",
    "neo.tests.test_logging",
    "neo.tests.test_ui",
    "neo.tests.test_netprobe",
    "neo.tests.test_tokens",
    "neo.tests.test_connectors",
    "neo.tests.test_models",
    "neo.tests.test_wizard",
    "neo.tests.test_connect_api",
    "neo.tests.test_connect_cli",
    "neo.tests.test_manager_cli",
    "neo.tests.test_start",
    "neo.tests.test_model_settings",
    "neo.tests.test_paths",
    "neo.tests.test_engine_setup",
    "neo.tests.test_deploy",
]
#: 快跑：这 16 个模块不起真进程（不起 serve/server.py、不起监督器），开发时用它。
#: 起真进程的是 test_supervisor / test_console / test_connect_api / test_model_settings
#: （test_model_settings 起的是控制台服务，不起引擎）。
QUICK_MODULES = [m for m in FULL_MODULES
                 if m not in ("neo.tests.test_supervisor", "neo.tests.test_console",
                              "neo.tests.test_connect_api")]
_RAN = re.compile(r"Ran (\d+) tests?")


def run_module(target: str, timeout_s: float) -> dict:
    """跑一个并行单元（模块或模块.类），返回 {target, rc, secs, tests_n, out, timeout}。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    t0 = time.perf_counter()
    try:
        proc = subprocess.run([state.python_executable(REPO), "-m", "unittest", "-v", target],
                              cwd=str(REPO), env=env, capture_output=True, timeout=timeout_s)
        out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
        res = {"target": target, "rc": proc.returncode, "secs": time.perf_counter() - t0,
               "tests": _RAN.search(out), "out": out, "timeout": False}
    except subprocess.TimeoutExpired as exc:
        out = ((exc.stdout or b"") + (exc.stderr or b"")).decode("utf-8", "replace")
        res = {"target": target, "rc": 124, "secs": time.perf_counter() - t0,
               "tests": None, "out": out, "timeout": True}
    res["tests_n"] = int(res["tests"].group(1)) if res["tests"] else 0
    return res


#: 真联网的并行单元：test_engine_setup 的克隆路径要从 GitHub 拉 pin 住的那个 tag。
#: 本机实测 2026-10-13：同一个单元 6.8 秒与 145.3 秒都出现过（同一台机器、同一天），
#: 全量套件的墙上时间就跟着网络走。--skip-network 把它摘出去，其余 94 个单元不碰网络
#: （netprobe 的探测是注入的假 connect，models/wizard 用的是 FAKE_SETUP）。
NETWORK_UNITS = {"neo.tests.test_engine_setup.TestRunAndPage"}


def module_classes(module: str) -> list[str]:
    """这个模块里的测试类，形如 ["neo.tests.test_supervisor.TestCrashRestart", ...]。

    并行按类切：`python -m unittest neo.tests.test_supervisor.TestCrashRestart` 是 unittest
    认的写法。test_supervisor 里最慢的类（TestCrashRestart：杀掉服务、看自动重启、退避、
    熔断）比它同模块里其他类慢一个量级，整模块并行时它就是墙上时间；按类切之后
    它和其他类同时跑。类内的 setUpClass 共享服务不受影响（一个类还是一个进程）。
    """
    import unittest

    suite = unittest.defaultTestLoader.loadTestsFromName(module)

    cls: list[str] = []
    stack = [suite]
    while stack:
        node = stack.pop()
        for child in node:
            if isinstance(child, unittest.TestSuite):
                stack.append(child)
            else:                   # 一个 TestCase 实例：取它的类，不切字符串
                kls = child.__class__
                target = f"{kls.__module__}.{kls.__name__}"
                if target not in cls:
                    cls.append(target)
    return cls


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="并行跑 Neo 的测试模块")
    ap.add_argument("--quick", action="store_true",
                    help="只跑不起真进程的 16 个模块（开发时用）")
    ap.add_argument("--jobs", type=int, default=0,
                    help="并发数（默认 min(4, CPU 核数)；1 就是串行）")
    ap.add_argument("-m", "--module", action="append", default=[],
                    help="只跑点名的模块（可重复）")
    ap.add_argument("--timeout", type=float, default=900.0, help="单模块超时秒数")
    ap.add_argument("--no-split", action="store_true",
                    help="按模块切（默认按测试类切，见 module_classes）")
    ap.add_argument("--retries", type=int, default=1,
                    help="失败的单元单独重跑几次（默认 1 次：并发下的端口/负载抖动要和真失败分开）")
    ap.add_argument("--skip-network", action="store_true",
                    help="跳过真联网的单元（见 NETWORK_UNITS）：慢链路或离线时用")
    ns = ap.parse_args(argv)
    # 控制台（cp936）装不下解码出来的中文与替换符，先把它换成 UTF-8（同 neo/deploy.py）
    wide_stdout()

    modules = ns.module or (QUICK_MODULES if ns.quick else FULL_MODULES)
    jobs = ns.jobs or min(4, os.cpu_count() or 1)
    for m in modules:
        if m not in FULL_MODULES:
            print(f"警告：{m} 不在已知模块清单里，照跑")
    targets = modules
    if not ns.no_split:
        targets = []
        for m in modules:
            try:
                targets.extend(module_classes(m))
            except Exception as exc:            # 导入失败：整模块跑，让它的失败出现在结果里
                print(f"警告：{m} 枚举测试类失败（{exc}），按整模块跑")
                targets.append(m)
    if ns.skip_network:
        def is_net(t: str) -> bool:
            return any(t == n or t.startswith(n + ".") or n.startswith(t + ".")
                       for n in NETWORK_UNITS)
        keep = [t for t in targets if not is_net(t)]
        print(f"--skip-network：跳过 {len(targets) - len(keep)} 个联网单元"
              f"（按模块切时会整模块跳过）")
        targets = keep

    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        results = list(pool.map(lambda m: run_module(m, ns.timeout), targets))
    total = time.perf_counter() - t0

    print(f"\n{'单元':<46}{'用例':>6}{'秒':>9}  结果")
    for r in sorted(results, key=lambda x: -x["secs"]):
        name = r["target"].replace("neo.tests.", "")
        flag = "OK" if r["rc"] == 0 else ("TIMEOUT" if r["timeout"] else "FAIL")
        print(f"{name:<46}{r['tests_n']:>6}{r['secs']:>9.1f}  {flag}")
    failed = [r for r in results if r["rc"] != 0]
    flakes: list[str] = []
    if failed and ns.retries > 0:
        # 并发跑时，机器上正跑着真推理服务（Defender 扫描新进程、端口释放慢），
        # 偶发一两个单元超时/端口抖动。失败的单元单独重跑一次：单独跑通过 = 抖动，
        # 单独跑还失败 = 真失败（保留第一次的输出，那才是现场）。
        print(f"\n{len(failed)} 个单元失败，单独重跑一次（{ns.retries} 轮）")
        for r in list(failed):
            for _ in range(ns.retries):
                time.sleep(0.5)
                again = run_module(r["target"], ns.timeout)
                if again["rc"] == 0:
                    flakes.append(r["target"])
                    results[results.index(r)] = again
                    break
        failed = [r for r in results if r["rc"] != 0]
    for r in failed:
        print(f"\n===== {r['target']} 的输出末尾 =====")
        print("\n".join(r["out"].splitlines()[-40:]))
    tests = sum(r["tests_n"] for r in results)
    print(f"\n{len(modules)} 个模块 / {len(targets)} 个并行单元、{tests} 个用例、"
          f"{jobs} 个并发，墙上 {total:.1f} 秒（串行相加约 {sum(r['secs'] for r in results):.1f} 秒）")
    if flakes:
        print("抖动（并发下失败、单独重跑通过）：" + ", ".join(flakes))
    if failed:
        print("失败：" + ", ".join(r["target"] for r in failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
