"""``cron install`` and ``cron uninstall``: the managed block, against a fake crontab only."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from session_audit import store as store_module
from session_audit.cli import main
from session_audit.commands.cron import (
    END,
    START,
    build_block,
    check_line_value,
    install_text,
    quote,
    split_crontab,
    uninstall_text,
)
from session_audit.errors import CliError
from session_audit.gateway import CallResult, CallStatus
from session_audit.grammar import parse_ingest_every

from .golden import assert_golden
from .storehelp import ClockedGateway

INTERVALS = [f"{n}m" for n in (5, 6, 10, 12, 15, 20, 30)] + [
    f"{n}h" for n in (1, 2, 3, 4, 6, 8, 12, 24)
]


class CronGateway(ClockedGateway):
    """The real gateway for files and the store, a fake crontab (never the real program)."""

    def __init__(self) -> None:
        super().__init__()
        self.crontab: str | None = None  # None: the user has no crontab
        self.read_result: CallResult | None = None
        self.write_result: CallResult | None = None
        self.writes: list[str] = []
        self.after_write: Any = None

    def crontab_read(self) -> CallResult:
        if self.read_result is not None:
            return self.read_result
        if self.crontab is None:
            return CallResult(CallStatus.EXITED, 1, stderr="no crontab for someone\n")
        return CallResult(CallStatus.EXITED, 0, stdout=self.crontab)

    def crontab_write(self, text: str) -> CallResult:
        if self.write_result is not None:
            return self.write_result
        self.writes.append(text)
        self.crontab = text
        if self.after_write is not None:
            self.after_write()
        return CallResult(CallStatus.EXITED, 0)


@pytest.fixture
def gw() -> CronGateway:
    return CronGateway()


@pytest.fixture
def bins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A PATH holding a claude-session and a claude (plain files, never run)."""
    one, two = tmp_path / "bin1", tmp_path / "bin2"
    for directory, name in ((one, "claude-session"), (two, "claude")):
        directory.mkdir()
        (directory / name).write_text("#!/bin/sh\n", encoding="utf-8")
        (directory / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{one}:{two}:/usr/bin")
    return tmp_path


def cron(capsys: pytest.CaptureFixture[str], gw: CronGateway, *argv: str) -> tuple[int, str, str]:
    code = main(["cron", *argv], gateway=gw, stdin_is_tty=False)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def doc(out: str) -> dict[str, Any]:
    return json.loads(out)  # type: ignore[no-any-return]


def error(out: str) -> dict[str, Any]:
    return doc(out)["error"]  # type: ignore[no-any-return]


def write_config(home: Path, text: str) -> None:
    directory = home / ".config" / "session-audit"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = directory / "config.yaml"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def meta(home: Path) -> dict[str, Any]:
    conn = sqlite3.connect(home / ".local" / "state" / "session-audit" / "state.db")
    try:
        return dict(conn.execute("SELECT key, value FROM meta"))
    finally:
        conn.close()


# --- goldens ----------------------------------------------------------------------------------


@pytest.mark.parametrize("interval", INTERVALS)
def test_install_golden_for_every_interval(
    interval: str,
    gw: CronGateway,
    bins: Path,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    code, out, _ = cron(capsys, gw, "install", "--ingest-every", interval, "--json")
    assert code == 0
    document = doc(out)

    def normalize(value: Any) -> Any:
        text = (
            json.dumps(value)
            .replace(str(bins), "<tmp>")
            .replace(str(_private_environment), "<home>")
        )
        return json.loads(text)

    assert_golden(document, f"cron-install-{interval}", normalize=normalize)
    assert gw.crontab == "\n".join(document["block"]) + "\n"


def test_golden_with_a_scheduled_retro_and_an_env_file(
    gw: CronGateway,
    bins: Path,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    write_config(
        _private_environment,
        "schedule:\n  retro:\n    weekday: 5\n    hour: 17\n    scope:\n"
        "      since: 7d\n      until: 1d\n      labels: ['team=a.b']\n"
        "      project: /work/it's\n      kind: review\n      outcome: done\n",
    )
    env = bins / "login.env"
    env.write_text("X=1\n", encoding="utf-8")
    env.chmod(0o600)
    code, out, _ = cron(capsys, gw, "install", "--env-file", str(env), "--json")
    assert code == 0

    def normalize(value: Any) -> Any:
        text = (
            json.dumps(value)
            .replace(str(bins), "<tmp>")
            .replace(str(_private_environment), "<home>")
        )
        return json.loads(text)

    assert_golden(doc(out), "cron-install-retro", normalize=normalize)


@pytest.mark.parametrize(
    ("case", "scope"),
    [
        ("retro-minimal", "      since: 7d\n"),
        ("retro-two-labels", "      since: 30d\n      labels: ['a=b', 'c=d']\n"),
    ],
)
def test_golden_with_a_minimal_retro_and_with_two_labels(
    case: str,
    scope: str,
    gw: CronGateway,
    bins: Path,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    write_config(_private_environment, "schedule:\n  retro:\n    scope:\n" + scope)
    code, out, _ = cron(capsys, gw, "install", "--json")
    assert code == 0

    def normalize(value: Any) -> Any:
        text = (
            json.dumps(value)
            .replace(str(bins), "<tmp>")
            .replace(str(_private_environment), "<home>")
        )
        return json.loads(text)

    assert_golden(doc(out), f"cron-install-{case}", normalize=normalize)


def test_uninstall_golden(
    gw: CronGateway, bins: Path, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    cron(capsys, gw, "install", "--json")
    code, out, _ = cron(capsys, gw, "uninstall", "--json")
    assert code == 0

    def normalize(value: Any) -> Any:
        text = (
            json.dumps(value)
            .replace(str(bins), "<tmp>")
            .replace(str(_private_environment), "<home>")
        )
        return json.loads(text)

    assert_golden(doc(out), "cron-uninstall-removed", normalize=normalize)
    assert gw.crontab == ""


# --- block lines ------------------------------------------------------------------------------


def test_the_block_lines(
    gw: CronGateway, bins: Path, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    cron(capsys, gw, "install", "--ingest-every", "15")
    assert gw.crontab is not None
    lines = gw.crontab.splitlines()
    assert lines[0] == START and lines[-1] == END and len(lines) == 4  # no retro line
    assert lines[1].startswith("*/15 * * * * PATH='")
    assert lines[1].endswith("' session-audit ingest --quiet")
    assert lines[2].startswith("17 3 * * * PATH='") and lines[2].endswith("prune --quiet")
    path = lines[1].split("PATH='")[1].split("'")[0].split(":")
    assert path == [
        f"{_private_environment}/.local/bin",
        f"{bins}/bin1",
        f"{bins}/bin2",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]


def test_hourly_and_daily_schedules() -> None:
    def schedule(interval: str) -> str:
        block = build_block(parse_ingest_every(interval), None, "/bin", None)
        return " ".join(block[1].split()[:5])

    assert schedule("60m") == "7 */1 * * *"
    assert schedule("1h") == "7 */1 * * *"
    assert schedule("6h") == "7 */6 * * *"
    assert schedule("24h") == "7 3 * * *"
    assert schedule("5") == "*/5 * * * *"


def test_a_real_install_records_the_interval_and_a_dry_run_does_not(
    gw: CronGateway, bins: Path, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    code, out, _ = cron(capsys, gw, "install", "--dry-run", "--ingest-every", "30m", "--json")
    assert code == 0 and doc(out)["outcome"] == "installed" and doc(out)["dry_run"] is True
    assert gw.crontab is None and gw.writes == []
    assert not (_private_environment / ".local" / "state" / "session-audit").exists()
    cron(capsys, gw, "install", "--ingest-every", "30m")
    assert meta(_private_environment)["ingest_interval"] == "30m"
    cron(capsys, gw, "install", "--ingest-every", "2h")
    assert meta(_private_environment)["ingest_interval"] == "2h"


def test_install_twice_is_unchanged_and_a_new_interval_replaces(
    gw: CronGateway, bins: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert doc(cron(capsys, gw, "install", "--json")[1])["outcome"] == "installed"
    assert doc(cron(capsys, gw, "install", "--json")[1])["outcome"] == "unchanged"
    assert len(gw.writes) == 1
    assert doc(cron(capsys, gw, "install", "--json", "--ingest-every", "5m")[1])["outcome"] == (
        "replaced"
    )
    dry = doc(cron(capsys, gw, "install", "--json", "--dry-run", "--ingest-every", "1h")[1])
    assert dry["outcome"] == "replaced" and len(gw.writes) == 2


def test_the_retro_line(
    gw: CronGateway, bins: Path, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    write_config(
        _private_environment,
        "schedule:\n  retro:\n    scope:\n      since: 7d\n      labels: [a=b]\n",
    )
    cron(capsys, gw, "install")
    assert gw.crontab is not None
    retro = gw.crontab.splitlines()[3]
    assert retro.startswith("23 9 * * 1 PATH='")
    assert retro.endswith(
        "' session-audit retro run --label 'a=b' --since '7d' --actor auto:schedule --quiet"
    )


def test_a_scheduled_retro_hostile_scope_values_are_refused_naming_the_scope(
    gw: CronGateway, bins: Path, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    for bad in ("a%b", "a\\nb", "a\\u0001b"):
        write_config(
            _private_environment,
            f'schedule:\n  retro:\n    scope:\n      since: 7d\n      project: "/x/{bad}"\n',
        )
        code, out, _ = cron(capsys, gw, "install", "--json")
        assert code == 2 and error(out)["details"]["field"] == "schedule.retro.scope", bad
    assert gw.crontab is None


def test_quoting_defeats_injection_shapes() -> None:
    for value in ["a'b", "$(id)", "`id`", "a b;c", "$HOME", '"q"', "\\", "*", "a\tb"[:1]]:
        quoted = quote(value)
        assert quoted.startswith("'") and quoted.endswith("'")
        assert "'" not in quoted[1:-1].replace("'\"'\"'", "")
    assert quote("a'b") == "'a'\"'\"'b'"


@pytest.mark.parametrize("bad", ["a%b", "a\nb", "a\rb", "a\x00b", "a\x7fb", "a\ud800b", "%"])
def test_values_that_cannot_be_written_are_refused(bad: str) -> None:
    with pytest.raises(CliError) as raised:
        check_line_value(bad, "KEY")
    assert raised.value.exit_code == 2 and raised.value.details["field"] == "KEY"
    assert check_line_value("fine $`'\"", "KEY")


def test_a_long_retro_line_is_refused(
    gw: CronGateway, bins: Path, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    write_config(
        _private_environment,
        "schedule:\n  retro:\n    scope:\n      since: 7d\n      project: /" + "p" * 1100 + "\n",
    )
    code, out, _ = cron(capsys, gw, "install", "--json")
    assert code == 2 and error(out)["details"]["field"] == "schedule.retro.scope"


def test_a_percent_in_a_path_directory_is_refused_naming_path(
    gw: CronGateway,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bad = tmp_path / "100%"
    bad.mkdir()
    (bad / "claude").write_text("#!/bin/sh\n", encoding="utf-8")
    (bad / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bad}:/usr/bin")
    code, out, _ = cron(capsys, gw, "install", "--json")
    assert code == 2 and error(out)["details"]["field"] == "PATH"
    assert gw.crontab is None


# --- the env file -----------------------------------------------------------------------------


def env_file(tmp_path: Path, mode: int = 0o600) -> Path:
    path = tmp_path / "e.env"
    path.write_text("A=1\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_the_env_file_form(
    gw: CronGateway, bins: Path, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    write_config(_private_environment, "schedule:\n  retro:\n    scope:\n      since: 1d\n")
    path = bins / "it's.env"
    path.write_text("A=1\n", encoding="utf-8")
    path.chmod(0o600)
    cron(capsys, gw, "install", "--env-file", str(path))
    assert gw.crontab is not None
    retro = gw.crontab.splitlines()[3]
    assert f". '{bins}/it'\"'\"'s.env' && PATH='" in retro
    assert " && " not in gw.crontab.splitlines()[1]


@pytest.mark.parametrize(
    "problem", ["relative", "missing", "dir", "symlink", "group", "other", "percent", "newline"]
)
def test_a_bad_env_file_is_exit_2_naming_it(
    problem: str, gw: CronGateway, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = env_file(tmp_path)
    path = str(good)
    if problem == "relative":
        path = "e.env"
    elif problem == "missing":
        path = str(tmp_path / "nope")
    elif problem == "dir":
        path = str(tmp_path)
    elif problem == "symlink":
        os.symlink(good, tmp_path / "link")
        path = str(tmp_path / "link")
    elif problem == "group":
        good.chmod(0o620)
    elif problem == "other":
        good.chmod(0o602)
    elif problem == "percent":
        path = str(tmp_path / "a%b")
    elif problem == "newline":
        path = str(tmp_path / "a\nb")
    code, out, _ = cron(capsys, gw, "install", "--env-file", path, "--json")
    assert code == 2 and error(out)["details"]["field"] == "--env-file"
    assert gw.crontab is None


def test_an_env_file_owned_by_someone_else_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = CronGateway()
    other._uid = os.geteuid() + 1
    code, out, _ = cron(capsys, other, "install", "--env-file", str(env_file(tmp_path)), "--json")
    assert code == 2 and error(out)["details"]["field"] == "--env-file"


def test_the_line_limit_counts_bytes(
    gw: CronGateway, bins: Path, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    write_config(
        _private_environment,
        "schedule:\n  retro:\n    scope:\n      since: 7d\n      project: /" + "é" * 450 + "\n",
    )
    code, out, _ = cron(capsys, gw, "install", "--json")
    assert code == 2 and error(out)["details"]["field"] == "schedule.retro.scope"
    assert gw.crontab is None


def test_a_long_env_file_path_is_blamed_not_the_scope(
    gw: CronGateway, bins: Path, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    write_config(_private_environment, "schedule:\n  retro:\n    scope:\n      since: 7d\n")
    deep = bins.joinpath(*("d" * 200 for _ in range(4)))
    deep.mkdir(parents=True)
    for directory in (deep, *deep.parents):
        if directory == bins:
            break
        directory.chmod(0o700)
    env = env_file(deep)
    code, out, _ = cron(capsys, gw, "install", "--env-file", str(env), "--json")
    assert code == 2 and error(out)["details"]["field"] == "--env-file"
    assert gw.crontab is None
    _, _, err = cron(capsys, gw, "install", "--env-file", str(env))
    for name in ("schedule.retro.scope", "--env-file", "PATH"):
        assert name in err


def test_an_env_file_in_a_world_writable_directory_is_refused_unless_sticky(
    tmp_path: Path, gw: CronGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    env = env_file(shared)
    shared.chmod(0o777)
    code, out, _ = cron(capsys, gw, "install", "--env-file", str(env), "--json")
    assert code == 2 and error(out)["details"]["field"] == "--env-file"
    shared.chmod(0o770)
    assert cron(capsys, gw, "install", "--env-file", str(env), "--json")[0] == 2
    shared.chmod(0o1777)
    assert cron(capsys, gw, "install", "--env-file", str(env), "--json")[0] == 0


# --- values: more characters, leading dashes --------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["a\x85b", "a\x80b", "a\x9fb", "a\u2028b", "a\u2029b", "a\u202eb", "a\u200fb", "a\u2067b"],
)
def test_c1_line_separators_and_bidi_controls_are_refused(bad: str) -> None:
    with pytest.raises(CliError) as raised:
        check_line_value(bad, "KEY")
    assert raised.value.exit_code == 2 and raised.value.details["field"] == "KEY"


@pytest.mark.parametrize("flag", ["project", "kind", "outcome"])
def test_a_scope_value_starting_with_a_dash_is_refused(
    flag: str,
    gw: CronGateway,
    bins: Path,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    write_config(
        _private_environment,
        f"schedule:\n  retro:\n    scope:\n      since: 7d\n      {flag}: '-x'\n",
    )
    code, out, _ = cron(capsys, gw, "install", "--json")
    assert code == 2 and error(out)["details"]["field"] == "schedule.retro.scope"
    assert gw.crontab is None


def test_a_label_starting_with_a_dash_is_refused(
    gw: CronGateway, bins: Path, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    write_config(
        _private_environment,
        "schedule:\n  retro:\n    scope:\n      since: 7d\n      labels: ['-a=b']\n",
    )
    code, out, _ = cron(capsys, gw, "install", "--json")
    assert code == 2 and error(out)["details"]["field"] == "schedule.retro.scope"


# --- the block rules and hostile crontabs -----------------------------------------------------

MALFORMED = [
    f"{START}\n",
    f"{END}\n",
    f"{END}\n{START}\n",
    f"{START}\n{START}\n{END}\n",
    f"{START}\n{END}\n{END}\n",
    f"{START}\nx\n{END}\n{START}\nx\n{END}\n",
    f"{START}",
]


@pytest.mark.parametrize("text", MALFORMED)
@pytest.mark.parametrize("verb", [["install"], ["uninstall"], ["install", "--dry-run"]])
def test_a_malformed_block_exits_20_and_changes_nothing(
    text: str, verb: list[str], gw: CronGateway, bins: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gw.crontab = text
    code, out, _ = cron(capsys, gw, *verb, "--json")
    assert code == 20
    assert (
        error(out)["code"] == "refused"
        and error(out)["details"]["reason"] == "cron_block_malformed"
    )
    assert gw.writes == [] and gw.crontab == text


HOSTILE_OUTSIDE = [
    "MAILTO=x\n0 1 * * * a",  # no trailing newline
    "0 1 * * * a\r\n# note\r\n",  # CRLF
    f"# {START}\n# {END}\n",  # comments that look like markers
    f"0 1 * * * echo '{START}'\n{START} \n {END}\n",  # markers inside or padded
    f"{START}\r\n{END}\r\n",  # CR-terminated markers are ordinary text
    "0 1 * * * echo \udcff\udcfe\n",  # non-UTF-8 bytes
    "#   \x0b \x85 \x0c odd separators\n",  # str.splitlines would cut here
    "".join(f"{i} * * * * job{i}\n" for i in range(50_000)),  # huge
    "\n\n\n",
]


@pytest.mark.parametrize("outside", HOSTILE_OUTSIDE)
def test_lines_outside_the_block_are_never_changed(
    outside: str, gw: CronGateway, bins: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gw.crontab = outside
    assert cron(capsys, gw, "install")[0] == 0
    installed = gw.crontab
    assert installed is not None
    prefix = outside if outside.endswith("\n") else outside + "\n"
    assert installed.startswith(prefix) and installed.count(START + "\n") >= 1
    # Replace the block in the middle of the hostile content, then remove it.
    gw.crontab = outside_wrap(outside, installed[len(prefix) :])
    assert cron(capsys, gw, "install", "--ingest-every", "5m")[0] == 0
    assert gw.crontab.startswith(outside) and gw.crontab.endswith("\n")
    assert cron(capsys, gw, "uninstall")[0] == 0
    assert gw.crontab == (outside if outside.endswith("\n") else outside + "\n")


@pytest.mark.parametrize("outside", HOSTILE_OUTSIDE)
def test_uninstall_keeps_the_outside_text_when_the_end_marker_has_no_newline(outside: str) -> None:
    prefix = outside if outside.endswith("\n") else outside + "\n"
    new, outcome, removed = uninstall_text(f"{prefix}{START}\nx\n{END}")
    assert (new, outcome, removed) == (prefix, "removed", [START, "x", END])


def test_uninstall_repro_of_a_missing_final_newline() -> None:
    assert uninstall_text(f"a\n{START}\n{END}")[0] == "a\n"
    assert uninstall_text(f"{START}\n{END}")[0] == ""


def outside_wrap(outside: str, block: str) -> str:
    return outside + block if outside.endswith("\n") else outside + "\n" + block


def test_text_after_the_block_is_kept_and_the_block_is_replaced_in_place() -> None:
    text = f"a\n{START}\nold\n{END}\nb"
    new, outcome = install_text(text, [START, "new", END])
    assert (new, outcome) == (f"a\n{START}\nnew\n{END}\nb\n", "replaced")
    assert uninstall_text(text) == ("a\nb", "removed", [START, "old", END])
    assert split_crontab("").block is None


def test_uninstall_of_a_crontab_with_only_the_block_empties_it(
    gw: CronGateway, bins: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cron(capsys, gw, "install")
    code, out, _ = cron(capsys, gw, "uninstall", "--json")
    assert code == 0 and doc(out)["outcome"] == "removed" and len(doc(out)["block"]) == 4
    assert gw.crontab == ""


def test_uninstall_without_a_block_or_a_crontab_is_unchanged(
    gw: CronGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = cron(capsys, gw, "uninstall", "--json")
    assert code == 0 and doc(out) == {
        "command": "uninstall",
        "outcome": "unchanged",
        "dry_run": False,
        "block": [],
    }
    gw.crontab = "0 1 * * * x\n"
    assert doc(cron(capsys, gw, "uninstall", "--json")[1])["outcome"] == "unchanged"
    assert gw.writes == []


def test_uninstall_dry_run_lists_the_lines_and_writes_nothing(
    gw: CronGateway, bins: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cron(capsys, gw, "install")
    before = gw.crontab
    code, out, _ = cron(capsys, gw, "uninstall", "--dry-run", "--json")
    assert code == 0 and doc(out)["dry_run"] is True and doc(out)["outcome"] == "removed"
    assert doc(out)["block"][0] == START and gw.crontab == before


def test_an_oversized_hand_edited_block_is_cut_to_fit_the_schema(
    gw: CronGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    gw.crontab = "\n".join([START, *["x" * 5000] * 100, END]) + "\n"
    code, out, _ = cron(capsys, gw, "uninstall", "--dry-run", "--json")
    block = doc(out)["block"]
    assert code == 0 and len(block) == 64 and all(len(line) <= 4096 for line in block)


def test_human_output(gw: CronGateway, bins: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = cron(capsys, gw, "install", "--dry-run")
    assert code == 0 and out.splitlines()[0] == "cron install: installed (dry run)"
    assert out.splitlines()[1].strip() == START
    assert cron(capsys, gw, "install", "--quiet")[1] == ""


# --- the crontab failing ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "result",
    [
        CallResult(CallStatus.SPAWN_FAILED, detail="missing"),
        CallResult(CallStatus.TIMEOUT),
        CallResult(CallStatus.SIGNALED, signal=9),
        CallResult(CallStatus.OUTPUT_LIMIT, stdout="x"),
        CallResult(CallStatus.EXITED, 2, stderr="boom"),
        CallResult(CallStatus.EXITED, 1, stderr="you are not allowed to use this program"),
        CallResult(CallStatus.EXITED, 1, stdout="0 * * * * x\n", stderr=""),
    ],
)
@pytest.mark.parametrize("verb", ["install", "uninstall"])
def test_a_crontab_that_cannot_be_read_is_exit_12(
    result: CallResult,
    verb: str,
    gw: CronGateway,
    bins: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gw.read_result = result
    code, out, _ = cron(capsys, gw, verb, "--json")
    assert code == 12 and error(out)["details"]["reason"] == "crontab_unavailable"
    assert error(out)["details"]["retryable"] is (
        result.status in (CallStatus.TIMEOUT, CallStatus.SIGNALED)
    )
    assert gw.writes == []


def test_a_failed_write_is_exit_12_and_records_nothing(
    gw: CronGateway, bins: Path, capsys: pytest.CaptureFixture[str], _private_environment: Path
) -> None:
    gw.write_result = CallResult(CallStatus.EXITED, 1, stderr="bad")
    code, out, _ = cron(capsys, gw, "install", "--json")
    assert code == 12 and error(out)["details"]["reason"] == "crontab_unavailable"
    assert "ingest_interval" not in meta(_private_environment)


def test_a_failed_meta_write_after_the_crontab_write_is_exit_12_with_a_hint(
    gw: CronGateway,
    bins: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def break_store() -> None:
        def broken(self: Any) -> Any:
            raise store_module.unwritable("the disk is full")

        monkeypatch.setattr(store_module.Store, "transaction", broken)

    gw.after_write = break_store
    code, out, err = cron(capsys, gw, "install", "--json")
    assert code == 12 and error(out)["details"]["reason"] == "store_unwritable"
    assert gw.crontab is not None and gw.crontab.startswith(START)
    assert "already updated" in err and "again" in err


def test_a_missing_session_audit_on_the_path_is_a_stderr_note(
    gw: CronGateway,
    bins: Path,
    capsys: pytest.CaptureFixture[str],
    _private_environment: Path,
) -> None:
    code, out, err = cron(capsys, gw, "install", "--json")
    assert code == 0 and "session-audit" in err and "PATH" in err
    assert doc(out)["outcome"] == "installed"
    exe = bins / "bin1" / "session-audit"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    code, out, err = cron(capsys, gw, "install", "--json")
    assert code == 0 and err == ""
    code, _, err = cron(capsys, gw, "install", "--dry-run", "--quiet")
    assert err == ""


def test_an_empty_crontab_with_exit_0_is_a_crontab_without_a_block(
    gw: CronGateway, bins: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gw.read_result = CallResult(CallStatus.EXITED, 0, stdout="")
    assert doc(cron(capsys, gw, "install", "--json")[1])["outcome"] == "installed"


def test_usage_errors_name_the_flag(gw: CronGateway, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = cron(capsys, gw, "install", "--ingest-every", "60", "--json")
    assert code == 2 and error(out)["details"]["field"] == "--ingest-every"
    assert gw.crontab is None


def test_the_real_crontab_is_never_started_by_these_tests() -> None:
    # CronGateway replaces both crontab methods; the process guard covers the rest.
    assert CronGateway.crontab_read is not ClockedGateway.crontab_read
    assert CronGateway.crontab_write is not ClockedGateway.crontab_write
