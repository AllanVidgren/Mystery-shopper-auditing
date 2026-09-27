"""Thin LLM layer. Talks to the Claude Messages API over HTTP (no SDK needed).

Two operations:
  text(system, messages)                 -> str
  structured(system, messages, schema)   -> dict   (forced tool call = reliable JSON)

`ScriptedLLM` replays canned answers so the whole engine can be tested offline.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Protocol

import httpx

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class LLM(Protocol):
    model: str

    def text(self, system: str, messages: list[dict], max_tokens: int = 800, temperature: float = 0.7) -> str: ...

    def structured(self, system: str, messages: list[dict], schema: dict, name: str = "record",
                   max_tokens: int = 1200) -> dict: ...


class LLMError(RuntimeError):
    pass


class ClaudeLLM:
    def __init__(self, api_key: str, model: str, timeout: float = 90.0, max_retries: int = 4):
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self.model = model
        self._client = httpx.Client(timeout=timeout)
        self._headers = {
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        self._max_retries = max_retries

    def _post(self, payload: dict) -> dict:
        delay = 2.0
        for attempt in range(self._max_retries + 1):
            r = self._client.post(API_URL, headers=self._headers, json=payload)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 529) and attempt < self._max_retries:
                time.sleep(float(r.headers.get("retry-after", delay)))
                delay *= 2
                continue
            raise LLMError(f"Claude API error {r.status_code}: {r.text[:500]}")
        raise LLMError("Claude API: retries exhausted")

    def text(self, system: str, messages: list[dict], max_tokens: int = 800, temperature: float = 0.7) -> str:
        data = self._post({
            "model": self.model, "system": system, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature,
        })
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()

    def structured(self, system: str, messages: list[dict], schema: dict, name: str = "record",
                   max_tokens: int = 1200) -> dict:
        data = self._post({
            "model": self.model, "system": system, "messages": messages,
            "max_tokens": max_tokens, "temperature": 0,
            "tools": [{"name": name, "description": "Return the result.", "input_schema": schema}],
            "tool_choice": {"type": "tool", "name": name},
        })
        for block in data.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == name:
                return block["input"]
        raise LLMError("Model did not return structured output")


class OpenAICompatibleLLM:
    """Any provider with an OpenAI-style /chat/completions endpoint:
    OpenAI, Google Gemini, Mistral, Groq, OpenRouter, Ollama (local), Azure-style gateways.
    Structured output uses function calling, like the Claude client uses tool use."""

    def __init__(self, api_key: str, model: str, base_url: str, timeout: float = 90.0, max_retries: int = 6):
        if not base_url:
            raise LLMError("MS_BASE_URL is not set")
        self.model = model
        self.url = base_url.rstrip("/") + "/chat/completions"
        self._client = httpx.Client(timeout=timeout)
        self._headers = {"content-type": "application/json"}
        if api_key:
            self._headers["authorization"] = f"Bearer {api_key}"
        self._max_retries = max_retries

    def _post(self, payload: dict) -> dict:
        delay = 2.0
        for attempt in range(self._max_retries + 1):
            r = self._client.post(self.url, headers=self._headers, json=payload)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429 and ("quota" in r.text.lower() and "per day" in r.text.lower()):
                raise LLMError(f"Daily quota used up: {r.text[:400]}")
            if r.status_code in (429, 500, 502, 503) and attempt < self._max_retries:
                wait = float(r.headers.get("retry-after", delay))
                logging.getLogger("mystery_shopper").info("  provider busy (HTTP %s), retrying in %.0fs", r.status_code, wait)
                time.sleep(wait)
                delay *= 2
                continue
            raise LLMError(f"LLM API error {r.status_code}: {r.text[:500]}")
        raise LLMError("LLM API: retries exhausted")

    @staticmethod
    def _msgs(system: str, messages: list[dict]) -> list[dict]:
        return [{"role": "system", "content": system}] + messages

    def text(self, system, messages, max_tokens=800, temperature=0.7):
        data = self._post({"model": self.model, "messages": self._msgs(system, messages),
                           "max_tokens": max_tokens, "temperature": temperature})
        return (data["choices"][0]["message"].get("content") or "").strip()

    def structured(self, system, messages, schema, name="record", max_tokens=1200):
        data = self._post({
            "model": self.model, "messages": self._msgs(system, messages), "max_tokens": max_tokens,
            "temperature": 0,
            "tools": [{"type": "function", "function": {"name": name, "description": "Return the result.",
                                                        "parameters": schema}}],
            "tool_choice": {"type": "function", "function": {"name": name}},
        })
        msg = data["choices"][0]["message"]
        for call in msg.get("tool_calls") or []:
            fn = call.get("function", {})
            if fn.get("name") == name:
                args = fn.get("arguments")
                return json.loads(args) if isinstance(args, str) else args
        content = (msg.get("content") or "").strip()  # some local models answer in plain JSON
        try:
            return json.loads(content.strip("`").removeprefix("json").strip())
        except (json.JSONDecodeError, AttributeError):
            raise LLMError("Model did not return structured output")


def make_llm(settings) -> "LLM":
    """Pick the provider from settings: MS_PROVIDER=anthropic (default) or openai."""
    if settings.provider == "openai":
        return OpenAICompatibleLLM(settings.llm_api_key, settings.model, settings.base_url)
    return ClaudeLLM(settings.anthropic_api_key, settings.model)


class ScriptedLLM:
    """Deterministic stand-in for tests and dry runs.

    text_fn(system, messages) -> str
    structured_fn(system, messages, schema, name) -> dict
    """

    model = "scripted"

    def __init__(self, text_fn: Callable[[str, list[dict]], str],
                 structured_fn: Callable[[str, list[dict], dict, str], dict]):
        self._text_fn = text_fn
        self._structured_fn = structured_fn
        self.calls: list[tuple[str, Any]] = []

    def text(self, system, messages, max_tokens=800, temperature=0.7):
        self.calls.append(("text", system[:60]))
        return self._text_fn(system, messages)

    def structured(self, system, messages, schema, name="record", max_tokens=1200):
        self.calls.append(("structured", name))
        return self._structured_fn(system, messages, schema, name)


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
