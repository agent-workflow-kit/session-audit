"""The daily limit (issue #28): ``reserved(day)`` checked inside step 13's transaction, the local
day in ``time_zone``, the refund rules, what never counts, a hook that starts ``retro run`` again,
and real processes racing for the last place."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from session_audit import retro, retrostate
from session_audit.errors import CliError
from session_audit.lease import identity

from .fake_claude_session import FakeClaudeSession, error_doc, exec_doc, start_doc, step
from .fake_gateway import FakeClock
from .ingesthelp import S1, S2, Corpus, connect
from .retrohelp import RetroGateway, add_retro, args_for, live_lease, retro_ctx
from .retrohelp import retro as run_retro
from .signalhelp import (
    corpus_fixture,
    cs_fixture,
    gw_fixture,
    hook_config,
    payloads,
    recorder,
    retro_child,
    write_config,
)
from .test_retro_launch import CSID, error_of, keys, launch, prepared_corpus, rows, session_run


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch) -> RetroGateway:
    return gw_fixture(monkeypatch)


@pytest.fixture
def corpus(_private_environment: Path) -> Corpus:
    return corpus_fixture(_private_environment)


@pytest.fixture
def cs(gw: RetroGateway, tmp_path: Path) -> FakeClaudeSession:
    return cs_fixture(gw, tmp_path)


def limit_config(limit: int, extra: str = "") -> str:
    return f"limits:\n  retros_per_day: {limit}\n{extra}"


def today(gw: RetroGateway) -> str:
    return gw.now().strftime("%Y-%m-%d")


def add_start(home: Path, day: str, retro_id: str) -> None:
    conn = connect(home)
    try:
        conn.execute("INSERT INTO retro_starts (day, retro_id) VALUES (?, ?)", (day, retro_id))
        conn.commit()
    finally:
        conn.close()


def starts(home: Path) -> list[sqlite3.Row]:
    return rows(home, "retro_starts")


# --- reserved(day) ------------------------------------------------------------------------------


def test_a_spent_day_refuses_with_no_row_no_hook_and_no_claude_session_call(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    pre, post = tmp_path / "pre", tmp_path / "post"
    write_config(
        corpus.home,
        limit_config(1, hook_config(pre_retro=recorder(pre), post_retro=recorder(post))),
    )
    assert run_retro(capsys, gw, "--project", "/work", "--dry-run").code == 0  # creates the store
    add_start(corpus.home, today(gw), "ra-aaaaaaaa")
    envelope = error_of(launch(capsys, gw), 20, "daily_limit")
    assert envelope["code"] == "refused" and envelope["details"]["limit"] == 1
    assert rows(corpus.home) == []  # no retro row
    assert payloads(pre) == [] and payloads(post) == []  # no hook
    assert keys(cs) == []  # nothing was launched
    assert len(starts(corpus.home)) == 1


def test_the_plan_reports_what_is_left_of_the_day(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    write_config(corpus.home, limit_config(3))
    assert run_retro(capsys, gw, "--project", "/work", "--dry-run").doc["daily_remaining"] == 3
    add_start(corpus.home, today(gw), "ra-aaaaaaaa")
    add_retro(
        corpus.home,
        "b" * 64,
        retro_id="ra-bbbbbbbb",
        state="digesting",
        summary=None,
        lease=live_lease(gw),
    )
    assert run_retro(capsys, gw, "--project", "/work", "--dry-run").doc["daily_remaining"] == 1


def test_reserved_counts_todays_starts_and_live_digesting_rows_only(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    ctx = retro_ctx(gw, corpus)
    try:
        conn = ctx.store().conn
        day = today(gw)
        assert retro.reserved(ctx, conn, day) == 0
        add_start(corpus.home, day, "ra-aaaaaaaa")
        add_start(corpus.home, "2000-01-01", "ra-cccccccc")  # another day
        assert retro.reserved(ctx, conn, day) == 1
        add_retro(
            corpus.home,
            "b" * 64,
            retro_id="ra-bbbbbbbb",
            state="digesting",
            summary=None,
            lease=live_lease(gw),
        )
        assert retro.reserved(ctx, conn, day) == 2
        # a digesting row whose lease expired, or whose holder is not running, reserves nothing
        add_retro(
            corpus.home,
            "d" * 64,
            retro_id="ra-dddddddd",
            state="digesting",
            summary=None,
            lease=(identity(gw).pid, identity(gw).start, int(gw.now().timestamp() * 1000) - 1),
        )
        add_retro(
            corpus.home,
            "e" * 64,
            retro_id="ra-eeeeeeee",
            state="digesting",
            summary=None,
            lease=(2**22 + 12345, "never-ran", int(gw.now().timestamp() * 1000) + 60_000),
        )
        # a running or final row without a retro_starts row reserves nothing either
        add_retro(corpus.home, "f" * 64, retro_id="ra-ffffffff", state="running", summary=None)
        add_retro(corpus.home, "9" * 64, retro_id="ra-gggggggg", state="canceled", summary=None)
        assert retro.reserved(ctx, conn, day) == 2
    finally:
        ctx.close()


def test_a_live_digesting_run_holds_a_place_for_the_limit(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    write_config(corpus.home, limit_config(1))
    assert run_retro(capsys, gw, "--project", "/work", "--dry-run").code == 0
    add_retro(
        corpus.home,
        "b" * 64,
        retro_id="ra-bbbbbbbb",
        state="digesting",
        summary=None,
        lease=live_lease(gw),
    )
    error_of(launch(capsys, gw), 20, "daily_limit")
    assert [r["id"] for r in rows(corpus.home)] == ["ra-bbbbbbbb"]


def test_the_check_is_inside_the_step_13_transaction_before_the_insert(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    ctx = retro_ctx(gw, corpus, retro_settings=None)
    try:
        prepared = retro.prepare(ctx, args_for(project="/work"), env={})
        seen: list[bool] = []
        real = retrostate.check_daily_limit

        def spy(c: Any, conn: sqlite3.Connection) -> None:
            seen.append(conn.in_transaction)
            assert conn.execute("SELECT COUNT(*) FROM retros").fetchone()[0] == 0
            real(c, conn)

        retrostate.check_daily_limit = spy  # type: ignore[assignment]
        try:
            found = retrostate.insert_retro(ctx, ctx.store(), args_for(project="/work"), prepared)
        finally:
            retrostate.check_daily_limit = real  # type: ignore[assignment]
        assert seen == [True]
        assert isinstance(found, retrostate.Inserted)
        found.retro.release()
    finally:
        ctx.close()


# --- the local day ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("zone", "instant", "day"),
    [
        # a 45-minute offset: Kathmandu is UTC+5:45, so its midnight is 18:15 UTC
        ("Asia/Kathmandu", "2026-03-01T18:14:59+00:00", "2026-03-01"),
        ("Asia/Kathmandu", "2026-03-01T18:15:00+00:00", "2026-03-02"),
        ("Australia/Eucla", "2026-03-01T15:14:59+00:00", "2026-03-01"),  # UTC+8:45
        ("Australia/Eucla", "2026-03-01T15:15:00+00:00", "2026-03-02"),
        # DST spring forward (23 hours): 2026-03-08 in New York ends at 04:00 UTC
        ("America/New_York", "2026-03-08T04:59:59+00:00", "2026-03-07"),
        ("America/New_York", "2026-03-08T05:00:00+00:00", "2026-03-08"),
        ("America/New_York", "2026-03-09T03:59:59+00:00", "2026-03-08"),
        ("America/New_York", "2026-03-09T04:00:00+00:00", "2026-03-09"),
        # DST fall back (25 hours): 2026-11-01 ends at 05:00 UTC
        ("America/New_York", "2026-11-01T03:59:59+00:00", "2026-10-31"),
        ("America/New_York", "2026-11-01T04:00:00+00:00", "2026-11-01"),
        ("America/New_York", "2026-11-02T04:59:59+00:00", "2026-11-01"),
        ("America/New_York", "2026-11-02T05:00:00+00:00", "2026-11-02"),
        ("UTC", "2026-12-31T23:59:59+00:00", "2026-12-31"),
        ("UTC", "2027-01-01T00:00:00+00:00", "2027-01-01"),
    ],
)
def test_the_local_day_is_the_date_in_the_configured_zone(
    gw: RetroGateway, corpus: Corpus, zone: str, instant: str, day: str
) -> None:
    gw.clock = FakeClock(datetime.fromisoformat(instant))
    ctx = retro_ctx(gw, corpus)
    ctx.settings = ctx.settings.model_copy(update={"time_zone": zone})
    assert retro.local_day(ctx) == day
    assert retro.day_of(ctx, datetime.fromisoformat(instant).astimezone(UTC)) == day


def test_the_limit_resets_at_the_zones_midnight_not_utcs(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    ctx = retro_ctx(gw, corpus)
    ctx.settings = ctx.settings.model_copy(
        update={
            "time_zone": "Asia/Kathmandu",
            "limits": ctx.settings.limits.model_copy(update={"retros_per_day": 1}),
        }
    )
    try:
        conn = ctx.store().conn
        add_start(corpus.home, "2026-03-01", "ra-aaaaaaaa")
        gw.clock = FakeClock(datetime.fromisoformat("2026-03-01T18:14:59+00:00"))
        with pytest.raises(CliError) as spent:
            retrostate.check_daily_limit(ctx, conn)
        assert spent.value.details["reason"] == "daily_limit"
        gw.clock = FakeClock(datetime.fromisoformat("2026-03-01T18:15:00+00:00"))
        retrostate.check_daily_limit(ctx, conn)  # a new local day: nothing reserved
    finally:
        ctx.close()


def test_the_start_row_carries_the_local_day_of_its_zone(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    write_config(corpus.home, "time_zone: Asia/Kathmandu\n")
    gw.clock = FakeClock(
        datetime.fromisoformat("2026-03-01T20:00:00+00:00")
    )  # 01:45 local, next day
    error_of(launch(capsys, gw), 13)
    [start] = starts(corpus.home)
    assert start["day"] == "2026-03-02"


# --- the refund ---------------------------------------------------------------------------------


def test_a_failed_start_refunds_and_the_next_run_may_go(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    write_config(corpus.home, limit_config(1))
    session_run(cs, "blocked")
    cs.script("start", step(error_doc("internal", "boom"), exit=1))
    error_of(launch(capsys, gw), 12)
    assert starts(corpus.home) == []  # refunded: the model never ran
    cs.script("start", step(start_doc(CSID)))
    error_of(launch(capsys, gw, "--again"), 13)  # the freed place is usable
    assert len(starts(corpus.home)) == 1


def test_after_the_model_ran_the_start_stays_counted(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    write_config(corpus.home, limit_config(1))
    session_run(cs, "failed")
    error_of(launch(capsys, gw), 18)
    assert len(starts(corpus.home)) == 1
    error_of(launch(capsys, gw, "--again"), 20, "daily_limit")


def test_an_exec_outcome_that_does_not_parse_keeps_the_start_counted(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    cs.script("exec", step("not json", exit=12, unvalidated=True))
    error_of(launch(capsys, gw), 12, "claude_session_error")
    assert len(starts(corpus.home)) == 1


def test_a_launch_phase_exec_refunds(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    cs.script(
        "exec",
        step(
            exec_doc(CSID, "not-started", phase="launch", end_reason="sandbox_unavailable"), exit=12
        ),
    )
    error_of(launch(capsys, gw), 12, "session_not_started")
    assert starts(corpus.home) == []


def test_runner_lost_keeps_the_start(
    corpus: Corpus, gw: RetroGateway, cs: FakeClaudeSession, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_corpus(gw, corpus, capsys)
    write_config(corpus.home, limit_config(1))
    assert run_retro(capsys, gw, "--project", "/work", "--dry-run").code == 0
    add_retro(
        corpus.home,
        "b" * 64,
        retro_id="ra-bbbbbbbb",
        state="running",
        summary=None,
        lease=(2**22 + 12345, "never-ran", int(gw.now().timestamp() * 1000) + 60_000),
    )
    add_start(corpus.home, today(gw), "ra-bbbbbbbb")
    session_run(cs)
    # the housekeeping of this run recovers the lost retro (failed/runner_lost) and keeps its start
    error_of(launch(capsys, gw), 20, "daily_limit")
    lost = [r for r in rows(corpus.home) if r["id"] == "ra-bbbbbbbb"]
    assert (lost[0]["state"], lost[0]["reason"]) == ("failed", "runner_lost")
    assert [s["retro_id"] for s in starts(corpus.home)] == ["ra-bbbbbbbb"]


# --- what never counts --------------------------------------------------------------------------


def test_vetoed_escalated_reused_and_dry_runs_never_count(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    assert run_retro(capsys, gw, "--project", "/work", "--dry-run").code == 0
    assert starts(corpus.home) == []
    write_config(corpus.home, limit_config(1, hook_config(pre_retro="exit 1")))
    error_of(launch(capsys, gw), 11, "hook_vetoed")
    write_config(corpus.home, limit_config(1, hook_config(pre_retro="exit 13")))
    error_of(launch(capsys, gw), 13, "hook_escalated")
    assert starts(corpus.home) == []
    # a refused run (limit) and a stored done retro that is reused never count
    write_config(corpus.home, limit_config(1))
    add_retro(corpus.home, retro_key_of(corpus, gw, capsys), retro_id="ra-aaaaaaab")
    reused = launch(capsys, gw)
    assert reused.code == 0 and json.loads(reused.out)["reused"] is True
    assert starts(corpus.home) == []
    # none of them took a place of the day: a real run may still go
    session_run(cs, "blocked")
    error_of(launch(capsys, gw, "--again"), 13)
    assert len(starts(corpus.home)) == 1


def retro_key_of(corpus: Corpus, gw: RetroGateway, capsys: pytest.CaptureFixture[str]) -> str:
    return str(run_retro(capsys, gw, "--project", "/work", "--dry-run").doc["key"])


# --- a hook that starts retro run again, and real processes --------------------------------------


def child_env(cs: FakeClaudeSession, tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    from .conftest import tripwire_dir

    return {
        **cs.env(),
        "PATH": f"{tripwire_dir(tmp_path_factory)}{os.pathsep}{os.environ['PATH']}",
    }


def test_a_pre_retro_hook_that_runs_retro_run_again_is_bounded_by_the_limit(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    import sys

    prepared_corpus(gw, corpus, capsys)
    session_run(cs, "blocked")
    inner_out = tmp_path / "inner.json"
    inner = f"{sys.executable} -m session_audit retro run --json --session {S2} > {inner_out}"
    pre = tmp_path / "pre"
    write_config(
        corpus.home,
        limit_config(
            1,
            f"harness:\n  claude_session_cmd: {cs.program}\n"
            + hook_config(pre_retro=recorder(pre, f"{inner}; true")),
        ),
    )
    gw.fake = None  # the nested run is a real process: it uses the config's command
    outer = retro_child(cs, corpus.home, tmp_path_factory, "--session", S1)
    try:
        out, err = outer.communicate(timeout=120)
    finally:
        if outer.poll() is None:
            os.killpg(outer.pid, 9)
            outer.wait()
    assert outer.returncode == 13, (out, err)  # the outer run went on (blocked)
    inner_result = json.loads(inner_out.read_text())
    assert inner_result["error"]["details"]["reason"] == "daily_limit"
    assert len(payloads(pre)) == 1  # the inner run never reached its own hook
    assert len(starts(corpus.home)) == 1
    assert len(rows(corpus.home)) == 1


GATE_SCRIPT = """\
import os
import sys
import time

from session_audit import cli, retrolaunch

ready, total, name = sys.argv[1], int(sys.argv[2]), sys.argv[3]
real = retrolaunch.insert_retro


def gated(*args, **kwargs):
    open(os.path.join(ready, name), "w").close()
    end = time.monotonic() + 60
    while len(os.listdir(ready)) < total and time.monotonic() < end:
        time.sleep(0.01)
    return real(*args, **kwargs)


retrolaunch.insert_retro = gated
raise SystemExit(cli.main(sys.argv[4:]))
"""


@pytest.mark.parametrize("limit", [1, 2])
def test_processes_racing_at_step_13_never_start_more_than_the_limit(
    corpus: Corpus,
    gw: RetroGateway,
    cs: FakeClaudeSession,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    limit: int,
) -> None:
    """Three real processes (three different keys) reach step 13 together (a barrier file); each
    winner's pre-retro hook holds its place for a while and vetoes, so a place is held by a live
    ``digesting`` row while the others try."""
    seed_third(corpus)
    prepared_corpus(gw, corpus, capsys)
    session_run(cs)
    hold = "python3 -c 'import time; time.sleep(4)'; exit 1"
    write_config(corpus.home, limit_config(limit, hook_config(pre_retro=hold)))
    script = tmp_path / "gate.py"
    script.write_text(GATE_SCRIPT, encoding="utf-8")
    ready = tmp_path / "ready"
    ready.mkdir()
    sessions = [S1, S2, THIRD]
    children = [
        retro_child(
            cs,
            corpus.home,
            tmp_path_factory,
            str(ready),
            str(len(sessions)),
            f"p{i}",
            "retro",
            "run",
            "--json",
            "--session",
            session,
            script=script,
        )
        for i, session in enumerate(sessions)
    ]
    results: list[int] = []
    try:
        for child in children:
            child.communicate(timeout=180)
            results.append(child.returncode)
    finally:
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, 9)
                child.wait()
    assert sorted(results) == sorted([11] * limit + [20] * (3 - limit)), results
    assert len(rows(corpus.home)) == limit  # a refused run stores no row
    assert starts(corpus.home) == []  # a vetoed run never started


THIRD = "33333333-3333-4333-8333-333333333333"


def seed_third(corpus: Corpus) -> None:
    from .retrohelp import seed

    seed(corpus, THIRD, "third", project="p3")
