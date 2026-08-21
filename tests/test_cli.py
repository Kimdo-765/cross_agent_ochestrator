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


def test_run_adhoc_parallel(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["init"])
    rc = main(["run", "-a", "claude,codex", "-i", "none", "--synthesizer", "claude", "-q", "hello world"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[ok] claude" in out and "[ok] codex" in out and "final answer" in out
    assert main(["runs"]) == 0
    assert "adhoc-parallel" in capsys.readouterr().out


def test_run_dry_run_and_missing_config(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["agents"]) == 2  # no config
    main(["init"])
    assert main(["run", "-w", "compare", "--dry-run", "x"]) == 0
    assert "workflow : compare (parallel)" in capsys.readouterr().out
    assert main(["run", "-w", "nope", "x"]) == 2
