"""Persist a RunReport as markdown + json inside the run directory."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import RunReport, TaskResult


def _result_dict(r: TaskResult) -> dict[str, Any]:
    d = asdict(r)
    d["task"]["isolation"] = r.task.isolation.value
    d["workdir"] = str(r.workdir) if r.workdir else None
    d.pop("stdout", None)  # logs/ has the full transcript
    d.pop("stderr", None)
    return d


def to_json(report: RunReport) -> dict[str, Any]:
    return {
        "run_id": report.run_id,
        "goal": report.goal,
        "workflow": report.workflow,
        "strategy": report.strategy.value,
        "ok": report.ok,
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "duration_s": round(report.duration_s, 1),
        "plan": report.plan,
        "results": [_result_dict(r) for r in report.results],
        "synthesis": report.synthesis,
    }


def to_markdown(report: RunReport) -> str:
    lines = [
        f"# cao run `{report.run_id}`",
        "",
        f"- **workflow**: `{report.workflow}` ({report.strategy.value})",
        f"- **status**: {'✅ ok' if report.ok else '❌ failed'}",
        f"- **duration**: {report.duration_s:.1f}s",
        f"- **started**: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(report.started_at))}",
        "",
        "## Goal",
        "",
        report.goal.strip(),
        "",
    ]
    if report.plan:
        lines += ["## Plan", ""]
        for i, p in enumerate(report.plan, 1):
            lines.append(f"{i}. **{p['title']}** → `{p['agent']}`")
        lines.append("")

    lines += ["## Agent runs", "", "| status | agent | task | time | branch | usage |", "|---|---|---|---|---|---|"]
    for r in report.results:
        usage = ""
        if r.usage:
            cost = r.usage.get("total_cost_usd")
            usage = f"${cost:.4f}" if isinstance(cost, (int, float)) else ",".join(f"{k}={v}" for k, v in list(r.usage.items())[:2] if not isinstance(v, dict))
        lines.append(
            f"| {'ok' if r.ok else 'FAIL'} | `{r.agent}` | {r.task.short_title()} | {r.duration_s:.1f}s | "
            f"{('`' + r.branch + '`') if r.branch else ''} | {usage} |"
        )
    lines.append("")

    for r in report.results:
        lines += [f"### {r.task.short_title()} — `{r.agent}`", ""]
        if r.error:
            lines += [f"> ⚠️ {r.error}", ""]
        lines += [r.output.strip() or "_(no output)_", ""]

    if report.synthesis:
        lines += ["## Final answer", "", report.synthesis.strip(), ""]

    branches = [r.branch for r in report.results if r.branch]
    if branches:
        lines += ["## Branches to review / merge", ""]
        lines += [f"- `{b}`" for b in branches]
        lines += ["", "```sh", *[f"git diff HEAD..{b} --stat" for b in branches], "```", ""]
    return "\n".join(lines)


def write_report(report: RunReport) -> Path:
    assert report.run_dir is not None
    report.run_dir.mkdir(parents=True, exist_ok=True)
    (report.run_dir / "report.json").write_text(json.dumps(to_json(report), indent=2, ensure_ascii=False), encoding="utf-8")
    md = report.run_dir / "report.md"
    md.write_text(to_markdown(report), encoding="utf-8")
    return md
