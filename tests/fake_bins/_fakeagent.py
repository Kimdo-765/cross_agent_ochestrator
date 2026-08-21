"""Shared behaviour for the fake agent CLIs used in tests.

Environment knobs:
  CAO_FAKE_SCORES        comma-separated per-iteration scores for the reviewer (default "9.5")
  CAO_FAKE_WORKER_NOOP   "1" -> worker changes nothing (exercises the NACK path); "once" -> only first call
  CAO_FAKE_WORKER_BLOCKED "1" -> worker reports status: blocked
  CAO_FAKE_REVIEW_GARBAGE "1" -> reviewer returns non-JSON once (exercises reviewer NACK + retry)
  CAO_FAKE_REVIEWER_EDITS "1" -> reviewer modifies the worktree (must be rejected)
"""

import json
import os
import re
from pathlib import Path

STATE = Path(os.environ.get("CAO_FAKE_STATE", "/tmp/cao-fake-state"))


def _bump(key: str) -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    f = STATE / key
    n = int(f.read_text()) + 1 if f.exists() else 1
    f.write_text(str(n))
    return n


def iteration_of(prompt: str) -> int:
    m = re.search(r"This is iteration (\d+)", prompt)
    return int(m.group(1)) if m else 1


def is_worker_prompt(prompt: str) -> bool:
    return "## Hand-off" in prompt


def is_reviewer_prompt(prompt: str) -> bool:
    return "Scoring rubric" in prompt


def act_as_worker(prompt: str, cwd: str, tag: str) -> str:
    n = iteration_of(prompt)
    noop = os.environ.get("CAO_FAKE_WORKER_NOOP", "")
    calls = _bump("worker_calls")
    if os.environ.get("CAO_FAKE_WORKER_BLOCKED") == "1":
        return "I cannot do this.\n\nHANDOFF\nstatus: blocked\nsummary: missing dependency\ntests: not run\n"
    if noop == "1" or (noop == "once" and calls == 1):
        return "Nothing to change.\n\nHANDOFF\nstatus: done\nsummary: no changes needed\ntests: not run (no changes)\n"
    target = Path(cwd) / "greeting.py"
    prev = target.read_text() if target.exists() else ""
    target.write_text(prev + f"def greet_v{n}():\n    return 'hello from {tag} iteration {n}'\n")
    if os.environ.get("CAO_FAKE_WORKER_PYCACHE") == "1":  # simulate running the tests
        pc = Path(cwd) / "__pycache__"
        pc.mkdir(exist_ok=True)
        (pc / "greeting.cpython-310.pyc").write_bytes(b"\x00bytecode")
        (Path(cwd) / ".pytest_cache").mkdir(exist_ok=True)
        (Path(cwd) / ".pytest_cache" / "v").write_text("x")
    fb = "addressed reviewer feedback" if "Feedback from the independent reviewer" in prompt else "initial implementation"
    return (
        f"Implemented greet_v{n} ({fb}).\n\n"
        "HANDOFF\nstatus: done\n"
        f"summary: added greet_v{n} in greeting.py\n"
        "tests: pytest -q -> 1 passed\n"
    )


def act_as_reviewer(prompt: str, cwd: str, tag: str) -> str:
    n = iteration_of(prompt)
    if os.environ.get("CAO_FAKE_REVIEW_GARBAGE") == "1" and _bump("garbage") == 1:
        return "Looks fine to me, ship it!"
    if os.environ.get("CAO_FAKE_REVIEWER_EDITS") == "1":
        Path(cwd, "reviewer_was_here.txt").write_text("oops")
    scores = [float(s) for s in os.environ.get("CAO_FAKE_SCORES", "9.5").split(",") if s.strip()]
    score = scores[min(n, len(scores)) - 1]
    assert "```diff" in prompt and "greet_v" in prompt, "reviewer must receive the real diff"
    issues = [] if score >= 9 else [
        {"severity": "major", "file": "greeting.py", "line": 1,
         "description": "greeting is hard-coded", "suggestion": "accept a name parameter"}
    ]
    return json.dumps({
        "scores": {k: score for k in ("requirements", "correctness", "security", "consistency", "tests", "minimality", "regression")},
        "overall": score,
        "verdict": "approve" if score >= 9 else "request_changes",
        "summary": f"[{tag}] iteration {n} reviewed; diff had {prompt.count('+def greet_v')} new function(s).",
        "strengths": ["small diff"],
        "issues": issues,
        "tests_observed": "worker claims pytest passed; no test file in diff" if score < 9 else "ok",
    })


def respond(prompt: str, cwd: str, tag: str) -> str:
    if is_worker_prompt(prompt):
        return act_as_worker(prompt, cwd, tag)
    if is_reviewer_prompt(prompt):
        return act_as_reviewer(prompt, cwd, tag)
    return ""
