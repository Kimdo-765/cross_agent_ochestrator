"""FastAPI application: task CRUD, start/cancel, live logs (SSE), metadata for the UI."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..adapters import make_adapter
from ..adapters.claude_code import EFFORT_LEVELS
from ..loop.engine import LoopEngine
from ..loop.models import BACKENDS, RoleConfig, TaskRun, TaskSpec, TaskStatus
from ..loop.review import CRITERIA, DEFAULT_WEIGHTS
from ..loop.roles import list_roles
from ..loop.store import Store

STATIC_DIR = Path(__file__).parent / "static"

LOGIN_HTML = """<!doctype html><meta charset="utf-8"><title>cao · sign in</title>
<style>body{font:15px system-ui;background:#0f1115;color:#e6e8ee;display:grid;place-items:center;height:100vh;margin:0}
form{background:#171a21;border:1px solid #2a2f3d;border-radius:10px;padding:24px;width:340px}
input{width:100%;box-sizing:border-box;margin:10px 0;padding:8px;background:#0f1115;color:#e6e8ee;border:1px solid #2a2f3d;border-radius:6px}
button{width:100%;padding:9px;background:#5b9cff;border:0;border-radius:6px;font-weight:600}</style>
<form method="get" action="/login"><h2>⟲ cao</h2><p>This instance requires an access token (shown where <code>cao web</code> was started).</p>
<input name="token" placeholder="access token" autofocus><button>Sign in</button></form>"""

BACKEND_META = {
    "claude_code": {
        "title": "Claude Code",
        "binary": "claude",
        "models": ["", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"],
        "auth": "claude login  (or ANTHROPIC_API_KEY)",
    },
    "codex": {
        "title": "Codex (OpenAI)",
        "binary": "codex",
        "models": ["", "gpt-5-codex", "gpt-5", "o4-mini"],
        "auth": "codex login  (or OPENAI_API_KEY)",
    },
    "grok": {
        "title": "Grok (xAI via Codex CLI)",
        "binary": "codex",
        "models": ["grok-code-fast-1", "grok-4", "grok-4-fast"],
        "auth": "XAI_API_KEY",
    },
}


class TaskManager:
    """Runs loop engines in the background and fans their log lines out to SSE subscribers."""

    def __init__(self, store: Store):
        self.store = store
        self.running: dict[str, asyncio.Task] = {}
        self.cancels: dict[str, asyncio.Event] = {}
        self.subscribers: dict[str, set[asyncio.Queue]] = {}

    # -- pub/sub ------------------------------------------------------------------

    def subscribe(self, task_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.setdefault(task_id, set()).add(q)
        return q

    def unsubscribe(self, task_id: str, q: asyncio.Queue) -> None:
        self.subscribers.get(task_id, set()).discard(q)

    def publish(self, task_id: str, event: str, data: Any) -> None:
        for q in list(self.subscribers.get(task_id, ())):
            q.put_nowait({"event": event, "data": data})

    # -- lifecycle --------------------------------------------------------------------

    def is_running(self, task_id: str) -> bool:
        t = self.running.get(task_id)
        return bool(t and not t.done())

    async def start(self, spec: TaskSpec) -> None:
        if self.is_running(spec.id):
            raise HTTPException(409, "task is already running")
        cancel = asyncio.Event()
        self.cancels[spec.id] = cancel
        loop = asyncio.get_running_loop()

        def listener(line: str) -> None:
            loop.call_soon_threadsafe(self.publish, spec.id, "log", {"line": line})

        engine = LoopEngine(self.store, listener=listener, cancel_event=cancel)

        async def _run() -> None:
            try:
                run = await engine.run(spec)
                self.publish(spec.id, "status", {"status": run.status.value, "final_score": run.final_score})
            except Exception as exc:  # engine.run already catches most; this is a last resort
                self.store.set_status(spec.id, TaskStatus.FAILED, f"{exc.__class__.__name__}: {exc}")
                self.publish(spec.id, "status", {"status": "failed", "error": str(exc)})
            finally:
                self.publish(spec.id, "done", {})

        self.running[spec.id] = asyncio.create_task(_run())

    def cancel(self, task_id: str) -> bool:
        ev = self.cancels.get(task_id)
        if ev and self.is_running(task_id):
            ev.set()
            return True
        return False

    async def shutdown(self) -> None:
        for tid in list(self.running):
            self.cancel(tid)
        for t in list(self.running.values()):
            try:
                await asyncio.wait_for(t, timeout=15)
            except Exception:
                pass


def _backend_status() -> list[dict[str, Any]]:
    out = []
    for key in BACKENDS:
        meta = BACKEND_META[key]
        try:
            adapter = make_adapter(RoleConfig(backend=key).to_agent_spec("probe", read_only=True))
            ok, detail = adapter.is_available()
        except Exception as exc:  # pragma: no cover
            ok, detail = False, str(exc)
        out.append({"key": key, "title": meta["title"], "available": ok, "detail": detail,
                    "models": meta["models"], "auth": meta["auth"]})
    return out


def create_app(store: Optional[Store] = None) -> FastAPI:
    store = store or Store()
    manager = TaskManager(store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await manager.shutdown()

    app = FastAPI(title="cao", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)
    app.state.store = store
    app.state.manager = manager

    # ---- access token (recommended whenever the UI is reachable from outside localhost) ------------
    # Set CAO_AUTH_TOKEN; `cao web --tunnel` generates one automatically. Clients authenticate once via
    # /login?token=... (sets a cookie) or send `Authorization: Bearer <token>` / `X-CAO-Token` headers.

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        token = os.environ.get("CAO_AUTH_TOKEN")
        path = request.url.path
        if not token or path in ("/login", "/api/health") or path.startswith("/static/"):
            return await call_next(request)
        presented = (
            request.cookies.get("cao_token")
            or request.headers.get("x-cao-token")
            or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            or request.query_params.get("token")
        )
        if not presented or not secrets.compare_digest(presented, token):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "unauthorized: provide the access token"}, status_code=401)
            return HTMLResponse(LOGIN_HTML, status_code=401)
        return await call_next(request)

    @app.get("/login", include_in_schema=False)
    def login(token: str = "") -> Any:
        expected = os.environ.get("CAO_AUTH_TOKEN")
        if not expected:
            return RedirectResponse("/", status_code=302)
        if not token or not secrets.compare_digest(token, expected):
            return HTMLResponse(LOGIN_HTML, status_code=401)
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie("cao_token", token, httponly=True, samesite="lax", max_age=30 * 86400)
        return resp

    # ---- meta ---------------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        """Unauthenticated liveness probe (Docker healthcheck); exposes nothing sensitive."""
        return {"ok": True, "version": __version__}

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        workspace = os.environ.get("CAO_WORKSPACE") or str(Path.cwd())
        return {
            "version": __version__,
            "backends": _backend_status(),
            "efforts": list(EFFORT_LEVELS),
            "roles": list_roles(),
            "criteria": [{"key": k, "title": t, "weight": w, "description": d} for k, t, w, d in CRITERIA],
            "default_weights": DEFAULT_WEIGHTS,
            "workspace": workspace,
            "gh_available": shutil.which("gh") is not None,
            "cloudflared_available": shutil.which("cloudflared") is not None,
            "tunnel_url": os.environ.get("CAO_TUNNEL_URL"),
            "defaults": {
                "worker": {"backend": "claude_code", "effort": "high", "role": "coder"},
                "reviewer": {"backend": "codex", "effort": "high"},
                "loop": {"max_iterations": 5, "pass_score": 9.0, "scoring": "weighted", "stop_if_no_progress": 2,
                         "on_success": "pr", "handshake_retries": 1},
            },
        }

    @app.get("/api/browse")
    def browse(path: str = "") -> dict[str, Any]:
        """List sub-directories so the UI can pick a repository path."""
        base = Path(path or os.environ.get("CAO_WORKSPACE") or Path.cwd()).expanduser()
        if not base.is_dir():
            raise HTTPException(404, f"{base} is not a directory")
        dirs = sorted(
            [p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")], key=lambda p: p.name.lower()
        )[:300]
        return {
            "path": str(base.resolve()),
            "parent": str(base.resolve().parent),
            "is_git": (base / ".git").exists(),
            "dirs": [{"name": d.name, "path": str(d.resolve()), "is_git": (d / ".git").exists()} for d in dirs],
        }

    # ---- tasks ----------------------------------------------------------------------

    @app.get("/api/tasks")
    def list_tasks(limit: int = 100) -> list[dict[str, Any]]:
        rows = store.list_runs(limit=limit)
        for r in rows:
            r["running"] = manager.is_running(r["id"])
        return rows

    @app.post("/api/tasks", status_code=201)
    async def create_task(request: Request) -> dict[str, Any]:
        body = await request.json()
        start = bool(body.pop("start", True))
        try:
            spec = TaskSpec.from_dict(body)
        except Exception as exc:
            raise HTTPException(400, f"bad task: {exc}")
        spec.repo_path = str(Path(spec.repo_path).expanduser())
        problems = spec.validate()
        if problems:
            raise HTTPException(422, {"problems": problems})
        run = TaskRun(spec=spec)
        store.save_run(run)
        if start:
            await manager.start(spec)
        return {"id": spec.id, "started": start}

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, Any]:
        data = store.get_run(task_id)
        if not data:
            raise HTTPException(404, "no such task")
        data["running"] = manager.is_running(task_id)
        return data

    @app.post("/api/tasks/{task_id}/start")
    async def start_task(task_id: str) -> dict[str, Any]:
        data = store.get_run(task_id)
        if not data:
            raise HTTPException(404, "no such task")
        if data["status"] not in ("pending",) and not _rerunnable(data["status"]):
            raise HTTPException(409, f"task is {data['status']}")
        spec = TaskSpec.from_dict(data["spec"])
        store.clear_logs(task_id)  # a re-run starts a fresh log (the branch/worktree is recreated too)
        await manager.start(spec)
        return {"id": task_id, "started": True}

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str) -> dict[str, Any]:
        if not manager.cancel(task_id):
            raise HTTPException(409, "task is not running")
        return {"id": task_id, "cancelling": True}

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: str) -> dict[str, Any]:
        if manager.is_running(task_id):
            raise HTTPException(409, "cancel the task before deleting it")
        store.delete_run(task_id)
        return {"id": task_id, "deleted": True}

    @app.post("/api/tasks/{task_id}/clone", status_code=201)
    async def clone_task(task_id: str, request: Request) -> dict[str, Any]:
        """New task with the same spec (fresh id), optional overrides in the body."""
        data = store.get_run(task_id)
        if not data:
            raise HTTPException(404, "no such task")
        spec_d = dict(data["spec"])
        spec_d.pop("id", None)
        spec_d.pop("created_at", None)
        try:
            overrides = await request.json()
        except Exception:
            overrides = {}
        start = bool(overrides.pop("start", True))
        spec_d.update(overrides or {})
        spec = TaskSpec.from_dict(spec_d)
        problems = spec.validate()
        if problems:
            raise HTTPException(422, {"problems": problems})
        store.save_run(TaskRun(spec=spec))
        if start:
            await manager.start(spec)
        return {"id": spec.id, "started": start}

    # ---- logs / diffs ----------------------------------------------------------------

    @app.get("/api/tasks/{task_id}/logs")
    def task_logs(task_id: str, after: int = 0) -> list[dict[str, Any]]:
        return list(store.logs(task_id, after_seq=after))

    @app.get("/api/tasks/{task_id}/iterations/{number}/diff", response_class=PlainTextResponse)
    def iteration_diff(task_id: str, number: int) -> str:
        data = store.get_run(task_id)
        if not data:
            raise HTTPException(404, "no such task")
        for it in data.get("iterations", []):
            if it["number"] == number:
                return it.get("diff") or ""
        raise HTTPException(404, "no such iteration")

    @app.get("/api/tasks/{task_id}/events")
    async def task_events(task_id: str, request: Request, after: int = 0) -> StreamingResponse:
        """Server-sent events: replays stored log lines, then streams live ones until the task finishes."""
        if not store.get_run(task_id):
            raise HTTPException(404, "no such task")

        async def gen():
            q = manager.subscribe(task_id)
            try:
                last = after
                for row in store.logs(task_id, after_seq=after):
                    last = row["seq"]
                    yield _sse("log", {"line": row["line"], "seq": row["seq"]})
                data = store.get_run(task_id) or {}
                yield _sse("status", {"status": data.get("status"), "final_score": data.get("final_score")})
                if not manager.is_running(task_id):
                    yield _sse("done", {})
                    return
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield _sse(msg["event"], msg["data"])
                    if msg["event"] == "done":
                        return
            finally:
                manager.unsubscribe(task_id, q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- static UI -----------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/new", include_in_schema=False)
    def new_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/tasks/{task_id}", include_in_schema=False)
    def task_page(task_id: str) -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


def _rerunnable(status: str) -> bool:
    return status in ("failed", "cancelled", "stopped", "exhausted")


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
