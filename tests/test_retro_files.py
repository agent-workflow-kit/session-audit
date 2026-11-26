"""Step 15's files: exact modes, atomic writes, hostile paths and a full disk (issue #26)."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from session_audit import retrofiles
from session_audit.errors import CliError
from session_audit.fencing import FencedDigest
from session_audit.gateway import SystemGateway
from session_audit.retrofiles import FilesError, delete_local, remove_digest, write_run_files

RETRO = "ra-abcdefgh"
TOKEN = "t" * 32


def fenced(text: bytes = b"digest text\n") -> FencedDigest:
    sha = hashlib.sha256(text).hexdigest()
    manifest = {"retro_id": RETRO, "run_token": TOKEN, "digest_sha256": sha}
    return FencedDigest(
        text, sha, manifest, (json.dumps(manifest) + "\n").encode(), (), "n", "15", 2
    )


@pytest.fixture
def state(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    return directory


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_the_files_are_written_with_exact_modes_and_the_manifest_hash(state: Path) -> None:
    previous = os.umask(0o022)  # the modes are exact whatever the umask is
    try:
        files = write_run_files(SystemGateway(), str(state), RETRO, TOKEN, fenced())
    finally:
        os.umask(previous)
    digest, manifest, prompt = Path(files.digest), Path(files.manifest), Path(files.prompt)
    assert digest.read_bytes() == b"digest text\n"
    assert (
        json.loads(manifest.read_text())["digest_sha256"]
        == hashlib.sha256(digest.read_bytes()).hexdigest()
    )
    for directory in (
        state / "digests",
        state / "digests" / RETRO,
        state / "prompts",
        state / "prompts" / RETRO,
        state / "outcomes",
    ):
        assert mode(directory) == 0o700, directory
    for file in (digest, manifest, prompt):
        assert mode(file) == 0o600, file
    assert not Path(files.outcome).exists()
    assert files.outcome == str(state / "outcomes" / f"{RETRO}.json")
    assert sorted(p.name for p in (state / "digests" / RETRO).iterdir()) == [
        "digest.md",
        "manifest.json",
    ]


def test_the_prompt_holds_ids_a_token_a_hash_and_relative_paths_only(state: Path) -> None:
    files = write_run_files(SystemGateway(), str(state), RETRO, TOKEN, fenced())
    assert Path(files.prompt).read_text() == (
        f"retro_id: {RETRO}\nrun_token: {TOKEN}\n"
        f"digest_sha256: {hashlib.sha256(b'digest text\n').hexdigest()}\n"
        "inputs:\n- inputs/1/digest.md\n- inputs/1/manifest.json\n"
    )


def test_an_existing_retro_directory_is_refused_and_left_alone(state: Path) -> None:
    (state / "digests" / RETRO).mkdir(parents=True, mode=0o700)
    (state / "digests" / RETRO / "mine").write_text("keep")
    with pytest.raises(FilesError):
        write_run_files(SystemGateway(), str(state), RETRO, TOKEN, fenced())
    assert (state / "digests" / RETRO / "mine").read_text() == "keep"
    assert not (state / "prompts" / RETRO).exists()


def test_an_existing_prompt_directory_is_refused_and_the_digest_directory_removed(
    state: Path,
) -> None:
    (state / "prompts" / RETRO).mkdir(parents=True, mode=0o700)
    with pytest.raises(FilesError):
        write_run_files(SystemGateway(), str(state), RETRO, TOKEN, fenced())
    assert not (state / "digests" / RETRO).exists()  # made by this call, removed again
    assert (state / "prompts" / RETRO).is_dir()  # not made by this call, left


def test_a_symlink_at_the_retro_directory_is_never_followed(state: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    (state / "digests").mkdir(mode=0o700)
    (state / "digests" / RETRO).symlink_to(target)
    with pytest.raises(FilesError):
        write_run_files(SystemGateway(), str(state), RETRO, TOKEN, fenced())
    assert list(target.iterdir()) == []
    assert (state / "digests" / RETRO).is_symlink()


@pytest.mark.parametrize("parent", ["digests", "prompts", "outcomes"])
def test_a_symlinked_parent_is_refused(state: Path, tmp_path: Path, parent: str) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    (state / parent).symlink_to(target)
    with pytest.raises(FilesError):
        write_run_files(SystemGateway(), str(state), RETRO, TOKEN, fenced())
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("bad", [0o755, 0o770, 0o500, 0o1700])
def test_a_parent_with_the_wrong_mode_is_refused_and_not_repaired(state: Path, bad: int) -> None:
    (state / "prompts").mkdir()
    (state / "prompts").chmod(bad)
    with pytest.raises(FilesError):
        write_run_files(SystemGateway(), str(state), RETRO, TOKEN, fenced())
    assert mode(state / "prompts") == bad


def test_a_parent_that_is_a_file_is_refused(state: Path) -> None:
    (state / "outcomes").write_text("x")
    with pytest.raises(FilesError):
        write_run_files(SystemGateway(), str(state), RETRO, TOKEN, fenced())


@pytest.mark.parametrize("failing", ["open_private", "replace"])
def test_a_full_disk_removes_what_was_made_and_is_retryable(
    state: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    gateway = SystemGateway()
    real = getattr(gateway, failing)
    calls = {"n": 0}

    def fail(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:  # the second file: the first is already in place
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(*args, **kwargs)

    monkeypatch.setattr(gateway, failing, fail)
    with pytest.raises(FilesError) as caught:
        write_run_files(gateway, str(state), RETRO, TOKEN, fenced())
    assert caught.value.retryable
    assert not (state / "digests" / RETRO).exists()
    assert not (state / "prompts" / RETRO).exists()
    assert sorted(p.name for p in (state / "digests").iterdir()) == []


def test_a_failed_write_leaves_no_temporary_file(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = SystemGateway()

    def boom(source: Any, target: Any) -> None:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(gateway, "replace", boom)
    with pytest.raises(FilesError) as caught:
        write_run_files(gateway, str(state), RETRO, TOKEN, fenced())
    assert not caught.value.retryable
    assert [p for p in state.rglob("*") if p.is_file()] == []


def test_a_planted_temporary_file_is_refused(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = SystemGateway()
    monkeypatch.setattr(gateway, "token_hex", lambda n: "00" * n)
    plant = state / "digests" / RETRO
    plant.mkdir(parents=True, mode=0o700)  # the directory exists: refused earlier, so plant after
    plant.rmdir()

    real_mkdir = gateway.mkdir

    def mkdir_and_plant(path: Any, mode_: int) -> None:
        real_mkdir(path, mode_)
        if str(path).endswith(f"digests/{RETRO}"):
            Path(path, ".digest.md.tmp-" + "00" * 8).write_text("planted")

    monkeypatch.setattr(gateway, "mkdir", mkdir_and_plant)
    with pytest.raises(FilesError):
        write_run_files(gateway, str(state), RETRO, TOKEN, fenced())
    assert not (state / "digests" / RETRO).exists()


def test_delete_local_removes_the_prompt_and_the_outcome_and_is_idempotent(state: Path) -> None:
    files = write_run_files(SystemGateway(), str(state), RETRO, TOKEN, fenced())
    Path(files.outcome).write_text("{}")
    assert delete_local(SystemGateway(), str(state), RETRO)
    assert not (state / "prompts" / RETRO).exists() and not Path(files.outcome).exists()
    assert (state / "digests" / RETRO).is_dir()  # the digest waits for its retention
    assert delete_local(SystemGateway(), str(state), RETRO)


@pytest.mark.parametrize(
    "bad", ["...........", "ra-ABCDEFGH", "ra-abcdefg1", "ra-abcdefgh/", "ra-.."]
)
def test_a_retro_id_that_is_not_an_id_removes_nothing(state: Path, bad: str) -> None:
    gateway = SystemGateway()
    kept = [state / "prompts" / "sibling", state / "digests" / "sibling"]
    for directory in kept:
        directory.mkdir(parents=True, mode=0o700)
        (directory / "keep").write_text("x")
    (state / "outcomes").mkdir(mode=0o700)
    (state / "outcomes" / "sibling.json").write_text("{}")
    (state / "prompts" / bad.strip("/")).mkdir(exist_ok=True, mode=0o700)
    assert delete_local(gateway, str(state), bad) is False
    assert remove_digest(gateway, str(state), bad) is False
    assert (state / "prompts" / bad.strip("/")).is_dir()
    assert all((d / "keep").exists() for d in kept)
    assert (state / "outcomes" / "sibling.json").exists()


def test_delete_local_never_follows_a_symlink(state: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep").write_text("x")
    (state / "prompts").mkdir(mode=0o700)
    (state / "prompts" / RETRO).symlink_to(target)
    assert delete_local(SystemGateway(), str(state), RETRO)
    assert (target / "keep").read_text() == "x"
    assert not (state / "prompts" / RETRO).exists()


def test_the_module_names_the_directories() -> None:
    assert (retrofiles.DIGESTS, retrofiles.PROMPTS, retrofiles.OUTCOMES) == (
        "digests",
        "prompts",
        "outcomes",
    )


def test_a_digest_that_does_not_match_its_hash_creates_nothing(state: Path) -> None:
    good = fenced()
    wrong_bytes = FencedDigest(
        b"tampered\n", good.digest_sha256, good.manifest, good.manifest_bytes, (), "n", "15", 2
    )
    wrong_manifest = FencedDigest(
        good.digest_bytes,
        good.digest_sha256,
        {**good.manifest, "digest_sha256": "0" * 64},
        good.manifest_bytes,
        (),
        "n",
        "15",
        2,
    )
    for candidate in (wrong_bytes, wrong_manifest):
        with pytest.raises(CliError) as caught:
            write_run_files(SystemGateway(), str(state), RETRO, TOKEN, candidate)
        assert caught.value.code == "internal"
    assert not (state / "digests").exists() and not (state / "prompts").exists()


@pytest.mark.parametrize("parent", ["prompts", "outcomes"])
def test_delete_local_does_not_enter_a_symlinked_parent(
    state: Path, tmp_path: Path, parent: str
) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / RETRO).mkdir()
    (target / RETRO / "keep").write_text("x")
    (target / f"{RETRO}.json").write_text("x")
    (state / parent).symlink_to(target)
    assert delete_local(SystemGateway(), str(state), RETRO) is False
    assert (target / RETRO / "keep").read_text() == "x"
    assert (target / f"{RETRO}.json").exists()


def test_remove_digest_does_not_enter_a_symlinked_parent(state: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    (target / RETRO).mkdir(parents=True)
    (target / RETRO / "keep").write_text("x")
    (state / "digests").symlink_to(target)
    assert retrofiles.remove_digest(SystemGateway(), str(state), RETRO) is False
    assert (target / RETRO / "keep").read_text() == "x"
