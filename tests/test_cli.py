import json
import os
from pathlib import Path

from cao.cli import main


def test_init_and_agents(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert (tmp_path / "cao.yaml").exists()
    assert main(["init"]) == 1  # refuses to overwrite
    assert main(["agents", "--json"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out[out.index("["):])
    assert {d["name"]: d["available"] for d in data} == {"claude": "yes", "codex": "yes"}


def test_flow_run_adhoc_parallel(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["init"])
    rc = main(["flow", "run", "-a", "claude,codex", "-i", "none", "--synthesizer", "claude", "-q", "hello world"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[ok] claude" in out and "[ok] codex" in out and "final answer" in out
    assert main(["flow", "runs"]) == 0
    assert "adhoc-parallel" in capsys.readouterr().out


def test_flow_dry_run_and_missing_config(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["agents"]) == 2  # no config
    main(["init"])
    assert main(["flow", "run", "-w", "compare", "--dry-run", "x"]) == 0
    assert "workflow : compare (parallel)" in capsys.readouterr().out
    assert main(["flow", "run", "-w", "nope", "x"]) == 2


def test_run_dry_run_and_validation(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main(["run", "--dry-run", "-w", "claude_code", "-r", "codex:gpt-x", "-a", "has tests", "add a thing"])
    assert rc == 0
    spec = json.loads(capsys.readouterr().out)
    assert spec["worker"]["backend"] == "claude_code"
    assert spec["reviewer"] == {**spec["reviewer"], "backend": "codex", "model": "gpt-x", "role": "reviewer"}
    assert spec["acceptance_criteria"] == ["has tests"]
    # same model on both sides is rejected
    assert main(["run", "--dry-run", "-w", "codex", "-r", "codex", "x"]) == 2
    assert "different model" in capsys.readouterr().err


def test_run_loop_via_cli(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "app.py").write_text("print('hi')\n")
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CAO_FAKE_SCORES", "9.5")
    rc = main(["--data-dir", str(data_dir), "run", "-C", str(repo), "-w", "claude_code", "-r", "codex",
               "--on-success", "none", "-q", "add a greeting function"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PASSED" in out and "iter 1: score=9.5" in out
    assert main(["--data-dir", str(data_dir), "tasks"]) == 0
    assert "passed" in capsys.readouterr().out
