"""Role presets: what a worker is told to focus on. Users can add/override them."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RolePreset:
    key: str
    title: str
    brief: str  # injected into the worker prompt


WORKER_ROLES: dict[str, RolePreset] = {
    "coder": RolePreset(
        "coder",
        "Coder",
        "You are the implementing engineer. Make the smallest correct change that fully satisfies the "
        "request and every acceptance criterion. Follow the existing code style and architecture. Add or "
        "update tests for the behaviour you change. Do not refactor unrelated code.",
    ),
    "planner": RolePreset(
        "planner",
        "Planner",
        "You are the planning engineer. Before touching code, write a short implementation plan into "
        "PLAN.md at the repository root (goals, affected files, step list, risks, test strategy). Then "
        "implement only the first, foundational step of that plan (interfaces, scaffolding, data model) "
        "so later iterations can build on it. Keep the plan in sync with what you actually did.",
    ),
    "tester": RolePreset(
        "tester",
        "Tester",
        "You are the test engineer. Your priority is coverage of the requested behaviour: write focused, "
        "deterministic tests first (unit tests; integration tests where the repo already has them), then "
        "make the minimal implementation change required for them to pass. Run the test suite before "
        "finishing and report the exact command and result.",
    ),
    "security": RolePreset(
        "security",
        "Security engineer",
        "You are the security engineer. Implement the request with a security-first mindset: validate all "
        "inputs, avoid injection (shell/SQL/path), never log secrets, use safe defaults, least privilege, "
        "and constant-time comparisons where relevant. Point out any pre-existing vulnerabilities you "
        "notice in the files you touch (fix them only if in scope).",
    ),
    "refactorer": RolePreset(
        "refactorer",
        "Refactorer",
        "You are the refactoring engineer. Improve structure, naming, and duplication in the area named by "
        "the request WITHOUT changing observable behaviour. Keep every existing test passing; add "
        "characterisation tests first if the area is untested.",
    ),
    "docs": RolePreset(
        "docs",
        "Documentation writer",
        "You are the technical writer. Produce or update documentation (README, docstrings, usage "
        "examples, CHANGELOG) so the requested behaviour is accurately documented. Verify every command "
        "or example you write actually works in this repository.",
    ),
}


def worker_brief(role_key: str, custom_instructions: str = "") -> str:
    preset = WORKER_ROLES.get(role_key)
    brief = preset.brief if preset else f"You are acting as: {role_key}."
    if custom_instructions.strip():
        brief += "\n\nAdditional instructions from the user:\n" + custom_instructions.strip()
    return brief


def list_roles() -> list[dict[str, str]]:
    return [{"key": r.key, "title": r.title, "brief": r.brief} for r in WORKER_ROLES.values()]
