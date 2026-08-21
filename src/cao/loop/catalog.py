"""Model catalog: which models each backend can run, discovered from local CLI state and APIs.

Sources, in order of trust (later sources never override an earlier entry's metadata):

claude_code  1. Anthropic Models API via the ``anthropic`` SDK, when it is installed and credentials
                resolve (ANTHROPIC_API_KEY / `ant auth login` profile)
             2. Static table of current models + the CLI's aliases (opus / sonnet / haiku)
codex        1. ~/.codex/models_cache.json -- the list the Codex CLI itself shows, incl. per-model
                reasoning-effort levels and the account's default
             2. OpenAI Models API (OPENAI_API_KEY), filtered to coding-relevant families
             3. Static fallback
grok         1. xAI Models API (XAI_API_KEY; OpenAI-compatible)
             2. Static fallback

Every entry carries its ``source`` so the UI can say where a name came from. Users can always
type a model id the catalog does not know about.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max", "ultra")
CACHE_TTL_S = 600.0
HTTP_TIMEOUT_S = 6.0


@dataclass
class ModelInfo:
    id: str
    label: str = ""
    source: str = "static"  # static | alias | codex-cache | api
    efforts: Optional[list[str]] = None  # None -> backend default list
    default_effort: Optional[str] = None
    is_default: bool = False  # the CLI's own default when no model is given
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Catalog:
    backend: str
    models: list[ModelInfo] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fetched_at: float = field(default_factory=time.time)
    efforts: list[str] = field(default_factory=list)  # backend-wide default effort list

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "models": [m.to_dict() for m in self.models],
            "sources": self.sources,
            "warnings": self.warnings,
            "fetched_at": self.fetched_at,
            "efforts": self.efforts,
        }

    def add(self, info: ModelInfo) -> None:
        if any(m.id == info.id for m in self.models):
            return
        self.models.append(info)


# --------------------------------------------------------------------------- static knowledge

# Current Anthropic models (Claude Code accepts the bare id or an alias). Cached 2026-06; the live
# Models API below supersedes this list whenever credentials are available.
CLAUDE_STATIC: list[tuple[str, str]] = [
    ("claude-opus-5", "Claude Opus 5"),
    ("claude-fable-5", "Claude Fable 5"),
    ("claude-sonnet-5", "Claude Sonnet 5"),
    ("claude-opus-4-8", "Claude Opus 4.8"),
    ("claude-opus-4-7", "Claude Opus 4.7"),
    ("claude-opus-4-6", "Claude Opus 4.6"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
    ("claude-haiku-4-5", "Claude Haiku 4.5"),
]
CLAUDE_ALIASES: list[tuple[str, str]] = [
    ("opus", "opus (alias → latest Opus)"),
    ("sonnet", "sonnet (alias → latest Sonnet)"),
    ("haiku", "haiku (alias → latest Haiku)"),
]
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]

CODEX_STATIC: list[tuple[str, str]] = [
    ("gpt-5.5", "GPT-5.5"),
    ("gpt-5.4", "GPT-5.4"),
    ("gpt-5.4-mini", "GPT-5.4-Mini"),
    ("gpt-5.3-codex-spark", "GPT-5.3-Codex-Spark"),
]
CODEX_EFFORTS = ["low", "medium", "high", "xhigh"]
CODEX_FAMILY_RE = re.compile(r"^(gpt-5|gpt-4\.1|o[1-9]|codex|gpt-daybreak)")

GROK_STATIC: list[tuple[str, str]] = [
    ("grok-code-fast-1", "Grok Code Fast 1"),
    ("grok-4-1-fast-reasoning", "Grok 4.1 Fast (reasoning)"),
    ("grok-4-1-fast-non-reasoning", "Grok 4.1 Fast (non-reasoning)"),
    ("grok-4-fast-reasoning", "Grok 4 Fast (reasoning)"),
    ("grok-4", "Grok 4"),
    ("grok-3", "Grok 3"),
    ("grok-3-mini", "Grok 3 Mini"),
]
GROK_EFFORTS = ["low", "medium", "high"]


# --------------------------------------------------------------------------- helpers


def _http_json(url: str, headers: dict[str, str]) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "cao", **headers})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _codex_config_model() -> Optional[str]:
    cfg = codex_home() / "config.toml"
    try:
        for line in cfg.read_text(encoding="utf-8").splitlines():
            m = re.match(r'^\s*model\s*=\s*"([^"]+)"', line)
            if m:
                return m.group(1)
            if line.strip().startswith("["):  # only the top-level table counts
                break
    except OSError:
        pass
    return None


# --------------------------------------------------------------------------- per-backend discovery


def _claude_catalog() -> Catalog:
    cat = Catalog(backend="claude_code", efforts=list(CLAUDE_EFFORTS))
    try:
        import anthropic  # optional dependency; also picks up `ant auth login` profiles

        client = anthropic.Anthropic(timeout=HTTP_TIMEOUT_S, max_retries=0)
        for m in client.models.list():
            mid = getattr(m, "id", "")
            if mid.startswith("claude"):
                cat.add(ModelInfo(id=mid, label=getattr(m, "display_name", "") or mid, source="api"))
        if cat.models:
            cat.sources.append("anthropic models api")
    except ImportError:
        cat.warnings.append("pip install anthropic (and set ANTHROPIC_API_KEY) to list models live")
    except Exception as exc:  # no credentials / offline -> static list is fine
        cat.warnings.append(f"models api unavailable: {exc.__class__.__name__}")
    for mid, label in CLAUDE_STATIC:
        cat.add(ModelInfo(id=mid, label=label, source="static"))
    for mid, label in CLAUDE_ALIASES:
        cat.add(ModelInfo(id=mid, label=label, source="alias"))
    cat.sources.append("static")
    return cat


def _codex_catalog() -> Catalog:
    cat = Catalog(backend="codex", efforts=list(CODEX_EFFORTS))
    default_model = _codex_config_model()
    cache = codex_home() / "models_cache.json"
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        entries = sorted(
            (m for m in data.get("models", []) if isinstance(m, dict) and m.get("visibility", "list") == "list"),
            key=lambda m: (m.get("priority") is None, m.get("priority", 0)),
        )
        for m in entries:
            slug = str(m.get("slug") or m.get("id") or "")
            if not slug:
                continue
            efforts = [str(e.get("effort")) for e in (m.get("supported_reasoning_levels") or []) if isinstance(e, dict) and e.get("effort")]
            cat.add(ModelInfo(
                id=slug,
                label=str(m.get("display_name") or slug),
                source="codex-cache",
                efforts=efforts or None,
                default_effort=m.get("default_reasoning_level"),
                is_default=(slug == default_model),
            ))
        if entries:
            fetched = str(data.get("fetched_at") or "")
            stamp = f" ({fetched[:10]})" if len(fetched) >= 10 else ""
            cat.sources.append(f"Codex CLI cache{stamp}")
    except FileNotFoundError:
        cat.warnings.append("no ~/.codex/models_cache.json yet -- run `codex` once to populate it")
    except Exception as exc:
        cat.warnings.append(f"could not read models_cache.json: {exc.__class__.__name__}")

    key = os.environ.get("OPENAI_API_KEY")
    if key:
        try:
            data = _http_json("https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"})
            ids = sorted(str(m.get("id")) for m in data.get("data", []) if CODEX_FAMILY_RE.match(str(m.get("id", ""))))
            for mid in ids:
                cat.add(ModelInfo(id=mid, label=mid, source="api", is_default=(mid == default_model)))
            if ids:
                cat.sources.append("openai models api")
        except Exception as exc:
            cat.warnings.append(f"openai models api: {exc.__class__.__name__}")

    for mid, label in CODEX_STATIC:
        cat.add(ModelInfo(id=mid, label=label, source="static", is_default=(mid == default_model)))
    if not any(s.startswith("Codex CLI cache") or s.startswith("openai") for s in cat.sources):
        cat.sources.append("static")
    if default_model and not any(m.id == default_model for m in cat.models):
        cat.models.insert(0, ModelInfo(id=default_model, label=f"{default_model} (config.toml)", source="codex-config", is_default=True))
    return cat


def _grok_catalog() -> Catalog:
    cat = Catalog(backend="grok", efforts=list(GROK_EFFORTS))
    key = os.environ.get("XAI_API_KEY")
    if key:
        try:
            data = _http_json("https://api.x.ai/v1/models", {"Authorization": f"Bearer {key}"})
            ids = sorted(str(m.get("id")) for m in data.get("data", []) if str(m.get("id", "")).startswith("grok"))
            for mid in ids:
                cat.add(ModelInfo(id=mid, label=mid, source="api"))
            if ids:
                cat.sources.append("xai models api")
        except Exception as exc:
            cat.warnings.append(f"xai models api: {exc.__class__.__name__}")
    else:
        cat.warnings.append("set XAI_API_KEY to list Grok models live")
    for mid, label in GROK_STATIC:
        cat.add(ModelInfo(id=mid, label=label, source="static", is_default=(mid == "grok-code-fast-1")))
    cat.sources.append("static")
    return cat


_BUILDERS = {"claude_code": _claude_catalog, "codex": _codex_catalog, "grok": _grok_catalog}
_cache: dict[str, Catalog] = {}
_lock = threading.Lock()


def list_models(backend: str, *, refresh: bool = False) -> Catalog:
    """Catalog for ``backend`` (cached for CACHE_TTL_S; ``refresh=True`` re-discovers)."""
    if backend not in _BUILDERS:
        raise KeyError(f"unknown backend '{backend}'")
    with _lock:
        cached = _cache.get(backend)
        if cached and not refresh and time.time() - cached.fetched_at < CACHE_TTL_S:
            return cached
    cat = _BUILDERS[backend]()
    with _lock:
        _cache[backend] = cat
    return cat


def all_catalogs(*, refresh: bool = False) -> dict[str, dict[str, Any]]:
    return {b: list_models(b, refresh=refresh).to_dict() for b in _BUILDERS}


def efforts_for(backend: str, model: Optional[str]) -> list[str]:
    """Effort levels valid for a backend/model pair (used by validation and the UI)."""
    try:
        cat = list_models(backend)
    except KeyError:
        return list(EFFORT_LEVELS)
    if model:
        for m in cat.models:
            if m.id == model and m.efforts:
                return list(m.efforts)
    return list(cat.efforts) or list(EFFORT_LEVELS)
