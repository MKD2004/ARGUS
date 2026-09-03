"""Shared LLM client factory for LLM-reasoning agents (Hypothesis Generator/Validator).

Anthropic Claude via langchain-anthropic (DECISIONS.md D-011), accessed only
through LangChain's structured-output interface so hypothesis data stays
typed end-to-end rather than parsed out of free-form text (CLAUDE.md §2).
"""

from __future__ import annotations

import os

from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")


def get_structured_llm(schema: type[BaseModel]):
    """Returns a Runnable whose .invoke(prompt) returns a validated `schema` instance."""
    return ChatAnthropic(model=ANTHROPIC_MODEL, timeout=30.0).with_structured_output(schema)
