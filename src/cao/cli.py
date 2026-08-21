"""``cao`` command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .adapters import REGISTRY, make_adapter
from .config import EXAMPLE_CONFIG, Config, ConfigError, WorkflowSpec, find_config, load_config
from .models import Isolation, Strategy
from .orchestrator import Orchestrator
from .reporting import to_json, write_report


def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr, flush=True)


# ---- commands ----------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path or "cao.yaml")
    if target.exists() and not args.force:
        _eprint(f"{target} already exists (use --force to overwrite)")
        return 1
    target.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    print(f"wrote {target}")
    print("next: edit it, then run  cao agents  to verify the CLIs are installed.")
    return 0


def cmd_agents(args: argparse.Namespace, cfg: Config) -> int:
    rows = []
    for spec in cfg.agents.values():
        try:
            adapter = make_adapter(spec)
            available, detail = adapter.is_available()
            version = adapter.version() if (available and args.versions) else ""
        except Exception as exc:
            available, detail, version = False, str(exc), ""
        rows.append((spec.name, spec.type, spec.model or "-", "yes" if available else "NO", detail, version))
    if args.json:
        print(json.dumps([dict(zip(("name", "type", "model", "available", "detail", "version"), r)) for r in rows], indent=2))
        return 0
    w = max(len(r[0]) for r in rows) if rows else 4
    print(f"{'agent':<{w}}  {'type':<12} {'model':<22} ready  location / version")
    for name, typ, model, ok, detail, version in rows:
        print(f"{name:<{w}}  {typ:<12} {model:<22} {ok:<5}  {detail}{('  (' + version + ')') if version else ''}")
    print()
    print("adapters:", ", ".join(sorted(REGISTRY)))
    if cfg.workflows:
        print("workflows:")
        for wf in cfg.workflows.values():
            print(f"  {wf.name:<24} {wf.strategy.value:<9} {wf.description or ''}".rstrip())
    return 0 if all(r[3] == "yes" for r in rows) else 2


def _goal_from_args(args: argparse.Namespace) -> str:
    if args.goal_file:
        return Path(args.goal_file).read_text(encoding="utf-8")
    if args.goal:
        return " ".join(args.goal)
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("error: provide a goal as an argument, via --goal-file, or on stdin")


def _adhoc_workflow(args: argparse.Namespace, cfg: Config) -> WorkflowSpec:
    """Build a workflow from --agents/--strategy flags when no --workflow is given."""
    agents = [a.strip() for a in (args.agents or "").split(",") if a.strip()]
    if not agents:
        raise ConfigError("ad-hoc run needs --agents a,b,c (or use --workflow NAME)")
    strategy = Strategy(args.strategy or "parallel")
    wf = WorkflowSpec(name=f"adhoc-{strategy.value}", strategy=strategy, synthesizer=args.synthesizer)
    if strategy is Strategy.PARALLEL:
        wf.agents = agents
    elif strategy is Strategy.PIPELINE:
        wf.steps = [{"agent": a, "prompt": "{goal}" if i == 0 else "Continue this work.\n\nTask: {goal}\n\nPrevious agent's output:\n{previous}"} for i, a in enumerate(agents)]
    else:  # PLAN
        wf.planner = args.planner or agents[0]
        wf.workers = agents
        wf.synthesizer = args.synthesizer or wf.planner
    missing = sorted(wf.referenced_agents() - set(cfg.agents))
    if missing:
        raise ConfigError(f"unknown agent(s): {', '.join(missing)}")
    return wf


def cmd_run(args: argparse.Namespace, cfg: Config) -> int:
    goal = _goal_from_args(args).strip()
    if not goal:
        _eprint("error: empty goal")
        return 1
    wf = cfg.workflow(args.workflow) if args.workflow else _adhoc_workflow(args, cfg)
    if args.isolation:
        wf.isolation = Isolation(args.isolation)
    if args.synthesizer:
        wf.synthesizer = args.synthesizer
    if args.no_synthesis:
        wf.synthesizer = None
        cfg.default_synthesizer = None

    project_dir = Path(args.project_dir or ".").resolve()
    quiet = args.quiet
    orch = Orchestrator(
        cfg, project_dir,
        listener=(lambda m: None) if quiet else (lambda m: _eprint(f"[cao] {m}")),
        max_concurrency=args.concurrency,
    )
    if args.dry_run:
        print(f"workflow : {wf.name} ({wf.strategy.value})")
        print(f"isolation: {(wf.isolation or cfg.default_isolation).value}")
        print(f"agents   : {', '.join(sorted(wf.referenced_agents()))}")
        print(f"project  : {project_dir}")
        print(f"goal     : {goal[:200]}{'...' if len(goal) > 200 else ''}")
        return 0

    try:
        report = asyncio.run(orch.run(goal, wf))
    except KeyboardInterrupt:
        _eprint("\ninterrupted")
        return 130
    md_path = write_report(report)

    if args.json:
        print(json.dumps(to_json(report), indent=2, ensure_ascii=False))
    else:
        print()
        print(f"== cao run {report.run_id} :: {'OK' if report.ok else 'FAILED'} in {report.duration_s:.1f}s ==")
        for r in report.results:
            print("  " + r.summary_line())
        if report.synthesis:
            print("\n--- final answer ---\n")
            print(report.synthesis.strip())
        branches = [r.branch for r in report.results if r.branch]
        if branches:
            print("\nbranches:")
            for b in branches:
                print(f"  {b}")
        print(f"\nreport: {md_path}")
    return 0 if report.ok else 1


def cmd_runs(args: argparse.Namespace) -> int:
    root = Path(args.project_dir or ".").resolve() / ".cao" / "runs"
    if not root.is_dir():
        print("no runs yet")
        return 0
    runs = sorted((p for p in root.iterdir() if (p / "report.json").is_file()), reverse=True)[: args.limit]
    for p in runs:
        try:
            data = json.loads((p / "report.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        status = "ok  " if data.get("ok") else "FAIL"
        goal = (data.get("goal") or "").strip().splitlines()[0][:60]
        print(f"{p.name}  {status}  {data.get('workflow', '?'):<20} {data.get('duration_s', 0):>7}s  {goal}")
    return 0


# ---- parser --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cao",
        description="Cross-agent orchestrator: run Claude Code, Codex, Gemini (or any CLI agent) together on one goal.",
    )
    p.add_argument("--version", action="version", version=f"cao {__version__}")
    p.add_argument("-c", "--config", help="path to cao.yaml (default: search upwards from cwd)")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="write an example cao.yaml")
    sp.add_argument("path", nargs="?", help="destination (default: ./cao.yaml)")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=lambda a, _cfg=None: cmd_init(a), needs_config=False)

    sp = sub.add_parser("agents", help="list configured agents and check their CLIs are installed")
    sp.add_argument("--versions", action="store_true", help="also query each CLI's --version")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_agents, needs_config=True)

    sp = sub.add_parser("run", help="run a goal through a workflow")
    sp.add_argument("goal", nargs="*", help="the task / question (or use --goal-file / stdin)")
    sp.add_argument("-w", "--workflow", help="workflow name from cao.yaml")
    sp.add_argument("-a", "--agents", help="ad-hoc: comma-separated agent names (instead of --workflow)")
    sp.add_argument("-s", "--strategy", choices=[s.value for s in Strategy], help="ad-hoc strategy (default: parallel)")
    sp.add_argument("--planner", help="ad-hoc plan strategy: planner agent (default: first of --agents)")
    sp.add_argument("--synthesizer", help="override the synthesizer agent")
    sp.add_argument("--no-synthesis", action="store_true", help="skip the synthesis step")
    sp.add_argument("-i", "--isolation", choices=[i.value for i in Isolation], help="override isolation")
    sp.add_argument("-C", "--project-dir", help="repository the agents work in (default: cwd)")
    sp.add_argument("--goal-file", help="read the goal from a file")
    sp.add_argument("--concurrency", type=int, default=4, help="max agents running at once (default: 4)")
    sp.add_argument("--dry-run", action="store_true", help="show what would run and exit")
    sp.add_argument("--json", action="store_true", help="print the full report as JSON")
    sp.add_argument("-q", "--quiet", action="store_true", help="suppress progress lines on stderr")
    sp.set_defaults(func=cmd_run, needs_config=True)

    sp = sub.add_parser("runs", help="list past runs in .cao/runs")
    sp.add_argument("-C", "--project-dir")
    sp.add_argument("-n", "--limit", type=int, default=20)
    sp.set_defaults(func=lambda a, _cfg=None: cmd_runs(a), needs_config=False)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if getattr(args, "needs_config", False):
            cfg = load_config(Path(args.config) if args.config else None)
            return int(args.func(args, cfg) or 0)
        return int(args.func(args) or 0)
    except ConfigError as exc:
        _eprint(f"config error: {exc}")
        hint = find_config()
        if hint is None:
            _eprint("hint: run 'cao init' to create cao.yaml")
        return 2
    except SystemExit as exc:
        if isinstance(exc.code, str):
            _eprint(exc.code)
            return 1
        raise


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
