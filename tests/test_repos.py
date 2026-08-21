import subprocess
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from cao.loop import repos
from cao.loop.repos import RepoRef, RepoUrlError, clone_repo, list_workspace_repos, parse_repo_url
from cao.loop.store import Store
from cao.web.app import create_app

from conftest import make_repo


@pytest.mark.parametrize("raw, url, name, ref", [
    ("https://github.com/Kimdo-765/cross_agent_ochestrator", "https://github.com/Kimdo-765/cross_agent_ochestrator.git", "cross_agent_ochestrator", None),
    ("https://github.com/Kimdo-765/cross_agent_ochestrator.git", "https://github.com/Kimdo-765/cross_agent_ochestrator.git", "cross_agent_ochestrator", None),
    ("github.com/owner/repo/", "https://github.com/owner/repo.git", "repo", None),
    ("https://github.com/owner/repo/tree/feat/x-y", "https://github.com/owner/repo.git", "repo", "feat"),
    ("https://gitlab.com/group/proj/-/tree/main", "https://gitlab.com/group/proj.git", "proj", "main"),
    ("git@github.com:owner/repo.git", "git@github.com:owner/repo.git", "repo", None),
    ("ssh://git@github.com/owner/repo", "ssh://git@github.com/owner/repo.git", "repo", None),
])
def test_parse_repo_url(raw, url, name, ref):
    r = parse_repo_url(raw)
    assert (r.url, r.name, r.ref) == (url, name, ref)
    assert r.web_url.startswith("https://")


@pytest.mark.parametrize("bad", ["", "file:///etc/passwd", "/home/x/repo", "../x", "http://github.com/o/r", "git://h/o/r", "https://localhost/o/r", "not a url"])
def test_parse_repo_url_rejects(bad):
    with pytest.raises(RepoUrlError):
        parse_repo_url(bad)


@pytest.fixture
def fake_remote(tmp_path, monkeypatch):
    """A local bare repo standing in for GitHub; parse_repo_url is patched to map a fake https URL onto it."""
    src = make_repo(tmp_path / "src")
    subprocess.run(["git", "checkout", "-q", "-b", "dev"], cwd=src, check=True)
    (src / "dev.txt").write_text("dev\n")
    subprocess.run(["git", "add", "-A"], cwd=src, check=True)
    subprocess.run(["git", "commit", "-qm", "dev commit"], cwd=src, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=src, check=True)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=bare, check=True)
    real = repos.parse_repo_url

    def patched(raw):
        if raw.startswith("https://example.test/acme/widget") or raw.startswith(str(bare)):
            ref = real("https://example.test/acme/widget" + ("/tree/dev" if raw.endswith("/tree/dev") else ""))
            return RepoRef(url=str(bare), host=ref.host, owner=ref.owner, name=ref.name, ref=ref.ref)
        return real(raw)

    monkeypatch.setattr(repos, "parse_repo_url", patched)
    return bare


def test_clone_into_workspace_and_reuse(tmp_path, fake_remote):
    ws = tmp_path / "ws"
    res = clone_repo("https://example.test/acme/widget", ws)
    assert res.path == str(ws / "widget") and res.default_branch == "main" and not res.existed
    assert (ws / "widget" / "README.md").exists()
    # second call reuses + fetches; /tree/dev switches branch
    again = clone_repo("https://example.test/acme/widget/tree/dev", ws)
    assert again.existed and again.fetched and again.default_branch == "dev"
    assert (ws / "widget" / "dev.txt").exists()
    # a different remote under the same name is refused
    (ws / "other").mkdir()
    with pytest.raises(RepoUrlError, match="not a git repository"):
        clone_repo("https://example.test/acme/widget", ws, name="other")
    with pytest.raises(RepoUrlError, match="invalid directory name"):
        clone_repo("https://example.test/acme/widget", ws, name="../escape")
    listed = list_workspace_repos(ws)
    assert [r["name"] for r in listed] == ["widget"] and listed[0]["branch"] == "dev"


def test_clone_unknown_branch_fails_cleanly(tmp_path, fake_remote):
    from cao.loop.gitops import GitError
    with pytest.raises(GitError):
        clone_repo("https://example.test/acme/widget", tmp_path / "ws", branch="nope")
    assert not (tmp_path / "ws" / "widget").exists()  # partial clone removed


async def test_clone_api_and_task_on_clone(tmp_path, fake_remote, monkeypatch):
    ws = tmp_path / "ws"
    monkeypatch.setenv("CAO_WORKSPACE", str(ws))
    monkeypatch.setenv("CAO_FAKE_SCORES", "9.5")
    app = create_app(Store(tmp_path / "web.db"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/repos/clone", json={"url": "https://example.test/acme/widget"})
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["path"] == str(ws / "widget") and data["default_branch"] == "main"
        assert (await c.get("/api/repos")).json()["repos"][0]["name"] == "widget"
        assert (await c.post("/api/repos/clone", json={"url": "file:///x"})).status_code == 400
        # a task on the clone runs end to end (on_success none: no push)
        r = await c.post("/api/tasks", json={
            "request": "Add greeting", "repo_path": data["path"], "repo_url": "https://example.test/acme/widget",
            "worker": {"backend": "claude_code"}, "reviewer": {"backend": "codex"},
            "loop": {"max_iterations": 1, "on_success": "none"},
        })
        tid = r.json()["id"]
        import asyncio
        for _ in range(100):
            d = (await c.get(f"/api/tasks/{tid}")).json()
            if not d["running"]:
                break
            await asyncio.sleep(0.05)
        assert d["status"] == "passed" and d["spec"]["repo_url"] == "https://example.test/acme/widget"
    await app.state.manager.shutdown()
    branches = subprocess.run(["git", "branch", "--list", "cao/*"], cwd=ws / "widget", capture_output=True, text=True).stdout
    assert "cao/" in branches


def test_cli_repo_url(tmp_path, fake_remote, monkeypatch, capsys):
    from cao.cli import main
    monkeypatch.setenv("CAO_FAKE_SCORES", "10")
    ws = tmp_path / "ws"
    rc = main(["--data-dir", str(tmp_path / "d"), "run", "--repo-url", "https://example.test/acme/widget", "-C", str(ws),
               "-w", "claude_code", "-r", "codex", "--on-success", "none", "-q", "add a greeting"])
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert "cloned" in out.err and "PASSED" in out.out
    assert (ws / "widget" / ".cao").exists()
