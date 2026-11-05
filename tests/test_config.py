"""Config tests: the .env parser, the three LLM keys, and how payback-mcp is launched."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from payback_agent import config

KEYS = ("AGENT_LLM_BASE_URL", "AGENT_LLM_API_KEY", "AGENT_LLM_MODEL",
        "PAYBACK_MCP_DIR", "PAYBACK_MCP_COMMAND", "AGENT_MAX_TURNS")


@pytest.fixture()
def clean_env(monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    # A real .env would satisfy the config the moment someone follows the README.
    monkeypatch.setattr(config, "DEFAULT_ENV_PATH", Path(__file__).parent / "no-such.env")
    return monkeypatch


def test_load_env_skips_comments_and_never_overrides_existing_keys(clean_env, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\n\nNOT_A_LINE\nAGENT_LLM_MODEL=from-file\n", encoding="utf8")
    clean_env.setenv("AGENT_LLM_MODEL", "from-process")
    config.load_env(str(env_file))
    assert os.environ["AGENT_LLM_MODEL"] == "from-process"
    assert "NOT_A_LINE" not in os.environ


def test_missing_keys_are_named_in_one_error(clean_env):
    with pytest.raises(config.ConfigError) as excinfo:
        config.get_llm_config()
    message = str(excinfo.value)
    for key in ("AGENT_LLM_BASE_URL", "AGENT_LLM_API_KEY", "AGENT_LLM_MODEL"):
        assert key in message
    assert "--dry-run" in message  # the way out is in the error, not in the README alone


def test_partial_config_names_only_what_is_missing(clean_env):
    clean_env.setenv("AGENT_LLM_BASE_URL", "https://g.test/v1")
    with pytest.raises(config.ConfigError) as excinfo:
        config.get_llm_config()
    listed = str(excinfo.value).split("missing environment variables:")[1].split(" -- ")[0]
    assert "AGENT_LLM_API_KEY" in listed and "AGENT_LLM_MODEL" in listed
    assert "AGENT_LLM_BASE_URL" not in listed


def test_base_url_trailing_slash_is_stripped_once(clean_env):
    clean_env.setenv("AGENT_LLM_BASE_URL", "https://g.test/v1//")
    clean_env.setenv("AGENT_LLM_API_KEY", "k")
    clean_env.setenv("AGENT_LLM_MODEL", "m")
    assert config.get_llm_config().base_url == "https://g.test/v1"


def test_mcp_launch_defaults_to_the_sibling_checkout(clean_env):
    argv = config.mcp_launch_args()
    assert argv[0] == "uv"
    assert argv[1:3] == ["--directory", str(config.REPO_ROOT.parent / "payback-mcp")]
    assert argv[-2:] == ["run", "payback-mcp"]


def test_mcp_dir_and_command_are_configuration_not_code(clean_env):
    clean_env.setenv("PAYBACK_MCP_DIR", "D:/elsewhere/payback-mcp")
    clean_env.setenv("PAYBACK_MCP_COMMAND", "python")
    argv = config.mcp_launch_args()
    assert argv == ["python", "--directory", "D:/elsewhere/payback-mcp", "run", "payback-mcp"]


def test_max_turns_default_and_validation(clean_env):
    assert config.max_turns() == 8
    clean_env.setenv("AGENT_MAX_TURNS", "3")
    assert config.max_turns() == 3
    for bad, expected in [("zero", "integer"), ("-1", ">= 1"), ("0", ">= 1")]:
        clean_env.setenv("AGENT_MAX_TURNS", bad)
        with pytest.raises(config.ConfigError, match=expected):
            config.max_turns()
