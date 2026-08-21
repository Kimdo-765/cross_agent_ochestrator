# cao -- cross-agent orchestrator web UI + agent CLIs
#
# Contains: Python 3.12, git, gh, Node 22, Claude Code CLI, Codex CLI (Grok runs through Codex).
# Agent credentials are NOT baked in: mount ~/.claude and ~/.codex, or pass API keys (see docker-compose.yml).

FROM python:3.12-slim-bookworm

ARG NODE_MAJOR=22
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    CAO_DATA_DIR=/data \
    CAO_WORKSPACE=/workspace \
    CAO_PORT=8000 \
    CAO_NO_BROWSER=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git gnupg openssh-client ripgrep procps \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Agent CLIs (pin with build args if you need reproducible images)
ARG CLAUDE_CODE_VERSION=latest
ARG CODEX_VERSION=latest
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} @openai/codex@${CODEX_VERSION} \
    && npm cache clean --force

# Non-root user: agents get a home for ~/.claude and ~/.codex mounts
RUN useradd -m -u 1000 -s /bin/bash cao \
    && mkdir -p /data /workspace \
    && chown -R cao:cao /data /workspace
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install ".[web]"

USER cao
# git identity for commits made by the orchestrator (override via env or mounted ~/.gitconfig)
RUN git config --global user.name "cao" && git config --global user.email "cao@localhost" \
    && git config --global --add safe.directory '*'

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD curl -fsS http://127.0.0.1:${CAO_PORT}/api/meta >/dev/null || exit 1
CMD ["sh", "-c", "cao web --host 0.0.0.0 --port ${CAO_PORT} --no-open"]
