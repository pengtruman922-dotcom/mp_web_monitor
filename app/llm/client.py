"""Unified LLM client supporting OpenAI-compatible APIs with function calling."""
import asyncio
import logging
from typing import Any

import openai
from openai import AsyncOpenAI
from sqlalchemy import select

from app.database.connection import async_session
from app.models.settings import LLMConfig
from app.config import LLM_MAX_RETRIES, LLM_MAX_CONCURRENCY

logger = logging.getLogger(__name__)

# Concurrency limiter for LLM API calls
_llm_semaphore = asyncio.Semaphore(LLM_MAX_CONCURRENCY)

# Transient error types that should be retried
_RETRYABLE_ERRORS = (
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.RateLimitError,
    openai.InternalServerError,
)


async def get_active_llm_config() -> LLMConfig | None:
    """Fetch the currently active LLM configuration from the database."""
    async with async_session() as session:
        result = await session.execute(
            select(LLMConfig).where(LLMConfig.is_active == True).limit(1)
        )
        return result.scalar_one_or_none()


def build_client(config: LLMConfig) -> AsyncOpenAI:
    """Build an AsyncOpenAI client from a config record."""
    # Strip /chat/completions suffix if present — the SDK appends it automatically
    base_url = config.api_url
    for suffix in ["/chat/completions", "/chat"]:
        if base_url.endswith(suffix):
            base_url = base_url[: -len(suffix)]
            break

    return AsyncOpenAI(api_key=config.api_key, base_url=base_url)


async def chat_completion(
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.3,
    max_tokens: int = 8192,
) -> dict:
    """Send a chat completion request and return the raw response dict.

    Returns a dict with keys: role, content, tool_calls (optional).
    Raises RuntimeError if no active LLM config is found.
    Retries on transient errors with exponential backoff.
    """
    config = await get_active_llm_config()
    if config is None:
        raise RuntimeError("No active LLM configuration found. Please configure an LLM in settings.")

    client = build_client(config)

    kwargs: dict[str, Any] = {
        "model": config.model_name,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    last_error = None
    for attempt in range(LLM_MAX_RETRIES):
        try:
            async with _llm_semaphore:
                response = await client.chat.completions.create(**kwargs)
            break
        except _RETRYABLE_ERRORS as e:
            last_error = e
            if attempt < LLM_MAX_RETRIES - 1:
                wait_time = 2 ** (attempt + 1)  # 2s, 4s
                logger.warning("LLM API call failed (attempt %d/%d), retrying in %ds: %s",
                               attempt + 1, LLM_MAX_RETRIES, wait_time, e)
                await asyncio.sleep(wait_time)
            else:
                logger.error("LLM API call failed after %d attempts: %s", LLM_MAX_RETRIES, e)
                raise
        except Exception as e:
            logger.error("LLM API call failed (non-retryable): %s", e)
            raise

    msg = response.choices[0].message

    result: dict[str, Any] = {
        "role": "assistant",
        "content": msg.content or "",
    }

    if msg.tool_calls:
        result["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in msg.tool_calls
        ]

    return result


async def simple_completion(prompt: str, system: str = "", temperature: float = 0.3, max_tokens: int = 4096) -> str:
    """Simplified helper: send a single prompt and return the text response."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    result = await chat_completion(messages, temperature=temperature, max_tokens=max_tokens)
    return result.get("content", "")
