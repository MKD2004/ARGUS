"""Tests for the LLM factory's provider selection (DECISIONS.md D-049).

Nothing here calls a model: building a LangChain chat model doesn't contact
the server, and the settings are patched per test.
"""

from unittest.mock import patch

import pytest
from pydantic import BaseModel

from app import llm


class _Answer(BaseModel):
    text: str


def _chat_model(runnable):
    """The chat model a structured-output runnable wraps (its first step)."""
    return runnable.first.bound if hasattr(runnable.first, "bound") else runnable.first


def test_ollama_is_built_with_the_configured_model_and_settings():
    with (
        patch.object(llm, "OLLAMA_MODEL", "granite4.1:8b"),
        patch.object(llm, "OLLAMA_BASE_URL", "http://ollama.test:11434"),
        patch.object(llm, "OLLAMA_NUM_CTX", 8192),
        patch.object(llm, "OLLAMA_TIMEOUT_SECONDS", 42.0),
    ):
        runnable = llm.get_structured_llm(_Answer, provider="ollama")

    model = _chat_model(runnable)
    assert type(model).__name__ == "ChatOllama"
    assert model.model == "granite4.1:8b"
    assert model.base_url == "http://ollama.test:11434"
    # Without this, Ollama's small default context would silently cut off long prompts.
    assert model.num_ctx == 8192
    assert model.temperature == 0
    assert model.client_kwargs == {"timeout": 42.0}


def test_ollama_uses_schema_constrained_json_output():
    runnable = llm.get_structured_llm(_Answer, provider="ollama")
    bound_format = runnable.first.kwargs.get("format")
    assert bound_format == _Answer.model_json_schema()


def test_anthropic_remains_selectable():
    with patch.object(llm, "ANTHROPIC_MODEL", "claude-test"):
        runnable = llm.get_structured_llm(_Answer, provider="anthropic")
    model = _chat_model(runnable)
    assert type(model).__name__ == "ChatAnthropic"
    assert model.model == "claude-test"


def test_provider_setting_is_used_when_none_is_passed_and_is_case_insensitive():
    with patch.object(llm, "LLM_PROVIDER", "  OLLAMA "):
        runnable = llm.get_structured_llm(_Answer)
    assert type(_chat_model(runnable)).__name__ == "ChatOllama"


def test_unknown_provider_fails_loudly():
    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER 'openai'"):
        llm.get_structured_llm(_Answer, provider="openai")
