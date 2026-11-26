"""The two file operations of prune: the ``log.jsonl`` rewrite (``LogWriter.prune_before``) and the
safe removal of a tree (``removal.remove_tree``)."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from session_audit import removal
from session_audit.errors import CliError
from session_audit.log import LogWriter
from session_audit.removal import remove_tree

from .storehelp import ClockedGateway, foreign_gateway

CUTOFF = datetime(2025, 6, 1, tzinfo=UTC)  # before the fake clock's 2026-01-01


def letters(n: int) -> str:
    return "".join("abcdefgh"[(n >> (3 * k)) & 7] for k in range(8))


def entry(when: datetime, n: int = 0) -> bytes:
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"
    doc = {"ts": stamp, "actor": "auto:x", "action": "retro_delete", "retro_id": f"ra-{n}"}
    return json.dumps(doc).encode() + b"\n"


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir(mode=0o700)
    return d


def writer(directory: Path) -> LogWriter:
    return LogWriter(ClockedGateway(), str(directory))


def make_log(directory: Path, *chunks: bytes) -> Path:
    path = directory / "log.jsonl"
    path.write_bytes(b"".join(chunks))
    path.chmod(0o600)
    return path


MS = timedelta(milliseconds=1)


def test_entries_before_the_cutoff_go_and_the_cutoff_itself_stays(directory: Path) -> None:
    path = make_log(
        directory,
        entry(CUTOFF - MS, 1),
        entry(CUTOFF, 2),
        entry(CUTOFF + MS, 3),
        entry(CUTOFF - 90 * MS, 4),
    )
    assert writer(directory).prune_before(CUTOFF) == 2
    assert path.read_bytes() == entry(CUTOFF, 2) + entry(CUTOFF + MS, 3)
    assert path.stat().st_mode & 0o777 == 0o600
    assert sorted(os.listdir(directory)) == ["log.jsonl", "log.lock"]


def test_nothing_to_remove_leaves_the_file_and_creates_no_temporary(directory: Path) -> None:
    path = make_log(directory, entry(CUTOFF, 1))
    inode = path.stat().st_ino
    assert writer(directory).prune_before(CUTOFF) == 0
    assert path.stat().st_ino == inode
    assert "log.jsonl.tmp" not in os.listdir(directory)


def test_a_missing_log_is_nothing_and_is_not_created(directory: Path) -> None:
    assert writer(directory).prune_before(CUTOFF) == 0
    assert writer(directory).count_before(CUTOFF) == 0
    assert "log.jsonl" not in os.listdir(directory)


def test_torn_and_undated_lines_are_kept_byte_for_byte(directory: Path) -> None:
    torn = b'{"ts": "2020-01-01T00:00:00.000Z", "actor": "au'  # a crash mid-write
    junk = [b"not json at all\n", b"[1, 2]\n", b'{"ts": 5}\n', b'{"ts": "yesterday"}\n', b"\n"]
    path = make_log(directory, entry(CUTOFF - MS, 1), *junk, entry(CUTOFF, 2))
    assert writer(directory).prune_before(CUTOFF) == 1
    assert path.read_bytes() == b"".join(junk) + entry(CUTOFF, 2)
    # a last line without its newline is not completed or dropped
    path = make_log(directory, entry(CUTOFF - MS, 1), torn)
    assert writer(directory).prune_before(CUTOFF) == 1
    assert path.read_bytes() == torn
    # and an append afterwards still ends it first, as before
    writer(directory).append("auto:x", "retro_delete", retro_id="ra-aaaaaaaa", key=None)
    assert path.read_bytes().startswith(torn + b"\n{")


def test_an_overlong_line_is_kept_whole(directory: Path) -> None:
    big = b'{"ts": "2000-01-01T00:00:00.000Z", "pad": "' + b"x" * (3 << 20) + b'"}\n'
    path = make_log(directory, entry(CUTOFF - MS, 1), big, entry(CUTOFF, 2))
    assert writer(directory).prune_before(CUTOFF) == 1
    assert path.read_bytes() == big + entry(CUTOFF, 2)


def test_a_stale_temporary_file_from_a_crash_is_overwritten(directory: Path) -> None:
    tmp = directory / "log.jsonl.tmp"
    tmp.write_bytes(b"garbage from a crashed rewrite\n" * 100)
    tmp.chmod(0o600)
    path = make_log(directory, entry(CUTOFF - MS, 1), entry(CUTOFF, 2))
    assert writer(directory).prune_before(CUTOFF) == 1
    assert path.read_bytes() == entry(CUTOFF, 2)
    assert not tmp.exists()


def test_a_crash_before_the_rename_leaves_the_old_log_whole(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = entry(CUTOFF - MS, 1) + entry(CUTOFF, 2)
    path = make_log(directory, original)
    gateway = ClockedGateway()

    def crash(source: object, target: object) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(gateway, "replace", crash)
    with pytest.raises(CliError):
        LogWriter(gateway, str(directory)).prune_before(CUTOFF)
    assert path.read_bytes() == original


def test_count_before_reads_only(directory: Path) -> None:
    path = make_log(directory, entry(CUTOFF - MS, 1), entry(CUTOFF, 2))
    before = path.read_bytes()
    assert writer(directory).count_before(CUTOFF) == 1
    assert path.read_bytes() == before
    assert "log.lock" not in os.listdir(directory) and "log.jsonl.tmp" not in os.listdir(directory)


def test_a_log_that_is_a_symlink_or_not_private_is_refused(directory: Path, tmp_path: Path) -> None:
    target = tmp_path / "other.jsonl"
    target.write_bytes(entry(CUTOFF - MS, 1))
    (directory / "log.jsonl").symlink_to(target)
    with pytest.raises(CliError):
        writer(directory).prune_before(CUTOFF)
    assert target.read_bytes() == entry(CUTOFF - MS, 1)


def test_a_held_log_lock_is_a_retryable_error_and_the_log_is_unchanged(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_log(directory, entry(CUTOFF - MS, 1))
    gateway = ClockedGateway()
    monkeypatch.setattr(gateway, "lock_exclusive", lambda handle, timeout: False)
    with pytest.raises(CliError) as caught:
        LogWriter(gateway, str(directory)).prune_before(CUTOFF)
    assert caught.value.details["retryable"] is True
    assert path.read_bytes() == entry(CUTOFF - MS, 1)


def test_no_entry_appended_during_a_rewrite_is_lost(directory: Path) -> None:
    """A live appender and the rewrite take turns on ``log.lock``: every new entry survives, in
    order, and every old one is gone."""
    old = [entry(CUTOFF - 2000 * MS + i * MS, i) for i in range(2000)]
    make_log(directory, *old)
    appended = 150
    started = threading.Event()
    errors: list[BaseException] = []

    def append() -> None:
        log = writer(directory)
        try:
            for i in range(appended):
                log.append("auto:x", "retro_delete", retro_id=f"ra-{letters(i)}", key=None)
                started.set()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            started.set()

    thread = threading.Thread(target=append)
    thread.start()
    started.wait(10)
    removed = 0
    for _ in range(5):  # rewrites racing the appender
        removed += writer(directory).prune_before(CUTOFF)
    thread.join(30)
    assert not errors
    assert removed == 2000
    lines = (directory / "log.jsonl").read_bytes().splitlines()
    assert len(lines) == appended
    ids = [json.loads(line)["retro_id"] for line in lines]
    assert ids == [f"ra-{letters(i)}" for i in range(appended)]  # none lost, in order


# --- remove_tree ------------------------------------------------------------------------------


def handle_of(path: Path):  # noqa: ANN201
    return ClockedGateway().open_root(str(path))


def test_a_tree_is_removed_and_a_missing_one_counts_as_gone(tmp_path: Path) -> None:
    tree = tmp_path / "parent" / "tree"
    (tree / "a" / "b").mkdir(parents=True)
    (tree / "a" / "b" / "f").write_text("x", encoding="utf-8")
    (tree / "g").write_text("y", encoding="utf-8")
    parent = handle_of(tmp_path / "parent")
    assert remove_tree(ClockedGateway(), parent, b"tree") is True
    assert not tree.exists()
    assert remove_tree(ClockedGateway(), parent, b"tree") is True
    parent.close()


def test_a_symlink_is_unlinked_and_its_target_is_never_entered(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("k", encoding="utf-8")
    tree = tmp_path / "parent" / "tree"
    tree.mkdir(parents=True)
    (tree / "dirlink").symlink_to(outside)
    (tree / "filelink").symlink_to(outside / "keep")
    parent = handle_of(tmp_path / "parent")
    assert remove_tree(ClockedGateway(), parent, b"tree") is True
    assert (outside / "keep").read_text(encoding="utf-8") == "k"
    parent.close()


def test_the_named_entry_itself_being_a_symlink_is_unlinked_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("k", encoding="utf-8")
    (tmp_path / "parent").mkdir()
    (tmp_path / "parent" / "tree").symlink_to(outside)
    parent = handle_of(tmp_path / "parent")
    assert remove_tree(ClockedGateway(), parent, b"tree") is True
    assert (outside / "keep").exists()
    parent.close()


def test_a_tree_of_another_owner_is_left_in_place(tmp_path: Path) -> None:
    tree = tmp_path / "parent" / "tree"
    tree.mkdir(parents=True)
    (tree / "f").write_text("x", encoding="utf-8")
    parent = handle_of(tmp_path / "parent")
    assert remove_tree(foreign_gateway(), parent, b"tree") is False
    assert (tree / "f").exists()
    parent.close()


def test_a_tree_deeper_than_the_limit_is_left_in_place(tmp_path: Path) -> None:
    deep = tmp_path / "parent" / "tree"
    path = deep
    for _ in range(removal.MAX_DEPTH + 2):
        path = path / "d"
    path.mkdir(parents=True)
    parent = handle_of(tmp_path / "parent")
    assert remove_tree(ClockedGateway(), parent, b"tree") is False
    assert path.exists()
    parent.close()


def test_a_directory_with_too_many_entries_is_left_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(removal, "MAX_ENTRIES", 3)
    tree = tmp_path / "parent" / "tree"
    tree.mkdir(parents=True)
    for i in range(4):
        (tree / f"f{i}").write_text("x", encoding="utf-8")
    parent = handle_of(tmp_path / "parent")
    assert remove_tree(ClockedGateway(), parent, b"tree") is False
    assert len(list(tree.iterdir())) == 4
    parent.close()


@pytest.mark.parametrize("name", [b"..", b".", b"", b"a/b", b"../x", b"x\x00y"])
def test_a_name_that_is_not_one_path_component_removes_nothing(tmp_path: Path, name: bytes) -> None:
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "keep").write_text("k", encoding="utf-8")
    (tmp_path / "parent" / "a").mkdir(parents=True)
    (tmp_path / "parent" / "a" / "b").write_text("b", encoding="utf-8")
    parent = handle_of(tmp_path / "parent")
    assert remove_tree(ClockedGateway(), parent, name) is False
    assert remove_tree(ClockedGateway(), parent, name, apply=False) is False
    assert (tmp_path / "x" / "keep").exists()
    assert (tmp_path / "parent" / "a" / "b").exists()
    parent.close()


def test_a_check_only_walk_removes_nothing_and_gives_the_same_answer(tmp_path: Path) -> None:
    tree = tmp_path / "parent" / "tree"
    (tree / "a").mkdir(parents=True)
    (tree / "a" / "f").write_text("x", encoding="utf-8")
    parent = handle_of(tmp_path / "parent")
    assert remove_tree(ClockedGateway(), parent, b"tree", apply=False) is True
    assert (tree / "a" / "f").exists()
    assert remove_tree(foreign_gateway(), parent, b"tree", apply=False) is False
    deep = tree
    for _ in range(removal.MAX_DEPTH + 2):
        deep = deep / "d"
    deep.mkdir(parents=True)
    assert remove_tree(ClockedGateway(), parent, b"tree", apply=False) is False
    assert deep.exists()
    parent.close()


def test_an_empty_read_only_directory_plans_removable_but_a_full_one_does_not(
    tmp_path: Path,
) -> None:
    parent_dir = tmp_path / "parent"
    (parent_dir / "empty").mkdir(parents=True, mode=0o755)
    (parent_dir / "empty").chmod(0o500)
    full = parent_dir / "full"
    full.mkdir()
    (full / "f").write_text("x", encoding="utf-8")
    full.chmod(0o500)
    parent = handle_of(parent_dir)
    try:
        assert remove_tree(ClockedGateway(), parent, b"empty", apply=False) is True
        assert remove_tree(ClockedGateway(), parent, b"full", apply=False) is False
        assert remove_tree(ClockedGateway(), parent, b"empty") is True
        assert not (parent_dir / "empty").exists()
    finally:
        parent.close()
        full.chmod(0o700)
