"""Adapter for OpenAI's Codex CLI (``codex exec``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..models import Task
from ..runner import ProcResult
from .base import AgentAdapter


_EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}


def _codex_effort(level: str) -> str:
    return _EFFORT_MAP.get(str(level).lower(), str(level))


class CodexAdapter(AgentAdapter):
    """Runs ``codex exec --json -o <file>`` and reads the last agent message.

    options:
      sandbox: read-only | workspace-write | danger-full-access  (default: workspace-write)
      full_auto: bool                -> --dangerously-bypass-approvals-and-sandbox (use with care)
      profile: str                   -> --profile
      config: {key: value}           -> -c key=value (repeatable)
      skip_git_repo_check: bool      (default: true)
      ephemeral: bool                (default: true)
      extra_args: [..]
    """

    key = "codex"
    binary = "codex"

    def provider_config(self) -> dict[str, str]:
        """Extra ``-c key=value`` pairs (overridden by provider-specific subclasses such as Grok)."""
        return {}

    def _last_message_path(self, task: Task, run_dir: Path) -> Path:
        return run_dir / "logs" / f"{task.id}-{self.spec.name}.last.txt"

    def build_command(self, task: Task, workdir: Path, run_dir: Path) -> tuple[list[str], Optional[str]]:
        o = self.spec.options
        last = self._last_message_path(task, run_dir)
        last.parent.mkdir(parents=True, exist_ok=True)
        argv = [self.executable(), "exec", "--json", "--color", "never", "-o", str(last), "-C", str(workdir)]
        if self.spec.read_only:
            argv += ["--sandbox", "read-only"]
        elif o.get("full_auto"):
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            argv += ["--sandbox", str(o.get("sandbox", "workspace-write"))]
        if self.spec.model:
            argv += ["--model", self.spec.model]
        if self.spec.effort:
            argv += ["-c", f"model_reasoning_effort={_codex_effort(self.spec.effort)}"]
        for k, v in self.provider_config().items():
            argv += ["-c", f"{k}={v}"]
        if o.get("profile"):
            argv += ["--profile", str(o["profile"])]
        for k, v in (o.get("config") or {}).items():
            argv += ["-c", f"{k}={json.dumps(v) if not isinstance(v, str) else v}"]
        if o.get("skip_git_repo_check", True):
            argv.append("--skip-git-repo-check")
        if o.get("ephemeral", True):
            argv.append("--ephemeral")
        argv += [str(a) for a in (o.get("extra_args") or [])]
        argv.append("-")  # read prompt from stdin
        self._pending_last = last
        return argv, task.prompt

    def parse_output(self, proc: ProcResult, run_dir: Path) -> tuple[str, dict[str, Any], dict[str, Any]]:
        last: Optional[Path] = getattr(self, "_pending_last", None)
        text = ""
        if last is not None and last.is_file():
            text = last.read_text(encoding="utf-8", errors="replace").strip()

        usage: dict[str, Any] = {}
        raw: dict[str, Any] = {"events": 0}
        last_agent_msg = ""
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            raw["events"] += 1
            etype = str(ev.get("type", ""))
            item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
            # token usage shows up on turn.completed / or a top-level usage field
            if "usage" in ev and isinstance(ev["usage"], dict):
                usage = ev["usage"]
            if etype.endswith("turn.completed") and isinstance(ev.get("usage"), dict):
                usage = ev["usage"]
            if item.get("type") in ("agent_message", "assistant_message") and item.get("text"):
                last_agent_msg = str(item["text"])
            if etype in ("error", "turn.failed"):
                raw["is_error"] = True
                raw["error"] = ev.get("message") or ev.get("error") or "codex reported an error"
        if not text:
            text = last_agent_msg
        return text, usage, raw
