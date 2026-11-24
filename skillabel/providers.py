"""AI provider layer — Anthropic direct or OpenRouter.

Adapted from an earlier providers module (same shapes, same tests
pattern). The AI layer (skillabel.ai) talks to exactly two operations:
`parse_structured` and `complete_text`.

- `SKILLABEL_AI_PROVIDER=anthropic` — Anthropic API via the anthropic SDK.
- `SKILLABEL_AI_PROVIDER=openrouter` — OpenRouter's OpenAI-compatible endpoint.
- `SKILLABEL_AI_PROVIDER=auto` (default) — Anthropic if ANTHROPIC_API_KEY is
  set, else OpenRouter if OPENROUTER_API_KEY is set, else raises
  AIProviderNotConfiguredError (callers degrade to keyless mode — the
  deterministic label still renders; only enrichment and report cards need AI).

Model names stay canonical (Anthropic ids) everywhere else; OpenRouter slugs
(dot-versioned, verified against openrouter.ai 2026-07-03) are a mapping
detail of this module.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)

# The default stays on the model the extraction was tuned and checked with
# until a real run on a newer one shows the same quality.
DEFAULT_MODEL = "claude-sonnet-5"
ALLOWED_MODELS = {
    "claude-sonnet-5", "claude-opus-4-8", "claude-haiku-4-5",
    "claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5",
}

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL_MAP = {
    "claude-sonnet-5": "anthropic/claude-sonnet-5",
    "claude-opus-4-8": "anthropic/claude-opus-4.8",
    "claude-haiku-4-5": "anthropic/claude-haiku-4.5",
    "claude-sonnet-5-5": "anthropic/claude-sonnet-5.5",
    "claude-opus-5-5": "anthropic/claude-opus-5.5",
    "claude-haiku-5-5": "anthropic/claude-haiku-5.5",
}
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


class AIProviderNotConfiguredError(Exception):
    """No AI provider key resolvable; callers fall back to keyless mode."""


class AIProvider(Protocol):
    async def parse_structured(self, system: str, user: str, model: str, output_model: type[M], max_tokens: int) -> M: ...

    async def complete_text(self, system: str, user: str, model: str, max_tokens: int) -> str: ...


class AnthropicProvider:
    """Anthropic API via the official SDK (primary path: messages.parse)."""

    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic()
        self.client = client

    async def parse_structured(self, system: str, user: str, model: str, output_model: type[M], max_tokens: int) -> M:
        messages = [{"role": "user", "content": user}]
        if hasattr(self.client.messages, "parse"):
            response = await self.client.messages.parse(
                model=model, max_tokens=max_tokens, system=system,
                messages=messages, output_format=output_model,
            )
            return response.parsed_output
        response = await self.client.messages.create(
            model=model, max_tokens=max_tokens, system=system, messages=messages,
            output_config={"format": {"type": "json_schema", "schema": output_model.model_json_schema()}},
        )
        raw = next(block.text for block in response.content if block.type == "text")
        return output_model.model_validate_json(raw)

    async def complete_text(self, system: str, user: str, model: str, max_tokens: int) -> str:
        response = await self.client.messages.create(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(block.text for block in response.content if block.type == "text")


class OpenRouterProvider:
    """OpenRouter's OpenAI-compatible chat/completions endpoint (httpx).

    Structured extraction first tries `response_format: json_schema` (strict);
    on HTTP 400/404 for that field it falls back to instructed JSON-only output
    parsed client-side (code fences stripped). `http` is injectable for tests.
    """

    def __init__(self, api_key: str, http: httpx.AsyncClient | None = None) -> None:
        self.api_key = api_key
        self.http = http or httpx.AsyncClient(base_url=OPENROUTER_BASE_URL, timeout=120.0)

    def _slug(self, model: str) -> str:
        return OPENROUTER_MODEL_MAP.get(model, model)

    async def _chat(self, payload: dict[str, Any]) -> httpx.Response:
        return await self.http.post(
            "/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": "https://github.com/yavuz-ozkan/skillabel",
                "X-Title": "Skillabel",
            },
            json=payload,
        )

    @staticmethod
    def _content(response: httpx.Response) -> str:
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    async def parse_structured(self, system: str, user: str, model: str, output_model: type[M], max_tokens: int) -> M:
        # Instructed-JSON is the PRIMARY strategy, deliberately. OpenRouter's
        # `response_format: json_schema` was probed live (2026-07-03) with
        # anthropic/claude-sonnet-5 and consistently produced degraded results
        # (empty nested arrays) while instructed JSON produced complete ones —
        # provider-dependent transforms make response_format unreliable here.
        # On a Pydantic validation failure, retry once quoting the error back.
        schema_msg = (
            system
            + "\n\nRespond with ONLY a valid JSON object matching this JSON Schema — no prose, no code fences:\n"
            + json.dumps(output_model.model_json_schema())
        )
        payload = {
            "model": self._slug(model),
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": schema_msg}, {"role": "user", "content": user}],
        }
        raw = _FENCE_RE.sub("", self._content(await self._chat(payload)).strip())
        try:
            return output_model.model_validate_json(raw)
        except Exception as first_error:  # pydantic.ValidationError / JSON errors
            retry = {
                **payload,
                "messages": payload["messages"]
                + [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": "That response failed schema validation: "
                     f"{first_error}. Respond again with ONLY the corrected JSON object."},
                ],
            }
            raw = _FENCE_RE.sub("", self._content(await self._chat(retry)).strip())
            return output_model.model_validate_json(raw)

    async def complete_text(self, system: str, user: str, model: str, max_tokens: int) -> str:
        response = await self._chat({
            "model": self._slug(model),
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        })
        return self._content(response)


def describe_provider() -> tuple[str, str] | None:
    """(provider name, the environment variable its key comes from), picked by
    the same rules as :func:`resolve_provider`, or None when none is set."""
    choice = os.environ.get("SKILLABEL_AI_PROVIDER", "auto").lower()
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    if choice == "anthropic":
        return ("anthropic", "ANTHROPIC_API_KEY") if anthropic_key else None
    if choice == "openrouter":
        return ("openrouter", "OPENROUTER_API_KEY") if openrouter_key else None
    if anthropic_key:
        return ("anthropic", "ANTHROPIC_API_KEY")
    if openrouter_key:
        return ("openrouter", "OPENROUTER_API_KEY")
    return None


def resolve_provider() -> AIProvider:
    """Pick the provider per SKILLABEL_AI_PROVIDER (auto|anthropic|openrouter)."""
    choice = os.environ.get("SKILLABEL_AI_PROVIDER", "auto").lower()
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")

    if choice == "anthropic":
        if not anthropic_key:
            raise AIProviderNotConfiguredError()
        return AnthropicProvider()
    if choice == "openrouter":
        if not openrouter_key:
            raise AIProviderNotConfiguredError()
        return OpenRouterProvider(openrouter_key)
    if anthropic_key:
        return AnthropicProvider()
    if openrouter_key:
        return OpenRouterProvider(openrouter_key)
    raise AIProviderNotConfiguredError()
