"""Git plumbing for the loop: repo bootstrap, task branch + worktree, diffs, commits, merge/PR."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence


class GitError(RuntimeError):
    pass


def git(args: Sequence[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess:
    res = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and res.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {res.stderr.strip() or res.stdout.strip()}")
    return res


def slug(text: str, limit: int = 32) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return s[:limit].rstrip("-") or "task"


# --------------------------------------------------------------------------- #


@dataclass
class RepoInfo:
    root: Path
    base_branch: str
    base_commit: str
    created: bool = False  # we ran git init
    initial_commit: bool = False  # we made the first commit


def ensure_repo(path: Path, preferred_base: Optional[str] = None) -> RepoInfo:
    """Make ``path`` a usable git repository with at least one commit.

    - not a repo        -> ``git init`` + initial commit of whatever is there
    - repo, no commits  -> initial commit
    - repo with commits -> untouched
    """
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    created = initial = False
    if git(["rev-parse", "--is-inside-work-tree"], path, check=False).returncode != 0:
        git(["init", "-q"], path)
        created = True
    root = Path(git(["rev-parse", "--show-toplevel"], path).stdout.strip())
    _ensure_identity(root)
    if git(["rev-parse", "--verify", "HEAD"], root, check=False).returncode != 0:
        if not any(p.name != ".git" for p in root.iterdir()):
            (root / ".gitkeep").write_text("")
        git(["add", "-A"], root)
        git(["commit", "-q", "-m", "chore: initial commit (created by cao)"], root)
        initial = True
    if preferred_base:
        if git(["rev-parse", "--verify", preferred_base], root, check=False).returncode != 0:
            raise GitError(f"base branch '{preferred_base}' does not exist")
        base_branch = preferred_base
    else:
        base_branch = git(["rev-parse", "--abbrev-ref", "HEAD"], root).stdout.strip()
        if base_branch == "HEAD":  # detached
            base_branch = git(["rev-parse", "HEAD"], root).stdout.strip()
    base_commit = git(["rev-parse", base_branch], root).stdout.strip()
    return RepoInfo(root=root, base_branch=base_branch, base_commit=base_commit, created=created, initial_commit=initial)


def _ensure_identity(root: Path) -> None:
    """Commits need an identity; fall back to a bot identity for this repo only."""
    if git(["config", "user.email"], root, check=False).returncode != 0:
        git(["config", "user.email", "cao@localhost"], root)
    if git(["config", "user.name"], root, check=False).returncode != 0:
        git(["config", "user.name", "cao"], root)


@dataclass
class TaskWorkspace:
    root: Path  # main repository
    path: Path  # worktree for this task
    branch: str
    base_commit: str


# Build artefacts that agents routinely produce while running tests. The orchestrator commits on the
# worker's behalf with `git add -A`, so these are excluded via .git/info/exclude (never the user's .gitignore).
DEFAULT_EXCLUDES = (
    ".cao/", "__pycache__/", "*.pyc", "*.pyo", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".tox/",
    "*.egg-info/", "node_modules/", ".DS_Store", "*.swp",
)


def create_task_worktree(repo: RepoInfo, task_id: str, title: str) -> TaskWorkspace:
    branch = f"cao/{task_id}-{slug(title)}"
    wt_dir = repo.root / ".cao" / "worktrees" / task_id
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    for pattern in DEFAULT_EXCLUDES:
        _ensure_ignored(repo.root, pattern)
    if wt_dir.exists():
        git(["worktree", "remove", "--force", str(wt_dir)], repo.root, check=False)
        shutil.rmtree(wt_dir, ignore_errors=True)
    if git(["rev-parse", "--verify", branch], repo.root, check=False).returncode == 0:
        git(["branch", "-D", branch], repo.root)
    git(["worktree", "add", "-q", "-b", branch, str(wt_dir), repo.base_commit], repo.root)
    return TaskWorkspace(root=repo.root, path=wt_dir, branch=branch, base_commit=repo.base_commit)


def _ensure_ignored(root: Path, pattern: str) -> None:
    """Add ``pattern`` to .git/info/exclude (shared by all worktrees; never touches the user's .gitignore)."""
    git_dir = Path(git(["rev-parse", "--git-common-dir"], root).stdout.strip())
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    exclude = git_dir / "info" / "exclude"
    try:
        existing = exclude.read_text().splitlines() if exclude.is_file() else []
        if pattern in existing:
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with open(exclude, "a", encoding="utf-8") as fh:
            if "# added by cao" not in existing:
                fh.write("\n# added by cao\n")
            fh.write(f"{pattern}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Per-iteration operations (run inside the worktree)
# --------------------------------------------------------------------------- #


def head(ws: TaskWorkspace) -> str:
    return git(["rev-parse", "HEAD"], ws.path).stdout.strip()


def iteration_diff(ws: TaskWorkspace, start_commit: str) -> str:
    """Everything the worker changed since ``start_commit``: its own commits (if it made any
    despite instructions), staged, unstaged and untracked files."""
    git(["add", "-A"], ws.path)
    diff = git(["diff", "--cached", "--no-color", "--no-ext-diff", start_commit], ws.path).stdout
    git(["reset", "-q"], ws.path)
    return diff


def commit_iteration(ws: TaskWorkspace, number: int, message: str) -> Optional[str]:
    """Commit the worker's changes; returns the sha (None if nothing to commit)."""
    git(["add", "-A"], ws.path)
    if not git(["diff", "--cached", "--quiet"], ws.path, check=False).returncode:
        return None
    git(["commit", "-q", "-m", f"cao iteration {number}: {message}"], ws.path)
    return git(["rev-parse", "HEAD"], ws.path).stdout.strip()


def branch_diff(ws: TaskWorkspace) -> str:
    """Cumulative diff of the task branch against the base commit -- what the reviewer sees."""
    return git(["diff", "--no-color", "--no-ext-diff", f"{ws.base_commit}..HEAD"], ws.path).stdout


def branch_diffstat(ws: TaskWorkspace) -> str:
    return git(["diff", "--stat", "--no-color", f"{ws.base_commit}..HEAD"], ws.path).stdout.strip()


def discard_uncommitted(ws: TaskWorkspace) -> None:
    git(["reset", "-q", "--hard"], ws.path)
    git(["clean", "-fdq"], ws.path)


# --------------------------------------------------------------------------- #
# Finishing
# --------------------------------------------------------------------------- #


def merge_into_base(ws: TaskWorkspace, base_branch: str, message: str) -> str:
    """Fast-forward or --no-ff merge of the task branch into ``base_branch`` inside the main checkout."""
    current = git(["rev-parse", "--abbrev-ref", "HEAD"], ws.root).stdout.strip()
    if current != base_branch:
        raise GitError(
            f"main checkout is on '{current}', not '{base_branch}'; check it out (with a clean tree) or choose on_success=pr"
        )
    if git(["status", "--porcelain"], ws.root).stdout.strip():
        raise GitError("main checkout has uncommitted changes; commit or stash them before merging")
    git(["merge", "--no-ff", "-q", "-m", message, ws.branch], ws.root)
    return git(["rev-parse", "HEAD"], ws.root).stdout.strip()


def has_remote(root: Path) -> bool:
    return bool(git(["remote"], root, check=False).stdout.strip())


def create_pr(ws: TaskWorkspace, base_branch: str, title: str, body: str) -> str:
    """Push the task branch and open a PR with ``gh``; returns the PR URL."""
    if not has_remote(ws.root):
        raise GitError("repository has no remote; cannot open a PR (use on_success=merge or none)")
    if shutil.which("gh") is None:
        raise GitError("'gh' CLI not found; cannot open a PR")
    remote = git(["remote"], ws.root).stdout.split()[0]
    git(["push", "-u", remote, ws.branch], ws.path)
    res = subprocess.run(
        ["gh", "pr", "create", "--base", base_branch, "--head", ws.branch, "--title", title, "--body", body],
        cwd=str(ws.path), capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise GitError(f"gh pr create failed: {res.stderr.strip()}")
    url = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""
    return url


def remove_worktree(ws: TaskWorkspace) -> None:
    git(["worktree", "remove", "--force", str(ws.path)], ws.root, check=False)
