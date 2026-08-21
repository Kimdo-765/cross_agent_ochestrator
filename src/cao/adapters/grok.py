"""Adapter for xAI Grok models.

xAI exposes an OpenAI-compatible API, and the Codex CLI supports custom
OpenAI-compatible providers via ``model_providers.<id>`` config. So Grok runs
through the Codex agent harness (tools, sandbox, diff-producing edits) while
the model itself is Grok. Requires ``XAI_API_KEY`` in the environment.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from ..models import Task
from .codex import CodexAdapter

XAI_BASE_URL = "https://api.x.ai/v1"
DEFAULT_GROK_MODEL = "grok-code-fast-1"


class GrokAdapter(CodexAdapter):
    """``type: grok`` -- Codex CLI driving an xAI model.

    options (in addition to the codex ones):
      base_url: str      (default: https://api.x.ai/v1)
      env_key: str       (default: XAI_API_KEY)
      wire_api: chat | responses   (default: chat)
    """

    key = "grok"
    binary = "codex"

    def provider_config(self) -> dict[str, str]:
        o = self.spec.options
        return {
            "model_provider": "xai",
            "model_providers.xai.name": '"xAI"',
            "model_providers.xai.base_url": f'"{o.get("base_url", XAI_BASE_URL)}"',
            "model_providers.xai.env_key": f'"{o.get("env_key", "XAI_API_KEY")}"',
            "model_providers.xai.wire_api": f'"{o.get("wire_api", "chat")}"',
        }

    def build_command(self, task: Task, workdir: Path, run_dir: Path) -> tuple[list[str], Optional[str]]:
        if not self.spec.model:
            self.spec.model = DEFAULT_GROK_MODEL
        return super().build_command(task, workdir, run_dir)

    def is_available(self) -> tuple[bool, str]:
        ok, detail = super().is_available()
        if not ok:
            return ok, detail
        env_key = str(self.spec.options.get("env_key", "XAI_API_KEY"))
        if not (os.environ.get(env_key) or self.spec.env.get(env_key)):
            return False, f"{detail} (but {env_key} is not set)"
        return True, f"{detail} via xAI ({env_key} set)"
