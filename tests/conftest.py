import os
import subprocess
from pathlib import Path

import pytest

FAKE_BINS = Path(__file__).parent / "fake_bins"


@pytest.fixture(autouse=True)
def fake_path(monkeypatch):
    """Put the fake `claude` / `codex` binaries first on PATH for every test."""
    monkeypatch.setenv("PATH", f"{FAKE_BINS}{os.pathsep}{os.environ.get('PATH', '')}")
    yield


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A throwaway git repo with one commit (needed for worktree isolation)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    return repo
