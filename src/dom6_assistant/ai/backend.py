"""Abstract model backend interface + factory.

All backends implement the ModelBackend protocol:

    backend = get_backend()          # reads config
    response = backend.chat(
        system="You are a Dominions 6 strategist.",
        messages=[{"role": "user", "content": "What should I do this turn?"}],
    )

To override the backend at runtime:
    backend = get_backend(name="anthropic")
"""

from __future__ import annotations

from typing import Any, Iterator, Protocol

from dom6_assistant import config as cfg


class ModelBackend(Protocol):
    """Common interface every backend must satisfy."""

    @property
    def name(self) -> str:
        """Human-readable identifier, e.g. 'ollama/llama3.2'."""
        ...

    def chat(
        self,
        system: str,
        messages: list[dict[str, str]],
    ) -> str:
        """Send a chat and return the full response text."""
        ...

    def stream(
        self,
        system: str,
        messages: list[dict[str, str]],
    ) -> Iterator[str]:
        """Stream the response, yielding text deltas."""
        ...


def get_backend(name: str | None = None, conf: dict[str, Any] | None = None) -> ModelBackend:
    """Return the configured backend, optionally overriding the name.

    Args:
        name: Backend name to use ('ollama', 'anthropic', 'openai', 'gemini').
              If None, reads from config.
        conf: Pre-loaded config dict. If None, loads from disk.

    Returns:
        A ModelBackend instance ready to use.

    Raises:
        ValueError: If the backend name is unknown.
        ImportError: If the required package for the backend is not installed.
    """
    if conf is None:
        conf = cfg.load()
    backend_name = name or cfg.active_backend(conf)
    bcfg = cfg.backend_config(conf, backend_name)

    if backend_name == "ollama":
        from dom6_assistant.ai.backends.ollama import OllamaBackend
        return OllamaBackend(bcfg)
    elif backend_name == "anthropic":
        from dom6_assistant.ai.backends.anthropic import AnthropicBackend
        return AnthropicBackend(bcfg)
    elif backend_name == "openai":
        from dom6_assistant.ai.backends.openai import OpenAIBackend
        return OpenAIBackend(bcfg)
    elif backend_name == "gemini":
        from dom6_assistant.ai.backends.gemini import GeminiBackend
        return GeminiBackend(bcfg)
    else:
        raise ValueError(
            f"Unknown backend '{backend_name}'. "
            "Valid options: ollama, anthropic, openai, gemini"
        )
