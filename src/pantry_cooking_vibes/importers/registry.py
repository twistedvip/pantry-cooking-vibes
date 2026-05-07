"""Plugin discovery for site-specific JSONL post-processors.

Scrapers that need pre-ingest cleanup (editorial-marker stripping, unit
canonicalisation, brand suffix tweaks, etc.) ship a class that implements
the :class:`RecipeImporter` protocol and register it under the entry-point
group ``pantry_cooking_vibes.importers``::

    [project.entry-points."pantry_cooking_vibes.importers"]
    example = "myscraper.plugin:ExampleImporter"

At ingest time the user passes ``--plugin example`` and the importer's
``post_process(records)`` runs over the raw record dicts before Pydantic
validation. Without ``--plugin`` the plain JSONL contract is used.
"""

from __future__ import annotations

import logging
from importlib import metadata
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "pantry_cooking_vibes.importers"


@runtime_checkable
class RecipeImporter(Protocol):
    """Plugin contract: name + version metadata and a record post-processor."""

    name: str
    version: str

    def post_process(self, records: list[dict]) -> list[dict]:
        """Transform raw JSONL records before Pydantic validation.

        Receives the parsed dicts (one per non-empty JSONL line) and returns
        the (possibly filtered or rewritten) sequence to ingest. Implementations
        must not touch the database.
        """
        ...


def discover_plugins() -> dict[str, RecipeImporter]:
    """Return ``{name: instance}`` for every plugin registered under the group.

    Each entry-point is loaded and instantiated (the registered object is
    expected to be a zero-arg class or callable). Plugins that fail to load
    are skipped silently so a broken third-party install can't take ingest
    offline.
    """
    found: dict[str, RecipeImporter] = {}
    for ep in metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            obj = ep.load()
            instance = obj() if isinstance(obj, type) else obj
        except Exception:
            logger.exception("failed to load plugin entry-point %r", ep.name)
            continue
        found[ep.name] = instance
    return found


def load_plugin(name: str) -> RecipeImporter:
    """Look up a single plugin by entry-point name.

    Raises ``ValueError`` with the list of installed plugins if ``name``
    isn't registered, so the user sees actionable output rather than a
    bare ``KeyError``.
    """
    plugins = discover_plugins()
    if name not in plugins:
        installed = ", ".join(sorted(plugins)) or "(none)"
        raise ValueError(
            f"plugin {name!r} not found in entry-point group "
            f"{ENTRY_POINT_GROUP!r}. Installed plugins: {installed}"
        )
    return plugins[name]
