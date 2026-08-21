"""Load and validate ``cao.yaml``.

Schema (all keys optional unless noted)::

    defaults:
      timeout: 1800            # per-agent seconds
      isolation: worktree      # shared | worktree | none
      synthesizer: claude      # agent used to merge parallel/plan results

    agents:                    # required: at least one
      claude:
        type: claude_code      # required: claude_code | codex | gemini | shell
        model: claude-sonnet-5
        timeout: 900
        tags: [review, python]
        env: { ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY}" }   # ${VAR} is expanded
        options:               # adapter-specific
          permission_mode: acceptEdits
          max_turns: 30
      codex:
        type: codex
        options: { sandbox: workspace-write }

    workflows:
      compare:
        strategy: parallel
        agents: [claude, codex]
        prompt: "{goal}"
        synthesizer: claude
      implement-then-review:
        strategy: pipeline
        steps:
          - { agent: codex,  prompt: "Implement: {goal}" }
          - { agent: claude, prompt: "Review this work:\\n{previous}" }
      build:
        strategy: plan
        planner: claude
        workers: [claude, codex]
        synthesizer: claude
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .models import AgentSpec, Isolation, Strategy

CONFIG_FILENAMES = ("cao.yaml", "cao.yml", ".cao.yaml")

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """Raised for malformed configuration."""


def _expand_env(value: Any) -> Any:
    """Expand ``${VAR}`` / ``${VAR:-default}`` inside strings, recursively."""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


@dataclass
class WorkflowSpec:
    name: str
    strategy: Strategy
    # parallel
    agents: list[str] = field(default_factory=list)
    prompt: str = "{goal}"
    # pipeline
    steps: list[dict[str, Any]] = field(default_factory=list)
    # plan
    planner: Optional[str] = None
    workers: list[str] = field(default_factory=list)
    max_tasks: int = 6
    # shared
    synthesizer: Optional[str] = None
    isolation: Optional[Isolation] = None
    description: str = ""

    def referenced_agents(self) -> set[str]:
        names = set(self.agents) | set(self.workers)
        names |= {s.get("agent") for s in self.steps if s.get("agent")}
        for extra in (self.planner, self.synthesizer):
            if extra:
                names.add(extra)
        return names


@dataclass
class Config:
    agents: dict[str, AgentSpec]
    workflows: dict[str, WorkflowSpec]
    default_timeout: float = 1800.0
    default_isolation: Isolation = Isolation.WORKTREE
    default_synthesizer: Optional[str] = None
    source: Optional[Path] = None

    def agent(self, name: str) -> AgentSpec:
        try:
            return self.agents[name]
        except KeyError:
            raise ConfigError(f"unknown agent '{name}' (configured: {', '.join(sorted(self.agents)) or 'none'})")

    def workflow(self, name: str) -> WorkflowSpec:
        try:
            return self.workflows[name]
        except KeyError:
            raise ConfigError(
                f"unknown workflow '{name}' (configured: {', '.join(sorted(self.workflows)) or 'none'})"
            )

    def validate(self) -> None:
        if not self.agents:
            raise ConfigError("no agents configured (add at least one under 'agents:')")
        for wf in self.workflows.values():
            missing = sorted(wf.referenced_agents() - set(self.agents))
            if missing:
                raise ConfigError(f"workflow '{wf.name}' references unknown agent(s): {', '.join(missing)}")
            if wf.strategy is Strategy.PARALLEL and not wf.agents:
                raise ConfigError(f"workflow '{wf.name}': parallel strategy needs 'agents: [...]'")
            if wf.strategy is Strategy.PIPELINE and not wf.steps:
                raise ConfigError(f"workflow '{wf.name}': pipeline strategy needs 'steps: [...]'")
            if wf.strategy is Strategy.PLAN and not (wf.planner and wf.workers):
                raise ConfigError(f"workflow '{wf.name}': plan strategy needs 'planner:' and 'workers: [...]'")
        if self.default_synthesizer and self.default_synthesizer not in self.agents:
            raise ConfigError(f"defaults.synthesizer references unknown agent '{self.default_synthesizer}'")


def find_config(start: Optional[Path] = None) -> Optional[Path]:
    """Walk up from ``start`` (default cwd) looking for a cao config file."""
    cur = (start or Path.cwd()).resolve()
    for directory in (cur, *cur.parents):
        for name in CONFIG_FILENAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    home = Path.home() / ".config" / "cao" / "cao.yaml"
    return home if home.is_file() else None


def _parse_agent(name: str, raw: Any, default_timeout: float) -> AgentSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"agent '{name}' must be a mapping")
    if "type" not in raw:
        raise ConfigError(f"agent '{name}' is missing required key 'type'")
    known = {"type", "model", "effort", "read_only", "timeout", "env", "options", "tags"}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"agent '{name}': unknown key(s) {sorted(unknown)} (adapter knobs go under 'options:')")
    return AgentSpec(
        name=name,
        type=str(raw["type"]),
        model=raw.get("model"),
        effort=str(raw["effort"]) if raw.get("effort") else None,
        read_only=bool(raw.get("read_only", False)),
        timeout=float(raw.get("timeout", default_timeout)),
        env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
        options=dict(raw.get("options") or {}),
        tags=[str(t) for t in (raw.get("tags") or [])],
    )


def _parse_workflow(name: str, raw: Any) -> WorkflowSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"workflow '{name}' must be a mapping")
    try:
        strategy = Strategy(str(raw.get("strategy", "parallel")))
    except ValueError:
        raise ConfigError(
            f"workflow '{name}': unknown strategy '{raw.get('strategy')}' "
            f"(choose from {', '.join(s.value for s in Strategy)})"
        )
    isolation = raw.get("isolation")
    return WorkflowSpec(
        name=name,
        strategy=strategy,
        agents=[str(a) for a in (raw.get("agents") or [])],
        prompt=str(raw.get("prompt", "{goal}")),
        steps=[dict(s) for s in (raw.get("steps") or [])],
        planner=raw.get("planner"),
        workers=[str(a) for a in (raw.get("workers") or [])],
        max_tasks=int(raw.get("max_tasks", 6)),
        synthesizer=raw.get("synthesizer"),
        isolation=Isolation(str(isolation)) if isolation else None,
        description=str(raw.get("description", "")),
    )


def parse_config(data: dict[str, Any], source: Optional[Path] = None) -> Config:
    data = _expand_env(data or {})
    defaults = data.get("defaults") or {}
    default_timeout = float(defaults.get("timeout", 1800))
    default_isolation = Isolation(str(defaults.get("isolation", "worktree")))

    agents = {
        str(name): _parse_agent(str(name), raw, default_timeout) for name, raw in (data.get("agents") or {}).items()
    }
    workflows = {str(name): _parse_workflow(str(name), raw) for name, raw in (data.get("workflows") or {}).items()}

    cfg = Config(
        agents=agents,
        workflows=workflows,
        default_timeout=default_timeout,
        default_isolation=default_isolation,
        default_synthesizer=defaults.get("synthesizer"),
        source=source,
    )
    cfg.validate()
    return cfg


def load_config(path: Optional[Path] = None) -> Config:
    path = Path(path) if path else find_config()
    if path is None:
        raise ConfigError("no cao.yaml found; run 'cao init' to create one")
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return parse_config(data, source=path)


EXAMPLE_CONFIG = """\
# cao.yaml -- cross-agent-orchestrator configuration
# Docs: https://github.com/Kimdo-765/cross_agent_ochestrator

defaults:
  timeout: 1800          # seconds per agent invocation
  isolation: worktree    # shared | worktree | none  (worktree = one git branch per parallel task)
  synthesizer: claude    # agent that merges parallel / plan results into one answer

agents:
  claude:
    type: claude_code
    # model: claude-sonnet-5
    tags: [review, architecture, python]
    options:
      permission_mode: acceptEdits   # acceptEdits | bypassPermissions | plan | default
      max_turns: 40
      # allowed_tools: ["Read", "Edit", "Write", "Bash(git *)"]

  codex:
    type: codex
    # model: gpt-5-codex
    tags: [implementation, refactor]
    options:
      sandbox: workspace-write       # read-only | workspace-write | danger-full-access

  # gemini:
  #   type: gemini
  #   tags: [research, docs]
  #   options:
  #     yolo: true                   # auto-approve tool calls

  # Any command can be an agent. {prompt} and {workdir} are substituted;
  # set prompt_via: stdin to pipe the prompt instead of passing it as an argument.
  # my-agent:
  #   type: shell
  #   options:
  #     command: ["my-agent", "--task", "{prompt}"]
  #     prompt_via: arg

workflows:
  compare:
    description: Ask every agent the same thing, then have the synthesizer pick/merge the best answer.
    strategy: parallel
    agents: [claude, codex]
    isolation: none                  # pure Q&A -- no repo edits needed
    prompt: |
      {goal}

  implement-then-review:
    description: Codex implements, Claude reviews and fixes.
    strategy: pipeline
    isolation: shared
    steps:
      - agent: codex
        prompt: |
          Implement the following in this repository. Make the changes directly and
          finish with a concise summary of what you changed and why.

          Task: {goal}
      - agent: claude
        prompt: |
          Another agent just implemented the task below. Review the working tree
          changes (git diff), fix any bugs or gaps you find, run the tests if there
          are any, and end with a short review report.

          Task: {goal}

          Implementer's summary:
          {previous}

  build:
    description: Planner splits the goal into subtasks, workers execute them in parallel worktrees, synthesizer merges.
    strategy: plan
    planner: claude
    workers: [claude, codex]
    synthesizer: claude
    max_tasks: 5
"""
