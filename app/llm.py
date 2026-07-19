"""LLM client for NVIDIA NIM (OpenAI-compatible endpoint).

Free API key: sign up at https://build.nvidia.com and create a key that starts
with ``nvapi-`` (takes ~2 minutes). Set it as NVIDIA_API_KEY.

The agent degrades gracefully when no key is configured: intent routing falls
back to keywords and FAQ answers return the top knowledge-base snippet verbatim.
"""

from __future__ import annotations

import logging
from typing import Optional

from .config import Settings

log = logging.getLogger("whatsapp_agent.llm")


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None
        if settings.llm_configured:
            try:
                from openai import OpenAI

                self._client = OpenAI(
                    base_url=settings.nim_base_url,
                    api_key=settings.nvidia_api_key,
                    timeout=30.0,
                )
            except Exception as exc:  # pragma: no cover - import/config failure
                log.warning("Could not initialise NIM client: %s", exc)
                self._client = None
        else:
            log.warning(
                "NVIDIA_API_KEY not set. Running in offline mode "
                "(keyword routing + raw KB answers). Get a free key at "
                "https://build.nvidia.com"
            )

    @property
    def available(self) -> bool:
        return self._client is not None

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 600,
    ) -> Optional[str]:
        """Return the assistant text, or None on any failure / when offline."""
        if self._client is None:
            return None
        try:
            resp = self._client.chat.completions.create(
                model=self.settings.nim_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            content = resp.choices[0].message.content
            return content.strip() if content else None
        except Exception as exc:
            log.warning("NIM request failed: %s", exc)
            return None
