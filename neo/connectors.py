"""neo/connectors.py - 一键接入四个编码 agent：Claude Code / Codex CLI / OpenCode / OpenClaw。

形状照参考实现（Unsloth Studio 的 unsloth_cli/commands/start.py，本机装的那份，
6990 行）里被验证过的部分，落到 Neo 的"只用标准库、不改上游、写用户文件先备份"
的规矩上：

    Claude Code   ANTHROPIC_BASE_URL=<base>（不带 /v1）+ ANTHROPIC_AUTH_TOKEN
                  + ANTHROPIC_MODEL，11 个云端开关变量要清空，
                  --settings '{"env":…,"availableModels":[模型 id]}'
    Codex CLI     ~/.codex/config.toml 里加 [model_providers.neo]
                  （base_url=<base>/v1、env_key=NEO_API_KEY、wire_api="responses"、
                   requires_openai_auth=false、stream_idle_timeout_ms=1200000），
                  顶部两行 model_provider / oss_provider 指到 neo
    OpenCode      ~/.config/opencode/opencode.json 里加 provider.neo
                  （npm=@ai-sdk/openai-compatible、options.baseURL=<base>/v1、
                   apiKey），model = "neo/<模型 id>"
    OpenClaw      ~/.openclaw/openclaw.json 里加 models.providers.neo
                  （api="openai-completions"），agents.defaults.model.primary
                  = "neo/<模型 id>"，memory.search 关掉（不写就会偷偷打 OpenAI）

三条硬规矩（参考实现里也是这么写的）：

1. 合并，不覆盖。JSON 文件解析不了就不动它，只回一句警告；TOML 按 "(?m)^(?=\\[)"
   切成块，只删自己写过的那一块，其余原样留着。
2. 写之前先备份成 <名字>.neo-bak。
3. 只撤销自己写过的：再跑一次是把同一块换成新的，不是越堆越多。

模型 id 用 /v1/models 报的那个（serve/server.py 的 svc.model，就是配置里的
model_name），上下文从 strata-*.json 的 --max-context 拿（neo/engines/strata.py）。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import tokens

#: 四个 agent 的名字（CLI 与页面都用这套）
AGENTS = ("claude", "codex", "opencode", "openclaw")
TITLES = {"claude": "Claude Code", "codex": "Codex CLI", "opencode": "OpenCode",
          "openclaw": "OpenClaw"}

PROVIDER_ID = "neo"                             # agent 配置里我们那一块的名字
CODEX_PROFILE = "neo"
CODEX_ENV_KEY = "NEO_API_KEY"
OPENCODE_PROVIDER = "neo"
OPENCODE_OUTPUT_TOKEN_MAX_ENV = "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"
#: Codex 对"这条 SSE 多久没字节就算断"的判定。默认 300000（5 分钟）是按整段安静期算的，
#: 而 llama-server 在算 prompt 期间一个字节都不发。参考实现把这条改成 1200000，
#: 理由写在它 start.py:58 的注释里（2 核机器 16.1 tok/s、首轮 ~7300 token ≈ 460 秒安静）。
#: Neo 沿用同一个值，理由一样。
CODEX_STREAM_IDLE_TIMEOUT_MS = 1_200_000


@dataclass
class Model:
    """要接入的那个模型：id 是 /v1/models 报的 id，context 是它的上下文窗口。"""
    id: str
    context: int = 0

    @classmethod
    def from_config(cls, cfg: dict) -> "Model":
        return cls(id=str(cfg.get("name") or cfg.get("id") or ""),
                   context=int(cfg.get("context") or 0))


# ---------------- 用户目录（认这几个环境变量，测试就靠它们指到临时目录） ----------------
def _env_map(env=None) -> dict:
    return dict(os.environ) if env is None else dict(env)


def claude_home(env=None) -> Path:
    env = _env_map(env)
    raw = env.get("CLAUDE_CONFIG_DIR") or ""
    return Path(raw) if raw.strip() else Path.home() / ".claude"


def codex_home(env=None) -> Path:
    env = _env_map(env)
    raw = env.get("CODEX_HOME") or ""
    return Path(raw) if raw.strip() else Path.home() / ".codex"


def opencode_config_dir(env=None) -> Path:
    env = _env_map(env)
    raw = env.get("XDG_CONFIG_HOME") or ""
    base = Path(raw) if raw.strip() else Path.home() / ".config"
    return base / "opencode"


def openclaw_home(env=None) -> Path:
    env = _env_map(env)
    raw = env.get("OPENCLAW_HOME") or ""
    return Path(raw) if raw.strip() else Path.home() / ".openclaw"


def config_path(agent: str, env=None) -> Path:
    """这个 agent 要写的那个配置文件在哪。"""
    if agent == "claude":
        return claude_home(env) / "settings.json"
    if agent == "codex":
        return codex_home(env) / "config.toml"
    if agent == "opencode":
        return opencode_config_dir(env) / "opencode.json"
    if agent == "openclaw":
        return openclaw_home(env) / "openclaw.json"
    raise ValueError(f"不认识的 agent：{agent}")


def extra_paths(agent: str, env=None) -> list[Path]:
    """除主配置外还要写的文件（Codex 的 profile 层）。"""
    if agent == "codex":
        return [codex_home(env) / f"{CODEX_PROFILE}.config.toml"]
    return []


# ---------------- 合并用的小工具 ----------------
def read_json_object(text: str):
    """解析成字典；解析不了回 None（调用方就不动这个文件，只警告）。"""
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def json_text(config: dict) -> str:
    return json.dumps(config, indent=2, ensure_ascii=False) + "\n"


def subdict(parent: dict, key: str) -> dict:
    child = parent.get(key)
    if not isinstance(child, dict):
        child = parent[key] = {}
    return child


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".neo-bak")


def write_with_backup(path: Path, text: str) -> Path:
    """写文件，旧内容留一份 .neo-bak（只留最近一份，不堆）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            backup_path(path).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass
    tmp = path.with_name(path.name + ".neo-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError:
        return ""


# ---------------- Claude Code ----------------
def claude_env(base: str, key: str, model: Model) -> dict:
    """Claude Code 的环境变量。base 不带 /v1（Anthropic 端点自己拼路径）。

    CLAUDE_CODE_MAX_CONTEXT_TOKENS / AUTO_COMPACT_WINDOW：Claude 认不出这个模型 id
    就按 200k 算，并把 AUTO_COMPACT_WINDOW 夹在 [100k, 200k] 里 —— 本地 32k 窗口
    会被它当成 200k 一路顶到压缩。参考实现也是这么钉的（start.py:3055 的注释）。
    """
    env = {
        "ANTHROPIC_BASE_URL": base,
        "ANTHROPIC_AUTH_TOKEN": key,
        "ANTHROPIC_MODEL": model.id,
        "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
        # 逐轮剩余 token 的提醒会改系统前缀，本地模型的 KV 前缀就白缓存了
        "CLAUDE_CODE_TOTAL_TOKENS_REMINDER": "off",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    }
    if model.context:
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(int(model.context))
        env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(int(model.context))
        env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = "90"
    return env


def claude_settings_overlay(model: Model, env: dict) -> str:
    """--settings 后面那段 JSON。命令层的 env 比用户自己的 settings 优先级高。"""
    settings_env = {name: "" for name in tokens.UNSET_ENV["claude"]}
    settings_env.update(env)
    return json.dumps({"env": settings_env, "availableModels": [model.id]},
                      ensure_ascii=False)


def merge_claude_settings(existing: str, base: str, key: str, model: Model):
    """把我们的 env 与 availableModels 并进 ~/.claude/settings.json。

    回 (新文本, 说明)。解析不了回 (None, 警告) —— 不动用户的文件。
    """
    config = read_json_object(existing) if existing.strip() else {}
    if config is None:
        return None, ["settings.json 不是能解析的 JSON：Neo 不动它，"
                      "自己加 provider 或把它挪走再跑一次"]
    env = subdict(config, "env")
    our = claude_env(base, key, model)
    changed = []
    for name, value in our.items():
        if env.get(name) != value:
            env[name] = value
            changed.append(name)
    for name in tokens.UNSET_ENV["claude"]:
        env.setdefault(name, "")
    models = config.get("availableModels")
    models = models if isinstance(models, list) else []
    if model.id not in models:
        models = [*models, model.id]
        config["availableModels"] = models
    return json_text(config), [f"settings.json 写入 {len(changed)} 个变量，"
                               f"availableModels 里有 {len(models)} 个"]


# ---------------- Codex CLI ----------------
def codex_provider_table(base: str, key: str = "") -> str:
    """[model_providers.neo] 那一块。

    key 给的就是 experimental_bearer_token（桌面壳起的进程没有 shell 环境，
    只能把 key 写进文件）；不给 key 就写 env_key，从环境变量读。
    """
    auth = (f"experimental_bearer_token = {json.dumps(key)}" if key
            else f'env_key = "{CODEX_ENV_KEY}"')
    return (f"[model_providers.{CODEX_PROFILE}]\n"
            'name = "Neo Strata"\n'
            f"base_url = {json.dumps(base + '/v1')}\n"
            f"{auth}\n"
            'wire_api = "responses"\n'
            "requires_openai_auth = false\n"
            f"stream_idle_timeout_ms = {CODEX_STREAM_IDLE_TIMEOUT_MS}\n")


_PROVIDER_HEADER = f"[model_providers.{CODEX_PROFILE}]"
_PROVIDER_HEADERS = (_PROVIDER_HEADER, _PROVIDER_HEADER[:-1] + ".")


def _set_top_line(preamble: str, key: str, value: str) -> str:
    """顶部（第一个 [ 之前）那一行：有就换值，没有就加一行。"""
    pattern = re.compile(r"(?m)^\s*" + re.escape(key) + r"\s*=.*$", re.M)
    line = f'{key} = "{value}"'
    if pattern.search(preamble):
        return pattern.sub(line, preamble, count=1)
    if preamble and not preamble.endswith("\n"):
        preamble += "\n"
    return preamble + line + "\n"


def merge_codex_config(existing: str, base: str, key: str = "") -> str:
    """把我们的 provider 块并进 ~/.codex/config.toml，其余块原样留着。

    切法与参考实现一样：re.split(r"(?m)^(?=\\[)") —— 第一块是顶部（第一个 [ 之前），
    后面每块一个表。删掉以我们的表头开头的那些块，再追加一份新的：
    跑第二次是换掉，不是堆第二份。顶部两行 model_provider / oss_provider 指到我们，
    这两行是换值（用户原来写的值在 .neo-bak 里）。
    """
    chunks = re.split(r"(?m)^(?=\[)", existing or "")
    preamble = _set_top_line(chunks[0], "model_provider", CODEX_PROFILE)
    preamble = _set_top_line(preamble, "oss_provider", CODEX_PROFILE)
    rest = "".join(c for c in chunks[1:] if not c.startswith(_PROVIDER_HEADERS))
    text = preamble + rest
    if text and not text.endswith("\n"):
        text += "\n"
    if text and not text.endswith("\n\n"):
        text += "\n"
    return text + codex_provider_table(base, key)


def codex_profile_text(model: Model) -> str:
    """~/.codex/neo.config.toml：codex --profile neo 用的那一层。"""
    text = (f'oss_provider = "{CODEX_PROFILE}"\n'
            f'model_provider = "{CODEX_PROFILE}"\n'
            f"model = {json.dumps(model.id)}\n")
    if model.context:
        text += f"model_context_window = {int(model.context)}\n"
    return text


# ---------------- OpenCode ----------------
OPENCODE_OUTPUT_TOKEN_MAX = 32_000


def opencode_output_limit(window: int) -> int:
    """一次回答最多多少 token：窗口的 1/4，封顶 32000（参考实现 start.py:4843 同式）。"""
    return max(1, min(int(window) // 4, OPENCODE_OUTPUT_TOKEN_MAX)) if window else 0


def opencode_compaction_reserved(window: int, output: int) -> int:
    """压缩要预留多少：max(1, min(output, max(window//10, 8192)))（同参考实现 4858）。"""
    return max(1, min(output, max(int(window) // 10, 8192)))


def opencode_provider(base: str, key: str, model: Model) -> dict:
    entry = {"name": model.id}
    if model.context:
        # 没有 limit 时 OpenCode 当上下文是 0，永远不压缩；没有 input 时它按
        # context - output 压缩，不理 compaction.reserved。两个都要给。
        entry["limit"] = {"context": int(model.context), "input": int(model.context),
                          "output": opencode_output_limit(model.context)}
    return {"npm": "@ai-sdk/openai-compatible", "name": "Neo Strata",
            "options": {"baseURL": f"{base}/v1", "apiKey": key},
            "models": {model.id: entry}}


def merge_opencode_config(existing: str, base: str, key: str, model: Model):
    config = read_json_object(existing) if existing.strip() else {}
    if config is None:
        return None, [f"opencode.json 不是能解析的 JSON：Neo 不动它，"
                      f"自己在 provider 里加一个 '{OPENCODE_PROVIDER}'，或把它挪走再跑"]
    config.setdefault("$schema", "https://opencode.ai/config.json")
    subdict(config, "provider")[OPENCODE_PROVIDER] = opencode_provider(base, key, model)
    config["model"] = f"{OPENCODE_PROVIDER}/{model.id}"
    enabled = config.get("enabled_providers")
    if isinstance(enabled, list) and OPENCODE_PROVIDER not in enabled:
        config["enabled_providers"] = [*enabled, OPENCODE_PROVIDER]
    disabled = config.get("disabled_providers")
    if isinstance(disabled, list) and OPENCODE_PROVIDER in disabled:
        # disabled_providers 压过 enabled_providers，必须摘掉
        config["disabled_providers"] = [p for p in disabled if p != OPENCODE_PROVIDER]
    if model.context:
        compaction = subdict(config, "compaction")
        compaction["auto"] = True
        compaction["reserved"] = opencode_compaction_reserved(
            model.context, opencode_output_limit(model.context))
    return json_text(config), [f"opencode.json 里 provider.{OPENCODE_PROVIDER} 已写好，"
                               f"model = {OPENCODE_PROVIDER}/{model.id}"]


# ---------------- OpenClaw ----------------
def openclaw_provider(base: str, key: str, model: Model) -> dict:
    entry = {"id": model.id, "name": model.id}
    if model.context:
        entry["contextWindow"] = int(model.context)
        # 不写 maxTokens 时 OpenClaw 把每条回答夹在 8192，不管窗口多大
        entry["maxTokens"] = opencode_output_limit(model.context)
    return {"baseUrl": f"{base}/v1", "apiKey": key, "api": "openai-completions",
            "models": [entry]}


def merge_openclaw_config(existing: str, base: str, key: str, model: Model):
    config = read_json_object(existing) if existing.strip() else {}
    if config is None:
        return None, ["openclaw.json 不是能解析的 JSON：Neo 不动它，"
                      "自己在 models.providers 里加一个 'neo'，或把它挪走再跑"]
    models = subdict(config, "models")
    models.setdefault("mode", "merge")          # merge = 加我们这一家，不清掉别人
    subdict(models, "providers")[PROVIDER_ID] = openclaw_provider(base, key, model)
    search = subdict(subdict(config, "memory"), "search")
    if "provider" not in search:
        # memory search 默认是开着的、默认走 openai：不写这一段，本地会话会打 OpenAI
        search.update({"provider": "none", "fallback": "none"})
    subdict(subdict(config, "env"), "shellEnv")["enabled"] = False
    primary = f"{PROVIDER_ID}/{model.id}"
    subdict(subdict(config, "agents"), "defaults")["model"] = {"primary": primary}
    return json_text(config), [f"openclaw.json 里 models.providers.{PROVIDER_ID} 已写好，"
                               f"agents.defaults.model.primary = {primary}"]


# ---------------- 每个 agent 的环境变量与启动命令 ----------------
def unset_list(agent: str) -> tuple:
    """这个 agent 启动前要先清空的变量（云端账号的开关，不清会绕过本地服务）。"""
    return tokens.UNSET_ENV[agent]


def env_for(agent: str, base: str, key: str, model: Model) -> dict:
    if agent == "claude":
        return claude_env(base, key, model)
    if agent == "codex":
        return {CODEX_ENV_KEY: key}
    if agent == "opencode":
        # OpenCode 认这段内联配置（优先级比项目里的 opencode.json 高）
        inline = {"provider": {OPENCODE_PROVIDER: opencode_provider(base, key, model)},
                  "model": f"{OPENCODE_PROVIDER}/{model.id}"}
        return {"OPENCODE_CONFIG_CONTENT": json.dumps(inline, ensure_ascii=False)}
    if agent == "openclaw":
        return {"OPENAI_API_KEY": key}
    raise ValueError(f"不认识的 agent：{agent}")


def command_for(agent: str, base: str, key: str, model: Model) -> list[str]:
    if agent == "claude":
        overlay = claude_settings_overlay(model, claude_env(base, key, model))
        return ["claude", "--model", model.id, "--settings", overlay]
    if agent == "codex":
        return ["codex", "--profile", CODEX_PROFILE]
    if agent == "opencode":
        return ["opencode"]
    if agent == "openclaw":
        return ["openclaw"]
    raise ValueError(f"不认识的 agent：{agent}")


def merge_config(agent: str, existing: str, base: str, key: str, model: Model):
    """(新文本, 说明列表)。新文本是 None 表示这个文件不能动。"""
    if agent == "claude":
        return merge_claude_settings(existing, base, key, model)
    if agent == "codex":
        return merge_codex_config(existing, base, key), [
            f"config.toml 顶部 model_provider = {CODEX_PROFILE}，"
            f"[model_providers.{CODEX_PROFILE}] 已写好"]
    if agent == "opencode":
        return merge_opencode_config(existing, base, key, model)
    if agent == "openclaw":
        return merge_openclaw_config(existing, base, key, model)
    raise ValueError(f"不认识的 agent：{agent}")


def write_config(agent: str, base: str, key: str, model: Model, env=None) -> dict:
    """把 provider 写进用户的配置文件（先备份成 .neo-bak）。"""
    path = config_path(agent, env)
    text, notes = merge_config(agent, read_text(path), base, key, model)
    if text is None:
        return {"path": str(path), "written": False, "notes": notes}
    write_with_backup(path, text)
    written = [str(path)]
    if agent == "codex":
        profile = extra_paths(agent, env)[0]
        write_with_backup(profile, codex_profile_text(model))
        written.append(str(profile))
    return {"path": str(path), "written": True, "notes": notes, "files": written}


# ---------------- 打印成能粘进终端的块（照参考实现的 _print_env） ----------------
def _powershell_quote(value: str) -> str:
    # PowerShell 里 ` 是转义符，" 与 $ 在双引号串里会被解释
    escaped = str(value).replace("`", "``").replace('"', '`"').replace("$", "`$")
    return f'"{escaped}"'


def _ps_quote_arg(arg: str) -> str:
    if not re.search(r'[\s"`$\'#]', arg):
        return arg
    return _powershell_quote(arg)


def powershell_block(agent: str, base: str, key: str, model: Model) -> list[str]:
    lines = [f"Remove-Item Env:{name} -ErrorAction SilentlyContinue"
             for name in tokens.UNSET_ENV[agent]]
    for name, value in env_for(agent, base, key, model).items():
        lines.append(f"$env:{name} = {_powershell_quote(value)}")
    lines.append(" ".join(_ps_quote_arg(a) for a in command_for(agent, base, key, model)))
    return lines


def posix_block(agent: str, base: str, key: str, model: Model) -> list[str]:
    lines = [f"unset {name}" for name in tokens.UNSET_ENV[agent]]
    env = env_for(agent, base, key, model)
    lines += [f"export {name}={shlex.quote(str(value))}" for name, value in env.items()]
    # 最后一行是自包含的一行：只粘这一行也不会拿到大盘账号去跑（参考实现同理由）
    inline = [f"{name}=" for name in tokens.UNSET_ENV[agent]]
    inline += [f"{name}={shlex.quote(str(value))}" for name, value in env.items()]
    lines.append(" ".join((*inline, shlex.join(command_for(agent, base, key, model)))))
    return lines


def recipe(agent: str, base: str, key: str, model: Model) -> dict:
    """一个 agent 的完整接入配方（页面与 CLI 共用这一份）。"""
    needs_config = agent in ("codex", "opencode", "openclaw")
    return {
        "agent": agent,
        "title": TITLES[agent],
        "base": base,
        "model": model.id,
        "context": model.context,
        "env": env_for(agent, base, key, model),
        "unset": list(tokens.UNSET_ENV[agent]),
        "command": command_for(agent, base, key, model),
        "config_path": str(config_path(agent)),
        "config_only": needs_config,
        "notes": (["Codex/OpenCode/OpenClaw 认配置文件：先点\"写进配置\"（--persist），"
                   "再在终端里跑那条命令"] if needs_config
                  else ["Claude Code 只靠环境变量与 --settings，不用动 ~/.claude"]),
        "powershell": powershell_block(agent, base, key, model),
        "posix": posix_block(agent, base, key, model),
    }


if __name__ == "__main__":                      # python -m neo.connectors <agent>
    import sys

    agent = sys.argv[1] if len(sys.argv) > 1 else "claude"
    item = recipe(agent, "http://127.0.0.1:8080", tokens.new_key(),
                  Model("qwen3.8-flash-next", 202072))
    for line in item["powershell"]:
        print(line)




