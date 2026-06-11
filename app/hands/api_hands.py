"""Direct provider API hands — the tail of each family's fallback chain.

Raw httpx by design (no provider SDKs in this project's dependency set).
Contract: never raise for API-level failures — 429 returns a structured
RateLimitInfo; any other HTTP error returns exit_code=1 with the body in
output (rate-limit/quota *wording* in non-429 error bodies still gets flagged
via detect_rate_limit).
"""
from __future__ import annotations

import logging
from abc import abstractmethod
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings
from .base import Hand, HandResult, OnChunk, RateLimitInfo
from .rate_limit import detect_rate_limit

log = logging.getLogger("institute.hands.api")

_ERROR_BODY_CAP = 4000


def _parse_retry_after(value: str | None) -> int | None:
    """Retry-After is either delta-seconds or an HTTP-date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(1, int(float(value)))
    except ValueError:
        pass
    try:
        delta = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return None
    return max(1, int(delta)) if delta > 0 else None


class _ApiHand(Hand):
    hand_type = "api"

    def __init__(self, settings: Settings):
        self.settings = settings

    @abstractmethod
    def _api_key(self) -> str | None: ...

    @abstractmethod
    def _request(self, prompt: str, model: str | None) -> tuple[str, dict[str, str], dict[str, Any]]:
        """Return (url, headers, json_body)."""

    @abstractmethod
    def _extract(self, data: dict[str, Any]) -> str:
        """Pull the response text out of a 2xx JSON body."""

    def available(self) -> bool:
        return bool(self._api_key())

    async def execute(
        self,
        prompt: str,
        workspace: Path,
        *,
        model: str | None = None,
        timeout_s: int = 1800,
        on_chunk: OnChunk | None = None,
    ) -> HandResult:
        url, headers, body = self._request(prompt, model)
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            return HandResult(output=f"{self.name} request failed: {exc}", exit_code=1)

        if resp.status_code == 429:
            return HandResult(
                output=resp.text[:_ERROR_BODY_CAP],
                exit_code=1,
                rate_limit=RateLimitInfo(
                    reason="rate_limit",
                    retry_after_s=_parse_retry_after(resp.headers.get("retry-after")),
                    raw=resp.text[:300],
                ),
            )
        if resp.status_code >= 400:
            return HandResult(
                output=f"{self.name} HTTP {resp.status_code}: {resp.text[:_ERROR_BODY_CAP]}",
                exit_code=1,
                rate_limit=detect_rate_limit(self.name, resp.text),
            )

        try:
            text = self._extract(resp.json())
        except (ValueError, LookupError, TypeError):
            log.warning("%s: unexpected response shape", self.name)
            return HandResult(
                output=f"{self.name} unexpected response shape: {resp.text[:_ERROR_BODY_CAP]}",
                exit_code=1,
            )
        if on_chunk:
            try:
                on_chunk({"type": "stdout", "text": text})
            except Exception:  # noqa: BLE001 - chunk consumers must not break the hand
                pass
        return HandResult(output=text, exit_code=0)


class ClaudeApiHand(_ApiHand):
    name = "claude-api"

    def _api_key(self) -> str | None:
        return self.settings.anthropic_api_key

    def _request(self, prompt: str, model: str | None) -> tuple[str, dict[str, str], dict[str, Any]]:
        return (
            "https://api.anthropic.com/v1/messages",
            {
                "x-api-key": self._api_key() or "",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            {
                "model": model or self.settings.anthropic_api_model,
                "max_tokens": 8000,
                "messages": [{"role": "user", "content": prompt}],
            },
        )

    def _extract(self, data: dict[str, Any]) -> str:
        return "".join(
            block.get("text", "") for block in data["content"] if block.get("type") == "text"
        )


class OpenAiApiHand(_ApiHand):
    name = "openai-api"

    def _api_key(self) -> str | None:
        return self.settings.openai_api_key

    def _request(self, prompt: str, model: str | None) -> tuple[str, dict[str, str], dict[str, Any]]:
        return (
            "https://api.openai.com/v1/chat/completions",
            {
                "Authorization": f"Bearer {self._api_key()}",
                "content-type": "application/json",
            },
            {
                "model": model or self.settings.openai_api_model,
                "messages": [{"role": "user", "content": prompt}],
            },
        )

    def _extract(self, data: dict[str, Any]) -> str:
        return data["choices"][0]["message"].get("content") or ""


class GeminiApiHand(_ApiHand):
    name = "gemini-api"

    def _api_key(self) -> str | None:
        return self.settings.google_api_key

    def _request(self, prompt: str, model: str | None) -> tuple[str, dict[str, str], dict[str, Any]]:
        effective_model = model or self.settings.google_api_model
        return (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{effective_model}:generateContent?key={self._api_key()}",
            {"content-type": "application/json"},
            {"contents": [{"role": "user", "parts": [{"text": prompt}]}]},
        )

    def _extract(self, data: dict[str, Any]) -> str:
        candidates = data.get("candidates") or []
        if not candidates:
            return ""
        parts = (candidates[0].get("content") or {}).get("parts") or []
        return "".join(part.get("text", "") for part in parts)
