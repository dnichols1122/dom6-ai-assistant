"""Known OpenAI-compatible endpoints, so nobody has to type a base URL.

Choosing a model used to mean knowing that KoboldCpp listens on 5001, that
llama.cpp listens on 8080, that OpenRouter wants `/api/v1` while OpenAI wants
`/v1`, and that leaving the `/v1` off is the single most common reason nothing
connects. None of that is interesting, and all of it is published.

The pattern -- pick a provider, paste a key, choose a model from a list the
server itself returns -- is the one SillyTavern uses, and it is a good one. The
implementation here is independent: SillyTavern is AGPL-3.0 and this project is
GPL-3.0, so no code was taken from it. The addresses below are published facts
from each provider's own documentation.

Only `digitalocean` and the local entries have been exercised from this
project. The rest are transcribed from documentation, which is why the
interface puts a Test connection button next to the list rather than claiming
any of them work.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Provider:
    """One endpoint someone might point the assistant at."""

    id: str
    name: str
    base_url: str
    #: "local" runs on this machine; "hosted" is somebody's API.
    kind: str
    needs_key: bool
    #: Where to get a key, for the hosted ones.
    key_url: str = ""
    note: str = ""


#: Local servers. No key, and the address is whatever the server prints when it
#: starts -- these are each project's documented default port.
LOCAL: tuple[Provider, ...] = (
    Provider("llamacpp", "llama.cpp", "http://localhost:8080/v1", "local", False,
             note="llama-server's default port"),
    Provider("koboldcpp", "KoboldCpp", "http://localhost:5001/v1", "local", False),
    Provider("lmstudio", "LM Studio", "http://localhost:1234/v1", "local", False,
             note="Developer tab, Start Server"),
    Provider("ollama", "Ollama", "http://localhost:11434/v1", "local", False,
             note="needs an exact model name; 'auto' will not do"),
    Provider("vllm", "vLLM", "http://localhost:8000/v1", "local", False),
    Provider("tabby", "TabbyAPI", "http://localhost:5000/v1", "local", False),
    Provider("textgenwebui", "text-generation-webui", "http://localhost:5000/v1",
             "local", False, note="enable the OpenAI extension"),
)

#: Hosted services speaking the OpenAI protocol.
HOSTED: tuple[Provider, ...] = (
    Provider("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "hosted",
             True, "https://openrouter.ai/keys",
             "hundreds of models from many vendors behind one key"),
    Provider("openai", "OpenAI", "https://api.openai.com/v1", "hosted", True,
             "https://platform.openai.com/api-keys"),
    Provider("anthropic", "Anthropic", "https://api.anthropic.com/v1", "hosted",
             True, "https://console.anthropic.com/settings/keys",
             "via Anthropic's OpenAI-compatible layer"),
    Provider("gemini", "Google AI Studio",
             "https://generativelanguage.googleapis.com/v1beta/openai/", "hosted",
             True, "https://aistudio.google.com/apikey",
             "Gemini through its OpenAI-compatible layer"),
    Provider("deepseek", "DeepSeek", "https://api.deepseek.com/v1", "hosted", True,
             "https://platform.deepseek.com/api_keys"),
    Provider("groq", "Groq", "https://api.groq.com/openai/v1", "hosted", True,
             "https://console.groq.com/keys", "very fast, limited model choice"),
    Provider("mistral", "Mistral", "https://api.mistral.ai/v1", "hosted", True,
             "https://console.mistral.ai/api-keys"),
    Provider("together", "Together AI", "https://api.together.xyz/v1", "hosted",
             True, "https://api.together.xyz/settings/api-keys"),
    Provider("fireworks", "Fireworks AI", "https://api.fireworks.ai/inference/v1",
             "hosted", True, "https://fireworks.ai/account/api-keys"),
    Provider("cerebras", "Cerebras", "https://api.cerebras.ai/v1", "hosted", True,
             "https://cloud.cerebras.ai"),
    Provider("xai", "xAI", "https://api.x.ai/v1", "hosted", True,
             "https://console.x.ai"),
    Provider("digitalocean", "DigitalOcean Inference",
             "https://inference.do-ai.run/v1", "hosted", True,
             "https://cloud.digitalocean.com", "verified against this project"),
)

CUSTOM = Provider("custom", "Something else", "", "custom", False,
                  note="type the address yourself; remember the /v1")

ALL: tuple[Provider, ...] = LOCAL + HOSTED + (CUSTOM,)
_BY_ID = {provider.id: provider for provider in ALL}


def catalogue() -> dict[str, Any]:
    """The list the interface builds its dropdown from."""
    return {
        "groups": [
            {"id": "local", "label": "On this machine",
             "providers": [asdict(p) for p in LOCAL]},
            {"id": "hosted", "label": "Hosted services",
             "providers": [asdict(p) for p in HOSTED]},
            {"id": "custom", "label": "Custom",
             "providers": [asdict(CUSTOM)]},
        ],
    }


def identify(base_url: str) -> str:
    """Which provider a stored address corresponds to, if any.

    Used to preselect the dropdown when the settings panel opens, so an
    endpoint configured earlier shows as its provider rather than as "custom".
    """
    cleaned = (base_url or "").strip().rstrip("/").lower()
    if not cleaned:
        return ""
    for provider in ALL:
        if provider.base_url and provider.base_url.rstrip("/").lower() == cleaned:
            return provider.id
    return "custom"


def get(provider_id: str) -> Provider | None:
    return _BY_ID.get(provider_id)
