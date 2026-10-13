"""neo/gguf_meta.py - 读 GGUF 文件头的元数据：只读头部，不加载模型，不写任何文件。

模型设置页要算"显存里大概要放多少"，KV 那一项要按层数算（见 neo/model_settings.py）：
层数在 GGUF 的元数据里。实测本机那份模型（Swift-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS
-00001-of-00002.gguf）的头部是 version 3、317 个张量、75 条元数据，架构名
qwen4exp，层数写在 qwen4exp.block_count —— 键名带架构前缀，general.block_count
这种老写法不一定有。所以这里按后缀认：任何 .block_count 都算。

GGUF 的头部是"magic + 版本 + 张量数 + 元数据条数 + 键值对"，在文件最前面，读它不用
把模型读进来 —— 一次最多读 META_MAX 字节，读完就关。分片模型（…-0001-of-0002.gguf）
的头部在第一片里，模型配置里 --native 给的正是那一片。

元数据里有几条上千项的数组（分词用的 preprocessor 表），要的键在它们后面，所以不能
"遇到大的就停"：不关心的值直接按字节跳过，关心的才解析。

读不到就回一张空表：文件不在、不是 GGUF、版本低于 2、头部比 META_MAX 还长、元数据里
没有那个键 —— 全部一样处理。设置页那一栏显示"—"，不影响启动，也不改任何文件。
"""
from __future__ import annotations

import struct
from pathlib import Path

MAGIC = b"GGUF"
META_MAX = 4 * 1024 * 1024          # 头部元数据最多读 4 MB：正常头部远小于这个数
#: 按后缀认：架构前缀（qwen4exp. / general. / llama.）都认
WANTED_SUFFIX = (".block_count", ".architecture", ".name", ".train_ctx",
                 ".context_length")
MAX_N_KV = 100_000                  # 元数据条数上限：超了就是文件坏了，不往下读
MAX_ARRAY = 4096                    # 要解析的数组长度上限（层数是标量，用不上）
MAX_SKIP = 4_000_000                # 跳过的数组元素数上限（preprocessor 表也就几万）
MAX_DEPTH = 6                       # 数组嵌套层数上限

#: GGUF_TYPE_* -> struct 格式（7 = bool，8 = 字符串，9 = 数组，单独处理）
_SCALARS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f",
            10: "<Q", 11: "<q", 12: "<d"}


class _Cut(Exception):
    """头部不够长：文件被截断，或元数据比 META_MAX 还长。"""


def _take(blob: bytes, off: int, fmt: str):
    size = struct.calcsize(fmt)
    if off + size > len(blob):
        raise _Cut()
    return struct.unpack_from(fmt, blob, off)[0], off + size


def _string(blob: bytes, off: int):
    n, off = _take(blob, off, "<Q")
    if n > len(blob) - off:
        raise _Cut()
    return blob[off:off + n].decode("utf-8", "replace"), off + n


def _value(blob: bytes, off: int, vtype: int, depth: int = 0):
    """(值, 新的偏移)。只解析要用的那些（层数、架构名都是标量/字符串）。"""
    if vtype == 7:
        raw, off = _take(blob, off, "<B")
        return bool(raw), off
    if vtype == 8:
        return _string(blob, off)
    if vtype == 9:
        if depth >= MAX_DEPTH:
            raise _Cut()
        etype, off = _take(blob, off, "<I")
        count, off = _take(blob, off, "<Q")
        if count > MAX_ARRAY:
            raise _Cut()
        out = []
        for _ in range(count):
            item, off = _value(blob, off, etype, depth + 1)
            out.append(item)
        return out, off
    fmt = _SCALARS.get(vtype)
    if fmt is None:
        raise _Cut()
    return _take(blob, off, fmt)


def _skip(blob: bytes, off: int, vtype: int, depth: int = 0) -> int:
    """不关心的值往后挪多少字节。上千项的分词表按整块跳，不一个个解析。"""
    if vtype == 7:
        return off + 1
    if vtype == 8:
        n, off = _take(blob, off, "<Q")
        if n > len(blob) - off:
            raise _Cut()
        return off + n
    if vtype == 9:
        if depth >= MAX_DEPTH:
            raise _Cut()
        etype, off = _take(blob, off, "<I")
        count, off = _take(blob, off, "<Q")
        if count > MAX_SKIP:
            raise _Cut()
        size = struct.calcsize(_SCALARS[etype]) if etype in _SCALARS else None
        if size is not None:                      # 定长元素：整块跳过去
            total = count * size
            if total > len(blob) - off:
                raise _Cut()
            return off + total
        if etype == 7:
            if count > len(blob) - off:
                raise _Cut()
            return off + count
        for _ in range(count):                     # 字符串数组 / 嵌套数组
            off = _skip(blob, off, etype, depth + 1)
        return off
    fmt = _SCALARS.get(vtype)
    if fmt is None:
        raise _Cut()
    return off + struct.calcsize(fmt)


def _wanted(key: str, keys) -> bool:
    return key in keys or key.endswith(WANTED_SUFFIX)


def read_meta(path, keys=()) -> dict:
    """GGUF 头部里的元数据。读不到回空表（version / tensors 之外的键都不保证有）。

    要的键按后缀认（.block_count / .architecture / .name / .train_ctx / .context_length），
    架构前缀无所谓；不关心的值按字节跳过，所以分词表那种上千项的数组挡不住后面的键。
    """
    try:
        with open(str(path), "rb") as f:
            blob = f.read(META_MAX)
    except OSError:
        return {}
    out: dict = {}
    if blob[:4] != MAGIC:
        return {}
    try:
        version, off = _take(blob, 4, "<I")
        out["version"] = version
        if version < 2:
            return out                      # v1 的张量数不是 64 位，不认
        n_tensors, off = _take(blob, off, "<Q")
        n_kv, off = _take(blob, off, "<Q")
        out["tensors"] = n_tensors
        if n_kv > MAX_N_KV:
            return out
        for _ in range(n_kv):
            key, off = _string(blob, off)
            vtype, off = _take(blob, off, "<I")
            if _wanted(key, keys):
                value, off = _value(blob, off, vtype)
                out[key] = value
            else:
                off = _skip(blob, off, vtype)
    except _Cut:
        out["truncated"] = True
    return out


def block_count(path) -> int:
    """这个 GGUF 的层数。架构前缀那份（qwen4exp.block_count）优先，其次 general.*。"""
    meta = read_meta(path)
    arch = str(meta.get("general.architecture") or "")
    for key in ([f"{arch}.block_count"] if arch else []) + ["general.block_count"]:
        raw = meta.get(key)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 < value <= 4096:
            return value
    for key, raw in meta.items():            # 最后兜底：任何 .block_count
        if key.endswith(".block_count"):
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if 0 < value <= 4096:
                return value
    return 0


if __name__ == "__main__":                                     # 看一眼某个 GGUF 的头部
    import sys
    for name in sys.argv[1:]:
        print(f"{name}: {read_meta(name)}")
