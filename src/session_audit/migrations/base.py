"""The shape of a migration (see ``__init__.py`` for the registry and the numbering rule)."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Additive only: a table, an index, a new column. Nothing that drops, renames, rewrites or deletes.
_ALLOWED = re.compile(
    r"\s*(CREATE\s+(UNIQUE\s+)?INDEX\s+(IF\s+NOT\s+EXISTS\s+)?\w+\s+ON\s"
    r"|CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?\w+\s*\("
    r"|ALTER\s+TABLE\s+\w+\s+ADD\s+COLUMN\s)",
    re.IGNORECASE,
)

# The only data a migration may write: seed a ``meta`` key with literals. Exactly this shape, so
# no ``ON CONFLICT ... DO UPDATE`` (an overwrite), ``SELECT`` (copying other tables), ``WITH`` or
# ``RETURNING`` can ride along.
_LITERAL = r"(?:'(?:[^']|'')*'|-?[0-9]+(?:\.[0-9]+)?|NULL)"
_META_SEED = re.compile(
    r"\s*INSERT\s+(?:OR\s+IGNORE\s+)?INTO\s+meta\s*\(\s*key\s*,\s*value\s*\)\s*"
    rf"VALUES\s*\(\s*{_LITERAL}\s*,\s*{_LITERAL}\s*\)\s*;?\s*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Migration:
    """One forward-only, additive step.

    ``number`` is the owning issue's number times 100 plus a sequence (0 to 99), so numbers never
    collide between issues and need no coordination; migrations are applied in number order, each
    in its own transaction, and a migration that is missing from the ledger is applied whatever
    the numbers of those already applied (branches may land in any order). A migration may refer
    only to objects created by itself or by a migration with a lower number.

    ``statements`` are single SQL statements (no ``;`` chains). ``INSERT [OR IGNORE] INTO meta
    (key, value) VALUES (<literal>, <literal>)`` is allowed for seeding a key and nothing else that
    writes data; nothing else outside CREATE TABLE, CREATE INDEX and ALTER TABLE ADD COLUMN.
    """

    number: int
    name: str
    statements: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.number, int) or not 100 <= self.number <= 99_999:
            raise ValueError("migration number must be an issue number times 100 plus 0 to 99")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.name):
            raise ValueError("migration name must be lowercase words joined by underscores")
        if not self.statements:
            raise ValueError("a migration needs at least one statement")
        for statement in self.statements:
            seed = _META_SEED.fullmatch(statement) is not None
            if not (seed or _ALLOWED.match(statement)) or ";" in statement.strip().rstrip(";"):
                raise ValueError(f"migration {self.number}: only additive single statements")
