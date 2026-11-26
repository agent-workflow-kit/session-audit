"""Discovery and safe open (issue #10), against a real directory tree and against the fake
gateway's in-memory tree: the same scenarios run on both through ``World``."""

from __future__ import annotations

import errno
import os
import posixpath
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import pytest

from session_audit import reader
from session_audit.errors import CliError
from session_audit.gateway import SystemGateway
from session_audit.reader import Candidate, Opened, Skip
from session_audit.store import open_store

from .fake_gateway import FakeGateway
from .storehelp import ClockedGateway

U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"
U3 = "33333333-3333-4333-8333-333333333333"
NS = 1_000_000_000


class World(Protocol):
    gateway: Any
    root: str

    def dir(self, rel: str, mtime: int = 0) -> None: ...
    def file(self, rel: str, data: bytes = b"x\n", mtime: int = 0) -> None: ...
    def symlink(self, rel: str, target: str) -> None: ...
    def fifo(self, rel: str) -> None: ...
    def hardlink(self, rel: str, existing: str) -> None: ...
    def remove(self, rel: str) -> None: ...
    def set_root_mode(self, mode: int) -> None: ...


class RealWorld:
    def __init__(self, tmp: Path) -> None:
        self.gateway = SystemGateway()
        self.base = tmp / "home"
        self.root = str(self.base / "projects")
        self.base.mkdir(mode=0o700)
        os.mkdir(self.root, 0o700)

    def _p(self, rel: str) -> str:
        return os.path.join(self.root, rel)

    def dir(self, rel: str, mtime: int = 0) -> None:
        os.makedirs(self._p(rel), mode=0o700, exist_ok=True)
        if mtime:
            os.utime(self._p(rel), ns=(mtime, mtime))

    def file(self, rel: str, data: bytes = b"x\n", mtime: int = 0) -> None:
        self.dir(os.path.dirname(rel))
        with open(self._p(rel), "wb") as handle:
            handle.write(data)
        if mtime:
            os.utime(self._p(rel), ns=(mtime, mtime))

    def symlink(self, rel: str, target: str) -> None:
        self.dir(os.path.dirname(rel))
        os.symlink(target, self._p(rel))

    def fifo(self, rel: str) -> None:
        self.dir(os.path.dirname(rel))
        os.mkfifo(self._p(rel))

    def hardlink(self, rel: str, existing: str) -> None:
        self.dir(os.path.dirname(rel))
        os.link(self._p(existing), self._p(rel))

    def remove(self, rel: str) -> None:
        os.remove(self._p(rel))

    def set_root_mode(self, mode: int) -> None:
        os.chmod(self.root, mode)


class FakeWorld:
    def __init__(self) -> None:
        self.gateway = FakeGateway()
        self.root = "/home/u/projects"
        for part in ("/home", "/home/u"):
            self.gateway.add_dir(part)
        self.gateway.add_dir(self.root)

    def _p(self, rel: str) -> str:
        return posixpath.join(self.root, rel)

    def _parents(self, rel: str) -> None:
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            path = self._p("/".join(parts[:i]))
            if path not in self.gateway.files:
                self.gateway.add_dir(path)

    def dir(self, rel: str, mtime: int = 0) -> None:
        if rel:
            self._parents(rel + "/x")
            self.gateway.files[self._p(rel)].mtime_ns = mtime

    def file(self, rel: str, data: bytes = b"x\n", mtime: int = 0) -> None:
        self._parents(rel)
        self.gateway.add_file(self._p(rel), data, mtime_ns=mtime)

    def symlink(self, rel: str, target: str) -> None:
        self._parents(rel)
        self.gateway.add_symlink(self._p(rel), target)

    def fifo(self, rel: str) -> None:
        self._parents(rel)
        self.gateway.add_fifo(self._p(rel))

    def hardlink(self, rel: str, existing: str) -> None:
        self._parents(rel)
        entry = self.gateway.files[self._p(existing)]
        entry.nlink += 1
        self.gateway.files[self._p(rel)] = entry

    def remove(self, rel: str) -> None:
        del self.gateway.files[self._p(rel)]

    def set_root_mode(self, mode: int) -> None:
        self.gateway.files[self.root].mode = mode


@pytest.fixture(params=["real", "fake"])
def world(request: pytest.FixtureRequest, tmp_path: Path) -> World:
    return RealWorld(tmp_path) if request.param == "real" else FakeWorld()  # type: ignore[return-value]


def never(_: str) -> bool:
    return False


def no_prune(_: str) -> int | None:
    return None


def discovery(
    w: World,
    *,
    roots: list[str] | None = None,
    excluded: Callable[[str], bool] = never,
    pruned_at: Callable[[str], int | None] = no_prune,
    paths: list[str] | None = None,
    cap: int = reader.SCAN_CAP,
) -> reader.Discovery:
    return reader.open_discovery(
        w.gateway,
        roots or [w.root],
        excluded=excluded,
        pruned_at=pruned_at,
        paths=paths or [],
        scan_cap=cap,
    )


def listed(w: World, **kw: Any) -> list[Candidate]:
    with discovery(w, **kw) as d:
        return list(d.candidates())


def rels(found: list[Candidate]) -> list[str]:
    return [b"/".join(c.rel).decode("utf-8", "replace") for c in found]


# --- the paths that are transcripts ------------------------------------------------------------


def test_discovers_main_subagent_and_workflow_paths(world: World) -> None:
    world.file(f"proj/{U1}.jsonl", b"main\n", mtime=5 * NS)
    world.file(f"proj/{U1}/subagents/agent-ab_1.jsonl", mtime=4 * NS)
    world.file(f"proj/{U1}/subagents/workflows/wf-1.x/agent-z9.jsonl", mtime=3 * NS)
    found = listed(world)
    assert rels(found) == [
        f"proj/{U1}.jsonl",
        f"proj/{U1}/subagents/agent-ab_1.jsonl",
        f"proj/{U1}/subagents/workflows/wf-1.x/agent-z9.jsonl",
    ]
    main, sub, wf = found
    assert (main.kind, main.session_id, main.agent_id, main.workflow) == ("main", U1, None, None)
    assert (sub.kind, sub.session_id, sub.agent_id, sub.parent_session_id) == (
        "subagent",
        U1,
        "ab_1",
        U1,
    )
    assert (wf.kind, wf.agent_id, wf.workflow) == ("workflow", "z9", "wf-1.x")
    assert main.path == os.fsencode(world.root) + f"/proj/{U1}.jsonl".encode()
    assert (main.project, main.project_dir, main.size, main.mtime_ns) == (
        b"proj",
        "proj",
        5,
        5 * NS,
    )
    assert main.parent_session_id is None


def test_a_subagent_file_without_its_parent_still_names_the_session(world: World) -> None:
    world.file(f"proj/{U2}/subagents/agent-a.jsonl")
    (only,) = listed(world)
    assert (only.kind, only.session_id) == ("subagent", U2)


@pytest.mark.parametrize(
    "rel",
    [
        "proj/AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA.jsonl",  # uppercase uuid
        "proj/not-a-uuid.jsonl",
        f"proj/{U1}.json",
        f"proj/{U1}.jsonl.bak",
        f"proj/{U1}/tool-results/agent-a.jsonl",
        f"proj/{U1}/memory/agent-a.jsonl",
        f"proj/{U1}/journal.jsonl",
        "proj/journal.jsonl",
        f"proj/{U1}/subagents/journal.jsonl",
        f"proj/{U1}/subagents/agent-.jsonl",  # empty id
        f"proj/{U1}/subagents/agent-a b.jsonl",
        f"proj/{U1}/subagents/agent-{'a' * 129}.jsonl",
        f"proj/{U1}/subagents/agent-a.meta.json",
        f"proj/{U1}/subagents/workflows/agent-a.jsonl",  # no wf directory
        f"proj/{U1}/subagents/workflows/wf/deeper/agent-a.jsonl",  # no deeper
        f"proj/{U1}/subagents/other/agent-a.jsonl",
        f"proj/{U1}/subagents/agent-a.jsonl/agent-b.jsonl",
        f"proj/{U1}/{U2}/subagents/agent-a.jsonl",
        f"{U1}.jsonl",  # directly under the root: no project directory
    ],
)
def test_other_names_and_depths_are_not_transcripts(world: World, rel: str) -> None:
    world.file(rel)
    assert listed(world) == []


def test_a_workflow_name_over_128_characters_is_not_a_transcript(world: World) -> None:
    world.file(f"proj/{U1}/subagents/workflows/{'w' * 129}/agent-a.jsonl")
    assert listed(world) == []


def test_never_read_directories_are_never_listed_or_opened(tmp_path: Path) -> None:
    w = FakeWorld()
    w.file(f"proj/{U1}/tool-results/big.jsonl")
    w.file(f"proj/{U1}/memory/m.md")
    w.file(f"proj/{U1}/subagents/agent-a.jsonl")
    opened: list[str] = []
    w.gateway.on_open = opened.append
    listed(w)
    assert all("tool-results" not in p and "memory" not in p for p in opened)


def test_projects_and_files_are_in_descending_mtime_order(world: World) -> None:
    world.dir("old", mtime=1 * NS)
    world.dir("new", mtime=9 * NS)
    world.dir("mid", mtime=5 * NS)
    world.file(f"old/{U1}.jsonl", mtime=1 * NS)
    world.file(f"new/{U1}.jsonl", mtime=2 * NS)
    world.file(f"new/{U2}.jsonl", mtime=8 * NS)
    world.file(f"new/{U3}.jsonl", mtime=3 * NS)
    world.file(f"mid/{U1}.jsonl", mtime=5 * NS)
    # the file writes touched the directories: set the order we mean
    for name, t in (("old", 1), ("new", 9), ("mid", 5)):
        if isinstance(world, RealWorld):
            os.utime(world._p(name), ns=(t * NS, t * NS))
        else:
            world.dir(name, mtime=t * NS)
    assert rels(listed(world)) == [
        f"new/{U2}.jsonl",
        f"new/{U3}.jsonl",
        f"new/{U1}.jsonl",
        f"mid/{U1}.jsonl",
        f"old/{U1}.jsonl",
    ]


def test_filtering_a_project_keeps_subagent_files_beside_it(world: World) -> None:
    world.file(f"a/{U1}.jsonl")
    world.file(f"b/{U1}.jsonl")
    assert rels(listed(world, paths=[world.root + "/a"])) == [f"a/{U1}.jsonl"]


# --- unsafe files ------------------------------------------------------------------------------


def test_symlinks_fifos_and_hard_links_are_unsafe_and_never_listed(world: World) -> None:
    world.file("elsewhere/secret.txt", b"secret")
    world.symlink(f"proj/{U1}.jsonl", world.root + "/elsewhere/secret.txt")
    world.fifo(f"proj/{U2}.jsonl")
    world.file(f"proj/{U3}.jsonl")
    world.hardlink(f"proj/{U3}.jsonl.keep", f"proj/{U3}.jsonl")  # a second name: nlink == 2
    world.symlink(f"proj/{U3}/subagents", world.root + "/elsewhere")  # symlinked directory
    with discovery(world) as d:
        found = list(d.candidates())
        stats = d.stats
    assert found == []
    assert stats.unsafe == 4  # symlinked file, fifo, hard link, symlinked "subagents" directory
    assert stats.seen == 3


def test_a_symlinked_project_directory_is_not_entered(world: World) -> None:
    world.file(f"real/{U1}.jsonl")
    world.symlink("link", world.root + "/real")
    with discovery(world) as d:
        found = list(d.candidates())
        assert rels(found) == [f"real/{U1}.jsonl"]
        assert d.stats.unsafe == 1


def test_a_symlinked_session_directory_is_not_entered(world: World) -> None:
    world.file("other/subagents/agent-a.jsonl")
    world.symlink(f"proj/{U1}", world.root + "/other")
    world.file(f"proj/{U2}.jsonl")
    with discovery(world) as d:
        assert rels(list(d.candidates())) == [f"proj/{U2}.jsonl"]
        assert d.stats.unsafe == 1


def test_wrong_owner_socket_and_device_are_unsafe() -> None:
    w = FakeWorld()
    w.file(f"proj/{U1}.jsonl")
    w.gateway.files[w._p(f"proj/{U1}.jsonl")].uid = 4321
    w.gateway.add_socket(w._p(f"proj/{U2}.jsonl"))
    w.gateway.add_device(w._p(f"proj/{U3}.jsonl"))
    with discovery(w) as d:
        assert list(d.candidates()) == []
        assert d.stats.unsafe == 3 and d.stats.seen == 3


def test_open_a_regular_file_gives_blocking_content_and_its_fstat(world: World) -> None:
    world.file(f"proj/{U1}.jsonl", b"hello\n", mtime=7 * NS)
    with discovery(world) as d:
        (c,) = d.candidates()
        got = d.open(c)
        assert isinstance(got, Opened)
        try:
            assert got.file.read() == b"hello\n"
            assert stat.S_ISREG(got.stat.st_mode) and got.stat.st_size == 6
            if isinstance(world, RealWorld):
                assert os.get_blocking(got.file.fileno())
            else:
                assert got.file.fake_blocking  # type: ignore[attr-defined]
        finally:
            got.close()


def test_a_file_replaced_by_a_symlink_after_listing_is_not_followed(world: World) -> None:
    world.file("elsewhere/secret.txt", b"secret")
    world.file(f"proj/{U1}.jsonl")
    with discovery(world) as d:
        (c,) = d.candidates()
        world.remove(f"proj/{U1}.jsonl")
        world.symlink(f"proj/{U1}.jsonl", world.root + "/elsewhere/secret.txt")
        assert d.open(c) is Skip.UNSAFE
        assert d.stats.unsafe == 1


def test_a_file_replaced_by_a_fifo_after_listing_does_not_block(world: World) -> None:
    world.file(f"proj/{U1}.jsonl")
    with discovery(world) as d:
        (c,) = d.candidates()
        world.remove(f"proj/{U1}.jsonl")
        world.fifo(f"proj/{U1}.jsonl")
        assert d.open(c) is Skip.UNSAFE


def test_a_file_hard_linked_after_listing_is_unsafe(world: World) -> None:
    world.file(f"proj/{U1}.jsonl")
    with discovery(world) as d:
        (c,) = d.candidates()
        world.hardlink("proj/copy", f"proj/{U1}.jsonl")
        assert d.open(c) is Skip.UNSAFE


def test_a_directory_replaced_by_a_symlink_after_listing_is_unsafe(world: World) -> None:
    world.file("elsewhere/subagents/agent-a.jsonl")
    world.file(f"proj/{U1}/subagents/agent-a.jsonl")
    with discovery(world) as d:
        (c,) = d.candidates()
        if isinstance(world, RealWorld):
            os.rename(world._p(f"proj/{U1}"), world._p("moved"))
        else:
            for key in [k for k in world.gateway.files if f"proj/{U1}" in k]:
                del world.gateway.files[key]
        world.symlink(f"proj/{U1}", world.root + "/elsewhere")
        assert d.open(c) is Skip.UNSAFE


def test_a_file_that_vanishes_after_listing_is_counted_vanished(world: World) -> None:
    world.file(f"proj/{U1}.jsonl")
    with discovery(world) as d:
        (c,) = d.candidates()
        world.remove(f"proj/{U1}.jsonl")
        assert d.open(c) is Skip.VANISHED
        assert d.stats.vanished == 1 and d.stats.unsafe == 0


# --- self-exclusion and pruned sessions --------------------------------------------------------


def test_excluded_is_asked_at_every_open_and_the_file_is_not_opened(world: World) -> None:
    world.file(f"retro-proj/{U1}.jsonl")
    world.file(f"other/{U2}.jsonl")
    excluded: set[str] = set()
    with discovery(world, excluded=lambda name: name in excluded) as d:
        found = list(d.candidates())
        assert len(found) == 2
        excluded.add("retro-proj")  # recorded after the listing, before the open
        results = {c.project_dir: d.open(c) for c in found}
        assert results["retro-proj"] is Skip.EXCLUDED
        assert isinstance(results["other"], Opened)
        results["other"].close()  # type: ignore[union-attr]
        assert d.stats.excluded == 1


def test_exclusion_reads_the_retro_sessions_table_at_each_open(
    world: World, tmp_path: Path
) -> None:
    gateway = ClockedGateway()
    with open_store(gateway, str(tmp_path / "state")) as store:
        world.file(f"retro-proj/{U1}.jsonl")
        with reader.open_discovery(
            world.gateway,
            [world.root],
            excluded=reader.store_excluded(store),
            pruned_at=reader.store_pruned_at(store),
        ) as d:
            (c,) = d.candidates()
            with store.transaction() as conn:
                conn.execute(
                    "INSERT INTO retro_sessions (csession_id, retro_id, csession_dir, project_dir,"
                    " created_at) VALUES ('cs-aaaaaaaa', 'ra-aaaaaaaa', '/x', 'retro-proj', 1)"
                )
            assert d.open(c) is Skip.EXCLUDED
            with store.transaction() as conn:
                conn.execute("DELETE FROM retro_sessions")
            got = d.open(c)
            assert isinstance(got, Opened)
            got.close()


def test_store_excluded_never_matches_a_name_that_is_not_text(tmp_path: Path) -> None:
    with open_store(ClockedGateway(), str(tmp_path / "state")) as store:
        assert reader.store_excluded(store)("p\udcff") is False


def test_a_pruned_session_is_skipped_unless_its_file_is_newer(world: World) -> None:
    world.file(f"proj/{U1}.jsonl", mtime=10 * NS)
    world.file(f"proj/{U2}.jsonl", mtime=30 * NS)
    world.file(f"proj/{U2}/subagents/agent-a.jsonl", mtime=10 * NS)
    pruned = {U1: 10_000, U2: 20_000}  # milliseconds
    with discovery(world, pruned_at=pruned.get) as d:
        got = {c.rel[-1]: d.open(c) for c in d.candidates()}
        assert got[f"{U1}.jsonl".encode()] is Skip.PRUNED  # mtime == pruned_at: not later
        assert isinstance(got[f"{U2}.jsonl".encode()], Opened)  # later: a new session
        assert got[b"agent-a.jsonl"] is Skip.PRUNED  # an agent follows its session's prune time
        assert d.stats.pruned == 2
        for each in got.values():
            if isinstance(each, Opened):
                each.close()


# --- roots -------------------------------------------------------------------------------------


def test_a_group_writable_root_is_unusable_and_the_others_continue(
    world: World, tmp_path: Path
) -> None:
    if isinstance(world, RealWorld):
        second = tmp_path / "second"
        second.mkdir(mode=0o700)
        spare = str(second)
    else:
        world.gateway.add_dir("/second")
        spare = "/second"
    world.file(f"proj/{U1}.jsonl")
    world.set_root_mode(0o770)
    with discovery(world, roots=[world.root, spare]) as d:
        assert list(d.candidates()) == []
        assert d.unusable_roots == [(world.root, "writable")]
        assert d.warnings == ["root_unusable"]


def test_an_other_writable_root_is_unusable(world: World) -> None:
    world.set_root_mode(0o757)
    with pytest.raises(CliError) as exc:
        discovery(world)
    assert exc.value.exit_code == 12
    assert exc.value.details == {"reason": "no_readable_roots", "retryable": False}


def test_every_root_unusable_exits_12_without_a_path_in_the_message(world: World) -> None:
    with pytest.raises(CliError) as exc:
        discovery(world, roots=["/nonexistent-root-dir"])
    assert exc.value.exit_code == 12 and exc.value.code == "failed"
    assert "nonexistent" not in exc.value.message


def test_a_root_that_is_a_file_or_owned_by_another_user_is_unusable() -> None:
    w = FakeWorld()
    w.gateway.add_file("/afile")
    w.gateway.add_dir("/foreign", uid=4321)
    with pytest.raises(CliError):
        discovery(w, roots=["/afile", "/foreign"])
    w.file(f"p/{U1}.jsonl")
    with discovery(w, roots=["/afile", "/foreign", w.root]) as d:
        assert d.unusable_roots == [("/afile", "not_directory"), ("/foreign", "not_owned")]
        assert len(list(d.candidates())) == 1


def test_a_symlinked_root_is_resolved_once(world: World, tmp_path: Path) -> None:
    world.file(f"proj/{U1}.jsonl")
    if isinstance(world, RealWorld):
        link = str(tmp_path / "link")
        os.symlink(world.root, link)
    else:
        link = "/linked"
        world.gateway.add_symlink(link, world.root)
    assert len(listed(world, roots=[link])) == 1


def test_the_same_root_twice_is_read_once(world: World, tmp_path: Path) -> None:
    world.file(f"proj/{U1}.jsonl")
    if isinstance(world, RealWorld):
        link = str(tmp_path / "again")
        os.symlink(world.root, link)
    else:
        link = "/again"
        world.gateway.add_symlink(link, world.root)
    with discovery(world, roots=[world.root, link]) as d:
        assert len(list(d.candidates())) == 1
        assert d.unusable_roots == []


# --- caps and hostile shapes -------------------------------------------------------------------


def test_a_huge_directory_is_capped_with_a_warning(world: World) -> None:
    for i in range(40):
        world.file(f"proj/{i:08x}-0000-4000-8000-000000000000.jsonl")
    with discovery(world, cap=10) as d:
        found = list(d.candidates())
        assert 0 < len(found) <= 9  # the root entry and at most 9 of the project's
        assert d.stats.capped_roots == [0]
        assert d.warnings == ["scan_capped"]


def test_a_directory_exactly_at_the_cap_is_not_capped(world: World) -> None:
    for i in range(4):
        world.file(f"proj/{i:08x}-0000-4000-8000-000000000000.jsonl")
    with discovery(world, cap=5) as d:  # one project entry plus four files
        assert len(list(d.candidates())) == 4
        assert d.warnings == []


def test_deep_nesting_below_a_transcript_directory_is_never_followed(world: World) -> None:
    deep = "/".join(f"d{i}" for i in range(200))
    world.file(f"proj/{U1}/subagents/workflows/wf/{deep}/agent-a.jsonl")
    world.file(f"proj/{U1}/subagents/agent-ok.jsonl")
    assert rels(listed(world)) == [f"proj/{U1}/subagents/agent-ok.jsonl"]


def test_a_non_utf8_name_keeps_its_bytes_and_displays_safely() -> None:
    w = FakeWorld()
    w.file("p\udcff\udce9/" + f"{U1}.jsonl")
    (c,) = listed(w)
    assert c.project == b"p\xff\xe9" and c.path.endswith(b"/p\xff\xe9/" + f"{U1}.jsonl".encode())
    shown = reader.display_path(c.path)
    assert "\udcff" not in shown and "�" in shown


def test_a_non_utf8_name_on_a_real_file_system_round_trips(tmp_path: Path) -> None:
    w = RealWorld(tmp_path)
    name = b"p\xff\xe9"
    os.mkdir(os.fsencode(w.root) + b"/" + name, 0o700)
    with open(os.fsencode(w.root) + b"/" + name + b"/" + f"{U1}.jsonl".encode(), "wb") as f:
        f.write(b"x\n")
    with discovery(w) as d:
        (c,) = d.candidates()
        got = d.open(c)
        assert isinstance(got, Opened)
        got.close()
    assert c.project == name and c.path == os.fsencode(w.root) + b"/" + name + b"/" + (
        f"{U1}.jsonl".encode()
    )


def test_display_path_neutralizes_escape_sequences() -> None:
    shown = reader.display_path(b"/r/\x1b[31mred\x07/\xff")
    assert "\x1b" not in shown and "\x07" not in shown and "\udcff" not in shown


# --- --path ------------------------------------------------------------------------------------


def test_path_below_a_root_selects_files_by_components(world: World) -> None:
    world.file(f"a/{U1}.jsonl")
    world.file(f"a/{U2}.jsonl")
    world.file(f"ab/{U1}.jsonl")
    assert sorted(rels(listed(world, paths=[world.root + "/a"]))) == [
        f"a/{U1}.jsonl",
        f"a/{U2}.jsonl",
    ]
    only = listed(world, paths=[world.root + f"/a/{U2}.jsonl"])
    assert rels(only) == [f"a/{U2}.jsonl"]
    assert {c.project for c in listed(world, paths=[world.root + "/a"])} == {b"a"}  # not "ab"


def test_the_root_itself_is_a_valid_path(world: World) -> None:
    world.file(f"a/{U1}.jsonl")
    assert len(listed(world, paths=[world.root, world.root + "/"])) == 1


def test_a_path_that_does_not_exist_below_a_root_matches_nothing(world: World) -> None:
    world.file(f"a/{U1}.jsonl")
    assert listed(world, paths=[world.root + "/missing/deeper"]) == []


@pytest.mark.parametrize(
    "tail",
    ["/a/../b", "/..", "/a/..", "/./../x"],
)
def test_path_with_dot_dot_exits_2_naming_path(world: World, tail: str) -> None:
    world.file(f"a/{U1}.jsonl")
    with pytest.raises(CliError) as exc:
        discovery(world, paths=[world.root + tail])
    assert exc.value.exit_code == 2 and exc.value.details == {"field": "--path"}


def test_path_outside_every_root_exits_2(world: World) -> None:
    for outside in ("/", "/tmp", world.root + "-evil/a", world.root[:-1], "relative/path"):
        with pytest.raises(CliError) as exc:
            discovery(world, paths=[outside])
        assert exc.value.exit_code == 2 and exc.value.details["field"] == "--path"


def test_a_symlinked_component_in_path_exits_2(world: World) -> None:
    world.file(f"real/{U1}.jsonl")
    world.symlink("link", world.root + "/real")
    with pytest.raises(CliError) as exc:
        discovery(world, paths=[world.root + "/link"])
    assert exc.value.exit_code == 2
    with pytest.raises(CliError):
        discovery(world, paths=[world.root + f"/link/{U1}.jsonl"])


def test_a_symlink_to_outside_is_not_a_way_in(world: World, tmp_path: Path) -> None:
    world.symlink("escape", "/")
    with pytest.raises(CliError):
        discovery(world, paths=[world.root + "/escape/etc"])


def test_path_matches_the_real_path_of_a_symlinked_root(tmp_path: Path) -> None:
    w = RealWorld(tmp_path)
    w.file(f"a/{U1}.jsonl")
    link = str(tmp_path / "link")
    os.symlink(w.root, link)
    assert len(listed(w, roots=[link], paths=[os.path.realpath(w.root) + "/a"])) == 1
    assert len(listed(w, roots=[link], paths=[link + "/a"])) == 1


# --- agent meta files --------------------------------------------------------------------------


def meta_for(w: World, data: bytes | None) -> reader.AgentMeta | None:
    w.file(f"proj/{U1}/subagents/agent-a.jsonl")
    if data is not None:
        w.file(f"proj/{U1}/subagents/agent-a.meta.json", data)
    with discovery(w) as d:
        (c,) = d.candidates()
        return d.read_meta(c)


def test_meta_gives_agent_type_and_tool_use_id(world: World) -> None:
    got = meta_for(world, b'{"agentType": "Explore", "toolUseId": "toolu_01", "x": 1}')
    assert got == reader.AgentMeta("Explore", "toolu_01")


@pytest.mark.parametrize(
    "data",
    [None, b"", b"not json", b"[]", b"\xff\xfe", b"null", b"[" * 100000],
)
def test_missing_empty_or_bad_meta_is_none(world: World, data: bytes | None) -> None:
    assert meta_for(world, data) is None


def test_meta_fields_that_are_not_strings_are_none(world: World) -> None:
    assert meta_for(world, b'{"agentType": 5, "toolUseId": ["x"]}') == reader.AgentMeta(None, None)


def test_meta_strings_are_cut_and_cleaned(world: World) -> None:
    got = meta_for(world, b'{"agentType": "' + b"a" * 300 + b'", "toolUseId": "t\\ud800x"}')
    assert got is not None and got.agent_type == "a" * 128 and got.tool_use_id == "t�x"


def test_meta_over_64_kib_is_none(world: World) -> None:
    pad = b" " * (64 * 1024)
    assert meta_for(world, b'{"agentType": "x"}' + pad) is None
    assert meta_for(world, b'{"agentType": "x"}' + pad[: 64 * 1024 - 18]) is not None


def test_meta_that_is_a_symlink_or_fifo_is_none(world: World) -> None:
    world.file("elsewhere/m.json", b'{"agentType": "x"}')
    world.file(f"proj/{U1}/subagents/agent-a.jsonl")
    world.symlink(f"proj/{U1}/subagents/agent-a.meta.json", world.root + "/elsewhere/m.json")
    world.file(f"proj/{U1}/subagents/agent-b.jsonl")
    world.fifo(f"proj/{U1}/subagents/agent-b.meta.json")
    with discovery(world) as d:
        for c in d.candidates():
            assert d.read_meta(c) is None


def test_a_main_file_has_no_meta(world: World) -> None:
    world.file(f"proj/{U1}.jsonl")
    with discovery(world) as d:
        (c,) = d.candidates()
        assert d.read_meta(c) is None


# --- the descriptor primitives of the real gateway ---------------------------------------------


def test_entries_stop_at_the_limit_without_listing_the_rest(tmp_path: Path) -> None:
    for i in range(50):
        (tmp_path / f"f{i}").write_text("x")
    handle = SystemGateway().open_root(str(tmp_path))
    try:
        names, more = handle.entries(10)
        assert len(names) == 10 and more
        names, more = handle.entries(50)
        assert len(names) == 50 and not more
        assert handle.entries(0) == ([], True)
    finally:
        handle.close()


def test_open_dir_refuses_a_symlink_and_open_file_refuses_a_final_symlink(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "f").write_text("x")
    os.symlink(tmp_path / "d", tmp_path / "ld")
    os.symlink(tmp_path / "f", tmp_path / "lf")
    handle = SystemGateway().open_root(str(tmp_path))
    try:
        with pytest.raises(OSError) as exc:
            handle.open_dir(b"ld")
        assert exc.value.errno in (errno.ELOOP, errno.ENOTDIR)
        with pytest.raises(OSError) as exc2:
            handle.open_file(b"lf")
        assert exc2.value.errno == errno.ELOOP
        handle.open_dir(b"d").close()
    finally:
        handle.close()


def test_open_file_on_a_fifo_returns_at_once(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "p")
    gw = SystemGateway()
    handle = gw.open_root(str(tmp_path))
    try:
        file = handle.open_file(b"p")
        assert stat.S_ISFIFO(gw.fstat(file).st_mode)
        file.close()
    finally:
        handle.close()


def test_no_leaked_descriptors_after_a_scan(world: World) -> None:
    if not isinstance(world, RealWorld):
        pytest.skip("real descriptors only")
    for i in range(5):
        world.file(f"p{i}/{U1}/subagents/workflows/w/agent-a.jsonl")
    before = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None
    if before is None:
        pytest.skip("no /proc")
    with discovery(world) as d:
        for c in d.candidates():
            got = d.open(c)
            assert isinstance(got, Opened)
            got.close()
    assert len(os.listdir("/proc/self/fd")) == before
