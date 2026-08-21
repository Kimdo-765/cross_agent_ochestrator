"""Adapter for Google's Gemini CLI (``gemini -p``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..models import Task
from ..runner import ProcResult
from .base import AgentAdapter


class GeminiAdapter(AgentAdapter):
    """Runs ``gemini -p <prompt>`` non-interactively.

    options:
      yolo: bool                     -> --yolo (auto-approve tool calls)
      sandbox: bool                  -> --sandbox
      output_format: text | json     (default: json if supported, falls back to text)
      extra_args: [..]
    """

    key = "gemini"
    binary = "gemini"

    def build_command(self, task: Task, workdir: Path, run_dir: Path) -> tuple[list[str], Optional[str]]:
        o = self.spec.options
        argv = [self.executable(), "-p", task.prompt]
        if self.spec.model:
            argv += ["--model", self.spec.model]
        if o.get("yolo"):
            argv.append("--yolo")
        if o.get("sandbox"):
            argv.append("--sandbox")
        fmt = o.get("output_format", "json")
        if fmt == "json":
            argv += ["--output-format", "json"]
        argv += [str(a) for a in (o.get("extra_args") or [])]
        return argv, None

    def parse_output(self, proc: ProcResult, run_dir: Path) -> tuple[str, dict[str, Any], dict[str, Any]]:
        text = proc.stdout.strip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    usage = data.get("stats") or {}
                    return str(data.get("response") or data.get("result") or ""), usage, {"is_error": bool(data.get("error"))}
            except json.JSONDecodeError:
                pass
        return text, {}, {}
