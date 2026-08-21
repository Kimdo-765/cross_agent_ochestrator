"""Adapter registry: map ``type:`` keys from cao.yaml to adapter classes."""

from __future__ import annotations

from typing import Type

from ..models import AgentSpec
from .base import AgentAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .gemini import GeminiAdapter
from .shell import ShellAdapter

REGISTRY: dict[str, Type[AgentAdapter]] = {
    cls.key: cls for cls in (ClaudeCodeAdapter, CodexAdapter, GeminiAdapter, ShellAdapter)
}


def register(cls: Type[AgentAdapter]) -> Type[AgentAdapter]:
    """Decorator to add a third-party adapter: ``@register class MyAdapter(AgentAdapter): key = 'x'``."""
    if not cls.key:
        raise ValueError("adapter classes must set a non-empty 'key'")
    REGISTRY[cls.key] = cls
    return cls


def make_adapter(spec: AgentSpec) -> AgentAdapter:
    try:
        cls = REGISTRY[spec.type]
    except KeyError:
        raise ValueError(f"agent '{spec.name}': unknown type '{spec.type}' (available: {', '.join(sorted(REGISTRY))})")
    return cls(spec)


__all__ = ["AgentAdapter", "REGISTRY", "register", "make_adapter"]
