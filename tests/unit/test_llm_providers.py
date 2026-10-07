"""Picking an endpoint from a list instead of typing one.

The point of the catalogue is that nobody needs to know KoboldCpp listens on
5001 or that OpenRouter wants `/api/v1` while OpenAI wants `/v1`. That only
helps if the entries are right, so the shape of every entry is checked here --
the addresses themselves are verified by the Test button, against the real
endpoint, which is the only check that means anything.
"""
from __future__ import annotations

import pytest

from dom6_assistant import llm_providers as providers
from dom6_assistant.web.routes import settings as settings_routes


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------

def test_every_entry_has_what_the_dropdown_needs():
    for provider in providers.ALL:
        assert provider.id and provider.name, provider
        assert provider.kind in {"local", "hosted", "custom"}, provider


def test_local_servers_ask_for_no_key():
    """Showing a key box for llama.cpp invites people to invent one."""
    for provider in providers.LOCAL:
        assert provider.needs_key is False, provider.name
        assert provider.base_url.startswith("http://localhost:"), provider.name


def test_hosted_services_say_where_to_get_a_key():
    """A key field with no way to obtain one is a dead end."""
    for provider in providers.HOSTED:
        assert provider.needs_key is True, provider.name
        assert provider.key_url.startswith("https://"), provider.name


def test_every_address_ends_in_the_api_path():
    """Leaving the /v1 off is the most common reason nothing connects.

    Google's compatibility layer is the one documented exception, ending in
    /openai/ instead.
    """
    for provider in providers.LOCAL + providers.HOSTED:
        url = provider.base_url.rstrip("/")
        assert url.endswith("/v1") or url.endswith("/openai"), provider.name


def test_ids_are_unique():
    seen = [p.id for p in providers.ALL]
    assert len(seen) == len(set(seen))


def test_the_catalogue_is_grouped_for_display():
    groups = providers.catalogue()["groups"]
    assert [g["id"] for g in groups] == ["local", "hosted", "custom"]
    assert all(g["providers"] for g in groups)


# ---------------------------------------------------------------------------
# Recognising what is already configured
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://inference.do-ai.run/v1", "digitalocean"),
    ("http://localhost:5001/v1", "koboldcpp"),
    ("http://localhost:5001/v1/", "koboldcpp"),
    ("HTTP://LOCALHOST:5001/V1", "koboldcpp"),
    ("https://openrouter.ai/api/v1", "openrouter"),
])
def test_a_configured_address_preselects_its_provider(url, expected):
    """Opening the panel should show what is set, not a blank list."""
    assert providers.identify(url) == expected


def test_an_unknown_address_is_custom_rather_than_wrong():
    # RFC 5737 reserves this range for documentation. A plausible-looking LAN
    # address such as 192.168.x.x would be indistinguishable from somebody's
    # real machine, and the publish audit refuses the snapshot over exactly
    # that -- correctly, since it cannot tell a fixture from a leak.
    assert providers.identify("http://198.51.100.7:9999/v1") == "custom"


def test_no_address_preselects_nothing():
    assert providers.identify("") == ""


# ---------------------------------------------------------------------------
# The routes behind the dropdowns
# ---------------------------------------------------------------------------

def test_the_providers_route_reports_the_current_selection(tmp_path, monkeypatch):
    from dom6_assistant import config as cfg_mod
    target = tmp_path / "config.toml"
    target.write_text('[model]\nbackend = "openai"\n\n'
                      '[model.openai]\nbase_url = "https://openrouter.ai/api/v1"\n')
    monkeypatch.setattr(cfg_mod, "_SEARCH_PATHS", [target])

    result = settings_routes.list_providers()

    assert result["current"] == "openrouter"
    assert result["groups"]


def test_listing_models_needs_an_address(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from dom6_assistant import config as cfg_mod
    target = tmp_path / "config.toml"
    target.write_text('[model]\nbackend = "openai"\n\n[model.openai]\nbase_url = ""\n')
    monkeypatch.setattr(cfg_mod, "_SEARCH_PATHS", [target])

    with pytest.raises(HTTPException):
        settings_routes.list_models(settings_routes.ModelQuery(base_url=""))


def test_an_unreachable_endpoint_explains_itself_rather_than_raising():
    """The dropdown needs a message to show, not a stack trace."""
    result = settings_routes.list_models(
        settings_routes.ModelQuery(base_url="http://127.0.0.1:9/v1",
                                   api_key="NOT-A-REAL-KEY-test-only"))

    assert result["ok"] is False
    assert result["models"] == []
    assert result["hint"]
    assert "NOT-A-REAL-KEY-test-only" not in str(result)


def test_a_missing_v1_is_called_out_when_listing_too():
    result = settings_routes.list_models(
        settings_routes.ModelQuery(base_url="http://127.0.0.1:9"))
    assert "/v1" in result["hint"]


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def test_the_panel_has_the_controls_the_routes_feed():
    from pathlib import Path
    from dom6_assistant.web.routes import agent as agent_routes

    page = (Path(agent_routes.__file__).parent.parent / "static"
            / "agent.html").read_text()

    for element in ("ep-provider", "ep-url", "ep-key", "ep-connect",
                    "ep-model", "ep-filter", "ep-save"):
        assert element in page, element
    assert "/api/settings/providers" in page
    assert "/api/settings/models" in page
