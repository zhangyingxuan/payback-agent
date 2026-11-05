"""Environment configuration loading (stdlib only, no python-dotenv).

Same shape as ``payback_mcp.config`` on purpose: the .env parser, the
"existing os.environ wins" rule and the missing-key error are already reviewed
there, and an agent that reads config differently from the server it drives is a
bug factory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_PATH = REPO_ROOT / ".env"

_ENV_KEYS = {
    "base_url": "AGENT_LLM_BASE_URL",
    "api_key": "AGENT_LLM_API_KEY",
    "model": "AGENT_LLM_MODEL",
}


class ConfigError(Exception):
    """Raised when required environment configuration is missing or invalid."""


@dataclass(frozen=True)
class LlmConfig:
    base_url: str
    api_key: str
    model: str


def load_env(path: str | None = None) -> None:
    """Load KEY=VALUE pairs from an env file into os.environ.

    Existing os.environ keys are never overridden. Blank lines, ``#`` comment
    lines and lines without ``=`` are skipped.
    """
    env_path = Path(path) if path is not None else DEFAULT_ENV_PATH
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


def get_llm_config() -> LlmConfig:
    """Build LlmConfig from AGENT_LLM_* environment variables.

    The base URL is kept verbatim: the client appends ``/chat/completions``, so a
    gateway that needs a longer path is configured by putting it here, not by
    editing code.
    """
    missing = [name for name in _ENV_KEYS.values() if not os.environ.get(name)]
    if missing:
        raise ConfigError(
            f"missing environment variables: {', '.join(missing)} -- copy .env.example to "
            ".env (or --dry-run, which needs no LLM at all)"
        )
    return LlmConfig(
        base_url=os.environ[_ENV_KEYS["base_url"]].rstrip("/"),
        api_key=os.environ[_ENV_KEYS["api_key"]],
        model=os.environ[_ENV_KEYS["model"]],
    )


def mcp_launch_args() -> list[str]:
    """argv that starts payback-mcp over stdio.

    Default target is the sibling checkout; ``PAYBACK_MCP_DIR`` overrides it so a
    relocated server stays a configuration change, not a code change.
    """
    directory = os.environ.get("PAYBACK_MCP_DIR") or str(REPO_ROOT.parent / "payback-mcp")
    command = os.environ.get("PAYBACK_MCP_COMMAND", "uv")
    return [command, "--directory", directory, "run", "payback-mcp"]


def max_turns() -> int:
    """Tool-calling iteration cap for one question."""
    raw = os.environ.get("AGENT_MAX_TURNS", "8")
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"AGENT_MAX_TURNS must be an integer, got {raw!r}") from None
    if value < 1:
        raise ConfigError(f"AGENT_MAX_TURNS must be >= 1, got {value}")
    return value


def create_llm_client():
    """按配置选择模型后端，默认保持原网关行为。"""
    backend = os.environ.get("AGENT_LLM_BACKEND", "gateway").strip().lower()
    if backend == "codex":
        from .codex import CodexClient
        return CodexClient()
    if backend == "gateway":
        from .llm import ChatClient
        return ChatClient(get_llm_config())
    raise ConfigError("AGENT_LLM_BACKEND 必须是 gateway 或 codex")
