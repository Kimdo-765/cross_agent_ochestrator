import json
from pathlib import Path

import pytest

from cao.adapters import make_adapter
from cao.loop import catalog
from cao.loop.models import RoleConfig
from cao.models import Task


@pytest.fixture(autouse=True)
def isolated_codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    catalog._cache.clear()
    yield home
    catalog._cache.clear()


def _write_cache(home: Path):
    (home / "models_cache.json").write_text(json.dumps({
        "fetched_at": "2026-08-21T11:22:22Z",
        "models": [
            {"slug": "gpt-9-sol", "display_name": "GPT-9-Sol", "visibility": "list", "priority": 1,
             "default_reasoning_level": "low",
             "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
            {"slug": "gpt-8", "display_name": "GPT-8", "visibility": "list", "priority": 2,
             "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh")]},
            {"slug": "secret-model", "visibility": "hide"},
        ],
    }))
    (home / "config.toml").write_text('model = "gpt-8"\nmodel_reasoning_effort = "xhigh"\n\n[projects."/x"]\ntrust_level = "trusted"\n')


def test_codex_catalog_reads_cli_cache_and_config(isolated_codex_home):
    _write_cache(isolated_codex_home)
    cat = catalog.list_models("codex")
    ids = [m.id for m in cat.models]
    assert ids[:2] == ["gpt-9-sol", "gpt-8"]  # cache first, ordered by priority
    assert "secret-model" not in ids  # hidden entries are skipped
    assert all(s in ids for s, _ in catalog.CODEX_STATIC)  # static fallbacks appended
    sol = cat.models[0]
    assert sol.source == "codex-cache" and sol.efforts[-1] == "ultra" and sol.default_effort == "low"
    assert next(m for m in cat.models if m.id == "gpt-8").is_default is True
    assert any(s.startswith("Codex CLI cache") for s in cat.sources)


def test_codex_catalog_without_cache_falls_back_to_static(isolated_codex_home):
    cat = catalog.list_models("codex")
    assert [m.id for m in cat.models] == [s for s, _ in catalog.CODEX_STATIC]
    assert cat.sources == ["static"] and any("models_cache.json" in w for w in cat.warnings)


def test_claude_and_grok_catalogs_static(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    claude = catalog.list_models("claude_code")
    ids = [m.id for m in claude.models]
    assert "claude-opus-5" in ids and "claude-sonnet-5" in ids and "opus" in ids
    assert next(m for m in claude.models if m.id == "opus").source == "alias"
    grok = catalog.list_models("grok")
    assert grok.models[0].id == "grok-code-fast-1" and grok.models[0].is_default
    assert any("XAI_API_KEY" in w for w in grok.warnings)
    with pytest.raises(KeyError):
        catalog.list_models("nope")


def test_efforts_for_and_cache(isolated_codex_home):
    _write_cache(isolated_codex_home)
    assert catalog.efforts_for("codex", "gpt-9-sol")[-1] == "ultra"
    assert catalog.efforts_for("codex", "gpt-8") == ["low", "medium", "high", "xhigh"]
    assert catalog.efforts_for("codex", None) == catalog.CODEX_EFFORTS
    assert catalog.efforts_for("claude_code", "claude-opus-5") == catalog.CLAUDE_EFFORTS
    first = catalog.list_models("codex")
    (isolated_codex_home / "models_cache.json").unlink()
    assert catalog.list_models("codex") is first  # cached
    assert catalog.list_models("codex", refresh=True) is not first


def test_codex_effort_is_clamped_to_model_support(isolated_codex_home, tmp_path):
    _write_cache(isolated_codex_home)
    argv, _ = make_adapter(RoleConfig(backend="codex", model="gpt-8", effort="ultra").to_agent_spec("w", read_only=False)) \
        .build_command(Task(prompt="p", agent="w"), tmp_path, tmp_path)
    assert "model_reasoning_effort=xhigh" in argv  # gpt-8 tops out at xhigh
    argv, _ = make_adapter(RoleConfig(backend="codex", model="gpt-9-sol", effort="ultra").to_agent_spec("w", read_only=False)) \
        .build_command(Task(prompt="p", agent="w"), tmp_path, tmp_path)
    assert "model_reasoning_effort=ultra" in argv
    argv, _ = make_adapter(RoleConfig(backend="claude_code", effort="ultra").to_agent_spec("w", read_only=False)) \
        .build_command(Task(prompt="p", agent="w"), tmp_path, tmp_path)
    assert argv[argv.index("--effort") + 1] == "max"  # claude has no ultra
