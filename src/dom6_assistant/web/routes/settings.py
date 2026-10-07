"""Config read/write API routes.

GET  /api/settings         — return current merged config (no secret values)
POST /api/settings         — persist changes to user config file
GET  /api/settings/backends — list backends with importability status
"""

from __future__ import annotations

import importlib.util
import os
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from dom6_assistant import config as cfg_mod

router = APIRouter(prefix="/settings", tags=["settings"])


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

class BackendInfo(BaseModel):
    name: str
    model: str
    key_is_set: bool
    extra: dict[str, str]   # other fields (base_url, api_key_env, etc.)


class ConfigResponse(BaseModel):
    active_backend: str
    backends: dict[str, BackendInfo]


@router.get("", response_model=ConfigResponse)
def get_config() -> ConfigResponse:
    conf = cfg_mod.load()
    active = cfg_mod.active_backend(conf)

    backends: dict[str, BackendInfo] = {}
    for name in ("ollama", "anthropic", "openai", "gemini"):
        bcfg = cfg_mod.backend_config(conf, name)
        model = bcfg.get("model", "")
        key_env = bcfg.get("api_key_env", "")
        # A key may now live in the config itself, so it has to be masked
        # here: this response reaches the browser.
        key_is_set = bool(cfg_mod.resolve_api_key(bcfg))
        extra = {
            k: ("********" if k == "api_key" else v)
            for k, v in bcfg.items()
            if k not in ("model",) and isinstance(v, str)
        }
        backends[name] = BackendInfo(
            name=name,
            model=model,
            key_is_set=key_is_set,
            extra=extra,
        )

    return ConfigResponse(active_backend=active, backends=backends)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

class ConfigUpdate(BaseModel):
    active_backend: str | None = None
    backends: dict[str, dict[str, Any]] | None = None


@router.post("")
def post_config(body: ConfigUpdate) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if body.active_backend is not None:
        valid = {"ollama", "anthropic", "openai", "gemini"}
        if body.active_backend not in valid:
            raise HTTPException(400, detail=f"Unknown backend: {body.active_backend!r}")
        updates["model"] = {"backend": body.active_backend}
    if body.backends:
        model_section = updates.setdefault("model", {})
        for name, fields in body.backends.items():
            if name not in {"ollama", "anthropic", "openai", "gemini"}:
                raise HTTPException(400, detail=f"Unknown backend: {name!r}")
            model_section[name] = fields

    try:
        cfg_mod.save(updates)
    except ImportError as e:
        raise HTTPException(500, detail=str(e))

    return {"ok": True}


def _connection_hint(lowered_error: str, base_url: str) -> str:
    """Turn a connection failure into something a player can act on."""
    hint = ""
    if "connection refused" in lowered_error or "failed to establish" in lowered_error:
        hint = ("nothing is listening there. Is the model server running, "
                "and is the port right?")
    elif "404" in lowered_error:
        hint = ("reached the host but not the API. base_url usually has to "
                "end in /v1.")
    elif "401" in lowered_error or "403" in lowered_error:
        hint = "the endpoint rejected the key."
    # A missing /v1 is the single most common mistake, and it does not reliably
    # show up as a 404: get the port wrong as well and the connection is
    # refused first, hiding it behind the other message. It is visible in the
    # address either way, so say so either way.
    if base_url and not base_url.rstrip("/").endswith("/v1"):
        hint = (hint + " " if hint else "") + (
            "Also: base_url usually has to end in /v1.")
    return hint


class ConnectionTest(BaseModel):
    """An endpoint to try, before it is saved."""
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None


@router.post("/test")
def test_connection(body: ConnectionTest) -> dict[str, Any]:
    """Ask the configured endpoint what it is serving.

    "Is my model set up right?" should be answerable in the interface rather
    than by starting a chat and reading a stack trace. Values given here are
    used without being saved, so a setting can be tried before it is kept.
    """
    from dom6_assistant.agent.llm import LLMError, OpenAICompatClient

    conf = cfg_mod.load()
    bcfg = dict(cfg_mod.backend_config(conf, "openai"))
    base_url = (body.base_url or bcfg.get("base_url") or "").strip()
    if not base_url:
        raise HTTPException(400, detail="no base_url to test")
    key = body.api_key if body.api_key not in (None, "", "********") else \
        cfg_mod.resolve_api_key(bcfg)

    client = OpenAICompatClient(
        base_url=base_url, model=body.model or bcfg.get("model") or "auto",
        api_key=key, timeout=20)
    try:
        served = client.models(timeout=15)
    except LLMError as exc:
        return {"ok": False, "error": str(exc),
                "hint": _connection_hint(str(exc).lower(), base_url),
                "base_url": base_url}
    return {"ok": True, "base_url": base_url, "models": served[:20],
            "count": len(served)}


# ---------------------------------------------------------------------------
# Picking an endpoint without typing one
# ---------------------------------------------------------------------------

@router.get("/providers")
def list_providers() -> dict[str, Any]:
    """Known endpoints, grouped, for the provider dropdown.

    Also reports which one the current configuration matches, so opening the
    panel preselects what is already set rather than showing a blank list.
    """
    from dom6_assistant import llm_providers

    bcfg = cfg_mod.backend_config(cfg_mod.load(), "openai")
    result = llm_providers.catalogue()
    result["current"] = llm_providers.identify(bcfg.get("base_url", ""))
    return result


class ModelQuery(BaseModel):
    """An endpoint to ask what it is serving, before anything is saved."""
    base_url: str | None = None
    api_key: str | None = None


@router.post("/models")
def list_models(body: ModelQuery) -> dict[str, Any]:
    """Ask the endpoint for its model list, to fill the model dropdown.

    Separate from /test because the two answer different questions: /test asks
    "did this work", this asks "what can I choose". A provider with hundreds of
    models is the normal case, so the whole list is returned and the interface
    filters it.
    """
    from dom6_assistant.agent.llm import LLMError, OpenAICompatClient

    bcfg = dict(cfg_mod.backend_config(cfg_mod.load(), "openai"))
    base_url = (body.base_url or bcfg.get("base_url") or "").strip()
    if not base_url:
        raise HTTPException(400, detail="no base_url to query")
    key = body.api_key if body.api_key not in (None, "", "********") else \
        cfg_mod.resolve_api_key(bcfg)

    client = OpenAICompatClient(base_url=base_url, model="auto", api_key=key,
                                timeout=25)
    try:
        served = client.models(timeout=20)
    except LLMError as exc:
        return {"ok": False, "error": str(exc), "models": [], "count": 0,
                "hint": _connection_hint(str(exc).lower(), base_url)}
    return {"ok": True, "models": sorted(served), "count": len(served),
            "base_url": base_url}


# ---------------------------------------------------------------------------
# Backend availability
# ---------------------------------------------------------------------------

_BACKEND_PACKAGES = {
    "ollama":    "ollama",
    "anthropic": "anthropic",
    "openai":    "openai",
    "gemini":    "google.generativeai",
}


class BackendAvailability(BaseModel):
    name: str
    installed: bool


class BackendsResponse(BaseModel):
    backends: list[BackendAvailability]


@router.get("/backends", response_model=BackendsResponse)
def get_backends() -> BackendsResponse:
    result = []
    for name, pkg in _BACKEND_PACKAGES.items():
        installed = importlib.util.find_spec(pkg) is not None
        result.append(BackendAvailability(name=name, installed=installed))
    return BackendsResponse(backends=result)
