"""Review contract: criteria, reviewer prompt, response parsing, scoring."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

# (key, title, default weight, what the reviewer must check)
CRITERIA: list[tuple[str, str, float, str]] = [
    ("requirements", "Requirements coverage", 2.0,
     "Does the diff fully satisfy the request and EVERY acceptance criterion? Missing criteria cap this at 4."),
    ("correctness", "Correctness / bug risk", 2.0,
     "Logic errors, edge cases, error handling, concurrency, off-by-one, nil/None handling, type misuse."),
    ("security", "Security", 1.5,
     "Injection, unsafe deserialization, path traversal, secrets in code/logs, auth/authz gaps, unsafe defaults."),
    ("consistency", "Style & architecture consistency", 1.0,
     "Matches the existing code style, naming, module boundaries and patterns of this repository."),
    ("tests", "Test coverage", 1.5,
     "Are the changed behaviours covered by meaningful, deterministic tests? Were they run?"),
    ("minimality", "No unnecessary changes", 0.5,
     "Only changes required by the request; no drive-by refactors, formatting churn, or leftover debug code."),
    ("regression", "Existing behaviour preserved", 1.5,
     "Could the change break existing functionality or callers? Are existing tests still valid?"),
]
CRITERIA_KEYS = [c[0] for c in CRITERIA]
DEFAULT_WEIGHTS = {c[0]: c[2] for c in CRITERIA}
SEVERITIES = ("blocker", "major", "minor", "nit")


@dataclass
class Issue:
    severity: str
    description: str
    file: Optional[str] = None
    line: Optional[int] = None
    suggestion: str = ""


@dataclass
class ReviewResult:
    scores: dict[str, float]  # criterion -> 0..10
    overall_llm: Optional[float]  # reviewer's own overall 0..10
    summary: str
    issues: list[Issue] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    tests_observed: str = ""
    verdict: str = ""  # approve | request_changes
    raw: dict[str, Any] = field(default_factory=dict)

    def weighted_score(self, weights: Optional[dict[str, float]] = None) -> float:
        w = dict(DEFAULT_WEIGHTS)
        w.update({k: float(v) for k, v in (weights or {}).items() if k in w})
        num = sum(self.scores.get(k, 0.0) * w[k] for k in CRITERIA_KEYS if k in self.scores)
        den = sum(w[k] for k in CRITERIA_KEYS if k in self.scores)
        return round(num / den, 2) if den else 0.0

    def final_score(
        self,
        scoring: str = "weighted",
        weights: Optional[dict[str, float]] = None,
        *,
        pass_score: Optional[float] = None,
        respect_verdict: bool = False,
    ) -> float:
        if scoring == "llm" and self.overall_llm is not None:
            score = float(self.overall_llm)
        else:
            score = self.weighted_score(weights)
        # A blocker can never pass, regardless of the arithmetic.
        if any(i.severity == "blocker" for i in self.issues):
            score = min(score, 6.0)
        # An explicit "request_changes" keeps the loop going even if the arithmetic says pass.
        if respect_verdict and pass_score is not None and self.verdict == "request_changes":
            score = min(score, max(0.0, pass_score - 0.1))
        return round(max(0.0, min(10.0, score)), 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scores": self.scores,
            "overall_llm": self.overall_llm,
            "summary": self.summary,
            "issues": [i.__dict__ for i in self.issues],
            "strengths": self.strengths,
            "tests_observed": self.tests_observed,
            "verdict": self.verdict,
        }


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #

REVIEWER_PROMPT = """\
You are an independent code reviewer in a multi-agent workflow. A DIFFERENT model wrote the change
below. You have READ-ONLY access to the repository checkout in your working directory: use it to check
style, architecture, callers, and existing tests -- but evaluate ONLY the diff shown here. Do not trust
any summary; the diff is the only statement of what was done. Do not modify files.

## Task given to the worker
{request}

## Acceptance criteria
{criteria}

## Iteration
This is iteration {iteration} of at most {max_iterations}.{previous_note}

## Diff to review (git diff {base}..HEAD)
```diff
{diff}
```

## Scoring rubric (score each 0-10; 10 = flawless, 7 = acceptable with minor nits, <=4 = must fix)
{rubric}

## Output format
Respond with ONLY a JSON object (no markdown fences, no prose before or after) of this exact shape:
{{
  "scores": {{{score_keys}}},
  "overall": <float 0-10, your holistic judgement>,
  "verdict": "approve" | "request_changes",
  "summary": "<2-4 sentences>",
  "strengths": ["<what is good>", ...],
  "issues": [
    {{"severity": "blocker|major|minor|nit", "file": "<path or null>", "line": <int or null>,
      "description": "<what is wrong and why it matters>", "suggestion": "<concrete fix>"}}
  ],
  "tests_observed": "<which tests exist/were added for this change and whether the diff shows they run>"
}}
Be specific and actionable: every issue must say what to change. Use "blocker" only for bugs, security
holes, or unmet acceptance criteria. An empty diff is an automatic 0 on every criterion.
"""


def build_reviewer_prompt(
    *,
    request: str,
    criteria: list[str],
    diff: str,
    base: str,
    iteration: int,
    max_iterations: int,
    previous_score: Optional[float] = None,
    max_diff_chars: int = 120_000,
) -> str:
    crit = "\n".join(f"- {c}" for c in criteria) if criteria else "- (none given: use the request itself)"
    rubric = "\n".join(f"- **{k}** ({t}): {d}" for k, t, _w, d in CRITERIA)
    score_keys = ", ".join(f'"{k}": <0-10>' for k in CRITERIA_KEYS)
    if len(diff) > max_diff_chars:
        diff = diff[:max_diff_chars] + f"\n... [diff truncated: {len(diff) - max_diff_chars} more characters; inspect the checkout for the rest]"
    prev = f" The previous iteration scored {previous_score:.1f}." if previous_score is not None else ""
    return REVIEWER_PROMPT.format(
        request=request.strip(),
        criteria=crit,
        iteration=iteration,
        max_iterations=max_iterations,
        previous_note=prev,
        base=base,
        diff=diff or "(EMPTY DIFF -- the worker changed nothing)",
        rubric=rubric,
        score_keys=score_keys,
    )


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


class ReviewParseError(ValueError):
    pass


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    candidates = [text]
    candidates += [m.group(1).strip() for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "scores" in obj:
            return obj
    raise ReviewParseError("reviewer output did not contain a JSON object with a 'scores' field")


def _num(v: Any, lo: float = 0.0, hi: float = 10.0) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, f))


def parse_review(text: str) -> ReviewResult:
    data = _extract_json(text)
    raw_scores = data.get("scores") or {}
    if not isinstance(raw_scores, dict):
        raise ReviewParseError("'scores' must be an object")
    scores: dict[str, float] = {}
    for key in CRITERIA_KEYS:
        val = _num(raw_scores.get(key))
        if val is not None:
            scores[key] = val
    missing = [k for k in CRITERIA_KEYS if k not in scores]
    if len(missing) > len(CRITERIA_KEYS) // 2:
        raise ReviewParseError(f"reviewer omitted too many criteria: {', '.join(missing)}")

    issues = []
    for raw in data.get("issues") or []:
        if not isinstance(raw, dict):
            continue
        sev = str(raw.get("severity", "minor")).lower()
        if sev not in SEVERITIES:
            sev = "minor"
        line = raw.get("line")
        issues.append(
            Issue(
                severity=sev,
                description=str(raw.get("description") or raw.get("message") or "").strip(),
                file=(str(raw["file"]) if raw.get("file") else None),
                line=int(line) if isinstance(line, (int, float)) else None,
                suggestion=str(raw.get("suggestion") or "").strip(),
            )
        )
    verdict = str(data.get("verdict") or "").lower()
    if verdict not in ("approve", "request_changes"):
        verdict = ""
    return ReviewResult(
        scores=scores,
        overall_llm=_num(data.get("overall")),
        summary=str(data.get("summary") or "").strip(),
        issues=[i for i in issues if i.description],
        strengths=[str(s) for s in (data.get("strengths") or []) if str(s).strip()],
        tests_observed=str(data.get("tests_observed") or "").strip(),
        verdict=verdict,
        raw=data,
    )


def feedback_for_worker(review: ReviewResult, score: float, pass_score: float) -> str:
    """Render the previous review as a brief the next worker iteration must address."""
    lines = [f"The previous iteration scored {score:.1f}/10 (pass threshold {pass_score:.1f}). Reviewer summary: {review.summary}"]
    weak = sorted(((k, v) for k, v in review.scores.items() if v < 8), key=lambda kv: kv[1])
    if weak:
        titles = {c[0]: c[1] for c in CRITERIA}
        lines.append("Weakest criteria: " + ", ".join(f"{titles.get(k, k)}={v:.0f}" for k, v in weak))
    if review.issues:
        lines.append("Issues you MUST address (in priority order):")
        order = {s: i for i, s in enumerate(SEVERITIES)}
        for n, issue in enumerate(sorted(review.issues, key=lambda i: order.get(i.severity, 9)), 1):
            loc = f" [{issue.file}{':' + str(issue.line) if issue.line else ''}]" if issue.file else ""
            sugg = f" -> {issue.suggestion}" if issue.suggestion else ""
            lines.append(f"  {n}. ({issue.severity}){loc} {issue.description}{sugg}")
    if review.tests_observed:
        lines.append(f"Reviewer's note on tests: {review.tests_observed}")
    return "\n".join(lines)
