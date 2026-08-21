"""Clone remote repositories into the workspace so tasks can target a URL instead of a local path."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from .gitops import GitError, git

# https://github.com/owner/repo(.git)?(/tree/<branch>/...)? | github.com/owner/repo | git@github.com:owner/repo.git | ssh://git@host/owner/repo
_HTTPS_RE = re.compile(r"^(?:https://)?(?P<host>[A-Za-z0-9.-]+)/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?(?:/(?:-/)?(?:tree|blob|commits)/(?P<ref>[^\s]+?))?/?$")
_SCP_RE = re.compile(r"^(?P<user>[A-Za-z0-9_.-]+)@(?P<host>[A-Za-z0-9.-]+):(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$")
_SSH_RE = re.compile(r"^ssh://(?:[A-Za-z0-9_.-]+@)?(?P<host>[A-Za-z0-9.-]+)(?::\d+)?/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class RepoUrlError(ValueError):
    pass


@dataclass
class RepoRef:
    url: str  # normalised clone URL
    host: str
    owner: str
    name: str
    ref: Optional[str] = None  # branch parsed from a /tree/<branch> web URL

    @property
    def web_url(self) -> str:
        return f"https://{self.host}/{self.owner}/{self.name}"

    def to_dict(self) -> dict:
        return asdict(self)


def parse_repo_url(raw: str) -> RepoRef:
    """Accept the URL shapes people paste from GitHub/GitLab and normalise them to a clone URL."""
    s = (raw or "").strip()
    if not s:
        raise RepoUrlError("empty repository URL")
    if s.startswith(("file:", "/", ".", "~")) or "://" in s and not s.startswith(("https://", "ssh://")):
        raise RepoUrlError("only https:// and ssh (git@host:owner/repo) URLs are accepted")
    m = _SCP_RE.match(s)
    if m:
        return RepoRef(url=f"{m['user']}@{m['host']}:{m['owner']}/{m['repo']}.git", host=m["host"], owner=m["owner"], name=m["repo"])
    m = _SSH_RE.match(s)
    if m:
        return RepoRef(url=s if s.endswith(".git") else s.rstrip("/") + ".git", host=m["host"], owner=m["owner"], name=m["repo"])
    m = _HTTPS_RE.match(s)
    if m and "." in m["host"]:
        ref = m["ref"]
        if ref:
            ref = ref.split("/")[0] if "/" in ref and not ref.startswith("refs/") else ref
        return RepoRef(url=f"https://{m['host']}/{m['owner']}/{m['repo']}.git", host=m["host"], owner=m["owner"], name=m["repo"], ref=ref)
    raise RepoUrlError(f"could not parse repository URL: {raw!r} (expected https://host/owner/repo or git@host:owner/repo.git)")


@dataclass
class CloneResult:
    path: str
    name: str
    url: str
    web_url: str
    default_branch: str
    head: str
    existed: bool  # an up-to-date clone was already there
    fetched: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _authenticated_url(ref: RepoRef, token: Optional[str]) -> str:
    """For https GitHub-style hosts, embed a token for the clone only (never written to .git/config)."""
    if token and ref.url.startswith("https://"):
        return ref.url.replace("https://", f"https://x-access-token:{token}@", 1)
    return ref.url


def clone_repo(
    raw_url: str,
    workspace: Path,
    *,
    name: Optional[str] = None,
    branch: Optional[str] = None,
    token: Optional[str] = None,
    timeout: float = 900.0,
) -> CloneResult:
    """Clone ``raw_url`` into ``workspace/<name>`` (or reuse + fetch an existing clone of the same remote)."""
    ref = parse_repo_url(raw_url)
    branch = branch or ref.ref
    name = (name or ref.name).strip()
    if not _NAME_RE.match(name) or name in (".", ".."):
        raise RepoUrlError(f"invalid directory name {name!r}")
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    dest = (workspace / name).resolve()
    if dest.parent != workspace:
        raise RepoUrlError("destination escapes the workspace")
    if shutil.which("git") is None:
        raise GitError("git is not installed")

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}  # never block on a password prompt
    if token:
        env.pop("GH_TOKEN", None)

    if dest.exists():
        if not (dest / ".git").exists():
            raise RepoUrlError(f"{dest} exists and is not a git repository; choose another name")
        remote = _strip_auth(git(["remote", "get-url", "origin"], dest, check=False).stdout.strip())
        try:
            same = parse_repo_url(remote)
            same_repo = (same.host, same.owner.lower(), same.name.lower()) == (ref.host, ref.owner.lower(), ref.name.lower())
        except RepoUrlError:
            same_repo = False
        if not same_repo:
            raise RepoUrlError(f"{dest} is a clone of {remote or 'another remote'}; choose another name")
        fetched = subprocess.run(["git", "fetch", "--all", "--prune", "-q"], cwd=dest, env=env, capture_output=True, text=True, timeout=timeout).returncode == 0
        if branch:
            res = subprocess.run(["git", "checkout", "-q", branch], cwd=dest, env=env, capture_output=True, text=True)
            if res.returncode != 0:
                raise GitError(f"branch '{branch}' not found in {ref.web_url}: {res.stderr.strip()}")
        return _result(dest, ref, existed=True, fetched=fetched)

    argv = ["git", "clone", "-q"]
    if branch:
        argv += ["--branch", branch]
    argv += [_authenticated_url(ref, token), str(dest)]
    try:
        res = subprocess.run(argv, cwd=str(workspace), env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        shutil.rmtree(dest, ignore_errors=True)
        raise GitError(f"clone timed out after {timeout:.0f}s")
    if res.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        msg = _strip_auth(res.stderr.strip() or res.stdout.strip())
        if "Authentication failed" in msg or "could not read Username" in msg or "Permission denied" in msg:
            msg += "  (private repo? use an ssh URL with a mounted ~/.ssh key, or set GH_TOKEN)"
        raise GitError(f"git clone failed: {msg}")
    if token:
        git(["remote", "set-url", "origin", ref.url], dest)  # keep the token out of .git/config
    return _result(dest, ref, existed=False)


def _strip_auth(text: str) -> str:
    return re.sub(r"https://[^/@\s]+@", "https://", text)


def _result(dest: Path, ref: RepoRef, *, existed: bool, fetched: bool = False) -> CloneResult:
    head_branch = git(["rev-parse", "--abbrev-ref", "HEAD"], dest).stdout.strip()
    head = git(["rev-parse", "HEAD"], dest, check=False).stdout.strip()
    return CloneResult(
        path=str(dest), name=dest.name, url=ref.url, web_url=ref.web_url,
        default_branch=head_branch, head=head, existed=existed, fetched=fetched,
    )


def list_workspace_repos(workspace: Path) -> list[dict]:
    """Git repositories directly under the workspace, with their origin URL (for the UI picker)."""
    out = []
    workspace = Path(workspace)
    if not workspace.is_dir():
        return out
    for p in sorted(workspace.iterdir(), key=lambda p: p.name.lower()):
        if not (p / ".git").exists():
            continue
        remote = git(["remote", "get-url", "origin"], p, check=False).stdout.strip()
        branch = git(["rev-parse", "--abbrev-ref", "HEAD"], p, check=False).stdout.strip()
        out.append({"name": p.name, "path": str(p.resolve()), "remote": _strip_auth(remote), "branch": branch})
    return out
