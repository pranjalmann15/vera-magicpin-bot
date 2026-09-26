"""Provider-agnostic LLM client (optional).

The bot is fully functional without an LLM — playbooks produce grounded drafts.
When a provider is configured, the LLM *polishes* those drafts and handles
open-ended replies; its output is always re-validated against the fact ledger.

Configuration (environment):
  VERA_LLM_PROVIDER   anthropic | openai | gemini | groq | deepseek | openrouter | ollama | none
                      (auto-detected from whichever *_API_KEY is set when omitted)
  VERA_LLM_MODEL      model id (sensible default per provider)
  VERA_LLM_API_KEY    key (falls back to the provider's usual env var)
  VERA_LLM_TIMEOUT    seconds per call (default 12)
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

import httpx

log = logging.getLogger("vera.llm")

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
    "groq": "llama-3.3-70b-versatile",
    "deepseek": "deepseek-chat",
    "openrouter": "anthropic/claude-sonnet-5",
    "ollama": "llama3.1",
}
KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "openrouter": "OPENROUTER_API_KEY",
}
OPENAI_COMPAT = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/") + "/v1",
}


class LLM:
    def __init__(self, provider: str, model: str, api_key: str, timeout: float = 12.0) -> None:
        self.provider, self.model, self.api_key, self.timeout = provider, model, api_key, timeout
        self._client = httpx.Client(timeout=timeout)

    @property
    def name(self) -> str:
        return f"{self.provider}:{self.model}"

    def complete(self, system: str, user: str, max_tokens: int = 700, timeout: Optional[float] = None) -> str:
        t = timeout or self.timeout
        if self.provider == "anthropic":
            return self._anthropic(system, user, max_tokens, t)
        if self.provider == "gemini":
            return self._gemini(system, user, max_tokens, t)
        return self._openai_compat(system, user, max_tokens, t)

    def complete_json(self, system: str, user: str, max_tokens: int = 700, timeout: Optional[float] = None) -> Optional[dict]:
        try:
            raw = self.complete(system, user, max_tokens, timeout)
        except Exception as e:  # network, auth, rate limit — caller falls back
            log.warning("LLM call failed: %s", e)
            return None
        m = re.search(r"\{[\s\S]*\}", raw or "")
        if not m:
            return None
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            return None

    # ---------------------------------------------------------------- providers
    def _anthropic(self, system: str, user: str, max_tokens: int, t: float) -> str:
        body = {"model": self.model, "max_tokens": max_tokens, "temperature": 0,
                "system": system, "messages": [{"role": "user", "content": user}]}
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        r = self._client.post("https://api.anthropic.com/v1/messages", json=body, headers=headers, timeout=t)
        if r.status_code == 400 and "temperature" in r.text:
            body.pop("temperature")  # some models fix sampling; determinism then comes from the cache
            r = self._client.post("https://api.anthropic.com/v1/messages", json=body, headers=headers, timeout=t)
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")

    def _openai_compat(self, system: str, user: str, max_tokens: int, t: float) -> str:
        base = OPENAI_COMPAT.get(self.provider, OPENAI_COMPAT["openai"])
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {"model": self.model, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        r = self._client.post(f"{base}/chat/completions", json=body, headers=headers, timeout=t)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    def _gemini(self, system: str, user: str, max_tokens: int, t: float) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens}}
        r = self._client.post(url, json=body, timeout=t)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def from_env() -> Optional[LLM]:
    provider = os.environ.get("VERA_LLM_PROVIDER", "").strip().lower()
    if provider in ("none", "off", "disabled"):
        return None
    if not provider:
        provider = next((p for p, env in KEY_ENV.items() if os.environ.get(env)), "")
        if not provider:
            return None
    key = os.environ.get("VERA_LLM_API_KEY") or os.environ.get(KEY_ENV.get(provider, ""), "")
    if not key and provider != "ollama":
        log.warning("VERA_LLM_PROVIDER=%s but no API key found; running playbook-only", provider)
        return None
    model = os.environ.get("VERA_LLM_MODEL") or DEFAULT_MODELS.get(provider, "")
    timeout = float(os.environ.get("VERA_LLM_TIMEOUT", "12"))
    return LLM(provider, model, key, timeout)
