"""neo/engines/ - 引擎适配层：每种"能被 Neo 监督的服务"提供一个适配器。

M1 只有 strata；mock 给测试用；llama.cpp 是 M4。
"""
from __future__ import annotations

from .base import EngineAdapter, LaunchPlan
from .strata import StrataEngine

__all__ = ["EngineAdapter", "LaunchPlan", "StrataEngine", "make_adapter"]


def make_adapter(name: str, root, cfg: dict | None = None) -> EngineAdapter:
    """按名字造适配器。名字不认识时回 Strata（默认引擎）。"""
    name = (name or "strata").strip().lower()
    if name == "mock":
        from .mock import MockEngine
        return MockEngine(root, cfg)
    return StrataEngine(root, cfg)
