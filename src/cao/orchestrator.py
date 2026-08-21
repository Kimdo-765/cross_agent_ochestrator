"""Strategy execution: parallel, pipeline, plan."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import prompts
from .adapters import make_adapter
from .config import Config, WorkflowSpec
from .models import Isolation, RunReport, Strategy, Task, TaskResult
from .runner import Workspace, prepare_workspace, worktree_diffstat

Listener = Callable[[str], None]


class Orchestrator:
    def __init__(
        self,
        config: Config,
        project_dir: Path,
        *,
        run_root: Optional[Path] = None,
        listener: Optional[Listener] = None,
        max_concurrency: int = 4,
    ):
        self.config = config
        self.project_dir = project_dir.resolve()
        self.run_root = (run_root or self.project_dir / ".cao" / "runs").resolve()
        self.listener = listener or (lambda msg: None)
        self.sem = asyncio.Semaphore(max_concurrency)
        self._diffstats: dict[str, str] = {}

    # ---- public -----------------------------------------------------------

    async def run(self, goal: str, wf: WorkflowSpec) -> RunReport:
        report = RunReport(goal=goal, workflow=wf.name, strategy=wf.strategy)
        report.run_dir = self.run_root / report.run_id
        report.run_dir.mkdir(parents=True, exist_ok=True)
        (report.run_dir / "goal.md").write_text(goal, encoding="utf-8")
        self.log(f"run {report.run_id}: workflow '{wf.name}' ({wf.strategy.value}) -> {report.run_dir}")

        try:
            if wf.strategy is Strategy.PARALLEL:
                await self._run_parallel(goal, wf, report)
            elif wf.strategy is Strategy.PIPELINE:
                await self._run_pipeline(goal, wf, report)
            elif wf.strategy is Strategy.PLAN:
                await self._run_plan(goal, wf, report)
            else:  # pragma: no cover
                raise ValueError(f"unsupported strategy {wf.strategy}")
        finally:
            report.finished_at = time.time()
        return report

    # ---- strategies ---------------------------------------------------------

    async def _run_parallel(self, goal: str, wf: WorkflowSpec, report: RunReport) -> None:
        isolation = self._isolation(wf)
        tasks = [
            Task(prompt=_fill(wf.prompt, goal=goal), agent=name, title=f"{name}: {goal[:40]}", isolation=isolation)
            for name in wf.agents
        ]
        results = await asyncio.gather(*(self._execute(t, report) for t in tasks))
        report.results.extend(results)
        await self._synthesize(
            report,
            wf,
            prompts.SYNTHESIS_PARALLEL_PROMPT.format(goal=goal, answers=prompts.format_answers(results)),
        )

    async def _run_pipeline(self, goal: str, wf: WorkflowSpec, report: RunReport) -> None:
        isolation = self._isolation(wf)
        previous = ""
        for i, step in enumerate(wf.steps, 1):
            agent = str(step["agent"])
            prompt = _fill(str(step.get("prompt", "{goal}")), goal=goal, previous=previous)
            task = Task(
                prompt=prompt, agent=agent, title=step.get("title") or f"step {i}: {agent}", isolation=isolation,
                context={"step": i},
            )
            result = await self._execute(task, report)
            report.results.append(result)
            previous = result.output
            if not result.ok and not step.get("continue_on_error", False):
                self.log(f"pipeline stopped at step {i} ({agent}): {result.error}")
                break
        report.synthesis = previous  # last step's output is the pipeline's answer

    async def _run_plan(self, goal: str, wf: WorkflowSpec, report: RunReport) -> None:
        assert wf.planner
        workers = [self.config.agent(w) for w in wf.workers]
        plan_task = Task(
            prompt=prompts.PLANNER_PROMPT.format(
                max_tasks=wf.max_tasks, workers=prompts.describe_workers(workers), goal=goal
            ),
            agent=wf.planner,
            title="plan",
            isolation=Isolation.SHARED if self._isolation(wf) is not Isolation.NONE else Isolation.NONE,
        )
        plan_result = await self._execute(plan_task, report, role="planner")
        report.results.append(plan_result)
        if not plan_result.ok:
            self.log(f"planner failed: {plan_result.error}")
            return

        plan = parse_plan(plan_result.output, allowed_agents=set(wf.workers), max_tasks=wf.max_tasks)
        if not plan:
            self.log("planner returned no parseable tasks; see logs")
            plan_result.ok = False
            plan_result.error = "planner output was not a JSON task list"
            return
        report.plan = plan
        (report.run_dir / "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
        self.log(f"plan: {len(plan)} task(s) -> " + ", ".join(f"{p['agent']}:{p['title']}" for p in plan))

        isolation = self._isolation(wf)
        tasks = [
            Task(prompt=p["prompt"], agent=p["agent"], title=p["title"], isolation=isolation, context={"index": i})
            for i, p in enumerate(plan, 1)
        ]
        results = await asyncio.gather(*(self._execute(t, report, role="worker") for t in tasks))
        report.results.extend(results)
        await self._synthesize(
            report,
            wf,
            prompts.SYNTHESIS_PLAN_PROMPT.format(
                goal=goal,
                plan=prompts.format_plan(plan),
                results=prompts.format_worker_results(results, self._diffstats),
            ),
        )

    # ---- helpers ------------------------------------------------------------

    async def _synthesize(self, report: RunReport, wf: WorkflowSpec, prompt: str) -> None:
        name = wf.synthesizer or self.config.default_synthesizer
        if not name:
            self.log("no synthesizer configured; skipping synthesis")
            return
        task = Task(prompt=prompt, agent=name, title="synthesis", isolation=Isolation.SHARED)
        result = await self._execute(task, report, role="synthesizer")
        report.results.append(result)
        report.synthesis = result.output if result.ok else None

    async def _execute(self, task: Task, report: RunReport, role: str = "agent") -> TaskResult:
        spec = self.config.agent(task.agent)
        adapter = make_adapter(spec)
        assert report.run_dir is not None
        async with self.sem:
            try:
                ws = prepare_workspace(self.project_dir, task.isolation, report.run_id, task.id, task.short_title())
            except Exception as exc:
                return TaskResult(task=task, agent=task.agent, ok=False, output="", error=f"workspace: {exc}")
            self.log(f"▶ {role} {task.agent} [{task.id}] {task.short_title()}" + (f" (branch {ws.branch})" if ws.branch else ""))
            try:
                result = await adapter.run(task, ws.path, report.run_dir)
            finally:
                if ws.isolation is Isolation.WORKTREE:
                    self._diffstats[task.id] = worktree_diffstat(ws)
                ws.cleanup()
            result.branch = ws.branch
            self.log(f"■ {result.summary_line()}" + (f" -- {result.error}" if result.error else ""))
            return result

    def _isolation(self, wf: WorkflowSpec) -> Isolation:
        return wf.isolation or self.config.default_isolation

    def log(self, msg: str) -> None:
        self.listener(msg)


# --------------------------------------------------------------------------- #


def _fill(template: str, **values: str) -> str:
    """``str.format`` that leaves unknown ``{placeholders}`` and literal braces alone."""

    def sub(m: re.Match) -> str:
        key = m.group(1)
        return values[key] if key in values else m.group(0)

    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", sub, template)


def parse_plan(text: str, *, allowed_agents: set[str], max_tasks: int) -> list[dict[str, Any]]:
    """Extract a JSON task list from planner output (tolerates fences / prose)."""
    candidates = []
    stripped = text.strip()
    candidates.append(stripped)
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S):
        candidates.append(m.group(1).strip())
    start, end = stripped.find("["), stripped.rfind("]")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])

    data: Any = None
    for c in candidates:
        try:
            data = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("tasks"), list):
            data = data["tasks"]
        if isinstance(data, list):
            break
        data = None
    if not isinstance(data, list):
        return []

    agents = sorted(allowed_agents)
    plan: list[dict[str, Any]] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict) or not str(item.get("prompt", "")).strip():
            continue
        agent = str(item.get("agent", "")).strip()
        if agent not in allowed_agents:
            agent = agents[len(plan) % len(agents)]  # round-robin fallback
        plan.append(
            {
                "title": str(item.get("title") or f"task {i + 1}").strip(),
                "agent": agent,
                "prompt": str(item["prompt"]).strip(),
            }
        )
        if len(plan) >= max_tasks:
            break
    return plan
