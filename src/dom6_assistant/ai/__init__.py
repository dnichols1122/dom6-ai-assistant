"""AI decision-making layer.

Entry points:
    from dom6_assistant.ai.backend import get_backend

    backend = get_backend()           # uses config
    backend = get_backend("ollama")   # explicit
    text = backend.chat(system=..., messages=[...])
"""

from dom6_assistant.ai.backend import get_backend, ModelBackend

__all__ = ["get_backend", "ModelBackend"]
