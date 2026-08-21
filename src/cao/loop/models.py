"""Data model for the Worker -> Reviewer iteration loop."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

from ..models import AgentSpec

BACKENDS = ("claude_code", "codex", "grok")
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")  # ultra: Codex-only; other backends clamp to max


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"  # reviewer score >= pass_score
    EXHAUSTED = "exhausted"  # max iterations reached without passing
    STOPPED = "stopped"  # early-stop rule (no progress / budget) fired
    FAILED = "failed"  # infrastructure or agent failure
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self not in (TaskStatus.PENDING, TaskStatus.RUNNING)


class Decision(str, Enum):
    PASS = "pass"
    ITERATE = "iterate"
    STOP = "stop"


@dataclass
class RoleConfig:
    """Which agent plays a role (worker or reviewer) and how it is tuned."""

    backend: str = "claude_code"  # claude_code | codex | grok
    model: Optional[str] = None  # None -> the CLI's default model
    effort: Optional[str] = None  # low | medium | high | xhigh | max
    role: str = "coder"  # preset key (see roles.py); reviewer uses "reviewer"
    instructions: str = ""  # extra user-supplied instructions appended to the role brief
    timeout: float = 1800.0
    options: dict[str, Any] = field(default_factory=dict)  # adapter-specific knobs
    env: dict[str, str] = field(default_factory=dict)

    def identity(self) -> str:
        """Backend + model; two roles with the same identity are 'the same model'."""
        from ..adapters.grok import DEFAULT_GROK_MODEL

        model = self.model or (DEFAULT_GROK_MODEL if self.backend == "grok" else "default")
        return f"{self.backend}:{model}"

    def to_agent_spec(self, name: str, *, read_only: bool) -> AgentSpec:
        return AgentSpec(
            name=name,
            type=self.backend,
            model=self.model,
            effort=self.effort,
            read_only=read_only,
            timeout=self.timeout,
            env=dict(self.env),
            options=dict(self.options),
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RoleConfig":
        d = dict(d or {})
        return cls(
            backend=str(d.get("backend", "claude_code")),
            model=(d.get("model") or None),
            effort=(d.get("effort") or None),
            role=str(d.get("role") or "coder"),
            instructions=str(d.get("instructions") or ""),
            timeout=float(d.get("timeout") or 1800),
            options=dict(d.get("options") or {}),
            env=dict(d.get("env") or {}),
        )


@dataclass
class LoopConfig:
    max_iterations: int = 5
    pass_score: float = 9.0  # >= pass_score -> done
    scoring: str = "weighted"  # weighted | llm
    weights: dict[str, float] = field(default_factory=dict)  # criterion -> weight (defaults in review.py)
    stop_if_no_progress: int = 2  # N consecutive iterations without a higher score -> stop (0 = off)
    budget_usd: Optional[float] = None  # stop when cumulative cost exceeds this (when CLIs report cost)
    min_score_delta: float = 0.0  # improvement below this counts as "no progress"
    on_success: str = "pr"  # pr | merge | none
    handshake_retries: int = 1  # re-OFFER after a NACK this many times per handoff
    require_tests: bool = False  # worker must report running tests or gets a NACK
    respect_verdict: bool = True  # reviewer verdict "request_changes" caps the score just below pass_score

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LoopConfig":
        d = dict(d or {})
        return cls(
            respect_verdict=bool(d.get("respect_verdict", True)),
            max_iterations=int(d.get("max_iterations") or 5),
            pass_score=float(d.get("pass_score") if d.get("pass_score") is not None else 9.0),
            scoring=str(d.get("scoring") or "weighted"),
            weights={str(k): float(v) for k, v in (d.get("weights") or {}).items()},
            stop_if_no_progress=int(d.get("stop_if_no_progress") if d.get("stop_if_no_progress") is not None else 2),
            budget_usd=float(d["budget_usd"]) if d.get("budget_usd") not in (None, "") else None,
            min_score_delta=float(d.get("min_score_delta") or 0.0),
            on_success=str(d.get("on_success") or "pr"),
            handshake_retries=int(d.get("handshake_retries") if d.get("handshake_retries") is not None else 1),
            require_tests=bool(d.get("require_tests", False)),
        )


def new_task_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


@dataclass
class TaskSpec:
    """User request + acceptance criteria + who works on it."""

    title: str
    request: str
    acceptance_criteria: list[str] = field(default_factory=list)
    repo_path: str = "."
    base_branch: Optional[str] = None  # None -> current HEAD branch
    worker: RoleConfig = field(default_factory=RoleConfig)
    reviewer: RoleConfig = field(default_factory=lambda: RoleConfig(backend="codex", role="reviewer"))
    loop: LoopConfig = field(default_factory=LoopConfig)
    id: str = field(default_factory=new_task_id)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskSpec":
        crit = d.get("acceptance_criteria") or []
        if isinstance(crit, str):
            crit = [c.strip(" -*\t") for c in crit.splitlines() if c.strip(" -*\t")]
        return cls(
            id=str(d.get("id") or new_task_id()),
            title=str(d.get("title") or (d.get("request") or "")[:60] or "untitled"),
            request=str(d.get("request") or ""),
            acceptance_criteria=[str(c) for c in crit],
            repo_path=str(d.get("repo_path") or "."),
            base_branch=(d.get("base_branch") or None),
            worker=RoleConfig.from_dict(d.get("worker") or {}),
            reviewer=RoleConfig.from_dict(d.get("reviewer") or {"backend": "codex", "role": "reviewer"}),
            loop=LoopConfig.from_dict(d.get("loop") or {}),
            created_at=float(d.get("created_at") or time.time()),
        )

    def validate(self) -> list[str]:
        """Return a list of problems (empty = valid)."""
        problems = []
        if not self.request.strip():
            problems.append("request must not be empty")
        for label, role in (("worker", self.worker), ("reviewer", self.reviewer)):
            if role.backend not in BACKENDS:
                problems.append(f"{label}.backend must be one of {', '.join(BACKENDS)}")
            if role.effort and role.effort not in EFFORTS:
                problems.append(f"{label}.effort must be one of {', '.join(EFFORTS)}")
        if self.worker.identity() == self.reviewer.identity():
            problems.append(
                f"reviewer must be a different model than the worker (both are {self.worker.identity()}); "
                "cross-model review is required"
            )
        if self.loop.max_iterations < 1:
            problems.append("loop.max_iterations must be >= 1")
        if not 0 <= self.loop.pass_score <= 10:
            problems.append("loop.pass_score must be within 0..10")
        if self.loop.scoring not in ("weighted", "llm"):
            problems.append("loop.scoring must be 'weighted' or 'llm'")
        if self.loop.on_success not in ("pr", "merge", "none"):
            problems.append("loop.on_success must be 'pr', 'merge' or 'none'")
        return problems


@dataclass
class HandshakeEvent:
    iteration: int
    handoff: str  # worker | reviewer | finish
    phase: str  # OFFER | ACK | NACK | COMMIT
    detail: str = ""
    at: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StageRecord:
    """One agent invocation (worker or reviewer) inside an iteration."""

    role: str  # worker | reviewer
    identity: str
    prompt: str = ""
    response: str = ""
    ok: bool = False
    error: Optional[str] = None
    duration_s: float = 0.0
    cost_usd: Optional[float] = None
    usage: dict[str, Any] = field(default_factory=dict)
    log_path: Optional[str] = None
    attempts: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IterationRecord:
    number: int
    worker: Optional[StageRecord] = None
    reviewer: Optional[StageRecord] = None
    diff: str = ""
    diffstat: str = ""
    commit: Optional[str] = None
    review: Optional[dict[str, Any]] = None  # parsed ReviewResult dict
    score: Optional[float] = None
    decision: Optional[str] = None
    events: list[HandshakeEvent] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    @property
    def cost_usd(self) -> float:
        return sum((s.cost_usd or 0.0) for s in (self.worker, self.reviewer) if s)

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "worker": self.worker.to_dict() if self.worker else None,
            "reviewer": self.reviewer.to_dict() if self.reviewer else None,
            "diff": self.diff,
            "diffstat": self.diffstat,
            "commit": self.commit,
            "review": self.review,
            "score": self.score,
            "decision": self.decision,
            "events": [e.to_dict() for e in self.events],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cost_usd": self.cost_usd,
        }


@dataclass
class TaskRun:
    """Mutable runtime state of a task (persisted by the store)."""

    spec: TaskSpec
    status: TaskStatus = TaskStatus.PENDING
    branch: Optional[str] = None
    worktree: Optional[str] = None
    base_commit: Optional[str] = None
    iterations: list[IterationRecord] = field(default_factory=list)
    final_score: Optional[float] = None
    outcome: dict[str, Any] = field(default_factory=dict)  # pr_url / merged_into / error
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None

    @property
    def total_cost_usd(self) -> float:
        return sum(i.cost_usd for i in self.iterations)

    @property
    def total_usage(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for it in self.iterations:
            for st in (it.worker, it.reviewer):
                if not st:
                    continue
                for k, v in (st.usage or {}).items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        totals[k] = totals.get(k, 0) + v
        return totals

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec.to_dict(),
            "status": self.status.value,
            "branch": self.branch,
            "worktree": self.worktree,
            "base_commit": self.base_commit,
            "iterations": [i.to_dict() for i in self.iterations],
            "final_score": self.final_score,
            "outcome": self.outcome,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "total_cost_usd": self.total_cost_usd,
            "total_usage": self.total_usage,
        }
