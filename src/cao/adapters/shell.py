"""Generic adapter: wrap any command as an agent."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from ..models import Task
from ..runner import ProcResult
from .base import AgentAdapter


_PLACEHOLDER = re.compile(r"\{(prompt|workdir|task_id|model)\}")


def _substitute(token: str, subs: dict[str, str]) -> str:
    """Replace only the known placeholders; leave any other braces untouched."""
    return _PLACEHOLDER.sub(lambda m: subs[m.group(1)], token)


class ShellAdapter(AgentAdapter):
    """Run an arbitrary command template.

    options:
      command: ["my-agent", "--task", "{prompt}"]   (required; list of argv tokens)
      prompt_via: arg | stdin                        (default: arg)
      output: text | json                            (json -> read ``result_key`` from stdout JSON)
      result_key: result

    Placeholders available in ``command`` tokens: ``{prompt}``, ``{workdir}``,
    ``{task_id}``, ``{model}``.
    """

    key = "shell"
    binary = ""

    def executable(self) -> str:
        cmd = self.spec.options.get("command") or []
        return str(cmd[0]) if cmd else ""

    def build_command(self, task: Task, workdir: Path, run_dir: Path) -> tuple[list[str], Optional[str]]:
        o = self.spec.options
        template = o.get("command")
        if not template or not isinstance(template, list):
            raise ValueError(f"agent '{self.spec.name}': shell adapter needs options.command as a list")
        subs = {"prompt": task.prompt, "workdir": str(workdir), "task_id": task.id, "model": self.spec.model or ""}
        via_stdin = str(o.get("prompt_via", "arg")) == "stdin"
        argv = []
        for tok in template:
            tok = str(tok)
            if via_stdin and "{prompt}" in tok:
                continue
            argv.append(_substitute(tok, subs))
        return argv, (task.prompt if via_stdin else None)

    def parse_output(self, proc: ProcResult, run_dir: Path) -> tuple[str, dict[str, Any], dict[str, Any]]:
        o = self.spec.options
        if o.get("output") == "json":
            data = json.loads(proc.stdout.strip() or "{}")
            key = str(o.get("result_key", "result"))
            return str(data.get(key, "")), {k: v for k, v in data.items() if k != key}, {}
        return proc.stdout.strip(), {}, {}
