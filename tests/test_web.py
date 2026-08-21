import asyncio
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from cao.loop.store import Store
from cao.web.app import create_app
from cao.web.server import free_port


def _task_body(repo: Path, **over):
    body = {
        "title": "Add greeting",
        "request": "Add a greeting function.",
        "acceptance_criteria": ["greeting.py exists"],
        "repo_path": str(repo),
        "worker": {"backend": "claude_code", "role": "coder", "effort": "high"},
        "reviewer": {"backend": "codex", "effort": "medium"},
        "loop": {"max_iterations": 3, "pass_score": 9, "on_success": "none", "stop_if_no_progress": 0},
    }
    body.update(over)
    return body


@pytest.fixture
async def client(tmp_path):
    app = create_app(Store(tmp_path / "web.db"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c
    await app.state.manager.shutdown()


async def test_meta_and_static(client):
    r = await client.get("/api/meta")
    assert r.status_code == 200
    meta = r.json()
    assert {b["key"] for b in meta["backends"]} == {"claude_code", "codex", "grok"}
    assert next(b for b in meta["backends"] if b["key"] == "claude_code")["available"] is True
    assert next(b for b in meta["backends"] if b["key"] == "grok")["available"] is False  # no XAI_API_KEY
    assert [r["key"] for r in meta["roles"]][:2] == ["coder", "planner"]
    assert len(meta["criteria"]) == 7
    assert (await client.get("/")).status_code == 200
    assert "cao" in (await client.get("/static/app.js")).text


async def test_validation_errors(client, git_repo):
    r = await client.post("/api/tasks", json=_task_body(git_repo, reviewer={"backend": "claude_code"}))
    assert r.status_code == 422
    assert "different model" in r.json()["detail"]["problems"][0]
    r = await client.post("/api/tasks", json={"request": ""})
    assert r.status_code == 422


async def test_create_run_stream_and_finish(client, git_repo, monkeypatch):
    monkeypatch.setenv("CAO_FAKE_SCORES", "7,9.5")
    r = await client.post("/api/tasks", json=_task_body(git_repo))
    assert r.status_code == 201
    tid = r.json()["id"]

    # list shows it (running or already finished -- the fakes are fast)
    rows = (await client.get("/api/tasks")).json()
    assert rows[0]["id"] == tid

    # SSE replays + streams until done
    events = []
    async with client.stream("GET", f"/api/tasks/{tid}/events") as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                events.append(line.split(":", 1)[1].strip())
            if line == "event: done":
                break
    assert "log" in events and events[-1] == "done"

    # wait for the background task to persist its final state
    for _ in range(100):
        data = (await client.get(f"/api/tasks/{tid}")).json()
        if not data["running"]:
            break
        await asyncio.sleep(0.05)
    assert data["status"] == "passed", data.get("error")
    assert [i["score"] for i in data["iterations"]] == [7.0, 9.5]
    assert data["iterations"][1]["events"][0]["phase"] == "OFFER"

    diff = (await client.get(f"/api/tasks/{tid}/iterations/2/diff")).text
    assert "+def greet_v2" in diff
    logs = (await client.get(f"/api/tasks/{tid}/logs")).json()
    assert any("PASS" in l["line"] for l in logs)

    # clone (without starting) keeps the spec but gets a new id
    r = await client.post(f"/api/tasks/{tid}/clone", json={"start": False, "title": "copy"})
    assert r.status_code == 201 and r.json()["id"] != tid
    copy = (await client.get(f"/api/tasks/{r.json()['id']}")).json()
    assert copy["status"] == "pending" and copy["spec"]["title"] == "copy" and copy["spec"]["repo_path"] == str(git_repo)

    # cancel on a finished task is a 409; delete works
    assert (await client.post(f"/api/tasks/{tid}/cancel")).status_code == 409
    assert (await client.delete(f"/api/tasks/{tid}")).status_code == 200
    assert (await client.get(f"/api/tasks/{tid}")).status_code == 404


async def test_start_pending_and_rerun_failed(client, git_repo, monkeypatch):
    monkeypatch.setenv("CAO_FAKE_WORKER_BLOCKED", "1")
    r = await client.post("/api/tasks", json={**_task_body(git_repo), "start": False})
    tid = r.json()["id"]
    assert (await client.get(f"/api/tasks/{tid}")).json()["status"] == "pending"
    assert (await client.post(f"/api/tasks/{tid}/start")).status_code == 200
    for _ in range(100):
        data = (await client.get(f"/api/tasks/{tid}")).json()
        if not data["running"]:
            break
        await asyncio.sleep(0.05)
    assert data["status"] == "failed" and "blocked" in data["error"]
    # a failed task can be re-run (fresh log)
    monkeypatch.delenv("CAO_FAKE_WORKER_BLOCKED")
    monkeypatch.setenv("CAO_FAKE_SCORES", "10")
    assert (await client.post(f"/api/tasks/{tid}/start")).status_code == 200
    for _ in range(100):
        data = (await client.get(f"/api/tasks/{tid}")).json()
        if not data["running"]:
            break
        await asyncio.sleep(0.05)
    assert data["status"] == "passed"


async def test_browse(client, tmp_path, git_repo):
    r = await client.get("/api/browse", params={"path": str(tmp_path)})
    assert r.status_code == 200
    d = r.json()
    assert any(x["name"] == "repo" and x["is_git"] for x in d["dirs"])
    assert (await client.get("/api/browse", params={"path": str(tmp_path / "nope")})).status_code == 404


async def test_spa_routes(client):
    for path in ("/", "/new", "/tasks/whatever"):
        r = await client.get(path)
        assert r.status_code == 200 and "<title>cao" in r.text


async def test_auth_token(client, monkeypatch):
    monkeypatch.setenv("CAO_AUTH_TOKEN", "s3cret")
    assert (await client.get("/api/tasks")).status_code == 401
    assert (await client.get("/api/health")).json()["ok"] is True  # liveness probe stays public
    r = await client.get("/")
    assert r.status_code == 401 and "access token" in r.text
    assert (await client.get("/static/app.js")).status_code == 200  # static assets are public
    assert (await client.get("/api/tasks", headers={"Authorization": "Bearer s3cret"})).status_code == 200
    assert (await client.get("/api/tasks", headers={"X-CAO-Token": "nope"})).status_code == 401
    r = await client.get("/login", params={"token": "s3cret"}, follow_redirects=False)
    assert r.status_code == 302 and "cao_token=s3cret" in r.headers["set-cookie"]
    assert (await client.get("/api/tasks", headers={"Cookie": "cao_token=s3cret"})).status_code == 200
    assert (await client.get("/login", params={"token": "bad"})).status_code == 401


def test_free_port():
    p = free_port()
    assert 1024 < p < 65536
    assert free_port(preferred=p) == p
