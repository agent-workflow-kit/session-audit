"""Incremental reading (issue #11): lines with offsets, budgets, change detection and the
progress record, against real files in a temporary directory."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from session_audit import incremental as inc
from session_audit.incremental import (
    IncrementalRead,
    Progress,
    Reason,
    Stop,
)
from session_audit.reader import Candidate, Opened
from session_audit.store import open_store

from .fake_gateway import FakeGateway
from .storehelp import ClockedGateway

MIB = 1024 * 1024
CAP = 8 * MIB
BUDGET = timedelta(seconds=60)


class Ticking(FakeGateway):
    """A clock that moves ``step`` seconds at every reading, so a budget runs out after a known
    number of line boundaries."""

    def __init__(self, step: float = 0.0) -> None:
        super().__init__()
        self.step = step

    def monotonic(self) -> float:
        now = self.clock.monotonic()
        if self.step:
            self.clock.advance(self.step)
        return now


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def opened(path: Path) -> Opened:
    handle = open(path, "rb")  # noqa: SIM115 - closed by the test through Opened.close
    info = os.fstat(handle.fileno())
    cand = Candidate(
        root_index=0,
        rel=(b"p", path.name.encode()),
        path=bytes(path),
        kind="main",
        session_id="11111111-1111-4111-8111-111111111111",
        agent_id=None,
        workflow=None,
        project=b"p",
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        dev=info.st_dev,
        ino=info.st_ino,
    )
    return Opened(cand, handle, info)


def read(
    path: Path,
    previous: Progress | None = None,
    *,
    gateway: FakeGateway | None = None,
    budget: timedelta = BUDGET,
    deadline: float | None = None,
    rules: int = 1,
    force: bool = False,
    each: Callable[[IncrementalRead], None] | None = None,
) -> tuple[list[tuple[int, bytes]], IncrementalRead]:
    """One run over a file: the lines (offset, bytes) and the finished reader."""
    handle = opened(path)
    try:
        run = IncrementalRead(
            handle,
            previous,
            gateway=gateway or Ticking(),
            rules_version=rules,
            file_budget=budget,
            run_deadline=deadline,
            force=force,
        )
        lines: list[tuple[int, bytes]] = []
        for line in run:
            lines.append((line.offset, line.data))
            if each is not None:
                each(run)
        return lines, run
    finally:
        handle.close()


def write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def append(path: Path, data: bytes) -> None:
    with open(path, "ab") as handle:
        handle.write(data)


@pytest.fixture
def f(tmp_path: Path) -> Path:
    return tmp_path / "t.jsonl"


# --- the plain read ---------------------------------------------------------------------------


def test_reads_every_line_with_its_start_offset(f: Path) -> None:
    write(f, b'{"a":1}\n{"b":2}\n\n{"c":3}\n')
    lines, run = read(f)
    assert lines == [(0, b'{"a":1}\n'), (8, b'{"b":2}\n'), (16, b"\n"), (17, b'{"c":3}\n')]
    assert run.reason is Reason.NEW
    assert run.stop is Stop.EOF
    assert run.counts.lines == 4
    assert run.counts.bytes_read == 25
    assert run.counts.oversized == 0


def test_checkpoint_records_offset_identity_and_hashes(f: Path) -> None:
    data = b'{"a":1}\n{"b":2}\n'
    write(f, data)
    _, run = read(f)
    info = f.stat()
    p = run.checkpoint()
    assert (p.dev, p.inode, p.size, p.offset) == (info.st_dev, info.st_ino, len(data), len(data))
    assert p.head_len == len(data)
    assert p.head_sha == sha(data)
    assert p.tail_sha == sha(data)
    assert p.rules_version == 1
    assert p.skip_to_lf is False


def test_empty_file_has_no_hashes(f: Path) -> None:
    write(f, b"")
    lines, run = read(f)
    p = run.checkpoint()
    assert lines == []
    assert (p.offset, p.head_len, p.head_sha, p.tail_sha) == (0, 0, None, None)


def test_file_without_a_line_feed_waits(f: Path) -> None:
    write(f, b'{"a":1}')
    lines, run = read(f)
    assert lines == []
    assert run.stop is Stop.EOF
    assert run.checkpoint().offset == 0
    append(f, b"\n")
    lines, run = read(f, run.checkpoint())
    assert lines == [(0, b'{"a":1}\n')]


def test_partial_tail_is_left_for_the_next_run(f: Path) -> None:
    write(f, b"one\ntw")
    lines, run = read(f)
    assert lines == [(0, b"one\n")]
    p = run.checkpoint()
    assert p.offset == 4
    append(f, b"o\nthree\n")
    lines, run2 = read(f, p)
    assert lines == [(4, b"two\n"), (8, b"three\n")]
    assert run2.reason is Reason.CONTINUE
    assert run2.counts.bytes_read == 10


def test_appended_bytes_only_are_read_on_an_unchanged_file(f: Path) -> None:
    write(f, b"a\nb\n")
    _, run = read(f)
    p = run.checkpoint()
    lines, again = read(f, p)
    assert lines == []
    assert again.reason is Reason.CONTINUE
    assert again.counts.bytes_read == 0
    assert again.checkpoint() == p
    append(f, b"c\n")  # a resumed session appends to the same file
    lines, again = read(f, p)
    assert lines == [(4, b"c\n")]
    assert again.checkpoint().offset == 6
    assert again.reason is Reason.CONTINUE


def test_binary_garbage_nul_bytes_and_cut_characters_never_stop_the_read(f: Path) -> None:
    emoji = "\U0001f600".encode()
    junk = bytes(range(256)).replace(b"\n", b"")
    write(f, b"\x00\x00\n" + junk + b"\n" + emoji[:2] + b"\n" + b'{"ok":1}\n')
    lines, run = read(f)
    assert [d for _, d in lines][-1] == b'{"ok":1}\n'
    assert len(lines) == 4
    assert lines[2][1] == emoji[:2] + b"\n"
    assert run.checkpoint().offset == f.stat().st_size


def test_decode_line_never_raises_and_ignores_the_locale() -> None:
    cut = "\U0001f600".encode()[:2]
    assert inc.decode_line(b'{"a":"' + cut + b'"}\n') == '{"a":"�"}\n'
    assert inc.decode_line(b"\xff\xfe\n") == "��\n"
    assert inc.decode_line("é\n".encode()) == "é\n"


def test_deleted_while_reading_is_still_read_to_the_end(f: Path) -> None:
    write(f, b"a\nb\nc\n")
    handle = opened(f)
    f.unlink()
    run = IncrementalRead(
        handle, None, gateway=Ticking(), rules_version=1, file_budget=BUDGET, run_deadline=None
    )
    assert [line.data for line in run] == [b"a\n", b"b\n", b"c\n"]
    handle.close()


def test_appended_while_reading_resumes_without_loss(f: Path) -> None:
    write(f, b"a\nb\n")
    seen: list[bytes] = []
    handle = opened(f)
    run = IncrementalRead(
        handle, None, gateway=Ticking(), rules_version=1, file_budget=BUDGET, run_deadline=None
    )
    for line in run:
        seen.append(line.data)
        if line.data == b"a\n":
            append(f, b"c\nd")
    handle.close()
    p = run.checkpoint()
    append(f, b"\n")
    lines, _ = read(f, p)
    seen.extend(d for _, d in lines)
    assert seen == [b"a\n", b"b\n", b"c\n", b"d\n"]


def test_a_reader_runs_once(f: Path) -> None:
    write(f, b"a\n")
    handle = opened(f)
    run = IncrementalRead(
        handle, None, gateway=Ticking(), rules_version=1, file_budget=BUDGET, run_deadline=None
    )
    assert len(list(run)) == 1
    assert list(run) == []
    handle.close()


# --- hashes -----------------------------------------------------------------------------------


def test_head_is_capped_and_tail_is_the_last_4_kib(f: Path) -> None:
    data = b"".join(b"%07d,%s\n" % (i, b"x" * 90) for i in range(200))  # 20,000 bytes
    write(f, data)
    _, run = read(f)
    p = run.checkpoint()
    assert p.head_len == 4096
    assert p.head_sha == sha(data[:4096])
    assert p.tail_sha == sha(data[len(data) - 4096 :])


def test_head_is_rewritten_while_it_is_short(f: Path) -> None:
    write(f, b"aaaa\n")
    _, run = read(f)
    p1 = run.checkpoint()
    assert p1.head_len == 5
    append(f, b"bbbb\n")
    _, run = read(f, p1)
    p2 = run.checkpoint()
    assert p2.head_len == 10
    assert p2.head_sha == sha(b"aaaa\nbbbb\n")
    assert run.reason is Reason.CONTINUE
    # head_len reaches the cap and stops growing
    append(f, b"c" * 5000 + b"\n")
    _, run = read(f, p2)
    p3 = run.checkpoint()
    assert p3.head_len == 4096
    assert p3.head_sha == sha((b"aaaa\nbbbb\n" + b"c" * 5000 + b"\n")[:4096])
    append(f, b"d\n")
    _, run = read(f, p3)
    assert run.checkpoint().head_sha == p3.head_sha


def test_growth_while_the_head_is_short_detects_a_changed_head(f: Path) -> None:
    write(f, b"aaaa\nbbbb\n")
    _, run = read(f)
    p = run.checkpoint()
    write(f, b"aaaa\nXXXX\n")  # rewritten in place, identical size, same inode
    _, run = read(f, p)
    assert run.reason is Reason.HEAD


def test_in_place_rewrite_of_the_tail_is_detected(f: Path) -> None:
    body = b"".join(b"%07d,%s\n" % (i, b"x" * 90) for i in range(200))
    write(f, body)
    _, run = read(f)
    p = run.checkpoint()
    changed = body[:-20] + b"Y" * 19 + b"\n"  # same size, same head
    write(f, changed)
    assert f.stat().st_ino == p.inode
    lines, run = read(f, p)
    assert run.reason is Reason.TAIL
    assert lines[0][0] == 0


def test_truncated_and_regrown_to_the_same_size_is_a_reread(f: Path) -> None:
    write(f, b"one\ntwo\nthree\n")
    _, run = read(f)
    p = run.checkpoint()
    with open(f, "r+b") as handle:
        handle.truncate(0)
    write(f, b"uno\ndos\ntres!\n"[: len(b"one\ntwo\nthree\n")])
    assert f.stat().st_size == p.size
    lines, run = read(f, p)
    assert run.reason in (Reason.HEAD, Reason.TAIL)
    assert lines and lines[0][0] == 0


def test_shrunk_file_is_a_reread_from_the_start(f: Path) -> None:
    write(f, b"one\ntwo\nthree\n")
    _, run = read(f)
    p = run.checkpoint()
    write(f, b"one\n")
    lines, run = read(f, p)
    assert run.reason is Reason.SHRUNK
    assert lines == [(0, b"one\n")]
    assert run.checkpoint().offset == 4


def test_replaced_file_with_the_same_content_is_a_reread(f: Path, tmp_path: Path) -> None:
    write(f, b"one\ntwo\n")
    _, run = read(f)
    p = run.checkpoint()
    other = write(tmp_path / "other", b"one\ntwo\n")
    os.replace(other, f)
    assert f.stat().st_ino != p.inode
    lines, run = read(f, p)
    assert run.reason is Reason.IDENTITY
    assert [o for o, _ in lines] == [0, 4]


def test_a_changed_device_is_a_reread(f: Path) -> None:
    write(f, b"one\n")
    _, run = read(f)
    p = run.checkpoint()
    moved = Progress(**{**p.__dict__, "dev": p.dev + 1})
    _, run = read(f, moved)
    assert run.reason is Reason.IDENTITY


def test_same_inode_and_content_rewritten_is_not_a_reread(f: Path) -> None:
    write(f, b"one\ntwo\n")
    _, run = read(f)
    p = run.checkpoint()
    write(f, b"one\ntwo\n")
    _, run = read(f, p)
    assert run.reason is Reason.CONTINUE


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: Progress(**{**p.__dict__, "head_len": 3}),
        lambda p: Progress(**{**p.__dict__, "head_sha": None}),
        lambda p: Progress(**{**p.__dict__, "tail_sha": None}),
        lambda p: Progress(**{**p.__dict__, "offset": p.offset + 1}),
        lambda p: Progress(**{**p.__dict__, "offset": -1}),
        lambda p: Progress(**{**p.__dict__, "head_len": 5000}),
    ],
)
def test_an_inconsistent_stored_row_is_reread_not_trusted(
    f: Path, mutate: Callable[[Progress], Progress]
) -> None:
    write(f, b"one\ntwo\n")
    _, run = read(f)
    _, again = read(f, mutate(run.checkpoint()))
    assert again.reason.reread
    assert again.counts.lines == 2


def test_force_and_older_rules_reread_a_file_that_is_unchanged(f: Path) -> None:
    write(f, b"one\ntwo\n")
    _, run = read(f, rules=1)
    p = run.checkpoint()
    lines, again = read(f, p, force=True)
    assert again.reason is Reason.FORCED
    assert len(lines) == 2
    lines, again = read(f, p, rules=2)
    assert again.reason is Reason.RULES
    assert len(lines) == 2
    assert again.checkpoint().rules_version == 2
    _, same = read(f, again.checkpoint(), rules=2)
    assert same.reason is Reason.CONTINUE


def test_reread_clears_a_pending_skip(f: Path) -> None:
    write(f, b"one\ntwo\n")
    _, run = read(f)
    p = Progress(**{**run.checkpoint().__dict__, "skip_to_lf": True})
    lines, again = read(f, p, force=True)
    assert [d for _, d in lines] == [b"one\n", b"two\n"]


# --- long lines -------------------------------------------------------------------------------


def long_line(content: int, fill: bytes = b"x") -> bytes:
    return fill * content + b"\n"


def test_a_line_of_exactly_8_mib_of_content_is_read(f: Path) -> None:
    write(f, long_line(CAP) + b"next\n")
    lines, run = read(f)
    assert [len(d) for _, d in lines] == [CAP + 1, 5]
    assert run.counts.oversized == 0


def test_a_line_one_byte_longer_is_skipped_and_counted(f: Path) -> None:
    write(f, b"before\n" + long_line(CAP + 1) + b"after\n")
    lines, run = read(f)
    assert [d for _, d in lines] == [b"before\n", b"after\n"]
    assert lines[1][0] == 7 + CAP + 2
    assert run.counts.oversized == 1
    assert run.checkpoint().offset == f.stat().st_size
    assert run.checkpoint().skip_to_lf is False


def test_a_huge_line_is_skipped_in_steps(f: Path) -> None:
    write(f, long_line(20 * MIB + 17) + b'{"a":1}\n' + long_line(CAP + 5) + b"z\n")
    lines, run = read(f)
    assert [d for _, d in lines] == [b'{"a":1}\n', b"z\n"]
    assert run.counts.oversized == 2


def test_the_skip_checks_the_budget_at_each_step_and_resumes(f: Path) -> None:
    write(f, long_line(20 * MIB) + b"after\n")
    # start (1), line check (2), then the step checks of the skip: budget of 4 ticks stops it
    lines, run = read(f, gateway=Ticking(1.0), budget=timedelta(seconds=4))
    p = run.checkpoint()
    assert lines == []
    assert run.stop is Stop.FILE_BUDGET
    assert run.counts.oversized == 1
    assert p.skip_to_lf is True
    assert 0 < p.offset < f.stat().st_size
    lines, run2 = read(f, p)
    assert lines == [(20 * MIB + 1, b"after\n")]
    assert run2.counts.oversized == 0  # counted once, in the run that found it
    assert run2.checkpoint().skip_to_lf is False


def test_an_unterminated_tail_over_8_mib_sets_skip_to_lf(f: Path) -> None:
    write(f, b"ok\n" + b"x" * (CAP + 1))
    lines, run = read(f)
    p = run.checkpoint()
    assert lines == [(0, b"ok\n")]
    assert p.skip_to_lf is True
    assert p.offset == f.stat().st_size
    assert run.counts.oversized == 1
    append(f, b"yy\n" + b'{"n":1}\n')
    lines, run2 = read(f, p)
    assert lines == [(f.stat().st_size - 8, b'{"n":1}\n')]
    assert run2.counts.oversized == 0
    assert run2.checkpoint().skip_to_lf is False


def test_an_unterminated_tail_of_exactly_8_mib_waits(f: Path) -> None:
    write(f, b"x" * CAP)
    lines, run = read(f)
    assert lines == []
    assert run.checkpoint().offset == 0
    assert run.checkpoint().skip_to_lf is False
    append(f, b"\n")
    lines, _ = read(f, run.checkpoint())
    assert [len(d) for _, d in lines] == [CAP + 1]


def test_skip_to_lf_over_several_runs_with_no_lf_yet(f: Path) -> None:
    write(f, b"x" * (CAP + 10))
    _, run = read(f)
    p = run.checkpoint()
    append(f, b"z" * 100)
    lines, run = read(f, p)
    assert lines == []
    p = run.checkpoint()
    assert p.skip_to_lf is True and p.offset == f.stat().st_size
    append(f, b"\nlast\n")
    lines, run = read(f, p)
    assert [d for _, d in lines] == [b"last\n"]


# --- budgets ----------------------------------------------------------------------------------


def lines_of(count: int) -> bytes:
    return b"".join(b'{"i":%d}\n' % i for i in range(count))


def resume_all(path: Path, **kw: object) -> tuple[list[tuple[int, bytes]], int]:
    """Run until the file is done; (all lines, number of runs)."""
    got: list[tuple[int, bytes]] = []
    p: Progress | None = None
    for runs in range(1, 200):
        lines, run = read(path, p, **kw)  # type: ignore[arg-type]
        got.extend(lines)
        p = run.checkpoint()
        if run.stop is Stop.EOF:
            return got, runs
        assert lines, "a run that hits its budget must still make progress"
    raise AssertionError("never finished")


@pytest.mark.parametrize("step", [0.4, 1.0, 2.5])
def test_file_budget_stops_at_a_line_boundary_and_resume_loses_nothing(
    f: Path, step: float
) -> None:
    data = lines_of(40)
    write(f, data)
    got, runs = resume_all(f, gateway=Ticking(step), budget=timedelta(seconds=5))
    whole, _ = read(f)
    assert got == whole
    assert b"".join(d for _, d in got) == data
    assert runs > 1


def test_file_budget_reports_why_it_stopped(f: Path) -> None:
    write(f, lines_of(10))
    lines, run = read(f, gateway=Ticking(1.0), budget=timedelta(seconds=3))
    assert run.stop is Stop.FILE_BUDGET
    assert not run.complete
    assert run.checkpoint().offset == sum(len(d) for _, d in lines)


def test_run_budget_wins_over_file_budget(f: Path) -> None:
    write(f, lines_of(10))
    gw = Ticking(1.0)
    deadline = inc.run_deadline(gw, timedelta(seconds=3))
    lines, run = read(f, gateway=gw, deadline=deadline)
    assert run.stop is Stop.RUN_BUDGET
    assert 0 < len(lines) < 10


def test_a_run_deadline_already_passed_reads_nothing(f: Path) -> None:
    write(f, lines_of(3))
    gw = Ticking()
    lines, run = read(f, gateway=gw, deadline=gw.monotonic() - 1)
    assert lines == []
    assert run.stop is Stop.RUN_BUDGET
    assert run.checkpoint().offset == 0


def test_run_deadline_is_a_monotonic_time(f: Path) -> None:
    gw = Ticking()
    assert inc.run_deadline(gw, timedelta(seconds=300)) == gw.monotonic() + 300


def test_checkpoint_between_lines_is_what_was_consumed(f: Path) -> None:
    write(f, lines_of(5))
    seen: list[int] = []
    _, run = read(f, each=lambda r: seen.append(r.checkpoint().offset))
    assert seen == [8, 16, 24, 32, 40]


def test_chunked_commits_match_one_read(f: Path) -> None:
    data = lines_of(25) + b'{"tail":'
    write(f, data)
    seen: list[tuple[int, bytes]] = []
    p: Progress | None = None
    for _ in range(10):  # a checkpoint after every 4 lines, a reopen after each
        handle = opened(f)
        run = IncrementalRead(
            handle, p, gateway=Ticking(), rules_version=1, file_budget=BUDGET, run_deadline=None
        )
        for n, line in enumerate(run, 1):
            seen.append((line.offset, line.data))
            if n == 4:
                break
        p = run.checkpoint()
        handle.close()
    assert len(seen) == 25
    whole, _ = read(f)
    assert seen == whole
    assert p is not None and p.offset == len(lines_of(25))


# --- the store ---------------------------------------------------------------------------------


def test_progress_round_trips_through_the_files_table(f: Path, tmp_path: Path) -> None:
    write(f, lines_of(3))
    _, run = read(f)
    p = run.checkpoint()
    with open_store(ClockedGateway(), str(tmp_path / "state")) as store:
        assert inc.load_progress(store.conn, bytes(f)) is None
        with store.transaction() as conn:
            inc.save_progress(conn, bytes(f), p)
        assert inc.load_progress(store.conn, bytes(f)) == p
        more = Progress(**{**p.__dict__, "offset": 5, "skip_to_lf": True})
        with store.transaction() as conn:
            inc.save_progress(conn, bytes(f), more)
        assert inc.load_progress(store.conn, bytes(f)) == more
        (count,) = store.conn.execute("SELECT count(*) FROM files").fetchone()
        assert count == 1


def test_save_progress_leaves_the_per_file_facts_alone(f: Path, tmp_path: Path) -> None:
    write(f, lines_of(3))
    _, run = read(f)
    with open_store(ClockedGateway(), str(tmp_path / "state")) as store:
        with store.transaction() as conn:
            inc.save_progress(conn, bytes(f), run.checkpoint())
            conn.execute("UPDATE files SET parse_errors = 7, first_cwd = '/w'")
            inc.save_progress(conn, bytes(f), run.checkpoint())
        row = store.conn.execute("SELECT parse_errors, first_cwd FROM files").fetchone()
        assert tuple(row) == (7, "/w")


def test_stale_rules_files_come_first(tmp_path: Path) -> None:
    def cand(name: bytes) -> Candidate:
        return Candidate(0, (name,), b"/r/" + name, "main", "s", None, None, b"p", 0, 0, 0, 0)

    with open_store(ClockedGateway(), str(tmp_path / "state")) as store:
        with store.transaction() as conn:
            for name, version in ((b"a", 1), (b"b", 0), (b"c", 2), (b"d", 0)):
                conn.execute(
                    "INSERT INTO files (path, dev, inode, rules_version) VALUES (?, 1, 1, ?)",
                    (b"/r/" + name, version),
                )
        assert inc.current_rules_version(store.conn) == 1
        stale = inc.stale_rules_paths(store.conn, 2)
        assert stale == {b"/r/a", b"/r/b", b"/r/d"}
        listed = [cand(n) for n in (b"a", b"e", b"c", b"b", b"d")]
        ordered = inc.stale_first(listed, stale)
        assert [c.rel[0] for c in ordered] == [b"a", b"b", b"d", b"e", b"c"]  # stable within parts
        assert inc.stale_rules_paths(store.conn, 0) == set()


# --- live fact: a 140 MB file ------------------------------------------------------------------


@pytest.fixture
def scratch() -> Iterator[Path]:
    """A directory beside the repository (the system temp directory is on the small partition)."""
    base = Path(__file__).resolve().parents[1]
    try:
        path = Path(tempfile.mkdtemp(prefix=".bigfile-", dir=base))
    except OSError:
        pytest.skip("no writable scratch directory beside the repository")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_a_140_mb_file_reads_in_one_pass_within_the_stated_bound(scratch: Path) -> None:
    """Fact (design, Live verification): a 140 MB file parses in under a second with
    line-by-line reading. Here only the reading is timed, with a generous bound (60 s) so a slow
    shared machine never makes it flaky; it also proves the work is linear (one pass)."""
    path = scratch / "big.jsonl"
    line = b'{"type":"assistant","n":%07d,"pad":"' + b"p" * 960 + b'"}\n'
    count = 140 * MIB // len(line % 0) + 1
    with open(path, "wb") as out:
        block = b"".join(line % i for i in range(1000))
        for _ in range(count // 1000 + 1):
            out.write(block)
    size = path.stat().st_size
    assert size >= 140 * 1000 * 1000
    started = time.monotonic()
    handle = opened(path)
    run = IncrementalRead(
        handle,
        None,
        gateway=ClockedGateway(),
        rules_version=1,
        file_budget=timedelta(seconds=600),
        run_deadline=None,
    )
    total = sum(1 for _ in run)
    handle.close()
    elapsed = time.monotonic() - started
    assert total == run.counts.lines > 100_000
    assert run.counts.bytes_read == size
    assert run.checkpoint().offset == size
    assert elapsed < 60, f"read took {elapsed:.1f}s"
    path.unlink()
