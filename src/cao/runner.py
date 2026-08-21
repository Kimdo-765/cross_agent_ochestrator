"""Subprocess execution, timeouts, logging, and workdir isolation."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .models import Isolation


@dataclass
class ProcResult:
    argv: list[str]
    exit_code: Optional[int]
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


async def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    stdin: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    timeout: float = 1800.0,
    log_path: Optional[Path] = None,
) -> ProcResult:
    """Run ``argv`` to completion, capturing output (and tee'ing to ``log_path``)."""
    full_env = dict(os.environ)
    full_env.update(env or {})
    full_env.setdefault("NO_COLOR", "1")
    full_env.setdefault("CI", "1")  # most agent CLIs disable interactive UI under CI

    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=full_env,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    timed_out = False
    try:
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(stdin.encode() if stdin is not None else None), timeout=timeout
        )
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        out_b, err_b = await proc.communicate()
    duration = time.monotonic() - started
    stdout = out_b.decode(errors="replace")
    stderr = err_b.decode(errors="replace")

    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(f"$ {' '.join(argv)}\n(cwd: {cwd})\n")
            if stdin is not None:
                fh.write(f"\n--- stdin ---\n{stdin}\n")
            fh.write(f"\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n")
            fh.write(f"--- exit {proc.returncode} in {duration:.1f}s{' (TIMEOUT)' if timed_out else ''} ---\n")

    return ProcResult(list(argv), proc.returncode, stdout, stderr, duration, timed_out)


# --------------------------------------------------------------------------- #
# Workdir isolation
# --------------------------------------------------------------------------- #


def _git(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def is_git_repo(path: Path) -> bool:
    return _git(["rev-parse", "--is-inside-work-tree"], path).returncode == 0


def git_root(path: Path) -> Path:
    res = _git(["rev-parse", "--show-toplevel"], path)
    if res.returncode != 0:
        raise RuntimeError(f"{path} is not inside a git repository")
    return Path(res.stdout.strip())


def has_commits(path: Path) -> bool:
    return _git(["rev-parse", "--verify", "HEAD"], path).returncode == 0


def _slug(text: str, limit: int = 24) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return s[:limit].rstrip("-") or "task"


@dataclass
class Workspace:
    """A directory an agent may run in, plus how to describe/clean it."""

    path: Path
    isolation: Isolation
    branch: Optional[str] = None
    _tmp: Optional[str] = None

    def cleanup(self) -> None:
        # Worktrees are intentionally kept so the user can inspect/merge branches.
        if self.isolation is Isolation.NONE and self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)


def prepare_workspace(project_dir: Path, isolation: Isolation, run_id: str, task_id: str, title: str) -> Workspace:
    """Create the directory an agent should run in according to ``isolation``."""
    project_dir = project_dir.resolve()

    if isolation is Isolation.NONE:
        tmp = tempfile.mkdtemp(prefix="cao-")
        return Workspace(Path(tmp), isolation, _tmp=tmp)

    if isolation is Isolation.SHARED:
        return Workspace(project_dir, isolation)

    # WORKTREE
    if not is_git_repo(project_dir) or not has_commits(project_dir):
        raise RuntimeError(
            "isolation=worktree requires a git repository with at least one commit; "
            "use isolation: shared (or commit first)"
        )
    root = git_root(project_dir)
    branch = f"cao/{run_id}/{task_id}-{_slug(title)}"
    wt_dir = root / ".cao" / "worktrees" / run_id / task_id
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    res = _git(["worktree", "add", "-b", branch, str(wt_dir), "HEAD"], root)
    if res.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {res.stderr.strip()}")
    return Workspace(wt_dir, isolation, branch=branch)


def worktree_diffstat(ws: Workspace) -> str:
    """Summarize what an agent changed inside its worktree (for the report)."""
    if ws.isolation is not Isolation.WORKTREE:
        return ""
    _git(["add", "-A"], ws.path)
    res = _git(["diff", "--cached", "--stat"], ws.path)
    _git(["reset", "-q"], ws.path)
    return res.stdout.strip()
