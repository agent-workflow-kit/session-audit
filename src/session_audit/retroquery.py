"""Reading stored retros for ``retro list``, ``retro show``, ``retro delete`` and ``export retros``
(issue #31).

Everything here reads the store through a connection the caller opened read-only (the repo's
``sessionquery.read_store`` path); nothing writes. A row that does not fit the documented layout
(``retroview``) is never listed, printed or exported: the id, state, reason, counts and scope are
checked by pattern and range exactly as ``load_retro`` checks them. ``retro list`` rows are built
from columns only: no artifact is decoded, so a row carries no model text.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from . import retroview
from .grammar import AmbiguousPrefixError, PrefixNotFoundError

__all__ = [
    "ListFilters",
    "export_lines",
    "list_rows",
    "resolve_retro",
    "resolve_retros",
    "superseded",
    "with_superseded",
]

MAX_PREFIX_MATCHES = 20
_ID_GLOB = "ra-[a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7]"
_PAGE = 200

# A stored artifact row with a summary that fits (``retroview`` checks the same: 1..2000 chars).
_ARTIFACT_OK = (
    "EXISTS (SELECT 1 FROM retro_artifacts a WHERE a.retro_id = {r}.id "
    "AND typeof(a.summary) = 'text' AND length(a.summary) BETWEEN 1 AND 2000)"
)

# A later ``done`` retro with the same key exists ("later": created later, the id breaks a tie).
# Only a ``done`` retro with a usable artifact row counts: one that ``retro show`` would refuse
# never supersedes another.
SUPERSEDED_SQL = (
    "EXISTS (SELECT 1 FROM retros n WHERE n.key = r.key AND n.state = 'done' "
    f"AND {_ARTIFACT_OK.format(r='n')} "
    "AND (n.created_at > r.created_at OR (n.created_at = r.created_at AND n.id > r.id)))"
)


def _prefix_range(prefix: str) -> tuple[str, str]:
    return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)


def resolve_retro(conn: sqlite3.Connection | None, prefix: str) -> str:
    """The stored retro id that ``prefix`` (an id or ``ra-`` and at least 4 characters) names.
    Raises ``PrefixNotFoundError`` (exit 10) or ``AmbiguousPrefixError`` (exit 21). Only ids of
    the shape of a retro id are looked at."""
    if conn is None:
        raise PrefixNotFoundError("not found", "RETRO_ID", details={"id": prefix})
    low, high = _prefix_range(prefix)
    rows = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM retros WHERE id >= ? AND id < ? AND typeof(id) = 'text' "
            "AND id GLOB ? ORDER BY id LIMIT ?",
            (low, high, _ID_GLOB, MAX_PREFIX_MATCHES + 1),
        )
    ]
    if prefix in rows:
        return prefix
    if not rows:
        raise PrefixNotFoundError("not found", "RETRO_ID", details={"id": prefix})
    if len(rows) == 1:
        return rows[0]
    total = conn.execute(
        "SELECT COUNT(*) FROM retros WHERE id >= ? AND id < ? AND typeof(id) = 'text' "
        "AND id GLOB ?",
        (low, high, _ID_GLOB),
    ).fetchone()[0]
    raise AmbiguousPrefixError(
        "ambiguous prefix",
        "RETRO_ID",
        details={"matches": rows[:MAX_PREFIX_MATCHES], "matched": total},
    )


def resolve_retros(conn: sqlite3.Connection | None, prefixes: list[str]) -> list[str]:
    """Every prefix resolved, in the order given, without repeats. The first one that is unknown
    or ambiguous raises (so a command that deletes nothing unless all are known can use it)."""
    found: list[str] = []
    for prefix in prefixes:
        retro_id = resolve_retro(conn, prefix)
        if retro_id not in found:
            found.append(retro_id)
    return found


def superseded(conn: sqlite3.Connection, retro_id: str) -> bool:
    row = conn.execute(
        f"SELECT {SUPERSEDED_SQL} FROM retros r WHERE r.id = ?", (retro_id,)
    ).fetchone()
    return bool(row and row[0])


def with_superseded(conn: sqlite3.Connection, retro: dict[str, Any]) -> dict[str, Any]:
    return {**retro, "superseded": superseded(conn, retro["id"])}


@dataclass(frozen=True)
class ListFilters:
    states: tuple[str, ...] = ()
    since_ms: int | None = None
    key: str | None = None
    skill: str | None = None
    session_id: str | None = None  # a full session id


_LIST_COLUMNS = (
    "SELECT r.id, r.key, r.state, r.reason, r.created_at, r.started_at, r.finished_at, "
    "r.scope, r.resolved_scope, r.counts, r.skill, r.model, r.session_map, r.warnings, "
    "r.dropped, r.cost_usd, r.turns, r.sandbox, r.outcome, r.csession_id, r.redacted, "
    "r.again_of, "
    f"{SUPERSEDED_SQL} AS superseded, "
    "EXISTS (SELECT 1 FROM retro_artifacts a WHERE a.retro_id = r.id) AS has_artifact, "
    "(SELECT a.summary FROM retro_artifacts a WHERE a.retro_id = r.id) AS summary, "
    "(SELECT s.cleanup_state FROM retro_sessions s WHERE s.csession_id = r.csession_id) "
    "AS cleanup_state FROM retros r"
)


def _where(filters: ListFilters) -> tuple[str, list[Any]]:
    clauses = ["typeof(r.id) = 'text'", "r.id GLOB ?"]
    params: list[Any] = [_ID_GLOB]
    if filters.states:
        clauses.append(f"r.state IN ({','.join('?' * len(filters.states))})")
        params.extend(filters.states)
    if filters.since_ms is not None:
        clauses.append("r.created_at >= ?")
        params.append(filters.since_ms)
    if filters.key is not None:
        clauses.append("r.key = ?")
        params.append(filters.key)
    if filters.skill is not None:
        clauses.append("r.skill = ?")
        params.append(filters.skill)
    if filters.session_id is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM json_each(r.session_map, '$.sessions') j "
            "WHERE json_extract(j.value, '$.session_id') = ?)"
        )
        params.append(filters.session_id)
    return " AND ".join(clauses), params


def list_rows(
    conn: sqlite3.Connection, filters: ListFilters, limit: int, unfit: list[int]
) -> tuple[list[dict[str, Any]], bool]:
    """Up to ``limit`` rows, newest first, and whether more matched. A stored row that does not fit
    the layout (``retroview.list_row``: exactly the rows ``load_retro`` would load, the artifact
    body aside) is skipped and counted in ``unfit[0]``, not toward ``limit``."""
    where, params = _where(filters)
    cursor = conn.execute(
        f"{_LIST_COLUMNS} WHERE {where} ORDER BY r.created_at DESC, r.id DESC", params
    )
    names = [column[0] for column in cursor.description]
    rows: list[dict[str, Any]] = []
    try:
        for stored in cursor:
            row = retroview.list_row(dict(zip(names, stored, strict=True)))
            if row is None:
                unfit[0] += 1
                continue
            if len(rows) == limit:
                return rows, True
            rows.append(row)
    finally:
        cursor.close()
    return rows, False


def export_lines(
    conn: sqlite3.Connection, since_ms: int | None, unfit: list[int]
) -> Iterator[dict[str, Any]]:
    """The Retro object (with ``superseded``) of every stored retro, oldest first. ``unfit[0]``
    counts the rows that did not fit and were left out (reset here: a retry
    starts over)."""
    unfit[0] = 0
    where = "typeof(id) = 'text' AND id GLOB ?"
    params: list[Any] = [_ID_GLOB]
    if since_ms is not None:
        where += " AND created_at >= ?"
        params.append(since_ms)
    after: tuple[int, str] | None = None
    while True:
        page_where, page_params = where, list(params)
        if after is not None:
            page_where += " AND (created_at > ? OR (created_at = ? AND id > ?))"
            page_params += [after[0], after[0], after[1]]
        page = conn.execute(
            f"SELECT created_at, id FROM retros WHERE {page_where} ORDER BY created_at, id LIMIT ?",
            (*page_params, _PAGE),
        ).fetchall()
        if not page:
            return
        for _created, retro_id in page:
            retro = retroview.load_retro(conn, retro_id)
            if retro is None:
                unfit[0] += 1
            else:
                yield with_superseded(conn, retro)
        after = (page[-1][0], page[-1][1])
