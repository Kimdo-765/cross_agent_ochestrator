import os
import subprocess
from pathlib import Path

import pytest

FAKE_BINS = Path(__file__).parent / "fake_bins"


@pytest.fixture(autouse=True)
def fake_path(monkeypatch, tmp_path):
    """Put the fake `claude` / `codex` binaries first on PATH for every test, with isolated state."""
    monkeypatch.setenv("PATH", f"{FAKE_BINS}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("CAO_FAKE_STATE", str(tmp_path / "fake-state"))
    monkeypatch.setenv("CAO_DATA_DIR", str(tmp_path / "cao-data"))
    for var in ("CAO_FAKE_SCORES", "CAO_FAKE_WORKER_NOOP", "CAO_FAKE_WORKER_BLOCKED", "CAO_FAKE_REVIEW_GARBAGE",
                "CAO_FAKE_REVIEWER_EDITS", "CAO_FAKE_WORKER_PYCACHE", "XAI_API_KEY", "CAO_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    yield


def make_repo(path: Path, *, commit: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n")
    if commit:
        subprocess.run(["git", "add", "-A"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)
    return path


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A throwaway git repo with one commit (needed for worktree isolation)."""
    return make_repo(tmp_path / "repo")
