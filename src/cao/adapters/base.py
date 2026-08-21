"""Adapter contract: turn (AgentSpec, Task, workdir) into a TaskResult."""

from __future__ import annotations

import shutil
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

from ..models import AgentSpec, Task, TaskResult
from ..runner import ProcResult, run_process


class AgentAdapter(ABC):
    """Base class for driving one kind of agent CLI.

    Subclasses implement :meth:`build_command` (argv + stdin) and
    :meth:`parse_output` (extract the final message / usage from the process
    output). Everything else -- timeouts, logging, error handling -- is shared.
    """

    key: str = ""  # value used in cao.yaml ``type:``
    binary: str = ""  # executable name looked up on PATH (for availability checks)

    def __init__(self, spec: AgentSpec):
        self.spec = spec

    # ---- things subclasses implement --------------------------------------

    @abstractmethod
    def build_command(self, task: Task, workdir: Path, run_dir: Path) -> tuple[list[str], Optional[str]]:
        """Return ``(argv, stdin_text)`` for this task."""

    @abstractmethod
    def parse_output(self, proc: ProcResult, run_dir: Path) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """Return ``(final_text, usage, raw)`` extracted from the finished process."""

    # ---- shared behaviour ----------------------------------------------------

    def executable(self) -> str:
        return str(self.spec.options.get("binary") or self.binary)

    def is_available(self) -> tuple[bool, str]:
        """``(available, detail)`` -- checks the binary is on PATH."""
        exe = self.executable()
        path = shutil.which(exe)
        return (path is not None, path or f"'{exe}' not found on PATH")

    def version(self) -> str:
        import subprocess

        try:
            res = subprocess.run([self.executable(), "--version"], capture_output=True, text=True, timeout=20)
            return (res.stdout or res.stderr).strip().splitlines()[0] if (res.stdout or res.stderr) else "?"
        except Exception as exc:  # pragma: no cover - best effort
            return f"? ({exc.__class__.__name__})"

    async def run(self, task: Task, workdir: Path, run_dir: Path) -> TaskResult:
        argv, stdin = self.build_command(task, workdir, run_dir)
        log_path = run_dir / "logs" / f"{task.id}-{self.spec.name}.log"
        started = time.monotonic()
        try:
            proc = await run_process(
                argv, cwd=workdir, stdin=stdin, env=self.spec.env, timeout=self.spec.timeout, log_path=log_path
            )
        except FileNotFoundError as exc:
            return TaskResult(
                task=task, agent=self.spec.name, ok=False, output="", error=f"executable not found: {exc}",
                duration_s=time.monotonic() - started, workdir=workdir,
            )

        text, usage, raw = "", {}, {}
        parse_error = None
        try:
            text, usage, raw = self.parse_output(proc, run_dir)
        except Exception as exc:  # keep going -- raw stdout is still useful
            parse_error = f"{exc.__class__.__name__}: {exc}"
            text = proc.stdout.strip()

        error = None
        if proc.timed_out:
            error = f"timed out after {self.spec.timeout:.0f}s"
        elif proc.exit_code != 0:
            error = f"exit code {proc.exit_code}"
            tail = proc.stderr.strip().splitlines()[-3:]
            if tail:
                error += ": " + " | ".join(tail)
        elif raw.get("is_error"):
            error = str(raw.get("error") or "agent reported an error")
        elif parse_error and not text:
            error = f"could not parse output ({parse_error})"

        return TaskResult(
            task=task,
            agent=self.spec.name,
            ok=error is None,
            output=text,
            stdout=proc.stdout,
            stderr=proc.stderr,
            exit_code=proc.exit_code,
            duration_s=proc.duration_s,
            workdir=workdir,
            usage=usage,
            error=error,
            raw=raw,
        )
