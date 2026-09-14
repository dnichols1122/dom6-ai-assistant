"""Anthropic Claude backend.

Config example (dom6-assistant.toml):
    [model]
    backend = "anthropic"

    [model.anthropic]
    api_key_env = "ANTHROPIC_API_KEY"
    model       = "claude-opus-4-6"
"""

from __future__ import annotations

import os
from typing import Any, Iterator

try:
    import anthropic as _anthropic
except ImportError as exc:
    raise ImportError(
        "anthropic package not installed. Run: pip install 'dom6-assistant[anthropic]'"
    ) from exc


class AnthropicBackend:
    def __init__(self, config: dict[str, Any]) -> None:
        self._model = config.get("model", "claude-opus-4-6")
        api_key_env = config.get("api_key_env", "ANTHROPIC_API_KEY")
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise EnvironmentError(
                f"Environment variable {api_key_env!r} is not set. "
                "Set it to your Anthropic API key."
            )
        self._client = _anthropic.Anthropic(api_key=api_key)
        self._max_tokens = config.get("max_tokens", 4096)

    @property
    def name(self) -> str:
        return f"anthropic/{self._model}"

    def chat(self, system: str, messages: list[dict[str, str]]) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=messages,  # type: ignore[arg-type]
        )
        return response.content[0].text

    def stream(self, system: str, messages: list[dict[str, str]]) -> Iterator[str]:
        with self._client.messages.stream(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=messages,  # type: ignore[arg-type]
        ) as stream:
            yield from stream.text_stream
