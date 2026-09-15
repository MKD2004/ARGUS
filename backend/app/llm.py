"""Shared LLM client factory for the LLM-reasoning agents (Hypothesis Generator,
Hypothesis Validator, Fix Planner, Patch Generator).

The one place a model is created. Agents only see LangChain's structured-output
interface (DECISIONS.md D-011), so data stays typed end to end rather than being
parsed out of free-form text (CLAUDE.md §2), and the provider is a setting:
a local Ollama model by default, or Anthropic (D-049).
"""

from __future__ import annotations

from pydantic import BaseModel

from app.config import (
    ANTHROPIC_MODEL,
    LLM_PROVIDER,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    OLLAMA_NUM_CTX,
    OLLAMA_TIMEOUT_SECONDS,
)

PROVIDERS = ("ollama", "anthropic")


def get_structured_llm(schema: type[BaseModel], provider: str | None = None):
    """Returns a Runnable whose .invoke(prompt) returns a validated `schema` instance."""
    provider = (provider or LLM_PROVIDER).strip().lower()

    if provider == "ollama":
        # Imported here so the Anthropic path doesn't need the Ollama client, and vice versa.
        from langchain_ollama import ChatOllama

        model = ChatOllama(
            model=OLLAMA_MODEL,
            base_url=OLLAMA_BASE_URL,
            # Ollama's default context is small and silently cuts off long
            # prompts; the Patch Generator sends whole source files (D-049).
            num_ctx=OLLAMA_NUM_CTX,
            temperature=0,
            client_kwargs={"timeout": OLLAMA_TIMEOUT_SECONDS},
        )
        # Ollama constrains generation to the schema's JSON, rather than asking nicely.
        return model.with_structured_output(schema, method="json_schema")

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=ANTHROPIC_MODEL, timeout=30.0).with_structured_output(schema)

    raise ValueError(f"Unknown LLM_PROVIDER {provider!r}; expected one of: {', '.join(PROVIDERS)}")
