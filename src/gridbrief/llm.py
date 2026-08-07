"""Small, bounded LLM client used by the generation graph.

The client deliberately has no SDK dependency.  Groq exposes an
OpenAI-compatible chat endpoint; when no key is configured callers can use
the deterministic fallbacks in :mod:`gridbrief.graph`.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from gridbrief.config import get_settings

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"


class LLMError(RuntimeError):
    """A readable model-service failure."""


@dataclass(slots=True)
class LLMClient:
    """OpenAI-compatible JSON client with retries and output bounds."""

    timeout: float = 30.0
    max_retries: int = 2
    max_tokens: int = 1_200

    @property
    def available(self) -> bool:
        settings = get_settings()
        return bool(
            os.getenv("GRIDBRIEF_ALLOW_REMOTE_LLM", "").lower() in {"1", "true", "yes"}
            and settings.groq_api_key
            and settings.groq_model
            and settings.groq_model != "<currently-supported-model>"
        )

    def complete_json(self, *, system: str, user: str) -> Any:
        settings = get_settings()
        if not self.available:
            raise LLMError("Groq is not configured")
        payload = json.dumps(
            {
                "model": settings.groq_model,
                "temperature": 0,
                "max_tokens": self.max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
        ).encode()
        request = Request(
            GROQ_CHAT_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {settings.groq_api_key.get_secret_value()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        for attempt in range(self.max_retries + 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                    body = json.loads(response.read().decode("utf-8"))
                content = body["choices"][0]["message"]["content"]
                return json.loads(content)
            except (HTTPError, URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
                if attempt == self.max_retries:
                    raise LLMError(
                        f"LLM request failed after {attempt + 1} attempts: {exc}"
                    ) from exc
                time.sleep(0.25 * (2**attempt))
        raise LLMError("LLM request failed")


def get_llm_client() -> LLMClient:
    return LLMClient()
