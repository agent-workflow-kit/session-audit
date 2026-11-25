"""Flag helpers and argument validators shared by the commands.

Validators raise ``CliError`` (usage, exit 2) naming the flag in ``details.field``; messages are
fixed text and never echo the value. Grammars that exist in ``grammar.py`` are used as they are.
"""

from __future__ import annotations

import argparse
import getpass
import re
from typing import Any

from . import errors
from .command import Context
from .errors import CliError
from .grammar import GrammarError, parse_actor, parse_int, parse_label, parse_when

PATH_MAX = 4096  # the stored project is capped here (interface, Session object)
ACTOR_USER_MAX = 64
_ACTOR_BAD = re.compile(r"[^A-Za-z0-9._@/-]")
SKILL_MAX = 128
MODEL_MAX = 128
# schemas/defs.json#skill: the plugin part is wider than the name part after the colon.
_SKILL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}:[a-z0-9][a-z0-9-]{0,63}", re.ASCII)
# A model name is stored as read: any text without control characters, DEL or surrogates.
_MODEL = re.compile(r"[^\x00-\x1f\x7f-\x9f\ud800-\udfff]+")
_TOKEN = re.compile(r"[a-z][a-z0-9_-]{0,63}", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
RETRO_STATES = ("digesting", "running", "validating", "done", "failed", "escalated", "canceled")
SESSION_LIMIT_MAX = 1000
LIMIT_RANGE = (1, 1000)


def add_dry_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would happen; change nothing"
    )


def add_yes(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--yes", action="store_true", help="confirm without asking")


def add_limit(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", metavar="N", help="at most N rows (1 to 1000, default 100)")


def add_time_filters(parser: argparse.ArgumentParser, *, until: bool = True) -> None:
    parser.add_argument("--since", metavar="WHEN", help="a time, a date or a duration ago")
    if until:
        parser.add_argument("--until", metavar="WHEN", help="a time, a date or a duration ago")


def add_session_filters(parser: argparse.ArgumentParser) -> None:
    """``--label``, ``--project``, ``--kind`` and ``--outcome`` (every command that filters)."""
    parser.add_argument("--label", action="append", metavar="K=V", help="a label pair (repeatable)")
    parser.add_argument("--project", metavar="PATH", help="a project directory")
    parser.add_argument("--kind", metavar="K", help="a claude-session kind")
    parser.add_argument("--outcome", metavar="O", help="a claude-session outcome")


def not_implemented() -> CliError:
    """What a registered command whose implementation has not landed returns after its flags
    validated: an internal error naming no input."""
    return CliError("internal", "this command is not implemented yet")


def check_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= PATH_MAX or "\x00" in value:
        raise errors.usage("invalid path", field=field, hint="Give a path of 1 to 4096 characters.")
    return value


def check_when(ctx: Context, value: object, field: str) -> None:
    """Check the ``WHEN`` grammar against the configured ``time_zone`` (a date is midnight there;
    the built-in UTC when the config is invalid). Commands resolve the instant themselves."""
    try:
        parse_when(value, field=field, now=ctx.gateway.now(), time_zone=ctx.settings.zone())
    except GrammarError as exc:
        raise errors.from_grammar(exc) from None


def check_limit(args: argparse.Namespace, default: int = 100) -> None:
    """``--limit`` as an int from 1 to 1000 (default 100)."""
    if args.limit is None:
        args.limit = default
        return
    try:
        args.limit = parse_int(
            args.limit, field="--limit", minimum=LIMIT_RANGE[0], maximum=LIMIT_RANGE[1]
        )
    except GrammarError as exc:
        raise errors.from_grammar(exc) from None


def check_token(value: object, field: str) -> str:
    """A lowercase word (a claude-session kind or outcome)."""
    if not isinstance(value, str) or len(value) > 64 or not _TOKEN.fullmatch(value):
        raise errors.usage(
            "invalid value", field=field, hint="Use a lowercase word such as review or done."
        )
    return value


def check_model(value: object, field: str = "--model", *, launch: bool = False) -> str:
    """A model name: 1 to 128 characters, no control characters, DEL or surrogates (as
    ``defs.json#model``). With ``launch`` (``retro run``, where the name reaches claude-session)
    it must also not start with a dash."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MODEL_MAX
        or not _MODEL.fullmatch(value)
        or (launch and value.startswith("-"))
    ):
        raise errors.usage(
            "invalid model", field=field, hint="Give a model name of 1 to 128 characters."
        )
    return value


def check_skill(value: object, field: str = "--skill") -> str:
    """``plugin:skill`` as ``defs.json#skill``: at most 128 characters, the plugin part
    ``[A-Za-z0-9][A-Za-z0-9_.-]{0,63}``, the name part ``[a-z0-9][a-z0-9-]{0,63}``; a bare name
    is refused."""
    if not isinstance(value, str) or len(value) > SKILL_MAX or not _SKILL.fullmatch(value):
        raise errors.usage(
            "invalid skill", field=field, hint="Use plugin:skill, such as session-audit:audit."
        )
    return value


def check_retro_key(value: object, field: str = "--key") -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise errors.usage(
            "invalid key", field=field, hint="A retro key is 64 lowercase hex characters."
        )
    return value


def check_actor(value: object) -> str:
    try:
        return parse_actor(value, field="--actor")
    except GrammarError as exc:
        raise errors.from_grammar(exc) from None


def normalize_user(name: str) -> str:
    """The OS user name as an actor part: every character outside ``[A-Za-z0-9._@/-]`` becomes
    ``_`` and the result is cut to 64 (``unknown`` when empty)."""
    cleaned = _ACTOR_BAD.sub("_", name)[:ACTOR_USER_MAX]
    return cleaned or "unknown"


def os_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # no passwd entry, no environment: an actor must still exist
        return "unknown"


def default_actor(*, is_tty: bool, me_name: str | None = None) -> str:
    """``human:<user>`` from a terminal, otherwise ``auto:unknown``. ``<user>`` is ``me.name``
    when set (the config's ``me.name``), by default the OS user name, normalized."""
    if not is_tty:
        return "auto:unknown"
    return "human:" + normalize_user(me_name if me_name else os_user())


def require_confirmation(ctx: Context) -> None:
    """Without a terminal, a destructive command needs ``--yes`` (usage error
    ``confirmation_required``). On a terminal the prompt itself belongs to the command."""
    if not ctx.yes and not ctx.stdin_is_tty:
        raise errors.usage(
            "confirmation required",
            reason="confirmation_required",
            hint="Pass --yes to confirm without a terminal.",
        )


def check_list_max(values: list[Any] | None, field: str, maximum: int) -> None:
    if values is not None and len(values) > maximum:
        raise errors.usage(
            "too many values",
            field=field,
            details={"limit": maximum, "matched": len(values)},
            hint=f"Give at most {maximum}.",
        )


def check_labels(values: list[str] | None, field: str = "--label") -> None:
    for value in values or []:
        try:
            parse_label(value, field=field)
        except GrammarError as exc:
            raise errors.from_grammar(exc) from None


def check_filters(ctx: Context, args: argparse.Namespace) -> None:
    """The time and session filters that are present on ``args`` (see Session filters)."""
    for name in ("since", "until"):
        value = getattr(args, name, None)
        if value is not None:
            check_when(ctx, value, f"--{name}")
    check_labels(getattr(args, "label", None))
    if getattr(args, "project", None) is not None:
        check_path(args.project, "--project")
    for name in ("kind", "outcome"):
        value = getattr(args, name, None)
        if value is not None:
            check_token(value, f"--{name}")
