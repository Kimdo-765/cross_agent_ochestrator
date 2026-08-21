"""The Worker -> Reviewer iteration engine."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ..adapters import make_adapter
from ..models import Task as AgentTask
from . import gitops
from .handshake import Handshake, HandshakeNack, check_available, check_distinct_models, check_nonempty, check_result_ok
from .models import Decision, IterationRecord, StageRecord, TaskRun, TaskSpec, TaskStatus
from .review import ReviewParseError, ReviewResult, build_reviewer_prompt, feedback_for_worker, parse_review
from .roles import worker_brief
from .store import Store

Listener = Callable[[str], None]


class TaskCancelled(Exception):
    pass


WORKER_PROMPT = """\
{role_brief}

## Context
You are one agent in an orchestrated, multi-agent workflow. You start with a clean context: nothing
from earlier iterations is in your memory except what is written here. You are working in a dedicated
git worktree on branch `{branch}` (base: `{base_branch}` @ {base_commit}). Another, different model will
review the ACTUAL `git diff` of your work -- not your description of it -- and score it 0-10 against the
acceptance criteria. The loop continues until the score is >= {pass_score} or {max_iterations} iterations
have been used. This is iteration {iteration}.

Rules:
- Make real file changes in this worktree. The orchestrator commits for you: do NOT run `git commit`,
  `git checkout`, `git push`, or touch other branches.
- Run the project's tests (or the relevant subset) before you finish whenever tests exist.
- Do not leave debug output, TODO placeholders, build artefacts (e.g. __pycache__, dist/), or unrelated
  refactors in the diff -- everything in the worktree that is not ignored will be committed.

## Request
{request}

## Acceptance criteria (the reviewer will check every one of these)
{criteria}
{feedback_section}
## Hand-off
End your reply with this exact block so the orchestrator can record the hand-off:

HANDOFF
status: done | blocked
summary: <what you changed and why, 1-3 sentences>
tests: <command(s) you ran and the result, or "not run" and why>
"""


def _criteria_md(criteria: list[str]) -> str:
    return "\n".join(f"- [ ] {c}" for c in criteria) if criteria else "- (none given: satisfy the request as written)"


def build_worker_prompt(spec: TaskSpec, run: TaskRun, iteration: int, feedback: Optional[str]) -> str:
    fb = ""
    if feedback:
        fb = (
            "\n## Feedback from the independent reviewer on the previous iteration\n"
            "The diff below already contains the previous iteration's work (it is on this branch). "
            "Fix what the reviewer flagged; do not start over unless an issue requires it.\n\n"
            f"{feedback}\n"
        )
    return WORKER_PROMPT.format(
        role_brief=worker_brief(spec.worker.role, spec.worker.instructions),
        branch=run.branch,
        base_branch=spec.base_branch or "HEAD",
        base_commit=(run.base_commit or "")[:10],
        pass_score=spec.loop.pass_score,
        max_iterations=spec.loop.max_iterations,
        iteration=iteration,
        request=spec.request.strip(),
        criteria=_criteria_md(spec.acceptance_criteria),
        feedback_section=fb,
    )


_HANDOFF_RE = re.compile(r"HANDOFF\s*\n(?P<body>(?:.*\n?)*)", re.IGNORECASE)


def parse_handoff(text: str) -> dict[str, str]:
    """Parse the trailing HANDOFF block (lenient: missing block -> empty dict)."""
    idx = text.rfind("HANDOFF")
    if idx == -1:
        return {}
    out: dict[str, str] = {}
    for line in text[idx + len("HANDOFF") :].splitlines():
        m = re.match(r"\s*(status|summary|tests)\s*:\s*(.*)", line, re.IGNORECASE)
        if m:
            out[m.group(1).lower()] = m.group(2).strip()
    return out


def _cost_from(result) -> Optional[float]:
    cost = result.usage.get("total_cost_usd") if result.usage else None
    return float(cost) if isinstance(cost, (int, float)) else None


class LoopEngine:
    def __init__(
        self,
        store: Optional[Store] = None,
        *,
        listener: Optional[Listener] = None,
        cancel_event: Optional[asyncio.Event] = None,
    ):
        self.store = store
        self._listener = listener or (lambda m: None)
        self.cancel_event = cancel_event or asyncio.Event()

    # ---------------------------------------------------------------- public

    async def run(self, spec: TaskSpec) -> TaskRun:
        problems = spec.validate()
        if problems:
            raise ValueError("invalid task: " + "; ".join(problems))

        run = TaskRun(spec=spec, status=TaskStatus.RUNNING, started_at=time.time())
        self._persist(run)
        self.log(run, f"task {spec.id} '{spec.title}' -- worker={spec.worker.identity()} reviewer={spec.reviewer.identity()}")

        try:
            repo = gitops.ensure_repo(Path(spec.repo_path), spec.base_branch)
            if repo.created:
                self.log(run, f"initialised new git repository at {repo.root}")
            elif repo.initial_commit:
                self.log(run, "repository had no commits; created an initial commit")
            ws = gitops.create_task_worktree(repo, spec.id, spec.title)
            run.branch, run.worktree, run.base_commit = ws.branch, str(ws.path), ws.base_commit
            spec.base_branch = repo.base_branch
            self.log(run, f"worktree {ws.path} on branch {ws.branch} (base {repo.base_branch} @ {ws.base_commit[:10]})")
            self._persist(run)

            task_dir = repo.root / ".cao" / "tasks" / spec.id
            task_dir.mkdir(parents=True, exist_ok=True)
            await self._loop(spec, run, ws, task_dir)
            await self._finish(spec, run, ws, task_dir)
        except TaskCancelled:
            run.status = TaskStatus.CANCELLED
            self.log(run, "cancelled")
        except Exception as exc:  # infrastructure failure
            run.status = TaskStatus.FAILED
            run.error = f"{exc.__class__.__name__}: {exc}"
            self.log(run, f"FAILED: {run.error}")
        finally:
            run.finished_at = time.time()
            self._persist(run)
            self.log(run, f"status={run.status.value} score={run.final_score} cost=${run.total_cost_usd:.4f} "
                          f"iterations={len(run.iterations)} duration={run.finished_at - (run.started_at or run.finished_at):.0f}s")
        return run

    # ---------------------------------------------------------------- loop

    async def _loop(self, spec: TaskSpec, run: TaskRun, ws: gitops.TaskWorkspace, task_dir: Path) -> None:
        worker_adapter = make_adapter(spec.worker.to_agent_spec("worker", read_only=False))
        reviewer_adapter = make_adapter(spec.reviewer.to_agent_spec("reviewer", read_only=True))
        feedback: Optional[str] = None
        best_score: Optional[float] = None
        stale = 0

        for n in range(1, spec.loop.max_iterations + 1):
            self._check_cancel()
            it = IterationRecord(number=n)
            run.iterations.append(it)
            it_dir = task_dir / f"iteration-{n:02d}"
            it_dir.mkdir(parents=True, exist_ok=True)
            self._persist(run)

            # ---- worker hand-off -------------------------------------------------
            await self._worker_handoff(spec, run, ws, it, it_dir, worker_adapter, feedback)
            self._persist(run)

            # ---- reviewer hand-off -----------------------------------------------
            review = await self._reviewer_handoff(spec, run, ws, it, it_dir, reviewer_adapter, worker_adapter, best_score)
            score = review.final_score(
                spec.loop.scoring, spec.loop.weights,
                pass_score=spec.loop.pass_score, respect_verdict=spec.loop.respect_verdict,
            )
            it.review, it.score = review.to_dict(), score
            run.final_score = score
            self._persist(run)

            # ---- decision ----------------------------------------------------------
            decision, reason = self._decide(spec, run, score, best_score, stale)
            it.decision = decision.value
            it.finished_at = time.time()
            capped = spec.loop.respect_verdict and review.verdict == "request_changes" and score < review.final_score(spec.loop.scoring, spec.loop.weights)
            self.log(run, f"[iter {n}] score {score:.2f}/10 -> {decision.value.upper()} ({reason})"
                          + (" [capped: reviewer verdict=request_changes]" if capped else ""))
            self._persist(run)

            if decision is Decision.PASS:
                run.status = TaskStatus.PASSED
                return
            if decision is Decision.STOP:
                run.status = TaskStatus.STOPPED if n < spec.loop.max_iterations else TaskStatus.EXHAUSTED
                run.error = reason
                return

            if best_score is None or score > best_score + spec.loop.min_score_delta:
                stale = 0
            else:
                stale += 1
            best_score = score if best_score is None else max(best_score, score)
            feedback = feedback_for_worker(review, score, spec.loop.pass_score)

        run.status = TaskStatus.EXHAUSTED
        run.error = f"max iterations ({spec.loop.max_iterations}) reached; best score {best_score}"

    def _decide(self, spec: TaskSpec, run: TaskRun, score: float, best: Optional[float], stale: int):
        if score >= spec.loop.pass_score:
            return Decision.PASS, f"{score:.2f} >= {spec.loop.pass_score}"
        n = len(run.iterations)
        if n >= spec.loop.max_iterations:
            return Decision.STOP, f"max iterations ({spec.loop.max_iterations}) reached"
        if spec.loop.budget_usd is not None and run.total_cost_usd >= spec.loop.budget_usd:
            return Decision.STOP, f"budget ${spec.loop.budget_usd:.2f} exhausted (spent ${run.total_cost_usd:.2f})"
        improved = best is None or score > best + spec.loop.min_score_delta
        if spec.loop.stop_if_no_progress and not improved and stale + 1 >= spec.loop.stop_if_no_progress:
            return Decision.STOP, f"no score improvement for {stale + 1} iterations"
        return Decision.ITERATE, f"{score:.2f} < {spec.loop.pass_score}"

    # ---------------------------------------------------------------- worker

    async def _worker_handoff(self, spec, run, ws, it, it_dir, adapter, feedback) -> None:
        hs = Handshake(it, "worker", lambda m: self.log(run, m))
        prompt = build_worker_prompt(spec, run, it.number, feedback)
        (it_dir / "worker.prompt.md").write_text(prompt, encoding="utf-8")
        start_commit = gitops.head(ws)
        stage = StageRecord(role="worker", identity=spec.worker.identity(), prompt=prompt)
        it.worker = stage
        note = ""

        for attempt in range(1, spec.loop.handshake_retries + 2):
            self._check_cancel()
            stage.attempts = attempt
            hs.offer(f"brief -> {spec.worker.identity()} (role={spec.worker.role}, attempt {attempt})", chars=len(prompt))
            try:
                hs.require(check_available(adapter), check_nonempty("worker brief", prompt))
                hs.ack("worker ready; workspace clean context")
                result = await self._run_agent(adapter, prompt + note, ws.path, it_dir)
                stage.response = result.output
                stage.ok, stage.error = result.ok, result.error
                stage.duration_s += result.duration_s
                stage.usage = result.usage
                stage.cost_usd = _cost_from(result)
                stage.log_path = str(it_dir / "logs")
                (it_dir / f"worker.response{'' if attempt == 1 else '.' + str(attempt)}.md").write_text(result.output, encoding="utf-8")

                handoff = parse_handoff(result.output)
                diff = gitops.iteration_diff(ws, start_commit)

                hs.require(
                    check_result_ok(result),
                    lambda: "worker reported status: blocked" if handoff.get("status", "").lower().startswith("block") else None,
                    check_nonempty("worker diff (no files changed)", diff),
                    lambda: (
                        "worker did not report running tests (require_tests=true)"
                        if spec.loop.require_tests and (not handoff.get("tests") or handoff["tests"].lower().startswith("not run"))
                        else None
                    ),
                    stage="postcondition",
                )
                sha = gitops.commit_iteration(ws, it.number, handoff.get("summary") or spec.title)
                it.commit = sha
                it.diff = gitops.branch_diff(ws)
                it.diffstat = gitops.branch_diffstat(ws)
                (it_dir / "diff.patch").write_text(it.diff, encoding="utf-8")
                hs.commit(f"committed {sha[:10] if sha else '-'}; {it.diffstat.splitlines()[-1] if it.diffstat else ''}".strip(),
                          commit=sha, handoff=handoff)
                return
            except HandshakeNack as nack:
                stage.error = nack.reason
                if attempt > spec.loop.handshake_retries:
                    raise RuntimeError(f"worker hand-off failed after {attempt} attempt(s): {nack.reason}")
                gitops.discard_uncommitted(ws)
                note = (
                    "\n\n## Orchestrator note\nYour previous attempt was rejected at hand-off: "
                    f"{nack.reason}. Make concrete file changes that satisfy the request, run the tests, and finish with the HANDOFF block."
                )

    # ---------------------------------------------------------------- reviewer

    async def _reviewer_handoff(self, spec, run, ws, it, it_dir, adapter, worker_adapter, prev_score) -> ReviewResult:
        hs = Handshake(it, "reviewer", lambda m: self.log(run, m))
        prompt = build_reviewer_prompt(
            request=spec.request,
            criteria=spec.acceptance_criteria,
            diff=it.diff,
            base=(run.base_commit or "")[:10],
            iteration=it.number,
            max_iterations=spec.loop.max_iterations,
            previous_score=prev_score,
        )
        (it_dir / "review.prompt.md").write_text(prompt, encoding="utf-8")
        stage = StageRecord(role="reviewer", identity=spec.reviewer.identity(), prompt=prompt)
        it.reviewer = stage
        note = ""

        for attempt in range(1, spec.loop.handshake_retries + 2):
            self._check_cancel()
            stage.attempts = attempt
            hs.offer(f"diff ({len(it.diff)} chars) -> {spec.reviewer.identity()} read-only (attempt {attempt})")
            try:
                hs.require(
                    check_available(adapter),
                    check_distinct_models(spec.worker.identity(), spec.reviewer.identity()),
                    check_nonempty("diff", it.diff),
                )
                hs.ack("reviewer ready; diff-only, read-only")
                result = await self._run_agent(adapter, prompt + note, ws.path, it_dir, name="reviewer")
                stage.response = result.output
                stage.ok, stage.error = result.ok, result.error
                stage.duration_s += result.duration_s
                stage.usage = result.usage
                stage.cost_usd = _cost_from(result)
                stage.log_path = str(it_dir / "logs")
                (it_dir / f"review.response{'' if attempt == 1 else '.' + str(attempt)}.md").write_text(result.output, encoding="utf-8")
                hs.require(check_result_ok(result), stage="postcondition")
                try:
                    review = parse_review(result.output)
                except ReviewParseError as exc:
                    raise hs.nack(f"postcondition: {exc}")
                # A reviewer must never have modified the worktree.
                dirty = gitops.git(["status", "--porcelain"], ws.path).stdout.strip()
                if dirty:
                    gitops.discard_uncommitted(ws)
                    raise hs.nack("postcondition: reviewer modified the worktree (changes discarded)")
                (it_dir / "review.json").write_text(json.dumps(review.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
                hs.commit(
                    f"score weighted={review.weighted_score(spec.loop.weights):.2f} llm={review.overall_llm} "
                    f"issues={len(review.issues)} verdict={review.verdict or '-'}",
                    scores=review.scores,
                )
                return review
            except HandshakeNack as nack:
                stage.error = nack.reason
                if attempt > spec.loop.handshake_retries:
                    raise RuntimeError(f"reviewer hand-off failed after {attempt} attempt(s): {nack.reason}")
                note = (
                    "\n\n## Orchestrator note\nYour previous reply was rejected: "
                    f"{nack.reason}. Reply with ONLY the JSON object described above."
                )

    # ---------------------------------------------------------------- finish

    async def _finish(self, spec: TaskSpec, run: TaskRun, ws: gitops.TaskWorkspace, task_dir: Path) -> None:
        hs_it = run.iterations[-1] if run.iterations else IterationRecord(number=0)
        hs = Handshake(hs_it, "finish", lambda m: self.log(run, m))
        summary = self._summary_md(spec, run)
        (task_dir / "report.md").write_text(summary, encoding="utf-8")
        (task_dir / "run.json").write_text(json.dumps(run.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        run.outcome["report"] = str(task_dir / "report.md")
        run.outcome["branch"] = ws.branch

        if run.status is not TaskStatus.PASSED:
            hs.offer(f"task did not pass ({run.status.value}); branch {ws.branch} kept for inspection")
            hs.commit("no merge / PR")
            return

        mode = spec.loop.on_success
        hs.offer(f"on_success={mode} for branch {ws.branch} -> {spec.base_branch}")
        try:
            if mode == "merge":
                sha = gitops.merge_into_base(ws, spec.base_branch or "HEAD", f"Merge {ws.branch}: {spec.title} (cao score {run.final_score})")
                run.outcome["merged_into"] = spec.base_branch
                run.outcome["merge_commit"] = sha
                hs.commit(f"merged into {spec.base_branch} @ {sha[:10]}")
            elif mode == "pr":
                url = gitops.create_pr(ws, spec.base_branch or "HEAD", f"{spec.title} (cao, score {run.final_score})", summary)
                run.outcome["pr_url"] = url
                hs.commit(f"PR opened: {url}")
            else:
                hs.commit("left on branch (on_success=none)")
        except gitops.GitError as exc:
            run.outcome["finish_error"] = str(exc)
            hs.nack(f"{mode} failed: {exc} -- branch {ws.branch} is intact")

    def _summary_md(self, spec: TaskSpec, run: TaskRun) -> str:
        lines = [
            f"# {spec.title}",
            "",
            f"- task: `{spec.id}`  status: **{run.status.value}**  final score: **{run.final_score}** / pass {spec.loop.pass_score}",
            f"- worker: `{spec.worker.identity()}` (role {spec.worker.role}, effort {spec.worker.effort or 'default'})",
            f"- reviewer: `{spec.reviewer.identity()}` (effort {spec.reviewer.effort or 'default'})",
            f"- branch: `{run.branch}`  base: `{spec.base_branch}` @ {(run.base_commit or '')[:10]}",
            f"- iterations: {len(run.iterations)} / {spec.loop.max_iterations}   cost: ${run.total_cost_usd:.4f}   tokens: "
            + (", ".join(f"{k}={int(v)}" for k, v in run.total_usage.items() if 'token' in k) or "n/a"),
            "",
            "## Request",
            "",
            spec.request.strip(),
            "",
            "## Acceptance criteria",
            "",
            *[f"- {c}" for c in spec.acceptance_criteria],
            "",
            "## Iterations",
            "",
            "| # | worker | reviewer | score | decision | cost |",
            "|---|---|---|---|---|---|",
        ]
        for it in run.iterations:
            lines.append(
                f"| {it.number} | {'ok' if it.worker and it.worker.ok else 'fail'} "
                f"| {'ok' if it.reviewer and it.reviewer.ok else 'fail'} | {it.score if it.score is not None else '-'} "
                f"| {it.decision or '-'} | ${it.cost_usd:.4f} |"
            )
        for it in run.iterations:
            if not it.review:
                continue
            lines += ["", f"### Iteration {it.number} review", "", it.review.get("summary", ""), ""]
            lines += [f"- {k}: {v}" for k, v in it.review.get("scores", {}).items()]
            for issue in it.review.get("issues", []):
                loc = f" `{issue['file']}{':' + str(issue['line']) if issue.get('line') else ''}`" if issue.get("file") else ""
                lines.append(f"- **{issue['severity']}**{loc}: {issue['description']}")
        if run.error:
            lines += ["", f"> {run.error}"]
        return "\n".join(lines) + "\n"

    # ---------------------------------------------------------------- plumbing

    async def _run_agent(self, adapter, prompt: str, workdir: Path, it_dir: Path, name: str = "worker"):
        task = AgentTask(prompt=prompt, agent=adapter.spec.name, title=name)
        agent_coro = adapter.run(task, workdir, it_dir)
        runner = asyncio.ensure_future(agent_coro)
        waiter = asyncio.ensure_future(self.cancel_event.wait())
        done, _ = await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if runner in done:
            waiter.cancel()
            return runner.result()
        runner.cancel()
        try:
            await runner
        except (asyncio.CancelledError, Exception):
            pass
        raise TaskCancelled()

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise TaskCancelled()

    def _persist(self, run: TaskRun) -> None:
        if self.store:
            self.store.save_run(run)

    def log(self, run: TaskRun, msg: str) -> None:
        if self.store:
            self.store.append_log(run.spec.id, msg)
        self._listener(msg)
