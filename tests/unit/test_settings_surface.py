"""Configuring the model from the interface instead of from a TOML file.

Every failure this file covers was silent. A settings router that is mounted on
one app but not the one serving the page returns 404, and the page says only
"could not read the current settings". A write that lands in the user-level file
while a project-local file exists is shadowed on load, so the setting appears not
to have stuck. And a read that returns every string field in a backend block now
returns an API key, because keys may live in the config itself.

Routes are called directly: Starlette's TestClient needs httpx, which this
project deliberately does not depend on.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dom6_assistant import config as cfg_mod
from dom6_assistant.ui.app import app as ui_app, root
from dom6_assistant.web.routes import settings as settings_routes


# ---------------------------------------------------------------------------
# The front door
# ---------------------------------------------------------------------------

def test_root_redirects_to_the_assistant():
    """`/` used to be the turn overview, which shows nothing until a turn is in."""
    response = root()
    assert response.status_code == 307
    assert response.headers["location"] == "/agent"


def test_the_old_interface_is_still_reachable():
    paths = {route.path for route in ui_app.routes}
    assert "/turns" in paths, "moving the old UI must not delete it"
    assert "/agent" in paths


def test_settings_routes_are_mounted_on_the_app_that_serves_the_page():
    """The endpoint editor lives in /agent, so /api/settings must exist there."""
    paths = {route.path for route in ui_app.routes}
    assert "/api/settings" in paths
    assert "/api/settings/test" in paths


# ---------------------------------------------------------------------------
# Where a write goes
# ---------------------------------------------------------------------------

def test_write_targets_the_project_file_when_one_exists(tmp_path, monkeypatch):
    """The file that wins on load is the file a save has to go to."""
    user = tmp_path / "user.toml"
    project = tmp_path / "dom6-assistant.toml"
    project.write_text('[model]\nbackend = "openai"\n')
    monkeypatch.setattr(cfg_mod, "_SEARCH_PATHS", [user, project])
    monkeypatch.setattr(cfg_mod, "_USER_CONFIG_PATH", user)

    assert cfg_mod.default_save_target() == project


def test_write_falls_back_to_the_user_file(tmp_path, monkeypatch):
    user = tmp_path / "user.toml"
    monkeypatch.setattr(cfg_mod, "_SEARCH_PATHS", [user, tmp_path / "absent.toml"])
    monkeypatch.setattr(cfg_mod, "_USER_CONFIG_PATH", user)

    assert cfg_mod.default_save_target() == user


def test_a_partial_save_keeps_the_rest_of_the_file(tmp_path):
    """Saving a base_url must not drop the timeout the user set by hand."""
    target = tmp_path / "config.toml"
    target.write_text(
        '[model]\nbackend = "openai"\n\n'
        '[model.openai]\nbase_url = "http://old/v1"\n'
        'model = "auto"\ntimeout_seconds = 900\n'
    )

    cfg_mod.save({"model": {"openai": {"base_url": "http://new/v1"}}}, path=target)

    written = target.read_text()
    assert "http://new/v1" in written
    assert "timeout_seconds = 900" in written
    assert 'model = "auto"' in written


# ---------------------------------------------------------------------------
# What a read gives back
# ---------------------------------------------------------------------------

def test_a_configured_key_is_never_returned(tmp_path, monkeypatch):
    """This response reaches the browser, and a key may now be in the config."""
    target = tmp_path / "config.toml"
    target.write_text(
        '[model]\nbackend = "openai"\n\n'
        '[model.openai]\nbase_url = "http://x/v1"\n'
        'api_key = "NOT-A-REAL-KEY-do-not-echo-this"\n'
    )
    monkeypatch.setattr(cfg_mod, "_SEARCH_PATHS", [target])

    response = settings_routes.get_config()
    openai = response.backends["openai"]

    assert openai.key_is_set is True, "the page has to know a key is there"
    assert openai.extra.get("api_key") == "********"
    assert "NOT-A-REAL-KEY-do-not-echo-this" not in response.model_dump_json()
    # The rest of the block is what the editor prefills from, so it must survive.
    assert openai.extra["base_url"] == "http://x/v1"


def test_a_placeholder_key_does_not_count_as_configured(tmp_path, monkeypatch):
    """The example file ships `api_key = ""`; a copy of it is not a key."""
    target = tmp_path / "config.toml"
    target.write_text(
        '[model]\nbackend = "openai"\n\n'
        '[model.openai]\nbase_url = "http://x/v1"\napi_key = "your-key-here"\n'
    )
    monkeypatch.setattr(cfg_mod, "_SEARCH_PATHS", [target])

    assert settings_routes.get_config().backends["openai"].key_is_set is False


# ---------------------------------------------------------------------------
# Test connection
# ---------------------------------------------------------------------------

def test_an_unreachable_endpoint_explains_itself():
    """Port 9 is discard: it is reliably not an HTTP server."""
    # The key is supplied, so the last assertion is about this call rather than
    # being vacuously true -- the whole response is shown in the browser, and a
    # client library that echoed its own headers into an error would leak it.
    body = settings_routes.ConnectionTest(
        base_url="http://127.0.0.1:9/v1", api_key="NOT-A-REAL-KEY-test-only")

    result = settings_routes.test_connection(body)

    assert result["ok"] is False
    assert result["hint"], "a failure with no hint is the stack trace we replaced"
    assert "NOT-A-REAL-KEY-test-only" not in str(result), \
        "the key must not come back in an error"


def test_a_missing_v1_is_called_out():
    """Far and away the most common setup mistake."""
    body = settings_routes.ConnectionTest(base_url="http://127.0.0.1:9")

    result = settings_routes.test_connection(body)

    assert result["ok"] is False
    assert "/v1" in result["hint"]


def test_the_test_route_does_not_write_anything(tmp_path, monkeypatch):
    """Testing an endpoint before saving it is the whole point of the button."""
    target = tmp_path / "config.toml"
    monkeypatch.setattr(cfg_mod, "_USER_CONFIG_PATH", target)
    monkeypatch.setattr(cfg_mod, "_SEARCH_PATHS", [target])

    settings_routes.test_connection(
        settings_routes.ConnectionTest(base_url="http://127.0.0.1:9/v1")
    )

    assert not target.exists()
