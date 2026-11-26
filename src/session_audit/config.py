"""Load and validate the config (interface.md, Config; ``schemas/config.json``).

``load_config`` reads ``config.yaml`` and the drop-ins ``config.d/*.yaml`` through the gateway,
merges them (maps merge, ``hooks`` lists append, everything else is replaced by the later file),
validates the merged document with Pydantic models and fills in the computed defaults. It never
raises ``CliError``: the result carries the settings (the built-in defaults when the config is
invalid), the ownership ``issues`` and the ``error`` that a command reports.

Rules that hold everywhere in this module:

* A message is a fixed string and never holds a config value. An error names the key (``field``)
  and never the value; a Pydantic error is read only through its ``loc`` and ``type``, never
  printed (it holds the input). An unknown key is named (its dotted path) only when that path
  fully matches the envelope's field pattern, otherwise it is reported as its parent, so key
  text, which is untrusted, is never echoed beyond that closed alphabet.
* YAML is read with a ``SafeLoader`` subclass that also refuses anchors, aliases (so no
  expansion or cycle bomb), merge keys, duplicate keys, non-string keys, nesting deeper than
  ``MAX_DEPTH`` and more than ``MAX_NODES`` nodes, and does not turn dates into objects. The
  parsed tree is then checked to hold only maps, lists, strings, numbers, booleans and null.
  Each file is at most ``MAX_FILE_BYTES`` and strict UTF-8.
* The config is read by safe means whatever its ownership: a symlink is never followed and a
  file that is not a regular file is not read. Ownership and mode problems are collected as
  ``ConfigIssue`` values; the commands that change something refuse on any issue, the others
  report it (``cli._dispatch``). Nothing in an issue holds a path.
"""

from __future__ import annotations

import errno
import os
import re
import stat as statmod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Annotated, Any, Literal, NoReturn
from zoneinfo import ZoneInfo

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    model_validator,
)

from .args import check_model, check_skill, normalize_user, os_user
from .errors import CliError
from .gateway import Gateway
from .grammar import (
    DEFAULT_SIZE_MAX,
    HOOK_TIMEOUT_MAX,
    INGEST_BUDGET_MAX,
    INGEST_FILE_BUDGET_MAX,
    QUIET_AFTER_MAX,
    RETENTION_MAX,
    RETRO_TIMEOUT_MAX,
    RETRO_TIMEOUT_MIN,
    GrammarError,
    parse_ago,
    parse_duration,
    parse_label,
    parse_size,
    parse_time_zone,
)

__all__ = [
    "BUNDLED_PRICES",
    "ConfigIssue",
    "LoadedConfig",
    "ModelPrice",
    "PriceTable",
    "Settings",
    "config_paths",
    "home_dir",
    "load_config",
    "profile_issues",
    "resolve_settings",
]

MAX_FILE_BYTES = 1024 * 1024  # interface, Config: a 1 MiB size cap per file
MAX_DROPINS = 32
MAX_DEPTH = 32
MAX_NODES = 20_000
PROGRAM_DIR = "session-audit"
CONFIG_NAME = "config.yaml"
DROPIN_DIR = "config.d"
DROPIN_SUFFIX = ".yaml"
PRICE_MAX = 1_000_000.0  # USD per million tokens; the schema's "maximum" of every price field

ENV_TIMEOUT = "SESSION_AUDIT_TIMEOUT"
ENV_MODEL = "SESSION_AUDIT_MODEL"

_ASCII = re.ASCII
_PLAIN_TEXT = re.compile(r"[^\x00-\x1f\x7f-\x9f\ud800-\udfff]+")
_FIELD_SEGMENT = re.compile(r"[a-z_][a-z0-9_-]*", _ASCII)
_ENVELOPE_FIELD = re.compile(r"[a-z_][a-z0-9_.-]{0,63}", _ASCII)  # errors.py: the "field" pattern
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}", _ASCII)
_TOOL_NAME = re.compile(r"[A-Z][A-Za-z]{1,31}", _ASCII)
DEFAULT_CLAUDE_SESSION_CMD = "claude-session"
_CMD_NAME = re.compile(r"[A-Za-z0-9._/+-]{1,256}", _ASCII)
# Starts with a letter or digit and holds no "..": the name is a path-like actor part.
_ME_NAME = re.compile(r"(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._@/-]{0,63}", _ASCII)
_PREFIX = re.compile(r"[A-Za-z0-9_-]{3,64}", _ASCII)
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}", _ASCII)
_AS_OF = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", _ASCII)
_TOKEN = re.compile(r"[a-z][a-z0-9_-]{0,63}", _ASCII)
_DROPIN_NAME = re.compile(r"[^/\x00]{1,200}")


# --- errors ---------------------------------------------------------------------------------


def _invalid(message: str, field: str | None = None, hint: str | None = None) -> CliError:
    details: dict[str, Any] = {} if field is None else {"field": field}
    return CliError("config_invalid", message, details=details, hint=hint)


class _Rejected(Exception):
    """Raised inside the YAML loader and the tree check; carries a fixed message only."""


# --- the YAML loader ------------------------------------------------------------------------


# YAML 1.1 reads ``010`` as octal, ``0x10`` as hex, ``0b11`` as binary, ``1_0`` as 10 and
# ``1:30`` as 90 (sexagesimal); none of those is meant in a config. The implicit int and float
# resolvers are replaced by strict decimal ones, so such a scalar stays a string and fails the
# validation of the key it is in.
_DROPPED_TAGS = frozenset(
    {"tag:yaml.org,2002:timestamp", "tag:yaml.org,2002:int", "tag:yaml.org,2002:float"}
)
_DECIMAL_INT = re.compile(r"^[-+]?(?:0|[1-9][0-9]*)$")
_DECIMAL_FLOAT = re.compile(r"^[-+]?(?:(?:0|[1-9][0-9]*)\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?$")


class _Loader(yaml.SafeLoader):
    """``SafeLoader`` without anchors, aliases, merge keys, duplicate or non-string keys, with
    bounded depth and node count, and without implicit timestamps (a date stays a string, as
    the schema's ``as_of`` is)."""

    yaml_implicit_resolvers = {
        first: [item for item in resolvers if item[0] not in _DROPPED_TAGS]
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._depth = 0
        self._nodes = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.events.AliasEvent):
            raise _Rejected("anchors and aliases are not allowed")
        event = self.peek_event()
        if getattr(event, "anchor", None) is not None:
            raise _Rejected("anchors and aliases are not allowed")
        self._nodes += 1
        if self._nodes > MAX_NODES:
            raise _Rejected("the document is too large")
        self._depth += 1
        try:
            if self._depth > MAX_DEPTH:
                raise _Rejected("the document is nested too deeply")
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        if isinstance(node, yaml.MappingNode):
            seen: set[str] = set()
            for key_node, _ in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    raise _Rejected("merge keys are not allowed")
                key = self.construct_object(key_node, deep=True)
                if not isinstance(key, str):
                    raise _Rejected("every key must be a string")
                if key in seen:
                    raise _Rejected("a key appears twice")
                seen.add(key)
        return super().construct_mapping(node, deep)


_Loader.add_implicit_resolver("tag:yaml.org,2002:int", _DECIMAL_INT, list("-+0123456789"))
_Loader.add_implicit_resolver("tag:yaml.org,2002:float", _DECIMAL_FLOAT, list("-+0123456789."))


def _check_plain(value: object, depth: int = 0) -> None:
    """Only maps with string keys, lists, strings, numbers, booleans and null; no NUL and no
    lone surrogate in any string."""
    if depth > MAX_DEPTH:
        raise _Rejected("the document is nested too deeply")
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if "\x00" in value or any("\ud800" <= ch <= "\udfff" for ch in value):
            raise _Rejected("a text holds a character that is not allowed")
        return
    if isinstance(value, list):
        for item in value:
            _check_plain(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _check_plain(key, depth + 1)
            _check_plain(item, depth + 1)
        return
    raise _Rejected("a value has a type the config does not use")


def parse_yaml(data: bytes) -> dict[str, Any]:
    """The mapping a config file holds (empty for an empty file). Raises ``CliError``."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid("a config file is not valid UTF-8") from None
    try:
        loader = _Loader(text)
        try:
            loaded = loader.get_single_data()
        finally:
            loader.dispose()
        _check_plain(loaded)
    except _Rejected as exc:
        raise _invalid(f"a config file is refused: {exc}") from None
    except (yaml.YAMLError, ValueError, RecursionError, MemoryError):
        raise _invalid("a config file is not valid YAML") from None
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise _invalid("a config file must hold a mapping at the top level")
    return loaded


def merge_documents(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """``over`` on top of ``base``: maps merge key by key, the lists under ``hooks`` append,
    every other value of ``over`` replaces the one of ``base``."""
    return _merge(base, over, ())


def _merge(base: dict[str, Any], over: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in over.items():
        here = (*path, key)
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge(current, value, here)
        elif path == ("hooks",) and isinstance(current, list) and isinstance(value, list):
            merged[key] = [*current, *value]
        else:
            merged[key] = value
    return merged


# --- the models -----------------------------------------------------------------------------


def _fail() -> NoReturn:
    raise ValueError("invalid")


def _text(
    *, maximum: int, pattern: re.Pattern[str] | None = None, minimum: int = 1
) -> BeforeValidator:
    def check(value: object) -> str:
        if not isinstance(value, str) or not minimum <= len(value) <= maximum:
            _fail()
        if pattern is not None and not pattern.fullmatch(value):
            _fail()
        if pattern is None and not _PLAIN_TEXT.fullmatch(value):
            _fail()
        return value

    return BeforeValidator(check)


def _int(low: int, high: int) -> BeforeValidator:
    def check(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            _fail()
        return value

    return BeforeValidator(check)


def _bool() -> BeforeValidator:
    def check(value: object) -> bool:
        if not isinstance(value, bool):
            _fail()
        return value

    return BeforeValidator(check)


def _number(low: float, high: float) -> BeforeValidator:
    def check(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _fail()
        try:
            number = float(value)
        except OverflowError:
            _fail()
        if number != number or not low <= number <= high:  # NaN fails the comparison too
            _fail()
        return number

    return BeforeValidator(check)


def _choice(*names: str) -> BeforeValidator:
    def check(value: object) -> str:
        if not isinstance(value, str) or value not in names:
            _fail()
        return value

    return BeforeValidator(check)


def _duration(
    key: str,
    maximum: timedelta,
    *,
    minimum: timedelta = timedelta(milliseconds=1),
    allow_days: bool = False,
) -> BeforeValidator:
    def check(value: object) -> timedelta:
        try:
            return parse_duration(
                value, field=key, maximum=maximum, minimum=minimum, allow_days=allow_days
            )
        except GrammarError:
            _fail()

    return BeforeValidator(check)


def _size(key: str, maximum: int) -> BeforeValidator:
    def check(value: object) -> int:
        try:
            return parse_size(value, field=key, maximum=maximum)
        except GrammarError:
            _fail()

    return BeforeValidator(check)


def _items(
    item: Callable[[object], object],
    *,
    maximum: int,
    minimum: int = 0,
    unique: bool = True,
) -> BeforeValidator:
    def check(value: object) -> tuple[object, ...]:
        if not isinstance(value, list) or not minimum <= len(value) <= maximum:
            _fail()
        checked = tuple(item(v) for v in value)
        if unique and len(set(checked)) != len(checked):
            _fail()
        return checked

    return BeforeValidator(check)


def _item_text(maximum: int, pattern: re.Pattern[str] | None = None, minimum: int = 1):
    def check(value: object) -> str:
        if not isinstance(value, str) or not minimum <= len(value) <= maximum:
            _fail()
        if pattern is not None and not pattern.fullmatch(value):
            _fail()
        if pattern is None and not _PLAIN_TEXT.fullmatch(value):
            _fail()
        return value

    return check


def _expand(value: str, home: str) -> str:
    if value == "~":
        return home
    if value.startswith("~/"):
        return home.rstrip("/") + value[1:]
    return value


def _home_of(info: ValidationInfo) -> str:
    context = info.context or {}
    return str(context.get("home", "/"))


def _has_dot_segment(path: str) -> bool:
    """A ``.`` or ``..`` segment: ``normpath`` would resolve it lexically, which a symlink in
    front of a ``..`` makes wrong, so such a path is refused rather than normalized."""
    return any(segment in (".", "..") for segment in path.split("/"))


def _absolute_path(maximum: int) -> BeforeValidator:
    """A path of 1 to ``maximum`` characters without NUL; a leading ``~`` is the home directory;
    the result must be absolute (a relative path would depend on the working directory) and has
    no ``.`` or ``..`` segment."""

    def check(value: object, info: ValidationInfo) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= maximum or "\x00" in value:
            _fail()
        path = _expand(value, _home_of(info))
        if not path.startswith("/") or _has_dot_segment(path):
            _fail()
        return path

    return BeforeValidator(check)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=False)


class IngestSettings(_Model):
    budget: Annotated[timedelta, _duration("ingest.budget", INGEST_BUDGET_MAX)] = timedelta(
        seconds=300
    )
    file_budget: Annotated[timedelta, _duration("ingest.file_budget", INGEST_FILE_BUDGET_MAX)] = (
        timedelta(seconds=60)
    )
    fail_threshold: Annotated[int, _int(1, 1000)] = 3


class HarnessSettings(_Model):
    # A YAML 1.1 loader reads an unquoted ``off`` or ``on`` as a boolean; the schema says the
    # value is a string, so it is refused (quote it) rather than guessed.
    enabled: Annotated[Literal["auto", "on", "off"], _choice("auto", "on", "off")] = "auto"
    claude_session_cmd: Annotated[str, _text(maximum=256, pattern=_CMD_NAME)] = (
        DEFAULT_CLAUDE_SESSION_CMD
    )


class ModelPrice(_Model):
    input: Annotated[float, _number(0, PRICE_MAX)]
    output: Annotated[float, _number(0, PRICE_MAX)]
    cache_read: Annotated[float, _number(0, PRICE_MAX)]
    cache_write: Annotated[float, _number(0, PRICE_MAX)]
    cache_write_1h: Annotated[float | None, _number(0, PRICE_MAX)] = None

    @property
    def cache_write_1h_or_default(self) -> float:
        return self.cache_write if self.cache_write_1h is None else self.cache_write_1h


def _models(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or len(value) > 256:
        _fail()
    for name in value:
        if not isinstance(name, str) or not _MODEL_NAME.fullmatch(name):
            _fail()
    return value


def _as_of(value: object) -> str:
    if not isinstance(value, str) or not _AS_OF.fullmatch(value):
        _fail()
    try:
        date.fromisoformat(value)
    except ValueError:
        _fail()
    return value


class PriceTable(_Model):
    """USD per million tokens per model. A model matches by exact name first, then by the longest
    configured prefix that ends at a ``-`` boundary in the model id."""

    as_of: Annotated[str | None, BeforeValidator(_as_of)] = None
    models: Annotated[dict[str, ModelPrice], BeforeValidator(_models)] = Field(default_factory=dict)

    def lookup(self, model: str) -> ModelPrice | None:
        exact = self.models.get(model)
        if exact is not None:
            return exact
        best: str | None = None
        for name in self.models:
            if model.startswith(name + "-") and (best is None or len(name) > len(best)):
                best = name
        return None if best is None else self.models[best]


def _price(
    input: float, output: float, cache_read: float, cache_write: float, cache_write_1h: float
) -> ModelPrice:
    return ModelPrice(
        input=input,
        output=output,
        cache_read=cache_read,
        cache_write=cache_write,
        cache_write_1h=cache_write_1h,
    )


# List prices in USD per million tokens (base input, output, cache read, 5 minute cache write,
# 1 hour cache write), copied on 2026-11-25 (``as_of``) from
# https://platform.claude.com/docs/en/about-claude/pricing; each id is the one given on
# https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions and
# https://platform.claude.com/docs/en/models/overview (a dated snapshot id matches its key as a
# prefix). A model not in the table has no estimated cost (its tokens count as
# ``unknown_tokens``); a price in the config replaces or extends this table, and ``validate``
# warns when the table is older than 180 days. Only entries verified against those pages are
# listed: no ``claude-3-*``, and no bare ``claude-opus-4``, ``claude-opus-4-1`` or
# ``claude-sonnet-4`` prefix (it would match later models, priced differently).
# todo(#14): tiered pricing (haiku 5.5 etc.) not representable; such models count as
# unknown_tokens
BUNDLED_PRICES = PriceTable(
    as_of="2026-11-25",
    models={
        "claude-fable-5-2": _price(8.0, 40.0, 0.2, 10.0, 16.0),
        "claude-opus-5-5": _price(4.0, 20.0, 0.2, 5.0, 8.0),
        "claude-opus-4-8": _price(5.0, 25.0, 0.5, 6.25, 10.0),
        "claude-opus-4-7": _price(5.0, 25.0, 0.5, 6.25, 10.0),
        "claude-opus-4-6": _price(5.0, 25.0, 0.5, 6.25, 10.0),
        "claude-opus-4-5": _price(5.0, 25.0, 0.5, 6.25, 10.0),
        "claude-sonnet-5-5": _price(2.0, 10.0, 0.1, 2.5, 4.0),
        "claude-sonnet-4-6": _price(3.0, 15.0, 0.3, 3.75, 6.0),
        "claude-sonnet-4-5": _price(3.0, 15.0, 0.3, 3.75, 6.0),
        "claude-haiku-4-5": _price(1.0, 5.0, 0.1, 1.25, 2.0),
    },
)


class CostSettings(_Model):
    mismatch_margin: Annotated[float, _number(0.0, 1.0)] = 0.10


class RetentionSettings(_Model):
    sessions: Annotated[
        timedelta, _duration("retention.sessions", RETENTION_MAX, allow_days=True)
    ] = timedelta(days=400)
    retros: Annotated[timedelta, _duration("retention.retros", RETENTION_MAX, allow_days=True)] = (
        timedelta(days=180)
    )
    digests: Annotated[
        timedelta, _duration("retention.digests", RETENTION_MAX, allow_days=True)
    ] = timedelta(days=7)
    log: Annotated[timedelta, _duration("retention.log", RETENTION_MAX, allow_days=True)] = (
        timedelta(days=90)
    )


class LimitsSettings(_Model):
    retros_per_day: Annotated[int, _int(1, 1000)] = 5


def _skill(value: object) -> str:
    try:
        return check_skill(value, "retro.skill")
    except CliError:
        _fail()


def _model_name(value: object) -> str:
    try:
        return check_model(value, "retro.model", launch=True)
    except CliError:
        _fail()


DEFAULT_ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LANG", "TERM")
DEFAULT_TOOLS = ("Read", "Write", "Bash", "Skill")


class RetroSettings(_Model):
    skill: Annotated[str, BeforeValidator(_skill)] = "session-audit:audit"
    skill_marker: Annotated[str | None, _text(maximum=256)] = None
    plugin_dirs: Annotated[
        tuple[str, ...],
        _items(
            lambda v: _absolute_path_item(v),
            maximum=8,
        ),
    ] = ()
    model: Annotated[str | None, BeforeValidator(_model_name)] = None
    timeout: Annotated[
        timedelta, _duration("retro.timeout", RETRO_TIMEOUT_MAX, minimum=RETRO_TIMEOUT_MIN)
    ] = timedelta(minutes=20)
    max_turns: Annotated[int, _int(1, 1000)] = 12
    max_sessions: Annotated[int, _int(1, 500)] = 20
    # ``d`` is not accepted here: the interface names only retention.* and the scope of
    # schedule.retro as day-valued keys.
    quiet_after: Annotated[timedelta, _duration("retro.quiet_after", QUIET_AFTER_MAX)] = timedelta(
        minutes=30
    )
    permission_profile: Annotated[str | None, _absolute_path(4096)] = None
    sandbox: Annotated[
        Literal["required", "preferred", "off"], _choice("required", "preferred", "off")
    ] = "required"
    setting_sources: Annotated[
        tuple[Literal["user", "project", "local"], ...],
        _items(lambda v: _enum_item(v, ("user", "project", "local")), maximum=3),
    ] = ()
    env_allowlist: Annotated[tuple[str, ...], _items(_item_text(64, _ENV_NAME), maximum=64)] = (
        DEFAULT_ENV_ALLOWLIST
    )
    tools: Annotated[tuple[str, ...], _items(_item_text(32, _TOOL_NAME), maximum=32, minimum=1)] = (
        DEFAULT_TOOLS
    )
    remove_run_transcripts: Annotated[bool, _bool()] = True


def _absolute_path_item(value: object) -> str:
    """``retro.plugin_dirs`` entries are absolute as written (the schema has no ``~``)."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 4096
        or "\x00" in value
        or not value.startswith("/")
    ):
        _fail()
    return value


def _enum_item(value: object, names: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in names:
        _fail()
    return value


class DigestSettings(_Model):
    # The interface gives these two keys no maximum of their own, so the parser's default
    # ceiling (1 GiB) applies, passed by name rather than left to the parser's default.
    max_bytes: Annotated[int, _size("digest.max_bytes", DEFAULT_SIZE_MAX)] = 200 * 1024
    session_max_bytes: Annotated[int, _size("digest.session_max_bytes", DEFAULT_SIZE_MAX)] = (
        20 * 1024
    )
    prompt_chars: Annotated[int, _int(20, 65536)] = 500
    assistant_chars: Annotated[int, _int(20, 65536)] = 300
    arg_chars: Annotated[int, _int(20, 65536)] = 200
    error_chars: Annotated[int, _int(20, 65536)] = 300


class RedactionSettings(_Model):
    mask_env: Annotated[tuple[str, ...], _items(_item_text(64, _ENV_NAME), maximum=64)] = ()
    extra_token_prefixes: Annotated[
        tuple[str, ...], _items(_item_text(64, _PREFIX, minimum=3), maximum=64, unique=False)
    ] = ()


def _label(value: object) -> str:
    try:
        parse_label(value, field="schedule.retro.scope")
    except GrammarError:
        _fail()
    assert isinstance(value, str)
    return value


def _ago(value: object) -> str:
    try:
        parse_ago(value, field="schedule.retro.scope")
    except GrammarError:
        _fail()
    assert isinstance(value, str)
    return value


class ScheduleScope(_Model):
    labels: Annotated[tuple[str, ...], _items(_label, maximum=64)] = ()
    project: Annotated[str | None, _text(maximum=4096)] = None
    since: Annotated[str, BeforeValidator(_ago)]
    until: Annotated[str | None, BeforeValidator(_ago)] = None
    kind: Annotated[str | None, _text(maximum=64, pattern=_TOKEN)] = None
    outcome: Annotated[str | None, _text(maximum=64, pattern=_TOKEN)] = None

    @model_validator(mode="after")
    def _window(self) -> ScheduleScope:
        # ``until`` is a shorter time ago than ``since``, or the window is empty.
        if self.until is not None and parse_ago(self.until, field="until") >= parse_ago(
            self.since, field="since"
        ):
            _fail()
        return self


class ScheduleRetro(_Model):
    weekday: Annotated[int, _int(0, 6)] = 1
    hour: Annotated[int, _int(0, 23)] = 9
    scope: ScheduleScope


class ScheduleSettings(_Model):
    retro: ScheduleRetro | None = None


def _cmd(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 4096
        or "\x00" in value
        or any("\ud800" <= ch <= "\udfff" for ch in value)
    ):
        _fail()
    return value


_HOOK_TIMEOUT = _duration("hooks", HOOK_TIMEOUT_MAX)
_HOOK_DEFAULT_TIMEOUT = timedelta(seconds=30)


class PreHook(_Model):
    cmd: Annotated[str, BeforeValidator(_cmd)]
    timeout: Annotated[timedelta, _HOOK_TIMEOUT] = _HOOK_DEFAULT_TIMEOUT


class PostHook(_Model):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=False)

    cmd: Annotated[str, BeforeValidator(_cmd)]
    timeout: Annotated[timedelta, _HOOK_TIMEOUT] = _HOOK_DEFAULT_TIMEOUT
    run_async: Annotated[bool, _bool()] = Field(default=False, alias="async")


def _hook_list(value: object) -> object:
    if not isinstance(value, list) or len(value) > 32:
        _fail()
    return tuple(value)


class HookSettings(_Model):
    pre_retro: Annotated[tuple[PreHook, ...], BeforeValidator(_hook_list)] = Field(
        default=(), alias="pre-retro"
    )
    post_retro: Annotated[tuple[PostHook, ...], BeforeValidator(_hook_list)] = Field(
        default=(), alias="post-retro"
    )
    post_attention: Annotated[tuple[PostHook, ...], BeforeValidator(_hook_list)] = Field(
        default=(), alias="post-attention"
    )


class MeSettings(_Model):
    name: Annotated[str, _text(maximum=64, pattern=_ME_NAME)] = "unknown"


def _roots(value: object, info: ValidationInfo) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        _fail()
    home = _home_of(info)
    roots: list[str] = []
    for item in value:
        if not isinstance(item, str) or not 1 <= len(item) <= 4096 or "\x00" in item:
            _fail()
        root = _expand(item, home)
        if not root.startswith("/") or _has_dot_segment(root):
            _fail()
        roots.append(root)
    return tuple(roots)


def _zone(value: object) -> str:
    try:
        return parse_time_zone(value, field="time_zone").key
    except GrammarError:
        _fail()


class Settings(_Model):
    """The merged, validated config. ``Settings()`` is the built-in default config, except that
    the three computed defaults (``transcript_roots``, ``time_zone`` and ``me.name``) hold
    placeholders until ``resolve_settings`` fills them.

    Under an unsafe config (``LoadedConfig.issues`` not empty) the exec-bearing keys
    (``harness.claude_session_cmd``, ``hooks``, ``retro.plugin_dirs``,
    ``retro.permission_profile``) are still applied here.
    """

    # A command that executes these keys must not honor them while ctx.config_issues is
    # non-empty: the commands that change something (``retro run`` among them) refuse an unsafe
    # config (exit 20), and ``validate`` runs the default claude-session command and no probe.

    transcript_roots: Annotated[tuple[str, ...], BeforeValidator(_roots)] = ()
    time_zone: Annotated[str, BeforeValidator(_zone)] = "UTC"
    ingest: IngestSettings = Field(default_factory=IngestSettings)
    harness: HarnessSettings = Field(default_factory=HarnessSettings)
    prices: PriceTable = BUNDLED_PRICES
    cost: CostSettings = Field(default_factory=CostSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    limits: LimitsSettings = Field(default_factory=LimitsSettings)
    retro: RetroSettings = Field(default_factory=RetroSettings)
    digest: DigestSettings = Field(default_factory=DigestSettings)
    redaction: RedactionSettings = Field(default_factory=RedactionSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    hooks: HookSettings = Field(default_factory=HookSettings)
    me: MeSettings = Field(default_factory=MeSettings)

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.time_zone)


# --- ownership --------------------------------------------------------------------------------

_SUBJECTS = {
    "config": "the config file",
    "dropins": "config.d",
    "dropin": "a drop-in",
    "directory": "a directory above the config",
    "profile": "the permission profile file",
}
_KINDS = {
    "symlink": "is a symlink",
    "not_owned": "is not owned by you",
    "writable": "is writable by group or other",
    "not_regular": "is not a regular file",
    "not_directory": "is not a directory",
    "unreadable": "cannot be inspected",
}


@dataclass(frozen=True)
class ConfigIssue:
    """One ownership or mode problem. Holds two words of a closed vocabulary, never a path."""

    subject: str
    kind: str

    def describe(self) -> str:
        return f"{_SUBJECTS[self.subject]} {_KINDS[self.kind]}"


def home_dir(env: Mapping[str, str]) -> str:
    """``$HOME`` when it is an absolute path, else the passwd entry's home directory."""
    home = env.get("HOME", "")
    if home.startswith("/") and "\x00" not in home:
        return os.path.normpath(home)
    return os.path.normpath(os.path.expanduser("~"))


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("/") + "/")


def _mode_issues(info: os.stat_result, uid: int, subject: str) -> list[ConfigIssue]:
    found: list[ConfigIssue] = []
    if info.st_uid != uid:
        found.append(ConfigIssue(subject, "not_owned"))
    if info.st_mode & 0o022:
        found.append(ConfigIssue(subject, "writable"))
    return found


def _directory_issues(
    gateway: Gateway, directory: str, home: str, subject: str = "directory"
) -> list[ConfigIssue]:
    """The checks of every directory from ``directory`` up to ``home`` (``home`` itself is
    stat-ed through a symlink, as the environment names it; its owner and mode count). A path
    outside ``home`` is checked up to the first root-owned directory (root-owned ancestors are not
    checked, interface Config)."""
    uid = gateway.uid()
    issues: list[ConfigIssue] = []
    inside = _under(directory, home)
    current = directory
    while True:
        at_home = inside and current == home
        try:
            info = gateway.stat(current) if at_home else gateway.lstat(current)
        except FileNotFoundError:
            return issues  # nothing there, so nothing was read through it
        except OSError:
            issues.append(ConfigIssue(subject, "unreadable"))
            return issues
        if not inside and info.st_uid == 0 and uid != 0:
            return issues
        if statmod.S_ISLNK(info.st_mode):
            issues.append(ConfigIssue(subject, "symlink"))
        elif not statmod.S_ISDIR(info.st_mode):
            issues.append(ConfigIssue(subject, "not_directory"))
        else:
            issues.extend(_mode_issues(info, uid, subject))
        parent = os.path.dirname(current)
        if at_home or parent == current:
            return issues
        current = parent


def profile_issues(gateway: Gateway, path: str, home: str) -> list[ConfigIssue]:
    """The ownership rule for ``retro.permission_profile``: the file and every directory above it
    (checked before a launch; a missing file is the caller's concern)."""
    # Called by retro.check_config (step 2) and again by retrolaunch.run_launch just before the
    # row is inserted (the file may change between the two).
    # ``validate`` reports it too (the ``permission_profile`` check).
    uid = gateway.uid()
    path = os.path.normpath(path)
    issues: list[ConfigIssue] = []
    try:
        info = gateway.lstat(path)
    except FileNotFoundError:
        return issues
    except OSError:
        return [ConfigIssue("profile", "unreadable")]
    if statmod.S_ISLNK(info.st_mode):
        issues.append(ConfigIssue("profile", "symlink"))
    elif not statmod.S_ISREG(info.st_mode):
        issues.append(ConfigIssue("profile", "not_regular"))
    else:
        issues.extend(_mode_issues(info, uid, "profile"))
    issues.extend(_directory_issues(gateway, os.path.dirname(path), home))
    return list(dict.fromkeys(issues))


# --- locating and reading ---------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigPaths:
    file: str
    dropin_dir: str | None  # None with ``--config``: that one file alone
    explicit: bool


def config_paths(env: Mapping[str, str], config: str | None) -> ConfigPaths:
    """``--config PATH`` names the one file (no drop-ins: a sibling directory of an arbitrary
    path is not ours to read); otherwise ``$XDG_CONFIG_HOME`` (when absolute, else
    ``~/.config``) ``/session-audit/config.yaml`` and ``config.d/`` beside it."""
    if config is not None:
        return ConfigPaths(os.path.abspath(config), None, True)
    xdg = env.get("XDG_CONFIG_HOME", "")
    base = xdg if xdg.startswith("/") and "\x00" not in xdg else f"{home_dir(env)}/.config"
    directory = os.path.normpath(f"{base}/{PROGRAM_DIR}")
    return ConfigPaths(f"{directory}/{CONFIG_NAME}", f"{directory}/{DROPIN_DIR}", False)


@dataclass(frozen=True)
class LoadedConfig:
    settings: Settings
    issues: tuple[ConfigIssue, ...] = ()
    error: CliError | None = None

    @property
    def safe(self) -> bool:
        return not self.issues


class _Reader:
    """Reads the files of one load and collects their ownership issues."""

    def __init__(self, gateway: Gateway, home: str) -> None:
        self.gateway = gateway
        self.home = home
        self.issues: list[ConfigIssue] = []
        self._chain_done = False

    def chain(self, directory: str) -> None:
        if not self._chain_done:
            self._chain_done = True
            self.issues.extend(_directory_issues(self.gateway, directory, self.home))

    def read(self, path: str, subject: str) -> dict[str, Any] | None:
        """The document of ``path``, or None when it is absent or must not be read (a symlink or
        a file that is not a regular file; the issue is recorded). Ownership and mode problems
        are recorded and the file is still read."""
        self.chain(os.path.dirname(path))
        try:
            info = self.gateway.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError:
            raise _invalid("a config file cannot be read") from None
        if statmod.S_ISLNK(info.st_mode):
            self.issues.append(ConfigIssue(subject, "symlink"))
            return None
        if not statmod.S_ISREG(info.st_mode):  # a socket, FIFO or directory is never opened
            self.issues.append(ConfigIssue(subject, "not_regular"))
            return None
        try:
            content = self.gateway.read_file(path, MAX_FILE_BYTES)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:  # swapped for a symlink since the lstat
                self.issues.append(ConfigIssue(subject, "symlink"))
                return None
            raise _invalid("a config file cannot be read") from None
        if not statmod.S_ISREG(content.stat.st_mode):
            self.issues.append(ConfigIssue(subject, "not_regular"))
            return None
        self.issues.extend(_mode_issues(content.stat, self.gateway.uid(), subject))
        if content.truncated:
            raise _invalid("a config file is larger than 1 MiB")
        return parse_yaml(content.data)

    def dropins(self, directory: str) -> list[str]:
        """The paths of the drop-ins in lexical order (``*.yaml``, not hidden)."""
        self.chain(directory)
        try:
            info = self.gateway.lstat(directory)
        except (FileNotFoundError, NotADirectoryError):
            return []
        except OSError:
            raise _invalid("config.d cannot be read") from None
        if statmod.S_ISLNK(info.st_mode):
            self.issues.append(ConfigIssue("dropins", "symlink"))
            return []
        if not statmod.S_ISDIR(info.st_mode):
            self.issues.append(ConfigIssue("dropins", "not_directory"))
            return []
        self.issues.extend(_mode_issues(info, self.gateway.uid(), "dropins"))
        try:
            names = self.gateway.list_dir(directory)
        except OSError:
            raise _invalid("config.d cannot be read") from None
        chosen = sorted(
            n
            for n in names
            if n.endswith(DROPIN_SUFFIX) and not n.startswith(".") and _DROPIN_NAME.fullmatch(n)
        )
        if len(chosen) > MAX_DROPINS:
            raise _invalid("config.d holds too many drop-ins", hint=f"Keep at most {MAX_DROPINS}.")
        return [f"{directory}/{n}" for n in chosen]


# --- validation and the computed defaults ------------------------------------------------------

_PRICES_FIELD = ("prices", "models")
_SCOPE_FIELD = ("schedule", "retro", "scope")


def _field_of(loc: tuple[Any, ...], kind: str) -> str | None:
    """The config key of a Pydantic error: its location up to the first list index or odd
    segment. An unknown key is reported as its parent (its text is untrusted), and everything
    under ``prices.models`` and ``schedule.retro.scope`` is reported as that key."""
    parts = [str(p) if isinstance(p, str) else None for p in loc]
    if kind == "extra_forbidden" and parts:
        parts.pop()
    names: list[str] = []
    for part in parts:
        if part is None or not _FIELD_SEGMENT.fullmatch(part):
            break
        names.append(part)
    for fixed in (_PRICES_FIELD, _SCOPE_FIELD):
        if tuple(names[: len(fixed)]) == fixed or (
            tuple(str(p) for p in loc[: len(fixed)]) == fixed and len(names) >= len(fixed) - 1
        ):
            names = list(fixed)
    # ``hooks.pre-retro`` has a dash; the closed envelope's pattern allows it.
    field = ".".join(names)
    return field[:64] or None


def _unknown_key_path(loc: tuple[Any, ...]) -> str | None:
    """The dotted path of an unknown key when every part is a string and the whole fits the
    envelope's field pattern; None otherwise (the caller reports the parent)."""
    if not loc or not all(isinstance(p, str) for p in loc):
        return None
    if "." in loc[-1]:
        return None  # a dot in the key's own name would read as a nested key
    path = ".".join(loc)
    if not _ENVELOPE_FIELD.fullmatch(path):
        return None
    if (
        tuple(loc[: len(_PRICES_FIELD)]) == _PRICES_FIELD
        or tuple(loc[: len(_SCOPE_FIELD)]) == _SCOPE_FIELD
    ):
        return None  # reported as that key, whatever is under it
    return path


def _from_validation(error: ValidationError) -> CliError:
    first = error.errors(include_url=False, include_context=False, include_input=False)[0]
    kind = first["type"]
    field = _field_of(first["loc"], kind)
    if field is not None and field.split(".")[0:2] == ["prices", "models"]:
        field = "prices.models"
    hint = "See config.example.yaml for every key and its range."
    if kind == "extra_forbidden":
        message = "the config has a key that is not known"
        field = _unknown_key_path(first["loc"]) or field
    elif kind == "missing":
        message = "the config lacks a required key"
    else:
        message = "the config has an invalid value"
        if field == "harness.enabled":
            hint = 'Use auto, "on" or "off"; quote on/off, as YAML reads them as booleans.'
    return _invalid(message, field, hint=hint)


def validate_document(document: dict[str, Any], home: str) -> Settings:
    """The settings of a merged document; raises ``CliError`` (``config_invalid``)."""
    try:
        return Settings.model_validate(document, context={"home": home})
    except ValidationError as exc:
        raise _from_validation(exc) from None


def default_roots(env: Mapping[str, str], home: str) -> tuple[str, ...]:
    roots = [f"{home.rstrip('/')}/.claude/projects"]
    custom = env.get("CLAUDE_CONFIG_DIR", "")
    if custom.startswith("/") and "\x00" not in custom:
        roots.append(f"{os.path.normpath(custom)}/projects")
    return tuple(dict.fromkeys(roots))


def resolve_settings(settings: Settings, gateway: Gateway, env: Mapping[str, str]) -> Settings:
    """Fill the computed defaults and apply the environment: transcript roots, the machine's
    time zone, the OS user name, the price table merged over the bundled one, and
    ``SESSION_AUDIT_TIMEOUT`` and ``SESSION_AUDIT_MODEL`` over ``retro.timeout`` and
    ``retro.model``. An empty variable counts as unset. Raises ``CliError`` for an invalid
    variable (its ``field`` is the variable's name)."""
    home = home_dir(env)
    given = settings.model_fields_set
    update: dict[str, Any] = {}
    if "transcript_roots" not in given:
        update["transcript_roots"] = default_roots(env, home)
    if "time_zone" not in given:
        machine = gateway.machine_time_zone(env)
        try:
            update["time_zone"] = parse_time_zone(machine, field="time_zone").key
        except GrammarError:
            update["time_zone"] = "UTC"
    if "name" not in settings.me.model_fields_set:
        update["me"] = settings.me.model_copy(update={"name": normalize_user(os_user())})
    if "prices" in given:
        update["prices"] = PriceTable(
            as_of=settings.prices.as_of or BUNDLED_PRICES.as_of,
            models={**BUNDLED_PRICES.models, **settings.prices.models},
        )
    retro: dict[str, Any] = {}
    timeout = env.get(ENV_TIMEOUT, "")
    if timeout:
        try:
            retro["timeout"] = parse_duration(
                timeout,
                field=ENV_TIMEOUT,
                maximum=RETRO_TIMEOUT_MAX,
                minimum=RETRO_TIMEOUT_MIN,
            )
        except GrammarError:
            raise _invalid(
                "an environment variable is invalid", ENV_TIMEOUT, "Use 1s to 2h, such as 20m."
            ) from None
    model = env.get(ENV_MODEL, "")
    if model:
        try:
            retro["model"] = check_model(model, ENV_MODEL, launch=True)
        except CliError:
            raise _invalid(
                "an environment variable is invalid",
                ENV_MODEL,
                "Give a model name of 1 to 128 characters.",
            ) from None
    if retro:
        update["retro"] = settings.retro.model_copy(update=retro)
    return settings.model_copy(update=update) if update else settings


def load_config(
    gateway: Gateway, env: Mapping[str, str], config: str | None = None
) -> LoadedConfig:
    """Load the config for one run. Never raises ``CliError``: an invalid config comes back as
    ``LoadedConfig.error`` with the (resolved) default settings, so a command that can work
    without a config (``validate``) still can."""
    home = home_dir(env)
    reader = _Reader(gateway, home)
    try:
        paths = config_paths(env, config)
        document = reader.read(paths.file, "config")
        if document is None:
            if paths.explicit and not any(i.subject == "config" for i in reader.issues):
                raise _invalid("the config file does not exist", "--config")
            document = {}
        if paths.dropin_dir is not None:
            for path in reader.dropins(paths.dropin_dir):
                extra = reader.read(path, "dropin")
                if extra is not None:
                    document = merge_documents(document, extra)
        settings = resolve_settings(validate_document(document, home), gateway, env)
        return LoadedConfig(settings, tuple(dict.fromkeys(reader.issues)))
    except CliError as error:
        defaults = _defaults(gateway, env)
        return LoadedConfig(defaults, tuple(dict.fromkeys(reader.issues)), error)


def _defaults(gateway: Gateway, env: Mapping[str, str]) -> Settings:
    """The built-in defaults, ignoring an invalid environment variable too."""
    clean = {k: v for k, v in env.items() if k not in (ENV_TIMEOUT, ENV_MODEL)}
    return resolve_settings(Settings(), gateway, clean)
