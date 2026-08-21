"""Adapter for Anthropic's Claude Code CLI (``claude -p``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..models import Task
from ..runner import ProcResult
from .base import AgentAdapter


READ_ONLY_TOOLS = ("Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch")

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


class ClaudeCodeAdapter(AgentAdapter):
    """Runs ``claude --print --output-format json`` and reads the JSON envelope.

    options:
      permission_mode: acceptEdits | bypassPermissions | plan | default  (default: acceptEdits)
      max_turns: int
      max_budget_usd: float          -> --max-budget-usd
      tools: [..]                    -> --tools (restrict the built-in tool set)
      session_persistence: bool      (default: false -> --no-session-persistence; every run is a clean context)
      allowed_tools: [..]            -> --allowedTools
      system_prompt: str             -> --system-prompt
      append_system_prompt: str      -> --append-system-prompt
      add_dirs: [..]                 -> --add-dir
      extra_args: [..]               -> appended verbatim
    """

    key = "claude_code"
    binary = "claude"

    def build_command(self, task: Task, workdir: Path, run_dir: Path) -> tuple[list[str], Optional[str]]:
        o = self.spec.options
        argv = [self.executable(), "--print", "--output-format", "json"]
        argv += ["--permission-mode", str(o.get("permission_mode", "acceptEdits"))]
        if self.spec.model:
            argv += ["--model", self.spec.model]
        if self.spec.effort:
            argv += ["--effort", str(self.spec.effort)]
        if o.get("max_turns"):
            argv += ["--max-turns", str(o["max_turns"])]
        if o.get("max_budget_usd"):
            argv += ["--max-budget-usd", str(o["max_budget_usd"])]
        if self.spec.read_only:
            # Reviewer mode: only read-only built-ins exist at all, and edits are explicitly disallowed.
            argv += ["--tools", *READ_ONLY_TOOLS, "--disallowedTools", "Edit", "Write", "NotebookEdit", "Bash"]
        elif o.get("tools"):
            argv += ["--tools", *[str(t) for t in o["tools"]]]
        if not o.get("session_persistence", False):
            argv.append("--no-session-persistence")
        if o.get("allowed_tools"):
            argv += ["--allowedTools", *[str(t) for t in o["allowed_tools"]]]
        if o.get("system_prompt"):
            argv += ["--system-prompt", str(o["system_prompt"])]
        if o.get("append_system_prompt"):
            argv += ["--append-system-prompt", str(o["append_system_prompt"])]
        for d in o.get("add_dirs") or []:
            argv += ["--add-dir", str(d)]
        argv += [str(a) for a in (o.get("extra_args") or [])]
        # Prompt goes through stdin so arbitrary length / quoting is safe.
        return argv, task.prompt

    def parse_output(self, proc: ProcResult, run_dir: Path) -> tuple[str, dict[str, Any], dict[str, Any]]:
        data = _last_json_object(proc.stdout)
        if data is None:
            return proc.stdout.strip(), {}, {}
        usage = {
            k: data[k]
            for k in ("total_cost_usd", "duration_ms", "duration_api_ms", "num_turns", "usage", "modelUsage")
            if k in data
        }
        text = data.get("result")
        if text is None:
            text = data.get("content") or ""
        raw = {k: v for k, v in data.items() if k not in ("result",)}
        raw["is_error"] = bool(data.get("is_error"))
        if data.get("subtype") and data["subtype"] != "success":
            raw.setdefault("error", f"claude subtype={data['subtype']}")
            if data["subtype"].startswith("error"):
                raw["is_error"] = True
        return str(text), usage, raw


def _last_json_object(text: str) -> Optional[dict]:
    """Claude prints one JSON object; tolerate leading noise / trailing newlines."""
    text = text.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    # fall back: last line that parses as an object
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return None
