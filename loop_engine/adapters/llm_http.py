"""Provider-neutral JSON client for OpenAI-compatible HTTP gateways."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class LLMJSONDecodeError(ValueError):
    def __init__(self, message: str, *, raw_content: str) -> None:
        super().__init__(message)
        self.raw_content = raw_content


class OpenAICompatibleJSONClient:
    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8000",
        model: str = "auto",
        timeout_seconds: float = 120.0,
        api_key: str | None = None,
        max_tokens: int = 2048,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("LLM timeout must be positive")
        self.url = f"{base_url.rstrip('/')}/v1/chat/completions"
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.api_key = api_key
        self.max_tokens = max_tokens

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": 0.0,
            "max_tokens": self.max_tokens,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM gateway HTTP {exc.code}: {body[:1000]}") from exc
        except URLError as exc:
            raise RuntimeError(f"LLM gateway unavailable: {exc.reason}") from exc

        try:
            content = response_payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("LLM gateway response has no assistant content") from exc
        try:
            return parse_json_object(content)
        except ValueError as exc:
            raise LLMJSONDecodeError(str(exc), raw_content=content) from exc


def parse_json_object(content: str) -> dict[str, Any]:
    normalized = content.strip()
    if normalized.startswith("```") and normalized.endswith("```"):
        lines = normalized.splitlines()
        if len(lines) >= 3:
            normalized = "\n".join(lines[1:-1]).strip()
            if normalized.lower().startswith("json\n"):
                normalized = normalized[5:].lstrip()
    try:
        value = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM response is not valid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("LLM response must be a JSON object")
    return value
