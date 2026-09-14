"""Configuration loader.

Config is read from (in order, later overrides earlier):
  1. ~/.config/dom6-assistant/config.toml   (user-level)
  2. ./dom6-assistant.toml                  (project-local)

Example config file:

    [model]
    backend = "ollama"           # ollama | anthropic | openai | gemini

    [model.ollama]
    base_url = "http://localhost:11434"
    model    = "llama3.2"

    [model.anthropic]
    api_key_env = "ANTHROPIC_API_KEY"
    model       = "claude-opus-4-6"

    [model.openai]
    api_key_env = "OPENAI_API_KEY"
    model       = "gpt-4o"
    base_url    = "https://api.openai.com/v1"   # override for local OpenAI-compatible servers
    timeout_seconds = 300       # 0 disables the per-completion deadline

    [model.gemini]
    api_key_env = "GOOGLE_API_KEY"
    model       = "gemini-2.0-flash"
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any

def _user_config_dir() -> Path:
    """Where a per-user config lives, by platform convention.

    Windows keeps application settings under the roaming profile rather than
    in a dotfile directory, and it is the platform most Dominions players are
    on. Both are searched regardless, so a config copied between machines
    keeps working.
    """
    appdata = os.environ.get("APPDATA")
    if sys.platform == "win32" and appdata:
        return Path(appdata) / "dom6-assistant"
    return Path.home() / ".config" / "dom6-assistant"


#: Later entries win, so a project-local file overrides the per-user one. The
#: XDG location is searched on every platform so a config copied from a Linux
#: machine still works, but it is only the default target on Linux and macOS.
_SEARCH_PATHS = list(dict.fromkeys([
    Path.home() / ".config" / "dom6-assistant" / "config.toml",
    _user_config_dir() / "config.toml",
    Path("dom6-assistant.toml"),
]))

_DEFAULTS: dict[str, Any] = {
    "model": {
        "backend": "ollama",
        "ollama": {
            "base_url": "http://localhost:11434",
            "model": "llama3.2",
        },
        "anthropic": {
            "api_key_env": "ANTHROPIC_API_KEY",
            "model": "claude-opus-4-6",
        },
        "openai": {
            "api_key_env": "OPENAI_API_KEY",
            "model": "gpt-4o",
            "base_url": "https://api.openai.com/v1",
            "timeout_seconds": 300,
        },
        "gemini": {
            "api_key_env": "GOOGLE_API_KEY",
            "model": "gemini-2.0-flash",
        },
    }
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load() -> dict[str, Any]:
    """Load and return the merged configuration."""
    cfg: dict[str, Any] = _DEFAULTS
    for path in _SEARCH_PATHS:
        if path.exists():
            with open(path, "rb") as f:
                cfg = _deep_merge(cfg, tomllib.load(f))
    return cfg


def model_section(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("model", {})


def active_backend(cfg: dict[str, Any]) -> str:
    return model_section(cfg).get("backend", "ollama")


def backend_config(cfg: dict[str, Any], backend: str | None = None) -> dict[str, Any]:
    backend = backend or active_backend(cfg)
    return model_section(cfg).get(backend, {})


def resolve_api_key(backend_cfg: dict[str, Any]) -> str | None:
    """The API key, from the config file or from the environment.

    ``api_key`` is read first because it is what people expect to find and
    fill in. ``api_key_env`` names a variable instead, which is the better
    habit for a file that might be shared or committed -- so both work and the
    example config explains the trade rather than deciding it.
    """
    direct = backend_cfg.get("api_key")
    if direct and str(direct).strip():
        value = str(direct).strip()
        # Placeholder text left in from the example is not a key. Sending it
        # produces a 401 that reads like a broken install.
        if value.lower() in {"", "your-key-here", "sk-...", "changeme", "none"}:
            return None
        return value
    env_var = backend_cfg.get("api_key_env")
    if env_var:
        return os.environ.get(env_var)
    return None


_USER_CONFIG_PATH = _user_config_dir() / "config.toml"


def default_save_target() -> Path:
    """Which config file a write should go to.

    It has to be the file that *wins* on load, not simply the user-level one.
    A project-local ``dom6-assistant.toml`` overrides the user-level config, so
    writing to the user-level file while a project file exists saves a value
    that is then immediately shadowed -- the setting looks like it did not
    stick, with nothing to show why.
    """
    for candidate in reversed(_SEARCH_PATHS):
        if candidate.exists():
            return candidate
    return _USER_CONFIG_PATH


def save(updates: dict[str, Any], path: Path | None = None) -> None:
    """Persist config changes to the user-level config file.

    Deep-merges *updates* over whatever is already on disk so partial updates
    (e.g. changing one backend's model) don't clobber the rest of the file.
    Writes atomically via a .tmp file + rename.

    Args:
        updates: Partial config dict to merge in.
        path:    Target file; defaults to ~/.config/dom6-assistant/config.toml.
    """
    try:
        import tomli_w
    except ImportError as exc:
        raise ImportError(
            "tomli-w is required to save config. "
            "Run: pip install 'dom6-assistant[web]'"
        ) from exc

    target = path or default_save_target()
    target.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, Any] = {}
    if target.exists():
        with open(target, "rb") as f:
            existing = tomllib.load(f)

    merged = _deep_merge(existing, updates)
    tmp = target.with_suffix(".toml.tmp")
    with open(tmp, "wb") as f:
        tomli_w.dump(merged, f)
    tmp.rename(target)
