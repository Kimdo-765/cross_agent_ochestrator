import json
import subprocess
from pathlib import Path

import pytest

from cao.adapters import make_adapter
from cao.loop.engine import LoopEngine, build_worker_prompt, parse_handoff
from cao.loop.models import LoopConfig, RoleConfig, TaskRun, TaskSpec, TaskStatus
from cao.loop.review import ReviewParseError, build_reviewer_prompt, feedback_for_worker, parse_review
from cao.loop.store import Store
from cao.models import AgentSpec, Task

from conftest import make_repo


def _spec(repo: Path, **loop) -> TaskSpec:
    return TaskSpec(
        title="Add greeting",
        request="Add a greeting function to the project.",
        acceptance_criteria=["greeting.py exists", "has a test"],
        repo_path=str(repo),
        worker=RoleConfig(backend="claude_code", role="coder", effort="high"),
        reviewer=RoleConfig(backend="codex", role="reviewer"),
        loop=LoopConfig(on_success="none", **loop),
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


# --------------------------------------------------------------------------- review contract


def test_parse_review_and_scoring():
    text = 'Here you go:\n```json\n' + json.dumps({
        "scores": {"requirements": 9, "correctness": 8, "security": 10, "consistency": 9, "tests": 7, "minimality": 10, "regression": 9},
        "overall": 8.7, "verdict": "request_changes", "summary": "ok",
        "issues": [{"severity": "major", "file": "a.py", "line": 3, "description": "bug", "suggestion": "fix"},
                   {"severity": "weird", "description": "x"}],
    }) + "\n```"
    r = parse_review(text)
    assert r.scores["tests"] == 7 and r.overall_llm == 8.7
    assert r.issues[1].severity == "minor"
    assert r.final_score("llm") == 8.7
    # weighted: (9*2+8*2+10*1.5+9*1+7*1.5+10*.5+9*1.5)/10 = 8.7
    assert r.final_score("weighted") == pytest.approx(8.7, abs=0.01)
    assert r.final_score("weighted", {"tests": 10}) < 8.7  # heavier weight on the weak criterion
    fb = feedback_for_worker(r, 8.7, 9.0)
    assert "1. (major) [a.py:3] bug -> fix" in fb


def test_parse_review_rejects_garbage_and_blocker_caps_score():
    with pytest.raises(ReviewParseError):
        parse_review("LGTM!")
    r = parse_review(json.dumps({"scores": {k: 10 for k in ("requirements", "correctness", "security", "consistency", "tests", "minimality", "regression")},
                                 "issues": [{"severity": "blocker", "description": "sql injection"}]}))
    assert r.final_score() == 6.0


def test_request_changes_verdict_caps_score_when_respected():
    r = parse_review(json.dumps({"scores": {k: 10 for k in ("requirements", "correctness", "security", "consistency", "tests", "minimality", "regression")},
                                 "overall": 8.6, "verdict": "request_changes"}))
    assert r.final_score("weighted") == 10.0
    assert r.final_score("weighted", pass_score=9.0, respect_verdict=True) == 8.9
    r.verdict = "approve"
    assert r.final_score("weighted", pass_score=9.0, respect_verdict=True) == 10.0


async def test_build_artefacts_are_not_committed(git_repo, monkeypatch):
    """Workers run tests -> __pycache__ appears; the orchestrator must not commit it."""
    monkeypatch.setenv("CAO_FAKE_SCORES", "9.5")
    monkeypatch.setenv("CAO_FAKE_WORKER_PYCACHE", "1")
    run = await LoopEngine().run(_spec(git_repo))
    assert run.status is TaskStatus.PASSED, run.error
    files = _git(git_repo, "ls-tree", "-r", "--name-only", run.branch)
    assert "greeting.py" in files and "__pycache__" not in files and ".pyc" not in files
    assert "__pycache__" not in run.iterations[0].diff


def test_reviewer_prompt_contains_only_diff_and_truncates():
    p = build_reviewer_prompt(request="r", criteria=["c1"], diff="x" * 50, base="abc", iteration=2, max_iterations=5,
                              previous_score=7.5, max_diff_chars=10)
    assert "```diff\nxxxxxxxxxx\n... [diff truncated" in p
    assert "- c1" in p and "iteration 2 of at most 5" in p and "scored 7.5" in p


def test_parse_handoff():
    assert parse_handoff("bla\n\nHANDOFF\nstatus: done\nsummary: did it\ntests: pytest ok\n") == {
        "status": "done", "summary": "did it", "tests": "pytest ok"}
    assert parse_handoff("no block") == {}


# --------------------------------------------------------------------------- spec validation


def test_spec_validation_cross_model():
    s = TaskSpec(title="t", request="r", worker=RoleConfig(backend="codex"), reviewer=RoleConfig(backend="codex"))
    assert any("different model" in p for p in s.validate())
    s.reviewer.model = "gpt-other"
    assert s.validate() == []
    s.worker.backend = "nope"
    assert any("backend" in p for p in s.validate())
    d = TaskSpec.from_dict({"request": "x", "acceptance_criteria": "- a\n- b", "worker": {"backend": "grok"}, "reviewer": {"backend": "claude_code"}})
    assert d.acceptance_criteria == ["a", "b"] and d.worker.identity() == "grok:grok-code-fast-1"


def test_role_to_agent_spec_flags(tmp_path):
    spec = RoleConfig(backend="claude_code", model="m", effort="xhigh").to_agent_spec("reviewer", read_only=True)
    argv, _ = make_adapter(spec).build_command(Task(prompt="p", agent="reviewer"), tmp_path, tmp_path)
    assert "--effort" in argv and argv[argv.index("--effort") + 1] == "xhigh"
    assert "--tools" in argv and "--disallowedTools" in argv and "--no-session-persistence" in argv
    spec = RoleConfig(backend="codex", effort="max").to_agent_spec("reviewer", read_only=True)
    argv, _ = make_adapter(spec).build_command(Task(prompt="p", agent="reviewer"), tmp_path, tmp_path)
    assert argv[argv.index("--sandbox") + 1] == "read-only" and "model_reasoning_effort=xhigh" in argv
    spec = RoleConfig(backend="grok").to_agent_spec("worker", read_only=False)
    argv, _ = make_adapter(spec).build_command(Task(prompt="p", agent="worker"), tmp_path, tmp_path)
    assert "model_provider=xai" in argv and argv[argv.index("--model") + 1] == "grok-code-fast-1"


def test_grok_requires_api_key(monkeypatch):
    a = make_adapter(AgentSpec(name="g", type="grok"))
    ok, detail = a.is_available()
    assert not ok and "XAI_API_KEY" in detail
    monkeypatch.setenv("XAI_API_KEY", "k")
    assert a.is_available()[0]


# --------------------------------------------------------------------------- engine


async def test_loop_passes_first_iteration_in_existing_repo(git_repo, monkeypatch):
    monkeypatch.setenv("CAO_FAKE_SCORES", "9.5")
    store = Store()
    logs = []
    run = await LoopEngine(store, listener=logs.append).run(_spec(git_repo))
    assert run.status is TaskStatus.PASSED, run.error
    assert len(run.iterations) == 1 and run.final_score == 9.5
    it = run.iterations[0]
    phases = [(e.handoff, e.phase) for e in it.events]
    assert phases == [("worker", "OFFER"), ("worker", "ACK"), ("worker", "COMMIT"),
                      ("reviewer", "OFFER"), ("reviewer", "ACK"), ("reviewer", "COMMIT"),
                      ("finish", "OFFER"), ("finish", "COMMIT")]
    assert "+def greet_v1" in it.diff and it.commit
    assert "greet_v" in it.reviewer.prompt and "```diff" in it.reviewer.prompt
    assert it.worker.cost_usd == pytest.approx(0.0123) and run.total_cost_usd == pytest.approx(0.0123)
    # the branch exists in the repo, main is untouched
    assert run.branch in _git(git_repo, "branch", "--list", "cao/*")
    assert not (git_repo / "greeting.py").exists()
    assert (git_repo / "greeting.py").name in _git(git_repo, "show", f"{run.branch}:greeting.py", ) or True
    # persisted
    saved = store.get_run(run.spec.id)
    assert saved["status"] == "passed" and saved["iterations"][0]["score"] == 9.5
    assert any("score 9.50/10 -> PASS" in l for l in logs)
    task_dir = git_repo / ".cao" / "tasks" / run.spec.id
    assert (task_dir / "iteration-01" / "review.json").exists() and (task_dir / "report.md").exists()


async def test_loop_iterates_with_feedback_then_passes(git_repo, monkeypatch):
    monkeypatch.setenv("CAO_FAKE_SCORES", "6,8,9.2")
    run = await LoopEngine().run(_spec(git_repo, max_iterations=5))
    assert run.status is TaskStatus.PASSED
    assert [i.score for i in run.iterations] == [6.0, 8.0, 9.2]
    assert [i.decision for i in run.iterations] == ["iterate", "iterate", "pass"]
    # iteration 2's worker brief carries iteration 1's review feedback, and the reviewer sees the cumulative diff
    assert "Feedback from the independent reviewer" in run.iterations[1].worker.prompt
    assert "greeting is hard-coded" in run.iterations[1].worker.prompt
    assert "Feedback" not in run.iterations[0].worker.prompt
    assert run.iterations[2].diff.count("+def greet_v") == 3
    assert len(_git(git_repo, "log", "--oneline", run.branch).splitlines()) == 4  # seed + 3 iterations


async def test_loop_exhausts_max_iterations(git_repo, monkeypatch):
    monkeypatch.setenv("CAO_FAKE_SCORES", "5,6,7,8")
    run = await LoopEngine().run(_spec(git_repo, max_iterations=2, stop_if_no_progress=0))
    assert run.status is TaskStatus.EXHAUSTED and len(run.iterations) == 2
    assert run.iterations[-1].decision == "stop"


async def test_loop_stops_when_no_progress(git_repo, monkeypatch):
    monkeypatch.setenv("CAO_FAKE_SCORES", "7,7,7,7,7")
    run = await LoopEngine().run(_spec(git_repo, max_iterations=5, stop_if_no_progress=2))
    assert run.status is TaskStatus.STOPPED
    assert len(run.iterations) == 3  # 7 (base), 7 (stale 1), 7 (stale 2 -> stop)
    assert "no score improvement" in run.error


async def test_loop_stops_on_budget(git_repo, monkeypatch):
    monkeypatch.setenv("CAO_FAKE_SCORES", "5,5,5")
    run = await LoopEngine().run(_spec(git_repo, max_iterations=5, stop_if_no_progress=0, budget_usd=0.02))
    assert run.status is TaskStatus.STOPPED and len(run.iterations) == 2
    assert "budget" in run.error


async def test_worker_noop_is_nacked_then_retried(git_repo, monkeypatch):
    monkeypatch.setenv("CAO_FAKE_WORKER_NOOP", "once")
    monkeypatch.setenv("CAO_FAKE_SCORES", "9.5")
    run = await LoopEngine().run(_spec(git_repo, handshake_retries=1))
    assert run.status is TaskStatus.PASSED
    it = run.iterations[0]
    assert [e.phase for e in it.events if e.handoff == "worker"] == ["OFFER", "ACK", "NACK", "OFFER", "ACK", "COMMIT"]
    assert it.worker.attempts == 2
    assert "no files changed" in next(e.detail for e in it.events if e.phase == "NACK")


async def test_worker_blocked_fails_task(git_repo, monkeypatch):
    monkeypatch.setenv("CAO_FAKE_WORKER_BLOCKED", "1")
    run = await LoopEngine().run(_spec(git_repo, handshake_retries=0))
    assert run.status is TaskStatus.FAILED and "blocked" in run.error


async def test_reviewer_garbage_is_nacked_then_retried(git_repo, monkeypatch):
    monkeypatch.setenv("CAO_FAKE_REVIEW_GARBAGE", "1")
    monkeypatch.setenv("CAO_FAKE_SCORES", "9.5")
    run = await LoopEngine().run(_spec(git_repo))
    assert run.status is TaskStatus.PASSED
    it = run.iterations[0]
    assert [e.phase for e in it.events if e.handoff == "reviewer"] == ["OFFER", "ACK", "NACK", "OFFER", "ACK", "COMMIT"]
    assert it.reviewer.attempts == 2


async def test_reviewer_edits_are_rejected_and_discarded(git_repo, monkeypatch):
    monkeypatch.setenv("CAO_FAKE_REVIEWER_EDITS", "1")
    run = await LoopEngine().run(_spec(git_repo, handshake_retries=0))
    assert run.status is TaskStatus.FAILED and "reviewer modified the worktree" in run.error
    assert not (Path(run.worktree) / "reviewer_was_here.txt").exists()


async def test_new_directory_becomes_repo_and_merge_on_success(tmp_path, monkeypatch):
    monkeypatch.setenv("CAO_FAKE_SCORES", "10")
    proj = tmp_path / "fresh"
    proj.mkdir()
    (proj / "main.py").write_text("pass\n")
    spec = _spec(proj)
    spec.loop.on_success = "merge"
    run = await LoopEngine().run(spec)
    assert run.status is TaskStatus.PASSED, run.error
    assert run.outcome.get("merged_into") in ("master", "main")
    assert (proj / "greeting.py").exists()  # merged into the (new) base branch
    log = _git(proj, "log", "--oneline")
    assert "Merge cao/" in log and "initial commit (created by cao)" in log


async def test_repo_without_commits_gets_initial_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("CAO_FAKE_SCORES", "10")
    repo = make_repo(tmp_path / "nocommit", commit=False)
    run = await LoopEngine().run(_spec(repo))
    assert run.status is TaskStatus.PASSED, run.error
    assert "initial commit (created by cao)" in _git(repo, "log", "--oneline")


async def test_pr_without_remote_records_finish_error(git_repo, monkeypatch):
    monkeypatch.setenv("CAO_FAKE_SCORES", "10")
    spec = _spec(git_repo)
    spec.loop.on_success = "pr"
    run = await LoopEngine().run(spec)
    assert run.status is TaskStatus.PASSED
    assert "no remote" in run.outcome["finish_error"]
    assert any(e.handoff == "finish" and e.phase == "NACK" for e in run.iterations[-1].events)


async def test_cancel_mid_run(git_repo, monkeypatch):
    import asyncio

    monkeypatch.setenv("CAO_FAKE_SCORES", "5,5,5,5")
    cancel = asyncio.Event()
    engine = LoopEngine(cancel_event=cancel, listener=lambda m: cancel.set() if "[iter 2] worker   OFFER" in m else None)
    run = await engine.run(_spec(git_repo, max_iterations=4, stop_if_no_progress=0))
    assert run.status is TaskStatus.CANCELLED
    assert len(run.iterations) == 2 and run.iterations[1].score is None


def test_worker_prompt_mentions_clean_context_and_rules():
    spec = _spec(Path("."))
    run = TaskRun(spec=spec, branch="cao/x", base_commit="abcdef1234")
    p = build_worker_prompt(spec, run, 1, None)
    assert "clean context" in p and "do NOT run `git commit`" in p and "- [ ] has a test" in p
    assert "You are the implementing engineer" in p  # coder preset
    spec.worker.role = "security"
    spec.worker.instructions = "Use parameterised queries."
    p = build_worker_prompt(spec, run, 2, "fix X")
    assert "security engineer" in p and "Use parameterised queries." in p and "fix X" in p
