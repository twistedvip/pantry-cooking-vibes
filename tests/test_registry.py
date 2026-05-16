"""Tests for the importer plugin registry."""

from __future__ import annotations

from importlib import metadata
from types import SimpleNamespace

import pytest

from pantry_cooking_vibes.importers import registry


class _GoodImporter:
    name = "good"
    version = "1.0"

    def post_process(self, records: list[dict]) -> list[dict]:
        return records


class _BadImporter:
    def __init__(self) -> None:
        raise RuntimeError("plugin boom")


def _make_entry_points(monkeypatch, ep_list):
    """Patch metadata.entry_points to return ep_list when filtered by our group."""

    def fake_entry_points(*, group: str):
        assert group == registry.ENTRY_POINT_GROUP
        return ep_list

    monkeypatch.setattr(metadata, "entry_points", fake_entry_points)


def _ep(name: str, target):
    """Build a SimpleNamespace that quacks like an EntryPoint."""
    return SimpleNamespace(name=name, load=lambda: target)


def test_discover_plugins_empty(monkeypatch):
    _make_entry_points(monkeypatch, [])
    assert registry.discover_plugins() == {}


def test_discover_plugins_loads_class_and_instantiates(monkeypatch):
    _make_entry_points(monkeypatch, [_ep("good", _GoodImporter)])
    plugins = registry.discover_plugins()
    assert set(plugins) == {"good"}
    assert isinstance(plugins["good"], _GoodImporter)
    assert plugins["good"].post_process([{"a": 1}]) == [{"a": 1}]


def test_discover_plugins_accepts_pre_instantiated_object(monkeypatch):
    instance = _GoodImporter()
    # A non-type callable/object is returned as-is.
    _make_entry_points(monkeypatch, [_ep("good", instance)])
    plugins = registry.discover_plugins()
    assert plugins["good"] is instance


def test_discover_plugins_skips_broken_plugin(monkeypatch, caplog):
    _make_entry_points(
        monkeypatch,
        [_ep("good", _GoodImporter), _ep("bad", _BadImporter)],
    )
    plugins = registry.discover_plugins()
    assert set(plugins) == {"good"}
    assert any("failed to load plugin" in rec.message for rec in caplog.records)


def test_discover_plugins_skips_when_ep_load_raises(monkeypatch):
    def boom():
        raise ImportError("cannot import")

    ep = SimpleNamespace(name="boom", load=boom)
    _make_entry_points(monkeypatch, [ep])
    assert registry.discover_plugins() == {}


def test_load_plugin_returns_known(monkeypatch):
    _make_entry_points(monkeypatch, [_ep("good", _GoodImporter)])
    plugin = registry.load_plugin("good")
    assert isinstance(plugin, _GoodImporter)


def test_load_plugin_unknown_raises_with_installed_list(monkeypatch):
    _make_entry_points(monkeypatch, [_ep("good", _GoodImporter)])
    with pytest.raises(ValueError, match="plugin 'missing' not found"):
        registry.load_plugin("missing")


def test_load_plugin_unknown_empty_registry(monkeypatch):
    _make_entry_points(monkeypatch, [])
    with pytest.raises(ValueError, match="Installed plugins: \\(none\\)"):
        registry.load_plugin("missing")


def test_recipe_importer_protocol_runtime_check():
    """Concrete class satisfying the protocol passes isinstance check."""
    assert isinstance(_GoodImporter(), registry.RecipeImporter)
    assert not isinstance(object(), registry.RecipeImporter)
