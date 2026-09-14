"""Google Gemini backend.

Config example (dom6-assistant.toml):
    [model]
    backend = "gemini"

    [model.gemini]
    api_key_env = "GOOGLE_API_KEY"
    model       = "gemini-2.0-flash"
"""

from __future__ import annotations

import os
from typing import Any, Iterator

try:
    import google.generativeai as _genai
except ImportError as exc:
    raise ImportError(
        "google-generativeai not installed. Run: pip install 'dom6-assistant[gemini]'"
    ) from exc


class GeminiBackend:
    def __init__(self, config: dict[str, Any]) -> None:
        self._model_name = config.get("model", "gemini-2.0-flash")
        api_key_env = config.get("api_key_env", "GOOGLE_API_KEY")
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise EnvironmentError(
                f"Environment variable {api_key_env!r} is not set. "
                "Set it to your Google AI API key."
            )
        _genai.configure(api_key=api_key)
        self._model = _genai.GenerativeModel(self._model_name)

    @property
    def name(self) -> str:
        return f"gemini/{self._model_name}"

    def _flatten(self, system: str, messages: list[dict[str, str]]) -> list[Any]:
        """Gemini uses a different message structure; prepend system as user turn."""
        parts = []
        # Inject system prompt as the first user message
        parts.append({"role": "user", "parts": [system]})
        parts.append({"role": "model", "parts": ["Understood."]})
        for msg in messages:
            role = "user" if msg["role"] == "user" else "model"
            parts.append({"role": role, "parts": [msg["content"]]})
        return parts

    def chat(self, system: str, messages: list[dict[str, str]]) -> str:
        response = self._model.generate_content(self._flatten(system, messages))
        return response.text

    def stream(self, system: str, messages: list[dict[str, str]]) -> Iterator[str]:
        for chunk in self._model.generate_content(
            self._flatten(system, messages), stream=True
        ):
            if chunk.text:
                yield chunk.text
