"""Ollama hand: local HTTP generation via /api/generate (non-streaming)."""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from ..config import Settings
from .base import Hand, HandResult, OnChunk

log = logging.getLogger("institute.hands.ollama")


class OllamaHand(Hand):
    name = "ollama"
    hand_type = "http"

    def __init__(self, settings: Settings):
        self.settings = settings

    def available(self) -> bool:
        return self.settings.enable_ollama

    @property
    def _base_url(self) -> str:
        return self.settings.ollama_host.rstrip("/")

    async def execute(
        self,
        prompt: str,
        workspace: Path,
        *,
        model: str | None = None,
        timeout_s: int = 1800,
        on_chunk: OnChunk | None = None,
    ) -> HandResult:
        body = {
            "model": model or self.settings.ollama_model,
            "prompt": prompt,
            "stream": False,
        }
        try:
            # trust_env=False: ollama is a loopback service — the machine-wide
            # SOCKS proxy env vars must not apply (same pitfall as api_hands)
            async with httpx.AsyncClient(timeout=timeout_s, trust_env=False) as client:
                resp = await client.post(f"{self._base_url}/api/generate", json=body)
        except httpx.HTTPError as exc:
            return HandResult(output=f"ollama request failed: {exc}", exit_code=1)

        if resp.status_code != 200:
            return HandResult(
                output=f"ollama HTTP {resp.status_code}: {resp.text[:4000]}", exit_code=1
            )
        try:
            text = resp.json().get("response", "")
        except ValueError:
            return HandResult(output=f"ollama returned non-JSON: {resp.text[:4000]}", exit_code=1)

        if on_chunk:
            try:
                on_chunk({"type": "stdout", "text": text})
            except Exception:  # noqa: BLE001 - chunk consumers must not break the hand
                pass
        return HandResult(output=text, exit_code=0)

    async def health_check(self) -> bool:
        if not self.available():
            return False
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                resp = await client.get(f"{self._base_url}/api/tags")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
