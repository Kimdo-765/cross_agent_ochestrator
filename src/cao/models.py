"""Core data types shared across the orchestrator."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class Strategy(str, Enum):
    """How a workflow distributes work across agents."""

    PARALLEL = "parallel"  # same task -> N agents concurrently, then synthesize
    PIPELINE = "pipeline"  # sequential handoff: step i output -> step i+1 input
    PLAN = "plan"  # planner decomposes -> workers fan-out -> synthesizer merges


class Isolation(str, Enum):
    """Where an agent is allowed to make file changes."""

    SHARED = "shared"  # run directly in the project working directory
    WORKTREE = "worktree"  # one git worktree per task (safe for parallel edits)
    NONE = "none"  # run in a scratch temp dir (no project access)


@dataclass
class AgentSpec:
    """A configured agent (one entry under ``agents:`` in cao.yaml)."""

    name: str
    type: str  # adapter key: claude_code | codex | gemini | shell
    model: Optional[str] = None
    timeout: float = 1800.0  # seconds
    env: dict[str, str] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)  # adapter-specific knobs
    tags: list[str] = field(default_factory=list)  # capability hints for the planner

    def describe(self) -> str:
        bits = [self.type]
        if self.model:
            bits.append(f"model={self.model}")
        if self.tags:
            bits.append("tags=" + ",".join(self.tags))
        return " ".join(bits)


@dataclass
class Task:
    """A unit of work handed to exactly one agent."""

    prompt: str
    agent: str  # AgentSpec.name
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    title: str = ""
    isolation: Isolation = Isolation.SHARED
    context: dict[str, Any] = field(default_factory=dict)

    def short_title(self) -> str:
        return self.title or (self.prompt.strip().splitlines() or [""])[0][:60]


@dataclass
class TaskResult:
    """What came back from running a Task on an agent."""

    task: Task
    agent: str
    ok: bool
    output: str  # final assistant message / primary text output
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    duration_s: float = 0.0
    workdir: Optional[Path] = None
    branch: Optional[str] = None  # set when isolation=worktree
    usage: dict[str, Any] = field(default_factory=dict)  # tokens / cost if the CLI reports them
    error: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    def summary_line(self) -> str:
        status = "ok" if self.ok else "FAIL"
        extra = f" branch={self.branch}" if self.branch else ""
        return f"[{status}] {self.agent:<12} {self.duration_s:6.1f}s  {self.task.short_title()}{extra}"


@dataclass
class RunReport:
    """Everything produced by one ``cao run`` invocation."""

    goal: str
    workflow: str
    strategy: Strategy
    run_id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4])
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    results: list[TaskResult] = field(default_factory=list)
    plan: list[dict[str, Any]] = field(default_factory=list)
    synthesis: Optional[str] = None
    run_dir: Optional[Path] = None

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results) if self.results else False

    @property
    def duration_s(self) -> float:
        end = self.finished_at or time.time()
        return end - self.started_at
