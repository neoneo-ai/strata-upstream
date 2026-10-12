"""neo/tokens.py - 局域网开放时要用的 token，和它记在哪。

三件事：

1. 生成 key。形状照参考实现（Unsloth Studio）的：前缀 + 十六进制随机串，
   前缀后面紧跟的 8 位就是"显示用的前缀"（它的库里 key_prefix 正是这 8 位，
   实测 C:\\Users\\tony\\.unsloth\\studio\\auth\\auth.db 里三条 key 都是这样）。
   Neo 的形状：sk-neo-<32 hex>。
2. 按服务器地址分桶记住它。也是参考实现的做法（它的 auth/agent_api_key.json：
   {"servers": {base: {"saved": [...], "minted": [...]}}}）：用户给的 key 与
   自动生成的 key 分开，只回给同一个地址 —— 别把给 8080 的 key 塞给别的机器。
3. 局域网地址。绑 0.0.0.0 之后要告诉用户"别人用哪个地址连你"。

key 存 neo/state/launcher.json（服务的 key）与 neo/state/agent_keys.json
（给 agent 的 key 缓存）。这两个文件都在 neo/.gitignore 忽略的目录里。
Windows 没有 POSIX 的 0o600；Neo 的状态目录在当前用户目录下，权限跟着用户走。
"""
from __future__ import annotations

import secrets
import socket
import time

from . import state

KEY_PREFIX = "sk-neo-"
KEY_HEX_BYTES = 16                              # 32 个十六进制字符
PREFIX_LEN = 8                                  # 显示用的前缀长度（参考实现也是 8）
#: 环境变量里放 key 的名字（参考实现用的是 UNSLOTH_STUDIO_AUTH_TOKEN /
#: UNSLOTH_API_KEY，Neo 对应 NEO_API_KEY）
ENV_KEY = "NEO_API_KEY"

#: 参考实现清的那批变量（start.py 的 _CLAUDE_ENV_UNSET / _CODEX_ENV_UNSET /
#: _OPENCLAW_ENV_UNSET）：这些变量在环境里会让 agent 走云端账号，
#: 局域网接入时必须先清掉，否则 key 给了也白给。
UNSET_ENV = {
    "claude": ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_UNIX_SOCKET",
               "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_FOUNDRY_BASE_URL",
               "ANTHROPIC_FOUNDRY_RESOURCE", "CLAUDE_CODE_USE_BEDROCK",
               "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_ANTHROPIC_AWS",
               "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD", "CLAUDE_CODE_USE_MANTLE"),
    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"),
    "opencode": ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"),
    "openclaw": ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"),
}


# ---------------- key ----------------
def new_key() -> str:
    """一把新 key：sk-neo- + 32 个十六进制字符（secrets，不是 random）。"""
    return KEY_PREFIX + secrets.token_hex(KEY_HEX_BYTES)


def prefix_of(key: str) -> str:
    """显示用的前缀：前缀标记后面紧跟的 8 位。认不出形状的回前 8 位。"""
    body = key[len(KEY_PREFIX):] if key.startswith(KEY_PREFIX) else key
    return body[:PREFIX_LEN]


def looks_like_key(value: str) -> bool:
    """形状对不对（不校验内容，服务端才算数）。"""
    body = str(value or "")
    if not body.startswith(KEY_PREFIX):
        return False
    body = body[len(KEY_PREFIX):]
    return len(body) >= PREFIX_LEN and all(c in "0123456789abcdef" for c in body)


def mask_of(key: str) -> str:
    """给页面看的掩码：只露前缀，不露全串。"""
    if not key:
        return ""
    return f"{KEY_PREFIX}{prefix_of(key)}…（{len(key)} 字符）"


# ---------------- 按服务器地址分桶的 key 缓存 ----------------
def key_cache_path():
    return state.STATE / "agent_keys.json"


def _cache_data() -> dict:
    data = state.read_json(key_cache_path(), {}) or {}
    if not isinstance(data, dict):
        return {}
    servers = data.get("servers")
    if not isinstance(servers, dict):
        data["servers"] = {}
    return data


def _bucket(servers: dict, base: str) -> dict:
    entry = servers.get(base)
    if isinstance(entry, list):                 # 老形状：一列 key 全算 minted
        return {"saved": [], "minted": [k for k in entry if isinstance(k, str)]}
    if not isinstance(entry, dict):
        return {"saved": [], "minted": []}

    def strs(name: str) -> list:
        value = entry.get(name)
        return [k for k in value if isinstance(k, str)] if isinstance(value, list) else []
    return {"saved": strs("saved"), "minted": strs("minted")}


def remember_key(base: str, key: str, source: str = "saved") -> dict:
    """记住一把 key（按 base 分桶，最多留 8 把，新的排最前）。"""
    data = _cache_data()
    servers = data["servers"]
    bucket = _bucket(servers, base)
    for name in ("saved", "minted"):
        bucket[name] = [k for k in bucket[name] if k != key]
    bucket[source if source in ("saved", "minted") else "saved"].insert(0, key)
    for name in ("saved", "minted"):
        bucket[name] = bucket[name][:8]
    servers[base] = bucket
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    state.write_json(key_cache_path(), data)
    return bucket


def cached_keys(base: str, source: str = "saved") -> list:
    """这个 base 上记过的 key（用户给的与自动生成的分开）。"""
    return _bucket(_cache_data().get("servers", {}), base)[
        source if source in ("saved", "minted") else "saved"]


def forget_key(base: str = "", key: str = "") -> int:
    """忘掉一把 key（回删掉几把）。base 空 = 所有服务器上这把都删。"""
    data = _cache_data()
    removed = 0
    for server, entry in list(data.get("servers", {}).items()):
        if base and server != base:
            continue
        bucket = _bucket(data["servers"], server)
        for name in ("saved", "minted"):
            kept = [k for k in bucket[name] if k != key]
            removed += len(bucket[name]) - len(kept)
            bucket[name] = kept
        data["servers"][server] = bucket
    state.write_json(key_cache_path(), data)
    return removed


# ---------------- 局域网地址 ----------------
def is_lan_ip(ip: str) -> bool:
    """这个地址适合告诉别人"用我连"吗：不是回环、不是 APIPA（169.254.*）、不是组播。"""
    text = str(ip or "").strip()
    if not text or text.startswith("127.") or text.startswith("169.254."):
        return False
    if text in ("0.0.0.0", "::", "[::]"):
        return False
    parts = text.split(".")
    if len(parts) == 4:                          # 组播与保留段
        try:
            first = int(parts[0])
        except ValueError:
            return False
        return first < 224
    return not text.lower().startswith("fe80:")  # 链路本地地址不给别人用


def lan_addresses() -> list[str]:
    """这台机器的局域网 IPv4。

    取法两条：一条 UDP "连"到公网地址（UDP 的 connect 只让内核选出口网卡，
    不发一个包），拿到出口地址；再把主机名解析出来的地址收进来。都不发数据。
    """
    found = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = str(sock.getsockname()[0])
        if is_lan_ip(ip):
            found.add(ip)
    except OSError:
        pass
    finally:
        sock.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = str(info[4][0])
            if is_lan_ip(ip):
                found.add(ip)
    except OSError:
        pass
    return sorted(found)


def loopback_base(port) -> str:
    return f"http://127.0.0.1:{int(port)}"


def lan_bases(port) -> list[str]:
    """局域网地址列表（绑 0.0.0.0 之后别人能用的地址）。"""
    return [f"http://{ip}:{int(port)}" for ip in lan_addresses()]


def base_for(host, port) -> str:
    """服务现在对外说的那个 base：绑 0.0.0.0 时用局域网地址，否则本机。"""
    if str(host) in ("0.0.0.0", "", "::"):
        ips = lan_addresses()
        return f"http://{ips[0]}:{int(port)}" if ips else loopback_base(port)
    return f"http://{host}:{int(port)}"


def firewall_hint(port) -> str:
    """Windows 防火墙放行这条端口的命令（只给命令，Neo 不自己执行——那要管理员）。"""
    return (f'netsh advfirewall firewall add rule name="Strata {int(port)}" '
            f'dir=in action=allow protocol=TCP localport={int(port)}')


def token_status(port, host="127.0.0.1", api_key: str = "") -> dict:
    """给设置页/接入页的一份局域网与 token 现状。"""
    lan = str(host) in ("0.0.0.0", "", "::")
    return {
        "lan_open": lan,
        "host": str(host),
        "port": int(port),
        "base": base_for(host, port),
        "loopback_base": loopback_base(port),
        "lan_bases": lan_bases(port),
        "has_api_key": bool(api_key),
        "api_key_masked": mask_of(api_key),
        "firewall_hint": firewall_hint(port) if lan else "",
        "note": ("已对局域网开放：这台机器上任何网页都能连这个端口，"
                 "所以必须设 token（设置页里生成一个）" if lan and not api_key
                 else ("已对局域网开放，且已设 token" if lan
                       else "只绑本机（127.0.0.1），局域网里的其他机器连不到")),
    }


if __name__ == "__main__":                      # python -m neo.tokens
    key = new_key()
    print("新 key:", key)
    print("前缀:", prefix_of(key))
    print("局域网地址:", lan_addresses())

