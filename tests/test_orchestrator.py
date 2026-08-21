import subprocess
from pathlib import Path

from cao.config import parse_config
from cao.models import Isolation, Strategy
from cao.orchestrator import Orchestrator, parse_plan, _fill
from cao.reporting import write_report


def _cfg():
    return parse_config({
        "defaults": {"isolation": "none", "synthesizer": "claude"},
        "agents": {"claude": {"type": "claude_code"}, "codex": {"type": "codex"}},
        "workflows": {
            "compare": {"strategy": "parallel", "agents": ["claude", "codex"], "prompt": "Q: {goal}"},
            "chain": {"strategy": "pipeline", "steps": [
                {"agent": "codex", "prompt": "Do: {goal}"},
                {"agent": "claude", "prompt": "Review: {previous}"},
            ]},
            "build": {"strategy": "plan", "planner": "claude", "workers": ["claude", "codex"], "isolation": "worktree"},
        },
    })


async def test_parallel_with_synthesis(tmp_path):
    cfg = _cfg()
    orch = Orchestrator(cfg, tmp_path, run_root=tmp_path / "runs")
    report = await orch.run("meaning of life", cfg.workflow("compare"))
    assert report.ok
    agents = [r.agent for r in report.results]
    assert agents == ["claude", "codex", "claude"]  # two answers + synthesis
    assert report.results[0].output == "[fake-claude:default] Q: meaning of life"
    assert report.synthesis and "Several independent agents" in report.synthesis
    md = write_report(report)
    assert md.exists() and "Final answer" in md.read_text()


async def test_pipeline_passes_previous(tmp_path):
    cfg = _cfg()
    orch = Orchestrator(cfg, tmp_path, run_root=tmp_path / "runs")
    report = await orch.run("add tests", cfg.workflow("chain"))
    assert report.ok
    assert report.results[0].output == "[fake-codex] Do: add tests"
    assert report.results[1].output == "[fake-claude:default] Review: [fake-codex] Do: add tests"
    assert report.synthesis == report.results[1].output


async def test_pipeline_stops_on_failure(tmp_path):
    cfg = _cfg()
    cfg.workflows["chain"].steps[0]["prompt"] = "FAIL_PLEASE {goal}"
    cfg.workflows["chain"].steps[0]["agent"] = "claude"
    orch = Orchestrator(cfg, tmp_path, run_root=tmp_path / "runs")
    report = await orch.run("x", cfg.workflow("chain"))
    assert not report.ok and len(report.results) == 1


async def test_plan_fans_out_into_worktrees(git_repo: Path):
    cfg = _cfg()
    logs = []
    orch = Orchestrator(cfg, git_repo, listener=logs.append)
    report = await orch.run("ship it", cfg.workflow("build"))
    assert report.ok, [r.error for r in report.results]
    assert [p["agent"] for p in report.plan] == ["claude", "codex"]
    roles = [r.task.title for r in report.results]
    assert roles[0] == "plan" and roles[-1] == "synthesis"
    workers = [r for r in report.results if r.branch]
    assert len(workers) == 2
    branches = subprocess.run(["git", "branch", "--list", "cao/*"], cwd=git_repo, capture_output=True, text=True).stdout
    for w in workers:
        assert w.branch in branches
    assert (git_repo / ".cao" / "runs" / report.run_id / "plan.json").exists()
    assert any("plan: 2 task(s)" in m for m in logs)


async def test_worktree_requires_commit(tmp_path):
    cfg = _cfg()
    cfg.workflows["compare"].isolation = Isolation.WORKTREE
    orch = Orchestrator(cfg, tmp_path, run_root=tmp_path / "runs")
    report = await orch.run("x", cfg.workflow("compare"))
    assert not report.ok
    assert all("workspace" in (r.error or "") for r in report.results[:2])


def test_parse_plan_tolerates_fences_and_bad_agents():
    text = 'Sure! ```json\n[{"title":"a","agent":"nope","prompt":"p1"},{"agent":"codex","prompt":"p2"},{"prompt":""}]\n```'
    plan = parse_plan(text, allowed_agents={"claude", "codex"}, max_tasks=5)
    assert [p["agent"] for p in plan] == ["claude", "codex"]
    assert plan[1]["title"] == "task 2"
    assert parse_plan("no json here", allowed_agents={"claude"}, max_tasks=3) == []
    assert len(parse_plan('{"tasks": [{"prompt": "a"}, {"prompt": "b"}]}', allowed_agents={"c"}, max_tasks=1)) == 1


def test_fill_leaves_unknown_placeholders():
    assert _fill("{goal} / {previous} / {other} / {{lit}}", goal="g", previous="p") == "g / p / {other} / {{lit}}"
