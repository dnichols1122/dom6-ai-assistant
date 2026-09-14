"""OpenAI-compatible backend.

Works with:
  - OpenAI API (ChatGPT models)
  - Any OpenAI-compatible local server: vLLM, LM Studio, Jan, llama.cpp server, etc.

Config example for OpenAI (dom6-assistant.toml):
    [model]
    backend = "openai"

    [model.openai]
    api_key_env = "OPENAI_API_KEY"
    model       = "gpt-4o"

Config example for a local OpenAI-compatible server (e.g. vLLM or LM Studio):
    [model.openai]
    api_key_env = "OPENAI_API_KEY"   # can be any non-empty string for local servers
    base_url    = "http://localhost:1234/v1"
    model       = "mistral-7b-instruct"
"""

from __future__ import annotations

import os
from typing import Any, Iterator

try:
    from openai import OpenAI as _OpenAI
except ImportError as exc:
    raise ImportError(
        "openai package not installed. Run: pip install 'dom6-assistant[openai]'"
    ) from exc


class OpenAIBackend:
    def __init__(self, config: dict[str, Any]) -> None:
        self._model = config.get("model", "gpt-4o")
        api_key_env = config.get("api_key_env", "OPENAI_API_KEY")
        api_key = os.environ.get(api_key_env, "local")  # "local" works for local servers
        base_url = config.get("base_url", "https://api.openai.com/v1")
        self._client = _OpenAI(api_key=api_key, base_url=base_url)

    @property
    def name(self) -> str:
        return f"openai/{self._model}"

    def chat(self, system: str, messages: list[dict[str, str]]) -> str:
        full_messages = [{"role": "system", "content": system}, *messages]
        response = self._client.chat.completions.create(
            model=self._model,
            messages=full_messages,  # type: ignore[arg-type]
        )
        return response.choices[0].message.content or ""

    def stream(self, system: str, messages: list[dict[str, str]]) -> Iterator[str]:
        full_messages = [{"role": "system", "content": system}, *messages]
        with self._client.chat.completions.create(
            model=self._model,
            messages=full_messages,  # type: ignore[arg-type]
            stream=True,
        ) as stream:
            for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
