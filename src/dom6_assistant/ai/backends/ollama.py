"""Ollama backend — runs local models via the Ollama daemon.

Install Ollama: https://ollama.com
Then: ollama pull llama3.2    (or any model you prefer)

Config example (dom6-assistant.toml):
    [model]
    backend = "ollama"

    [model.ollama]
    base_url = "http://localhost:11434"
    model    = "llama3.2"
"""

from __future__ import annotations

from typing import Any, Iterator

try:
    import ollama as _ollama
except ImportError as exc:
    raise ImportError(
        "ollama package not installed. Run: pip install 'dom6-assistant[ollama]'"
    ) from exc


class OllamaBackend:
    def __init__(self, config: dict[str, Any]) -> None:
        self._model = config.get("model", "llama3.2")
        base_url = config.get("base_url", "http://localhost:11434")
        self._client = _ollama.Client(host=base_url)

    @property
    def name(self) -> str:
        return f"ollama/{self._model}"

    def _build_messages(
        self, system: str, messages: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        return [{"role": "system", "content": system}, *messages]

    def chat(self, system: str, messages: list[dict[str, str]]) -> str:
        response = self._client.chat(
            model=self._model,
            messages=self._build_messages(system, messages),
        )
        return response["message"]["content"]

    def stream(self, system: str, messages: list[dict[str, str]]) -> Iterator[str]:
        for chunk in self._client.chat(
            model=self._model,
            messages=self._build_messages(system, messages),
            stream=True,
        ):
            delta = chunk.get("message", {}).get("content", "")
            if delta:
                yield delta
