"""Prompt templates used by the planner and synthesizer roles."""

from __future__ import annotations

import json
from typing import Sequence

from .models import AgentSpec, TaskResult

PLANNER_PROMPT = """\
You are the planning stage of a multi-agent orchestrator. Break the GOAL below into
at most {max_tasks} independent subtasks that different coding agents can execute
IN PARALLEL, each in its own copy of the repository. Subtasks must not depend on
each other's results; if the goal is small, return a single task.

Available worker agents (pick the best fit for each task using the tags):
{workers}

Respond with ONLY a JSON array (no prose, no markdown fences) of objects:
  [{{"title": "<short title>", "agent": "<worker name>", "prompt": "<complete, self-contained instructions for that agent>"}}]

Each "prompt" must be self-contained: restate relevant context from the goal,
say exactly which files/areas to touch, and tell the agent to finish with a
concise summary of what it changed.

GOAL:
{goal}
"""

SYNTHESIS_PARALLEL_PROMPT = """\
Several independent agents were given the SAME task. Compare their answers,
then produce one final answer that is at least as good as the best of them:
merge complementary insights, drop mistakes, and resolve disagreements
explicitly (say which agent you sided with and why). Do NOT modify any files;
only write the final answer.

TASK:
{goal}

{answers}

Write the final answer now.
"""

SYNTHESIS_PLAN_PROMPT = """\
You are the integration stage of a multi-agent orchestrator. A planner split the
GOAL into subtasks, each executed by a worker agent. Below are each worker's
summary and (if they edited files) their git branch and diffstat.

Produce a final report with:
  1. Overall status -- is the goal achieved? what is left?
  2. Per-subtask verdict (done / partial / failed) with one-line rationale.
  3. Integration notes: conflicts or overlaps between branches, and the
     recommended merge order (list the branch names).
  4. Follow-up tasks, if any.

Do NOT modify files or merge branches yourself; only write the report.

GOAL:
{goal}

PLAN:
{plan}

WORKER RESULTS:
{results}
"""


def describe_workers(workers: Sequence[AgentSpec]) -> str:
    return "\n".join(f"  - {w.name}: {w.describe()}" for w in workers)


def format_answers(results: Sequence[TaskResult]) -> str:
    chunks = []
    for i, r in enumerate(results, 1):
        status = "" if r.ok else f"  (FAILED: {r.error})"
        body = r.output.strip() or "(no output)"
        chunks.append(f"=== Answer {i}: agent '{r.agent}'{status} ===\n{body}\n")
    return "\n".join(chunks)


def format_plan(plan: Sequence[dict]) -> str:
    return json.dumps(list(plan), indent=2, ensure_ascii=False)


def format_worker_results(results: Sequence[TaskResult], diffstats: dict[str, str]) -> str:
    chunks = []
    for r in results:
        head = f"--- task {r.task.id} '{r.task.short_title()}' -> agent '{r.agent}' [{'ok' if r.ok else 'FAILED'}]"
        if r.branch:
            head += f"\nbranch: {r.branch}"
        if r.error:
            head += f"\nerror: {r.error}"
        ds = diffstats.get(r.task.id, "")
        if ds:
            head += f"\ndiffstat:\n{ds}"
        chunks.append(f"{head}\nsummary:\n{r.output.strip() or '(no output)'}\n")
    return "\n".join(chunks)
