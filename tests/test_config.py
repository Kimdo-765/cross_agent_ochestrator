import yaml
import pytest

from cao.config import EXAMPLE_CONFIG, ConfigError, parse_config
from cao.models import Isolation, Strategy


def test_example_config_parses():
    cfg = parse_config(yaml.safe_load(EXAMPLE_CONFIG))
    assert set(cfg.agents) == {"claude", "codex"}
    assert cfg.workflows["compare"].strategy is Strategy.PARALLEL
    assert cfg.workflows["compare"].isolation is Isolation.NONE
    assert cfg.workflows["implement-then-review"].steps[0]["agent"] == "codex"
    assert cfg.workflows["build"].planner == "claude"
    assert cfg.default_synthesizer == "claude"


def test_env_expansion(monkeypatch):
    monkeypatch.setenv("MY_KEY", "sekrit")
    cfg = parse_config({"agents": {"a": {"type": "shell", "env": {"K": "${MY_KEY}", "D": "${NOPE:-dflt}"}}}})
    assert cfg.agents["a"].env == {"K": "sekrit", "D": "dflt"}


@pytest.mark.parametrize(
    "data, msg",
    [
        ({}, "no agents"),
        ({"agents": {"a": {"type": "codex"}}, "workflows": {"w": {"strategy": "parallel", "agents": ["zzz"]}}}, "unknown agent"),
        ({"agents": {"a": {"type": "codex"}}, "workflows": {"w": {"strategy": "bogus"}}}, "unknown strategy"),
        ({"agents": {"a": {"type": "codex"}}, "workflows": {"w": {"strategy": "plan", "planner": "a"}}}, "needs 'planner:'"),
        ({"agents": {"a": {"type": "codex", "sandbox": "x"}}}, "unknown key"),
        ({"agents": {"a": {}}}, "missing required key 'type'"),
    ],
)
def test_validation_errors(data, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_config(data)
