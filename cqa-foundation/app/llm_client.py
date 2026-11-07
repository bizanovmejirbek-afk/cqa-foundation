"""LLM clients with one interface: complete_json(system, user, schema) -> str | dict.

Provider is chosen by LLM_PROVIDER in .env:
  openai   - any OpenAI-compatible /chat/completions API (OpenAI, OpenRouter, Groq,
             Ollama, vLLM, Gemini's OpenAI-compatible endpoint, ...)
  anthropic - Anthropic Messages API (structured output via a forced tool call)
  mock     - offline demo judge, no key needed (default when no key is set)

Every client counts tokens so the cost of one evaluation can be reported.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from decimal import Decimal

import httpx


class LLMError(RuntimeError):
    """LLM provider error with a human-readable message (never contains the API key)."""


def inline_refs(schema: dict) -> dict:
    """Replace $ref/$defs with inline copies; some providers reject $ref."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)


class _Base:
    provider = "base"

    def __init__(self) -> None:
        self.model = os.getenv("LLM_MODEL", "")
        self.p_in = Decimal(os.getenv("LLM_PRICE_IN_PER_1M", "0") or "0")
        self.p_out = Decimal(os.getenv("LLM_PRICE_OUT_PER_1M", "0") or "0")
        self.tokens_in = 0
        self.tokens_out = 0
        self.calls = 0
        self._lock = threading.Lock()  # criteria are judged in parallel threads

    def _count(self, t_in: int, t_out: int) -> None:
        with self._lock:
            self.tokens_in += int(t_in or 0)
            self.tokens_out += int(t_out or 0)
            self.calls += 1

    @property
    def cost_usd(self) -> Decimal:
        return (self.tokens_in * self.p_in + self.tokens_out * self.p_out) / Decimal(1_000_000)

    def _post(self, url: str, payload: dict, headers: dict) -> httpx.Response:
        """POST with retries on rate limits and server errors."""
        delay = 2.0
        for attempt in range(4):
            try:
                r = httpx.post(url, json=payload, headers=headers,
                               timeout=float(os.getenv("LLM_TIMEOUT", "120")))
            except httpx.TimeoutException:
                raise LLMError(f"{self.provider}: no answer within {os.getenv('LLM_TIMEOUT', '120')} s "
                               f"from {url.split('?')[0]} (increase LLM_TIMEOUT or lower LLM_MAX_WORKERS)")
            except httpx.RequestError as e:
                raise LLMError(f"{self.provider}: cannot connect to {url.split('?')[0]} ({type(e).__name__}). "
                               "Check LLM_BASE_URL and your internet connection.")
            if r.status_code in (429, 500, 502, 503, 504, 529) and attempt < 3:
                try:
                    wait = float(r.headers.get("retry-after", delay))
                except ValueError:
                    wait = delay
                time.sleep(min(wait, 30))
                delay *= 2
                continue
            return r
        return r

    def _check(self, r: httpx.Response) -> None:
        if r.status_code < 400:
            return
        hints = {400: "request format or model name rejected", 401: "API key is invalid",
                 403: "API key has no access to this model/region", 404: "model or URL not found (check LLM_MODEL / LLM_BASE_URL)",
                 429: "rate limit or free quota exhausted"}
        body = r.text.strip().replace("\n", " ")[:600]
        raise LLMError(f"{self.provider} HTTP {r.status_code} ({hints.get(r.status_code, 'provider error')}): {body}")


class OpenAICompatibleClient(_Base):
    provider = "openai"

    def __init__(self) -> None:
        super().__init__()
        self.url = os.environ["LLM_BASE_URL"].rstrip("/") + "/chat/completions"
        self.headers = {"Authorization": f"Bearer {os.getenv('LLM_API_KEY', '')}"}
        self.json_schema_supported = True

    def complete_json(self, system: str, user: str, schema: dict) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        payload = {"model": self.model, "temperature": 0, "messages": messages}
        if self.json_schema_supported:
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "judge", "schema": inline_refs(schema), "strict": False}}
        else:  # JSON mode + schema in the prompt
            messages[1]["content"] = user + "\n\nJSON schema:\n" + json.dumps(inline_refs(schema))
            payload["response_format"] = {"type": "json_object"}
        if not getattr(self, "send_temperature", True):
            payload.pop("temperature", None)
        r = self._post(self.url, payload, self.headers)
        if r.status_code == 400 and "temperature" in r.text.lower() and getattr(self, "send_temperature", True):
            self.send_temperature = False  # some models reject temperature: retry without it
            return self.complete_json(system, user, schema)
        if r.status_code == 400 and self.json_schema_supported and "model" not in r.text.lower()[:300]:
            self.json_schema_supported = False  # provider rejects json_schema: retry in JSON mode
            return self.complete_json(system, user, schema)
        self._check(r)
        data = r.json()
        usage = data.get("usage") or {}
        self._count(usage.get("prompt_tokens"), usage.get("completion_tokens"))
        return data["choices"][0]["message"]["content"] or ""


class AnthropicClient(_Base):
    provider = "anthropic"

    def __init__(self) -> None:
        super().__init__()
        self.url = (os.getenv("LLM_BASE_URL") or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        self.headers = {"x-api-key": os.getenv("LLM_API_KEY", ""), "anthropic-version": "2023-06-01"}

    def complete_json(self, system: str, user: str, schema: dict) -> dict | str:
        payload = {
            "model": self.model, "max_tokens": 2000, "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [{"name": "submit_evaluation", "description": "Submit the evaluation.",
                       "input_schema": inline_refs(schema)}],
            "tool_choice": {"type": "tool", "name": "submit_evaluation"},
        }
        r = self._post(self.url, payload, self.headers)
        self._check(r)
        data = r.json()
        usage = data.get("usage") or {}
        self._count(usage.get("input_tokens"), usage.get("output_tokens"))
        for block in data.get("content", []):
            if block.get("type") == "tool_use":
                return block["input"]
        return "".join(b.get("text", "") for b in data.get("content", []))


_LINE = re.compile(r"^#(\d+) \[[^\]]*\] OPERATOR: (.+)$", re.M)


class MockClient(_Base):
    """Offline demo judge. It is NOT an evaluator: it gives full marks with the first
    operator message as evidence, so the whole pipeline can be shown without a key.
    Confidence 0.5 sends every result to the review queue."""
    provider = "mock"

    def __init__(self) -> None:
        super().__init__()
        self.model = "mock-judge"

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        self._count(len(system + user) // 4, 150)
        m = _LINE.search(user.split("<transcript>", 1)[-1])
        evidence = [{"message_index": int(m.group(1)), "quote": m.group(2)[:60].strip()}] if m else []
        props = schema.get("properties", {})
        verdicts = props.get("verdict", {}).get("enum") or ["NO"]
        is_violation = "(YES = a violation was found" in user
        verdict = "NO" if is_violation else verdicts[0]
        out = {"evidence": [] if is_violation else evidence,
               "reasoning": "MOCK judge (no LLM key configured): placeholder verdict.",
               "verdict": verdict, "confidence": 0.5}
        if "sub_checks" in props:
            ids = re.findall(r"^- ([a-z][a-z0-9_]+): ", user.split("SUB-CHECKS", 1)[-1], re.M)
            out["sub_checks"] = [{"check_id": i, "answer": "YES", "evidence": evidence} for i in ids]
        return out


def make_client() -> _Base:
    key = os.getenv("LLM_API_KEY")
    provider = (os.getenv("LLM_PROVIDER") or ("openai" if key else "mock")).lower()
    if provider == "anthropic" and not key:
        provider = "mock"
    if provider == "openai" and not os.getenv("LLM_BASE_URL"):
        provider = "mock"  # nothing to call (local Ollama: set LLM_BASE_URL, key may stay empty)
    if provider == "anthropic":
        return AnthropicClient()
    if provider == "openai":
        return OpenAICompatibleClient()
    return MockClient()
