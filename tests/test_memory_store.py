"""``store.open_memory_copy``: the private in-memory store of a dry run (issue #24)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from session_audit import errors
from session_audit.store import open_memory_copy, open_store

from .fake_gateway import FakeGateway
from .storehelp import V1, V2, ClockedGateway, ledger, state_path


def listing(path: Path) -> dict[str, tuple[int, int]]:
    return {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(path.iterdir())}


def test_the_copy_holds_the_rows_of_the_store_and_writes_to_it_stay_in_memory(
    tmp_path: Path,
) -> None:
    gateway = ClockedGateway()
    directory = state_path(tmp_path)
    store = open_store(gateway, directory, migrations=V1)
    with store.transaction() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES ('marker', 'real')")
    store.close()
    before = listing(Path(directory))

    copy = open_memory_copy(gateway, directory, migrations=V1)
    try:
        assert copy.conn.execute("SELECT value FROM meta WHERE key = 'marker'").fetchone() == (
            "real",
        )
        with copy.transaction() as conn:
            conn.execute("UPDATE meta SET value = 'copy' WHERE key = 'marker'")
            conn.execute("INSERT INTO meta (key, value) VALUES ('extra', '1')")
        assert copy.conn.execute("SELECT value FROM meta WHERE key = 'marker'").fetchone() == (
            "copy",
        )
    finally:
        copy.close()

    assert listing(Path(directory)) == before  # not a byte, a side file or a timestamp changed
    check = open_store(gateway, directory, migrations=V1)
    try:
        assert check.conn.execute("SELECT value FROM meta WHERE key = 'marker'").fetchone() == (
            "real",
        )
        assert check.conn.execute("SELECT COUNT(*) FROM meta WHERE key = 'extra'").fetchone() == (
            0,
        )
    finally:
        check.close()


def test_without_a_store_the_copy_is_empty_and_migrated_and_nothing_is_created(
    tmp_path: Path,
) -> None:
    directory = state_path(tmp_path)
    copy = open_memory_copy(ClockedGateway(), directory, migrations=V2)
    try:
        assert copy.version == 900
        assert [r[0] for r in copy.conn.execute("SELECT number FROM schema_migrations")] == [
            800,
            900,
        ]
        assert copy.conn.execute("SELECT COUNT(*) FROM sessions").fetchone() == (0,)
    finally:
        copy.close()
    assert not Path(directory).exists() and not (tmp_path / "state").exists()


def test_pending_migrations_are_applied_to_the_copy_only(tmp_path: Path) -> None:
    gateway = ClockedGateway()
    directory = state_path(tmp_path)
    open_store(gateway, directory, migrations=V1).close()
    copy = open_memory_copy(gateway, directory, migrations=V2)
    try:
        assert copy.version == 900
        assert copy.conn.execute("SELECT COUNT(*) FROM t_nine").fetchone() == (0,)
    finally:
        copy.close()
    assert ledger(directory) == [800]  # the real store still waits for its own migration


def test_a_newer_store_is_refused_and_not_touched(tmp_path: Path) -> None:
    gateway = ClockedGateway()
    directory = state_path(tmp_path)
    open_store(gateway, directory, migrations=V2).close()
    before = listing(Path(directory))
    with pytest.raises(errors.CliError) as raised:
        open_memory_copy(gateway, directory, migrations=V1)
    assert raised.value.details["reason"] == "store_newer"
    assert listing(Path(directory)) == before


def test_an_unsafe_store_directory_is_refused_like_any_other_open(tmp_path: Path) -> None:
    gateway = ClockedGateway()
    directory = state_path(tmp_path)
    open_store(gateway, directory, migrations=V1).close()
    os.chmod(directory, 0o755)
    with pytest.raises(errors.CliError) as raised:
        open_memory_copy(gateway, directory, migrations=V1)
    assert raised.value.details["reason"] == "store_unwritable"


def test_the_fake_gateway_run_token_fits_what_the_digest_accepts() -> None:
    from session_audit.fencing import _check_run_token  # the shape finish_digest requires

    gateway = FakeGateway()
    first, second = gateway.token_urlsafe(24), gateway.token_urlsafe(24)
    assert _check_run_token(first) == first and first != second
    assert len(first) == 32
    gateway.tokens = ["x" * 20]
    assert gateway.token_urlsafe(24) == "x" * 20
    with pytest.raises(ValueError, match="run token"):
        gateway.token_urlsafe(5)
