import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from time import perf_counter, sleep
from typing import Any, Protocol

import httpx
from pydantic import SecretStr

ChatMessage = dict[str, Any]


class ChatConfig(Protocol):
    base_url: str
    model: str
    api_key: SecretStr | None
    timeout: float
    max_retries: int


@dataclass(frozen=True, slots=True)
class ChatResult:
    message: ChatMessage
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0
    retries: int = 0
    cache_hit: bool = False


class ChatModel(Protocol):
    model: str

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ChatMessage] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult: ...


class ChatClient:
    def __init__(
        self,
        config: ChatConfig,
        cache_dir: Path | None = Path("eval/cache/llm"),
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not config.model.strip():
            raise ValueError("Configure the model before sending requests")
        self.model = config.model
        self.url = f"{config.base_url.rstrip('/')}/chat/completions"
        self.max_retries = config.max_retries
        self.temperature = getattr(config, "temperature", 0)
        self.max_output_tokens = getattr(config, "max_output_tokens", None)
        self.cache_path = None if cache_dir is None else cache_dir / "responses.sqlite3"
        if self.cache_path is not None:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.cache_path)) as connection, connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, body TEXT)"
                )
        headers = (
            {"Authorization": f"Bearer {config.api_key.get_secret_value()}"}
            if config.api_key is not None
            else {}
        )
        self.client = httpx.Client(headers=headers, transport=transport, timeout=config.timeout)

    def __enter__(self) -> "ChatClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.client.close()

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ChatMessage] | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> ChatResult:
        started = perf_counter()
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools is not None:
            body["tools"] = tools
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        elif self.max_output_tokens is not None:
            body["max_tokens"] = self.max_output_tokens
        if response_format is not None:
            body["response_format"] = response_format
        fingerprint = json.dumps([self.url, body], sort_keys=True, separators=(",", ":"))
        key = sha256(fingerprint.encode()).hexdigest()
        result = self._cached(key)
        if result is not None:
            return replace(result, latency_ms=(perf_counter() - started) * 1000, cache_hit=True)

        for attempt in range(self.max_retries + 1):
            response = self.client.post(self.url, json=body)
            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or attempt == self.max_retries:
                response.raise_for_status()
                break
            retry_after = response.headers.get("Retry-After", "")
            delay = min(float(retry_after), 60) if retry_after.isdecimal() else 2 ** min(attempt, 6)
            sleep(delay)
        raw = response.json()
        message = raw["choices"][0]["message"]
        usage = raw.get("usage") or {}
        result = ChatResult(
            message=message,
            input_tokens=usage.get("prompt_tokens") or 0,
            output_tokens=usage.get("completion_tokens") or 0,
            latency_ms=(perf_counter() - started) * 1000,
            retries=attempt,
        )
        if self.cache_path is not None:
            with closing(sqlite3.connect(self.cache_path)) as connection, connection:
                connection.execute(
                    "INSERT OR IGNORE INTO responses VALUES (?, ?)",
                    (key, json.dumps(raw)),
                )
        return result

    def _cached(self, key: str) -> ChatResult | None:
        if self.cache_path is None:
            return None
        with closing(sqlite3.connect(self.cache_path)) as connection:
            stored = connection.execute(
                "SELECT body FROM responses WHERE key = ?", (key,)
            ).fetchone()
        if stored is None:
            return None
        raw = json.loads(stored[0])
        usage = raw.get("usage") or {}
        return ChatResult(
            message=raw["choices"][0]["message"],
            input_tokens=usage.get("prompt_tokens") or 0,
            output_tokens=usage.get("completion_tokens") or 0,
        )
