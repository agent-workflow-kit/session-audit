"""Validate JSON documents against schemas.

Two sets are registered under their ``$id``, so relative ``$ref``s between files resolve:

- this tool's own schemas, ``schemas/`` (ids ``session-audit/...``);
- claude-session's published schemas, copied into ``tests/schemas/claude-session/`` and pinned
  to ``PINNED_VERSION`` (ids ``claude-session/...``). They are data: nothing of claude-session
  is imported. ``tests/schemas/claude-session/PINNED`` records where they came from.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

TESTS_ROOT = Path(__file__).resolve().parent
OWN_SCHEMA_ROOT = TESTS_ROOT.parent / "schemas"
PINNED_SCHEMA_ROOT = TESTS_ROOT / "schemas" / "claude-session"
PINNED_VERSION = "0.45.3"
MIN_CLAUDE_SESSION_VERSION = "0.45.2"  # design.md, Uses: the first release the retro can use


@cache
def registry() -> Registry[Any]:
    resources: list[tuple[str, Resource[Any]]] = []
    for root in (OWN_SCHEMA_ROOT, PINNED_SCHEMA_ROOT):
        for path in sorted(root.rglob("*.json")):
            contents = json.loads(path.read_text(encoding="utf-8"))
            resources.append((contents["$id"], Resource.from_contents(contents)))
    return Registry().with_resources(resources)


def validate(instance: object, schema_id: str) -> None:
    """Raise ``jsonschema.ValidationError`` unless ``instance`` matches ``schema_id``, string
    formats (such as ``date-time``) included."""
    schema = registry().contents(schema_id)
    validator = Draft202012Validator(
        schema, registry=registry(), format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    validator.validate(instance)
