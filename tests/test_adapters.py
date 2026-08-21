from pathlib import Path

import pytest

from cao.adapters import REGISTRY, make_adapter, register
from cao.adapters.base import AgentAdapter
from cao.adapters.claude_code import ClaudeCodeAdapter
from cao.adapters.codex import CodexAdapter
from cao.models import AgentSpec, Task


def test_registry_has_builtins():
    assert {"claude_code", "codex", "gemini", "shell"} <= set(REGISTRY)
    with pytest.raises(ValueError, match="unknown type"):
        make_adapter(AgentSpec(name="x", type="nope"))


def test_register_custom_adapter():
    @register
    class Dummy(AgentAdapter):
        key = "dummy_test"
        binary = "true"

        def build_command(self, task, workdir, run_dir):
            return ["true"], None

        def parse_output(self, proc, run_dir):
            return "", {}, {}

    assert isinstance(make_adapter(AgentSpec(name="d", type="dummy_test")), Dummy)
    REGISTRY.pop("dummy_test")


def test_claude_command_flags(tmp_path):
    spec = AgentSpec(name="c", type="claude_code", model="claude-sonnet-5",
                     options={"permission_mode": "plan", "max_turns": 7, "allowed_tools": ["Read", "Bash(git *)"]})
    argv, stdin = ClaudeCodeAdapter(spec).build_command(Task(prompt="hi", agent="c"), tmp_path, tmp_path)
    assert argv[:4] == ["claude", "--print", "--output-format", "json"]
    assert "--permission-mode" in argv and argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert argv[argv.index("--allowedTools") + 1 : argv.index("--allowedTools") + 3] == ["Read", "Bash(git *)"]
    assert stdin == "hi"


def test_codex_command_flags(tmp_path):
    spec = AgentSpec(name="x", type="codex", options={"sandbox": "read-only", "config": {"a.b": 1}})
    argv, stdin = CodexAdapter(spec).build_command(Task(prompt="hi", agent="x"), tmp_path, tmp_path)
    assert argv[:3] == ["codex", "exec", "--json"]
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("-C") + 1] == str(tmp_path)
    assert "-c" in argv and "a.b=1" in argv
    assert argv[-1] == "-" and stdin == "hi"


async def test_claude_adapter_end_to_end(tmp_path):
    spec = AgentSpec(name="claude", type="claude_code", model="m1")
    res = await make_adapter(spec).run(Task(prompt="what is 2+2", agent="claude"), tmp_path, tmp_path / "run")
    assert res.ok, res.error
    assert res.output == "[fake-claude:m1] what is 2+2"
    assert res.usage["total_cost_usd"] == pytest.approx(0.0123)
    assert (tmp_path / "run" / "logs").exists()


async def test_claude_adapter_reports_agent_error(tmp_path):
    spec = AgentSpec(name="claude", type="claude_code")
    res = await make_adapter(spec).run(Task(prompt="FAIL_PLEASE", agent="claude"), tmp_path, tmp_path / "run")
    assert not res.ok
    assert "error" in (res.error or "")


async def test_codex_adapter_end_to_end(tmp_path):
    spec = AgentSpec(name="codex", type="codex")
    res = await make_adapter(spec).run(Task(prompt="EDIT_FILE please", agent="codex"), tmp_path, tmp_path / "run")
    assert res.ok, res.error
    assert res.output.startswith("[fake-codex]")
    assert res.usage == {"input_tokens": 10, "output_tokens": 5}
    assert (tmp_path / "codex_was_here.txt").exists()


async def test_shell_adapter_stdin_and_json(tmp_path):
    spec = AgentSpec(name="sh", type="shell", options={
        "command": ["python3", "-c", "import sys,json;print(json.dumps({'result': sys.stdin.read().upper(), 'n': 1}))", "{prompt}"],
        "prompt_via": "stdin", "output": "json",
    })
    res = await make_adapter(spec).run(Task(prompt="abc", agent="sh"), tmp_path, tmp_path / "run")
    assert res.ok, res.error
    assert res.output == "ABC" and res.usage == {"n": 1}


async def test_missing_binary(tmp_path):
    spec = AgentSpec(name="g", type="gemini", options={"binary": "definitely-not-installed-xyz"})
    res = await make_adapter(spec).run(Task(prompt="hi", agent="g"), tmp_path, tmp_path / "run")
    assert not res.ok and "not found" in (res.error or "")


async def test_timeout(tmp_path):
    spec = AgentSpec(name="slow", type="shell", timeout=0.3, options={"command": ["sleep", "5"]})
    res = await make_adapter(spec).run(Task(prompt="x", agent="slow"), tmp_path, tmp_path / "run")
    assert not res.ok and "timed out" in (res.error or "")
