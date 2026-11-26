"""``validate``: check the installation (interface, Commands, ``validate``).

Offline and read-only: the store is opened through ``inspect_store`` and the read-only reader
(nothing is created, migrated or recovered), the crontab is only read, no prompt is made and no
hook is ever run. The document is printed on exits 0, 2 and 12 (``run`` returns those codes
itself), also with a config that does not parse: ``store`` and ``claude_session`` then still run
on the built-in defaults.

Nothing the checks read is printed: a check has a name, a flag and a closed reason, a warning is
one closed token.

Exec-bearing config (``harness.claude_session_cmd``, ``hooks``, ``retro.plugin_dirs``,
``retro.permission_profile``) is applied by the loader even when the config is unsafe
(``ctx.config_issues`` not empty). ``validate`` honors it only while the config is safe: under an
unsafe config the ``claude_session`` check runs the default command and ``--probe`` runs nothing.
Hooks are never run (a parsed config already holds only well-formed entries).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import stat as statmod
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .. import plugins
from ..command import Command, Context
from ..config import ConfigIssue, home_dir, profile_issues
from ..csversion import Presence, check_version
from ..errors import CliError
from ..gateway import Gateway
from ..grammar import GrammarError, parse_ingest_every
from ..harness import Invalid, strict_loads
from ..launchplan import PROFILE_RELATIVE
from ..reader import open_root_checked
from ..retroartifact import bundled_marker
from ..retrorecovery import planned
from ..sessionquery import read_store
from ..store import StoreStatus, inspect_store
from . import cron

PRICE_MAX_AGE_DAYS = 180
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
MIN_STALE_MS = 24 * HOUR_MS
PROBE_TIMEOUT = 30.0
PROBE_OUTPUT_LIMIT = 1024 * 1024
SKILL_LIMIT = 1024 * 1024
BUNDLED_SKILL = "skills/audit/SKILL.md"
DEFAULT_CS = "claude-session"
RETRO_KIND = "audit"

WARNING_ORDER = (
    "price_table_old",
    "migration_pending",
    "root_unusable",
    "ingest_stale",
    "retros_unrecovered",
    "cleanup_incomplete",
    "sandbox_lowered",
    "env_file_unsafe",
    "claude_session_missing",
)


@dataclass
class Report:
    checks: list[tuple[str, str | None]] = field(default_factory=list)
    warnings: set[str] = field(default_factory=set)

    def add(self, name: str, reason: str | None = None) -> None:
        self.checks.append((name, reason))

    def warn(self, name: str) -> None:
        self.warnings.add(name)

    @property
    def ok(self) -> bool:
        return all(reason is None for _, reason in self.checks)

    def document(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [{"name": n, "ok": r is None, "reason": r} for n, r in self.checks],
            "warnings": [w for w in WARNING_ORDER if w in self.warnings],
        }


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--probe", action="store_true", help="also probe claude-session and the sandbox"
    )


def check(ctx: Context, args: argparse.Namespace) -> None:
    return None


# --- the config checks -------------------------------------------------------------------------


def _config_reason(ctx: Context) -> str | None:
    return "config_invalid" if ctx.config_error is not None else None


def _ownership_reason(issues: tuple[ConfigIssue, ...]) -> str | None:
    return "config_unsafe" if issues else None


def _roots(ctx: Context, report: Report) -> str | None:
    seen: set[tuple[int, int]] = set()
    opened = []
    try:
        for root in ctx.settings.transcript_roots:
            opened.append(open_root_checked(ctx.gateway, root, seen))
        usable = [r for r in opened if r.usable]
        unusable = [r for r in opened if r.reason not in (None, "duplicate")]
    finally:
        for root in opened:
            if root.handle is not None:
                root.handle.close()
    if not usable:
        return "no_readable_roots"
    if unusable:
        report.warn("root_unusable")
    return None


def _price_table(ctx: Context, report: Report) -> None:
    as_of = ctx.settings.prices.as_of
    if as_of is None:
        return
    try:
        age = (ctx.gateway.now().date() - date.fromisoformat(as_of)).days
    except ValueError:
        return
    if age > PRICE_MAX_AGE_DAYS:
        report.warn("price_table_old")


def _is_file(gateway: Gateway, path: str) -> bool:
    try:
        return statmod.S_ISREG(gateway.lstat(path).st_mode)
    except OSError:
        return False


def _permission_profile(ctx: Context, bundled: str | None) -> str | None:
    gateway = ctx.gateway
    configured = ctx.settings.retro.permission_profile
    if configured is None:
        if bundled is None or not _is_file(gateway, os.path.join(bundled, PROFILE_RELATIVE)):
            return "permission_profile_missing"
        return None
    try:
        gateway.lstat(configured)
    except FileNotFoundError:
        return "permission_profile_missing"
    except OSError:
        return "config_unsafe"
    if profile_issues(gateway, configured, home_dir(os.environ)):
        return "config_unsafe"
    return None


def _marker_matches(gateway: Gateway, bundled: str) -> bool:
    """The bundled marker is ``audit-`` and the first 24 hex digits of the SHA-256 of the skill."""
    try:
        content = gateway.read_file(os.path.join(bundled, BUNDLED_SKILL), SKILL_LIMIT)
    except OSError:
        return False
    if content.truncated or not statmod.S_ISREG(content.stat.st_mode):
        return False
    expected = "audit-" + hashlib.sha256(content.data).hexdigest()[:24]
    return bundled_marker(gateway, bundled) == expected


def _plugin(ctx: Context, bundled: str | None) -> str | None:
    if bundled is None:
        return "plugin_missing"
    settings = ctx.settings.retro
    try:
        choice = plugins.skill_preflight(
            ctx.gateway,
            settings.skill,
            plugin_dirs=settings.plugin_dirs,
            env=os.environ,
            state_directory=ctx.state_path(),
            bundled=bundled,
        )
    except CliError as exc:
        if exc.details.get("reason") == "unsafe_plugin_dir":
            return "unsafe_plugin_dir"
        return "plugin_missing"
    except OSError:
        return "plugin_missing"
    if choice.bundled is not None and choice.found == choice.bundled:
        if not _marker_matches(ctx.gateway, choice.bundled):
            return "plugin_missing"
    return None


# --- the store ---------------------------------------------------------------------------------


@dataclass
class Facts:
    """What the read-only reader found (all False/None without a store)."""

    last_complete: int | None = None
    interval_minutes: int | None = None
    unrecovered: bool = False
    cleanup_incomplete: bool = False


def _meta(conn: sqlite3.Connection, key: str) -> object:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def _read_facts(ctx: Context) -> Facts:
    """Read the facts the warnings need, in one read transaction. A table a pending migration
    has not created yet is skipped. ``OSError`` (a lease check that cannot answer) and ``CliError``
    propagate."""
    gateway = ctx.gateway
    cutoff = int(gateway.now().timestamp() * 1000) - DAY_MS

    def work(conn: sqlite3.Connection | None) -> Facts:
        facts = Facts()
        if conn is None:
            return facts
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "meta" in tables:
            last = _meta(conn, "last_complete_ingest_at")
            facts.last_complete = last if type(last) is int else None
            interval = _meta(conn, "ingest_interval")
            if isinstance(interval, str):
                try:
                    facts.interval_minutes = parse_ingest_every(interval).minutes
                except GrammarError:
                    pass
        if "retros" in tables:
            facts.unrecovered = bool(planned(conn, gateway))
        if "retro_sessions" in tables:
            facts.cleanup_incomplete = (
                conn.execute(
                    "SELECT 1 FROM retro_sessions WHERE cleanup_state IN ('pending', 'incomplete') "
                    "AND created_at < ? LIMIT 1",
                    (cutoff,),
                ).fetchone()
                is not None
            )
        return facts

    return read_store(ctx, work)


def _store(ctx: Context, report: Report) -> Facts | None:
    """The ``store`` check; the facts when the store can be read (None: not readable, or a newer
    one that is never read)."""
    try:
        status: StoreStatus = inspect_store(ctx.gateway, ctx.state_path())
    except CliError:
        report.add("store", "store_unreadable")
        return None
    if status.state == "newer":
        report.add("store", "store_newer")
        return None
    if status.state == "pending":
        report.warn("migration_pending")
    if status.state == "missing":
        report.add("store")
        return Facts()
    try:
        facts = _read_facts(ctx)
    except (CliError, OSError, sqlite3.Error):
        # A lease whose liveness cannot be read is an error, never a dead holder.
        report.add("store", "store_unreadable")
        return None
    report.add("store")
    return facts


def _warn_retro_state(report: Report, facts: Facts | None) -> None:
    if facts is None:
        return
    if facts.unrecovered:
        report.warn("retros_unrecovered")
    if facts.cleanup_incomplete:
        report.warn("cleanup_incomplete")


# --- the crontab -------------------------------------------------------------------------------

_ENV_PREFIX = re.compile(r"\S+ \S+ \S+ \S+ \S+ \. '")


def env_file_of(line: str) -> str | None:
    """The env file a retro line of the managed block sources: ``<5 fields> . '<path>' && ...``
    where the path is POSIX single-quoted by ``cron.quote``."""
    found = _ENV_PREFIX.match(line)
    if found is None:
        return None
    index = found.end()
    parts: list[str] = []
    while True:
        end = line.find("'", index)
        if end < 0:
            return None
        parts.append(line[index:end])
        if line.startswith("'\"'\"'", end):
            parts.append("'")
            index = end + 5
            continue
        return "".join(parts) if line.startswith(" && ", end + 1) else None


def _managed_block(gateway: Gateway) -> list[str] | None:
    try:
        return cron.split_crontab(cron.read_crontab(gateway)).block
    except CliError:
        return None  # no crontab, one that cannot be read, or a malformed block: no block known


def _crontab_warnings(ctx: Context, report: Report, facts: Facts | None) -> None:
    block = _managed_block(ctx.gateway)
    if block is None:
        return
    for line in block[1:-1]:
        path = env_file_of(line)
        if path is None:
            continue
        try:
            cron.check_env_file(ctx.gateway, path)
        except (CliError, OSError):
            report.warn("env_file_unsafe")
    if facts is None:
        return
    interval = facts.interval_minutes or 0
    threshold = max(MIN_STALE_MS, 2 * interval * 60_000)
    now = int(ctx.gateway.now().timestamp() * 1000)
    if facts.last_complete is None or now - facts.last_complete > threshold:
        report.warn("ingest_stale")


# --- claude-session ----------------------------------------------------------------------------


def _claude_session(ctx: Context, report: Report, command: str) -> None:
    check_ = check_version(ctx.gateway, command)
    mode = ctx.settings.harness.enabled
    if check_.presence is Presence.OK:
        report.add("claude_session")
    elif check_.presence is Presence.MISSING:
        if mode == "on":
            report.add("claude_session", "claude_session_missing")
        else:
            report.add("claude_session")
            if mode == "auto" and ctx.settings.schedule.retro is not None:
                report.warn("claude_session_missing")
    else:  # too old, an unparsable first line or a failed call: not a usable claude-session
        report.add("claude_session", "claude_session_too_old")


def _audit_profile(result_ok: bool, text: str) -> dict[str, Any] | None:
    """The ``audit`` profile of a ``kinds --json`` document when the document is usable: strict
    JSON, a claude that was found and is new enough, and a profile named ``audit`` that is not a
    fallback."""
    if not result_ok:
        return None
    try:
        document = strict_loads(text)
    except Invalid:
        return None
    if not isinstance(document, dict):
        return None
    if not isinstance(document.get("claude_bin_resolved"), str):
        return None
    if document.get("claude_version_ok") is not True:
        return None
    kinds = document.get("kinds")
    if not isinstance(kinds, list):
        return None
    for one in kinds:
        if isinstance(one, dict) and one.get("name") == RETRO_KIND and one.get("fallback") is False:
            return one
    return None


def _probe(ctx: Context, report: Report) -> None:
    if ctx.config_error is not None:
        reason: str | None = "config_invalid"
    elif ctx.config_issues:
        reason = "config_unsafe"  # exec-bearing config is not honored: nothing is run
    else:
        reason = None
    if reason is not None:
        report.add("probe_kinds", reason)
        report.add("probe_sandbox", reason)
        return
    result = ctx.gateway.run(
        [ctx.settings.harness.claude_session_cmd, "kinds", "--json"],
        timeout=PROBE_TIMEOUT,
        stdout_limit=PROBE_OUTPUT_LIMIT,
    )
    profile = _audit_profile(result.ok, result.stdout)
    if profile is None:
        report.add("probe_kinds", "probe_failed")
        report.add("probe_sandbox", "probe_failed")
        return
    report.add("probe_kinds")
    sandbox = ctx.settings.retro.sandbox
    available = profile.get("sandbox_available")
    if sandbox == "off" or available is True:
        report.add("probe_sandbox")
    elif sandbox == "preferred":
        report.add("probe_sandbox")  # without a sandbox the run is lowered, which is allowed
    elif available is False:
        report.add("probe_sandbox", "sandbox_unavailable")
    else:
        report.add("probe_sandbox", "probe_failed")


# --- the command -------------------------------------------------------------------------------


def build_report(ctx: Context, args: argparse.Namespace) -> Report:
    report = Report()
    broken = ctx.config_error is not None
    invalid = "config_invalid" if broken else None
    report.add("config", _config_reason(ctx))
    report.add("config_ownership", _ownership_reason(ctx.config_issues))

    report.add("transcript_roots", invalid or _roots(ctx, report))
    facts = _store(ctx, report)
    report.add("price_table", invalid)
    if not broken:
        _price_table(ctx, report)
    bundled = None if broken else plugins.bundled_plugin_dir(ctx.gateway)
    report.add("permission_profile", invalid or _permission_profile(ctx, bundled))
    report.add("plugin", invalid or _plugin(ctx, bundled))
    # An unsafe config's own claude-session command is never run.
    command = ctx.settings.harness.claude_session_cmd if not ctx.config_issues else DEFAULT_CS
    _claude_session(ctx, report, command)
    if args.probe:
        _probe(ctx, report)

    _warn_retro_state(report, facts)
    if not broken and ctx.settings.retro.sandbox != "required":
        report.warn("sandbox_lowered")
    _crontab_warnings(ctx, report, facts)
    return report


def run(ctx: Context, args: argparse.Namespace) -> int:
    report = build_report(ctx, args)
    document = report.document()
    if ctx.json:
        ctx.emit_json(document)
    else:
        for name, reason in report.checks:
            ctx.say(f"{name}: ok" if reason is None else f"{name}: failed ({reason})")
        for warning in document["warnings"]:
            ctx.say(f"warning: {warning}")
    if ctx.config_error is not None:
        return 2
    return 0 if report.ok else 12


COMMANDS = (
    Command(
        ("validate",),
        "check config, store, roots and claude-session",
        add_arguments,
        check,
        run,
        requires_config=False,
    ),
)
