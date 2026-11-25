"""Config loading: drop-ins, precedence, validation, hostile YAML, the ownership matrix.

Most tests run on the ``FakeGateway`` file tree (so a file can belong to someone else); the ones
that need a real kernel (a symlink, a real mode, the real gateway's reads) use the private HOME.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from session_audit.config import (
    BUNDLED_PRICES,
    MAX_DROPINS,
    MAX_FILE_BYTES,
    LoadedConfig,
    Settings,
    load_config,
    profile_issues,
)
from session_audit.gateway import SystemGateway
from tests.fake_gateway import FakeGateway

ROOT = Path(__file__).resolve().parent.parent
HOME = "/home/u"
CONFIG_DIR = f"{HOME}/.config/session-audit"
CONFIG = f"{CONFIG_DIR}/config.yaml"
DROPINS = f"{CONFIG_DIR}/config.d"
ENV = {"HOME": HOME}
SECRET = "SEKRET-VALUE-9f3a"


def tree() -> FakeGateway:
    gw = FakeGateway()
    gw.add_dir("/", uid=0, mode=0o755)
    gw.add_dir("/home", uid=0, mode=0o755)
    gw.add_dir(HOME, mode=0o700)
    gw.add_dir(f"{HOME}/.config", mode=0o700)
    gw.add_dir(CONFIG_DIR, mode=0o700)
    return gw


def load(gw: FakeGateway, env: dict[str, str] | None = None, config: str | None = None):
    return load_config(gw, ENV if env is None else env, config)


def write(gw: FakeGateway, text: str, path: str = CONFIG) -> None:
    gw.add_file(path, text)


def ok(gw: FakeGateway, env: dict[str, str] | None = None) -> Settings:
    loaded = load(gw, env)
    assert loaded.error is None, loaded.error
    return loaded.settings


def field_of(loaded: LoadedConfig) -> str | None:
    assert loaded.error is not None
    assert loaded.error.code == "config_invalid" and loaded.error.exit_code == 2
    return loaded.error.details.get("field")


# --- defaults and the example ------------------------------------------------------------------


def test_no_config_gives_the_documented_defaults() -> None:
    s = ok(tree())
    assert s.ingest.budget == timedelta(seconds=300) and s.ingest.file_budget == timedelta(
        seconds=60
    )
    assert s.retro.timeout == timedelta(minutes=20) and s.retro.max_turns == 12
    assert s.retention.sessions == timedelta(days=400) and s.limits.retros_per_day == 5
    assert s.digest.max_bytes == 200 * 1024 and s.digest.session_max_bytes == 20 * 1024
    assert s.retro.env_allowlist == ("PATH", "HOME", "USER", "LANG", "TERM")
    assert s.retro.sandbox == "required" and s.harness.enabled == "auto"
    assert s.transcript_roots == (f"{HOME}/.claude/projects",)
    assert s.prices.as_of == BUNDLED_PRICES.as_of and s.schedule.retro is None


def test_default_roots_add_claude_config_dir_when_absolute() -> None:
    s = ok(tree(), {**ENV, "CLAUDE_CONFIG_DIR": "/x/cc"})
    assert s.transcript_roots == (f"{HOME}/.claude/projects", "/x/cc/projects")
    s = ok(tree(), {**ENV, "CLAUDE_CONFIG_DIR": "rel"})
    assert s.transcript_roots == (f"{HOME}/.claude/projects",)


def test_the_example_config_loads_to_the_defaults() -> None:
    gw = tree()
    write(gw, (ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert load(gw).error is None
    assert ok(gw) == ok(tree())


def test_machine_time_zone_is_the_default_and_an_invalid_one_falls_back() -> None:
    gw = tree()
    gw.time_zone = "Europe/Berlin"
    assert ok(gw).time_zone == "Europe/Berlin"
    gw.time_zone = "Not/AZone"
    assert ok(gw).time_zone == "UTC"
    gw.time_zone = None
    assert ok(gw).time_zone == "UTC"


def test_me_name_defaults_to_the_os_user_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("session_audit.config.os_user", lambda: "a b$c")
    assert ok(tree()).me.name == "a_b_c"
    gw = tree()
    write(gw, "me:\n  name: given\n")
    assert ok(gw).me.name == "given"


# --- locations -----------------------------------------------------------------------------------


def test_xdg_config_home_is_respected_when_absolute() -> None:
    gw = tree()
    gw.add_dir("/xdg", mode=0o700)
    gw.add_dir("/xdg/session-audit", mode=0o700)
    write(gw, "limits:\n  retros_per_day: 7\n", "/xdg/session-audit/config.yaml")
    assert ok(gw, {**ENV, "XDG_CONFIG_HOME": "/xdg"}).limits.retros_per_day == 7
    write(gw, "limits:\n  retros_per_day: 9\n")
    assert ok(gw, {**ENV, "XDG_CONFIG_HOME": "relative"}).limits.retros_per_day == 9


def test_explicit_config_reads_only_that_file_and_no_drop_ins() -> None:
    gw = tree()
    gw.add_file(f"{HOME}/mine.yaml", "limits:\n  retros_per_day: 3\n")
    gw.add_dir(DROPINS, mode=0o700)
    write(gw, "limits:\n  retros_per_day: 4\n", f"{DROPINS}/a.yaml")
    loaded = load(gw, config=f"{HOME}/mine.yaml")
    assert loaded.error is None and loaded.settings.limits.retros_per_day == 3
    assert f"{DROPINS}/a.yaml" not in gw.reads


def test_explicit_config_that_is_missing_is_an_error_naming_the_flag() -> None:
    loaded = load(tree(), config=f"{HOME}/nope.yaml")
    assert field_of(loaded) == "--config"


# --- drop-ins and precedence --------------------------------------------------------------------


def test_drop_ins_merge_in_lexical_order_maps_merge_lists_replace_hooks_append() -> None:
    gw = tree()
    write(
        gw,
        "retro:\n  max_turns: 5\n  tools: [Read]\nhooks:\n  post-retro:\n    - cmd: one\n",
    )
    gw.add_dir(DROPINS, mode=0o700)
    write(
        gw,
        "retro:\n  max_sessions: 9\n  tools: [Read, Bash]\nhooks:\n  post-retro:\n    - cmd: three\n",
        f"{DROPINS}/20-b.yaml",
    )
    write(
        gw,
        "retro:\n  max_turns: 6\nhooks:\n  post-retro:\n    - cmd: two\n",
        f"{DROPINS}/10-a.yaml",
    )
    write(gw, "retro:\n  max_turns: 99\n", f"{DROPINS}/ignored.yml")
    write(gw, "retro:\n  max_turns: 98\n", f"{DROPINS}/.hidden.yaml")
    s = ok(gw)
    assert (s.retro.max_turns, s.retro.max_sessions) == (6, 9)
    assert s.retro.tools == ("Read", "Bash")
    assert [h.cmd for h in s.hooks.post_retro] == ["one", "two", "three"]


def test_a_later_drop_in_wins_a_scalar_and_can_clear_the_schedule() -> None:
    gw = tree()
    write(gw, "schedule:\n  retro:\n    scope:\n      since: 7d\n")
    gw.add_dir(DROPINS, mode=0o700)
    write(gw, "schedule:\n  retro: null\n", f"{DROPINS}/a.yaml")
    assert ok(gw).schedule.retro is None


def test_too_many_drop_ins_are_refused() -> None:
    gw = tree()
    gw.add_dir(DROPINS, mode=0o700)
    for i in range(MAX_DROPINS + 1):
        write(gw, "{}\n", f"{DROPINS}/{i:03}.yaml")
    assert load(gw).error is not None


def test_env_timeout_and_model_beat_the_config_and_empty_means_unset() -> None:
    gw = tree()
    write(gw, "retro:\n  timeout: 10m\n  model: from-config\n")
    s = ok(gw, {**ENV, "SESSION_AUDIT_TIMEOUT": "90s", "SESSION_AUDIT_MODEL": "from-env"})
    assert (s.retro.timeout, s.retro.model) == (timedelta(seconds=90), "from-env")
    s = ok(gw, {**ENV, "SESSION_AUDIT_TIMEOUT": "", "SESSION_AUDIT_MODEL": ""})
    assert (s.retro.timeout, s.retro.model) == (timedelta(minutes=10), "from-config")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SESSION_AUDIT_TIMEOUT", SECRET),
        ("SESSION_AUDIT_TIMEOUT", "0s"),
        ("SESSION_AUDIT_TIMEOUT", "3h"),
        ("SESSION_AUDIT_MODEL", "-" + SECRET),
        ("SESSION_AUDIT_MODEL", "x" * 129),
    ],
)
def test_an_invalid_env_variable_names_itself_and_never_its_value(name: str, value: str) -> None:
    loaded = load(tree(), {**ENV, name: value})
    assert field_of(loaded) == name
    assert loaded.error is not None
    assert value not in loaded.error.message + str(loaded.error.hint) + str(loaded.error.details)
    assert loaded.settings.retro.timeout == timedelta(minutes=20)  # defaults still usable


def test_profiles_skills_prices_and_redaction_cannot_come_from_the_environment() -> None:
    env = {
        **ENV,
        "SESSION_AUDIT_PERMISSION_PROFILE": "/evil",
        "SESSION_AUDIT_PROFILE": "/evil",
        "SESSION_AUDIT_SKILL": "evil:skill",
        "SESSION_AUDIT_PRICES": "{}",
        "SESSION_AUDIT_REDACTION": "off",
        "SESSION_AUDIT_MASK_ENV": "X",
        "SESSION_AUDIT_SANDBOX": "off",
        "SESSION_AUDIT_PLUGIN_DIRS": "/evil",
    }
    assert ok(tree(), env) == ok(tree())


# --- validation ----------------------------------------------------------------------------------


def test_every_key_of_a_full_config_is_applied() -> None:
    gw = tree()
    write(
        gw,
        """
transcript_roots: [~/a, /b]
time_zone: America/New_York
ingest: {budget: 2m, file_budget: 30, fail_threshold: 4}
harness: {enabled: "on", claude_session_cmd: cs}
prices:
  as_of: 2026-02-03
  models:
    my-model: {input: 1, output: 2, cache_read: 0.5, cache_write: 1.5}
cost: {mismatch_margin: 0.2}
retention: {sessions: 10d, retros: 5d, digests: 1d, log: 2d}
limits: {retros_per_day: 2}
retro:
  skill: p:s
  skill_marker: mark
  plugin_dirs: [/p1, /p2]
  model: m1
  timeout: 5m
  max_turns: 3
  max_sessions: 4
  quiet_after: 10m
  permission_profile: ~/prof.json
  sandbox: preferred
  setting_sources: [user, local]
  env_allowlist: [PATH]
  tools: [Read]
  remove_run_transcripts: false
digest: {max_bytes: 1MB, session_max_bytes: 5000B, prompt_chars: 30, assistant_chars: 40, arg_chars: 50, error_chars: 60}
redaction: {mask_env: [TOK], extra_token_prefixes: [abc-]}
schedule:
  retro: {weekday: 0, hour: 23, scope: {since: 7d, until: 1d, labels: ["a=b"], project: /p, kind: review, outcome: done}}
hooks:
  pre-retro: [{cmd: pre, timeout: 5}]
  post-attention: [{cmd: att, async: true}]
me: {name: me}
""",
    )
    s = ok(gw)
    assert s.transcript_roots == (f"{HOME}/a", "/b") and s.time_zone == "America/New_York"
    assert s.ingest.budget == timedelta(minutes=2) and s.ingest.file_budget == timedelta(seconds=30)
    assert s.harness.enabled == "on" and s.cost.mismatch_margin == 0.2
    assert s.retention.sessions == timedelta(days=10) and s.limits.retros_per_day == 2
    assert (
        s.retro.plugin_dirs == ("/p1", "/p2") and s.retro.permission_profile == f"{HOME}/prof.json"
    )
    assert s.retro.quiet_after == timedelta(minutes=10) and s.retro.remove_run_transcripts is False
    assert s.digest.max_bytes == 1024 * 1024 and s.digest.session_max_bytes == 5000
    assert s.redaction.mask_env == ("TOK",) and s.me.name == "me"
    retro = s.schedule.retro
    assert retro is not None and (retro.weekday, retro.hour) == (0, 23)
    assert (retro.scope.since, retro.scope.until, retro.scope.kind) == ("7d", "1d", "review")
    assert s.hooks.pre_retro[0].timeout == timedelta(seconds=5)
    assert s.hooks.post_attention[0].run_async is True and s.hooks.post_retro == ()
    assert s.hooks.post_attention[0].timeout == timedelta(seconds=30)


BAD = [
    # (yaml, the key reported)
    ("ingest: {budget: 3601}", "ingest.budget"),
    ("ingest: {budget: 0}", "ingest.budget"),
    ("ingest: {budget: 1h}", "ingest.budget"),
    ("ingest: {file_budget: 601s}", "ingest.file_budget"),
    ("ingest: {fail_threshold: 0}", "ingest.fail_threshold"),
    ("ingest: {fail_threshold: true}", "ingest.fail_threshold"),
    ("ingest: {fail_threshold: '3'}", "ingest.fail_threshold"),
    ("ingest: {fail_threshold: 3.0}", "ingest.fail_threshold"),
    ("harness: {enabled: off}", "harness.enabled"),
    ("harness: {enabled: maybe}", "harness.enabled"),
    ("harness: {claude_session_cmd: 'a b'}", "harness.claude_session_cmd"),
    ("cost: {mismatch_margin: 1.5}", "cost.mismatch_margin"),
    ("cost: {mismatch_margin: .nan}", "cost.mismatch_margin"),
    ("cost: {mismatch_margin: .inf}", "cost.mismatch_margin"),
    ("cost: {mismatch_margin: true}", "cost.mismatch_margin"),
    ("retention: {sessions: 3651d}", "retention.sessions"),
    ("retention: {log: 0d}", "retention.log"),
    ("retention: {log: 1h}", "retention.log"),
    ("limits: {retros_per_day: 1001}", "limits.retros_per_day"),
    ("retro: {timeout: 999ms}", "retro.timeout"),
    ("retro: {timeout: 7201}", "retro.timeout"),
    ("retro: {quiet_after: 7d}", "retro.quiet_after"),
    ("retro: {quiet_after: 8d}", "retro.quiet_after"),
    ("retro: {max_turns: 1001}", "retro.max_turns"),
    ("retro: {max_sessions: 501}", "retro.max_sessions"),
    ("retro: {skill: bare}", "retro.skill"),
    ("retro: {sandbox: none}", "retro.sandbox"),
    ("retro: {sandbox: false}", "retro.sandbox"),
    ("retro: {setting_sources: [user, user]}", "retro.setting_sources"),
    ("retro: {setting_sources: [other]}", "retro.setting_sources"),
    ("retro: {plugin_dirs: [relative]}", "retro.plugin_dirs"),
    ("retro: {plugin_dirs: [/a, /a]}", "retro.plugin_dirs"),
    ("retro: {plugin_dirs: [/1, /2, /3, /4, /5, /6, /7, /8, /9]}", "retro.plugin_dirs"),
    ("retro: {permission_profile: relative.json}", "retro.permission_profile"),
    ("retro: {model: -x}", "retro.model"),
    ("retro: {tools: []}", "retro.tools"),
    ("retro: {tools: [read]}", "retro.tools"),
    ("retro: {env_allowlist: ['A B']}", "retro.env_allowlist"),
    ("retro: {skill_marker: ''}", "retro.skill_marker"),
    ("retro: {remove_run_transcripts: yes_}", "retro.remove_run_transcripts"),
    ("digest: {max_bytes: 0}", "digest.max_bytes"),
    ("digest: {max_bytes: 1.5KB}", "digest.max_bytes"),
    ("digest: {max_bytes: 2GB}", "digest.max_bytes"),
    ("digest: {max_bytes: 1048577MB}", "digest.max_bytes"),
    ("digest: {session_max_bytes: 01KB}", "digest.session_max_bytes"),
    ("digest: {prompt_chars: 19}", "digest.prompt_chars"),
    ("digest: {error_chars: 65537}", "digest.error_chars"),
    ("redaction: {mask_env: ['1X']}", "redaction.mask_env"),
    ("redaction: {extra_token_prefixes: [ab]}", "redaction.extra_token_prefixes"),
    ("redaction: {extra_token_prefixes: ['a b c']}", "redaction.extra_token_prefixes"),
    ("me: {name: 'a b'}", "me.name"),
    ("time_zone: Nowhere/Land", "time_zone"),
    ("time_zone: localtime", "time_zone"),
    ("transcript_roots: []", "transcript_roots"),
    ("transcript_roots: [relative]", "transcript_roots"),
    ("transcript_roots: /a", "transcript_roots"),
    ("prices: {as_of: 2026-13-45}", "prices.as_of"),
    ("prices: {as_of: yesterday}", "prices.as_of"),
    (
        "prices: {models: {'bad name': {input: 1, output: 1, cache_read: 1, cache_write: 1}}}",
        "prices.models",
    ),
    (
        "prices: {models: {m: {input: -1, output: 1, cache_read: 1, cache_write: 1}}}",
        "prices.models",
    ),
    ("prices: {models: {m: {input: 1, output: 1, cache_read: 1}}}", "prices.models"),
    (
        "prices: {models: {m: {input: 1, output: 1, cache_read: 1, cache_write: 1, x: 1}}}",
        "prices.models",
    ),
    ("schedule: {retro: {scope: {}}}", "schedule.retro.scope"),
    ("schedule: {retro: {scope: {since: 2h30m}}}", "schedule.retro.scope"),
    ("schedule: {retro: {scope: {since: 0d}}}", "schedule.retro.scope"),
    ("schedule: {retro: {scope: {since: 1d, until: 2d}}}", "schedule.retro.scope"),
    ("schedule: {retro: {scope: {since: 1d, labels: [nope]}}}", "schedule.retro.scope"),
    ("schedule: {retro: {scope: {since: 1d, kind: Bad}}}", "schedule.retro.scope"),
    ("schedule: {retro: {weekday: 7, scope: {since: 1d}}}", "schedule.retro.weekday"),
    ("schedule: {retro: {hour: 24, scope: {since: 1d}}}", "schedule.retro.hour"),
    ("schedule: {retro: {weekday: 1}}", "schedule.retro.scope"),
    ("hooks: {pre-retro: [{cmd: x, async: true}]}", "hooks.pre-retro"),
    ("hooks: {post-retro: [{cmd: ''}]}", "hooks.post-retro"),
    ("hooks: {post-retro: [{timeout: 5}]}", "hooks.post-retro"),
    ("hooks: {post-retro: [{cmd: x, timeout: 601}]}", "hooks.post-retro"),
    ("hooks: {post-retro: [{cmd: x, async: 1}]}", "hooks.post-retro"),
    ("hooks: {post-retro: cmd}", "hooks.post-retro"),
    ("hooks: {other: []}", "hooks"),
    ("retro: []", "retro"),
    ("retro:\n", "retro"),
]


@pytest.mark.parametrize(("text", "key"), BAD)
def test_an_invalid_value_names_the_key_and_never_the_value(text: str, key: str) -> None:
    gw = tree()
    write(gw, text.replace("}", "}") + "\n")
    loaded = load(gw)
    assert field_of(loaded) == key
    assert loaded.error is not None and loaded.settings == ok(tree())


def test_a_key_never_prints_its_value_even_a_secret_one() -> None:
    for text in (
        f"retro: {{max_turns: {SECRET}}}",
        f"retro: {{skill_marker: [{SECRET}]}}",
        f"hooks: {{post-retro: [{{cmd: x, timeout: {SECRET}}}]}}",
        f"{SECRET}: 1",
        f"retro: {{{SECRET}: 1}}",
        f"prices: {{models: {{{SECRET}: {SECRET}}}}}",
        f"time_zone: {SECRET}",
    ):
        gw = tree()
        write(gw, text + "\n")
        error = load(gw).error
        assert error is not None
        shown = error.message + str(error.hint) + str(error.details) + repr(error.envelope())
        assert SECRET not in shown, text


def test_unknown_keys_are_reported_as_their_parent() -> None:
    gw = tree()
    write(gw, "mystery: 1\n")
    assert field_of(load(gw)) is None
    write(gw, "retro:\n  nope: 1\n")
    assert field_of(load(gw)) == "retro"
    write(gw, "repos: {}\n")
    assert load(gw).error is not None  # there is no repos layer


def test_a_valid_value_at_each_documented_boundary_is_accepted() -> None:
    gw = tree()
    write(
        gw,
        "ingest: {budget: 3600s, file_budget: 10m}\nretention: {sessions: 3650d}\n"
        "retro: {timeout: 2h_}\n".replace("2h_", "7200")
        + "digest: {max_bytes: 1024MB}\n"
        "hooks: {post-retro: [{cmd: x, timeout: 600}]}\nlimits: {retros_per_day: 1000}\n",
    )
    s = ok(gw)
    assert s.ingest.budget == timedelta(hours=1) and s.retro.timeout == timedelta(hours=2)
    assert s.digest.max_bytes == 1 << 30
    assert s.retention.sessions == timedelta(days=3650)


def test_a_scope_with_no_until_and_a_since_in_every_unit() -> None:
    for since in ("90s", "30m", "12h", "7d"):
        gw = tree()
        write(gw, f"schedule: {{retro: {{scope: {{since: {since}}}}}}}\n")
        retro = ok(gw).schedule.retro
        assert retro is not None and retro.scope.since == since and retro.weekday == 1
        assert retro.hour == 9


# --- prices --------------------------------------------------------------------------------------


def test_bundled_prices_are_complete_and_dated() -> None:
    assert BUNDLED_PRICES.as_of is not None and len(BUNDLED_PRICES.models) >= 5
    for price in BUNDLED_PRICES.models.values():
        assert price.cache_write_1h is not None and price.input > 0


def test_config_prices_extend_and_replace_the_bundled_table() -> None:
    gw = tree()
    write(
        gw,
        "prices:\n  models:\n    claude-haiku-4-5: {input: 9, output: 9, cache_read: 9, cache_write: 9}\n"
        "    zzz: {input: 1, output: 2, cache_read: 3, cache_write: 4}\n",
    )
    p = ok(gw).prices
    assert p.as_of == BUNDLED_PRICES.as_of  # no as_of given: the bundled date stays
    assert p.models["claude-haiku-4-5"].input == 9 and p.models["claude-opus-4-5"].input == 5
    zzz = p.models["zzz"]
    assert zzz.cache_write_1h is None and zzz.cache_write_1h_or_default == 4
    write(gw, "prices:\n  as_of: 2026-01-01\n")
    assert ok(gw).prices.as_of == "2026-01-01"


def test_price_lookup_is_exact_then_longest_prefix() -> None:
    p = BUNDLED_PRICES
    assert p.lookup("claude-opus-4-5") is p.models["claude-opus-4-5"]
    assert p.lookup("claude-opus-4-5-20251101") is p.models["claude-opus-4-5"]
    assert p.lookup("claude-opus-4-20250514") is p.models["claude-opus-4"]
    assert p.lookup("claude-opus-3") is None and p.lookup("") is None


# --- hostile YAML --------------------------------------------------------------------------------

HOSTILE_YAML = {
    "alias": "a: &x 1\nb: *x\n",
    "anchor only": "limits: &x {retros_per_day: 1}\n",
    "billion laughs": "a: &a [x, x]\nb: &b [*a, *a]\nc: &c [*b, *b]\nd: &d [*c, *c]\ne: [*d, *d]\n",
    "cycle": "a: &a [*a]\n",
    "merge key": "retro: {<<: {max_turns: 1}}\n",
    "duplicate key": "limits: {retros_per_day: 1}\nlimits: {retros_per_day: 2}\n",
    "duplicate nested": "retro: {max_turns: 1, max_turns: 2}\n",
    "integer key": "1: x\n",
    "null key": "~: x\n",
    "python tag": "a: !!python/object/apply:os.system [true]\n",
    "set tag": "a: !!set {x}\n",
    "binary tag": "a: !!binary aGk=\n",
    "timestamp tag": "a: !!timestamp 2020-01-01\n",
    "deep": "a: " + "[" * 500 + "]" * 500 + "\n",
    "many nodes": "a: [" + ",".join(["1"] * 30000) + "]\n",
    "list top": "- a\n- b\n",
    "scalar top": "just text\n",
    "bad syntax": "a: [unclosed\n",
    "tab indent": "a:\n\tb: 1\n",
    "control char": "a: \x07\n",
    "multi document": "a: 1\n---\nb: 2\n",
    "lone surrogate escape": 'retro: {skill_marker: "\\ud800"}\n',
    "nul escape": 'hooks: {post-retro: [{cmd: "a\\0b"}]}\n',
    "huge int": "ingest: {fail_threshold: " + "9" * 5000 + "}\n",
}


@pytest.mark.parametrize("name", sorted(HOSTILE_YAML))
def test_hostile_yaml_is_refused_with_a_fixed_message(name: str) -> None:
    gw = tree()
    write(gw, HOSTILE_YAML[name])
    loaded = load(gw)
    assert loaded.error is not None and loaded.error.code == "config_invalid"
    assert loaded.error.exit_code == 2
    assert loaded.settings == ok(tree())


def test_a_billion_laughs_document_is_refused_quickly() -> None:
    gw = tree()
    write(gw, HOSTILE_YAML["billion laughs"])
    assert load(gw).error is not None  # refused at the first alias: nothing is expanded


def test_a_file_over_one_mib_is_refused_and_exactly_one_mib_is_read() -> None:
    gw = tree()
    comment = "#" * (MAX_FILE_BYTES - 1) + "\n"
    write(gw, comment)
    assert load(gw).error is None and len(comment) == MAX_FILE_BYTES
    write(gw, comment + "#")
    assert load(gw).error is not None


def test_non_utf8_and_a_bom_less_latin1_file_are_refused() -> None:
    gw = tree()
    gw.add_file(CONFIG, b"me: {name: caf\xe9}\n")
    assert load(gw).error is not None
    gw.add_file(CONFIG, b"\xff\xfe#\x00")
    assert load(gw).error is not None


def test_an_empty_file_and_a_comment_only_file_are_the_defaults() -> None:
    for text in ("", "# nothing\n", "---\n", "{}\n"):
        gw = tree()
        write(gw, text)
        assert ok(gw) == ok(tree())


def test_a_date_stays_a_string_and_yaml_booleans_are_not_guessed() -> None:
    gw = tree()
    write(gw, "prices:\n  as_of: 2026-01-01\n")
    assert ok(gw).prices.as_of == "2026-01-01"
    write(gw, "retro:\n  sandbox: off\n")
    assert field_of(load(gw)) == "retro.sandbox"
    write(gw, "retro:\n  sandbox: 'off'\n")
    assert ok(gw).retro.sandbox == "off"


def test_a_config_error_in_a_drop_in_is_a_config_error() -> None:
    gw = tree()
    gw.add_dir(DROPINS, mode=0o700)
    write(gw, "retro: {max_turns: 0}\n", f"{DROPINS}/a.yaml")
    assert field_of(load(gw)) == "retro.max_turns"


# --- ownership -----------------------------------------------------------------------------------


def issues(gw: FakeGateway) -> set[tuple[str, str]]:
    return {(i.subject, i.kind) for i in load(gw).issues}


def populated() -> FakeGateway:
    gw = tree()
    write(gw, "limits: {retros_per_day: 2}\n")
    gw.add_dir(DROPINS, mode=0o700)
    write(gw, "limits: {retros_per_day: 3}\n", f"{DROPINS}/a.yaml")
    return gw


def test_a_clean_tree_has_no_issue() -> None:
    assert issues(populated()) == set()
    assert load(populated()).safe


def retarget(gw: FakeGateway, path: str, **changes: Any) -> None:
    entry = gw.files[path]
    for name, value in changes.items():
        setattr(entry, name, value)


@pytest.mark.parametrize(
    ("path", "subject"),
    [
        (CONFIG, "config"),
        (DROPINS, "dropins"),
        (f"{DROPINS}/a.yaml", "dropin"),
        (CONFIG_DIR, "directory"),
        (f"{HOME}/.config", "directory"),
        (HOME, "directory"),
    ],
)
@pytest.mark.parametrize(
    ("change", "kind"),
    [
        ({"uid": 4242}, "not_owned"),
        ({"mode": 0o620}, "writable"),
        ({"mode": 0o602}, "writable"),
    ],
)
def test_ownership_and_mode_matrix(
    path: str, subject: str, change: dict[str, Any], kind: str
) -> None:
    gw = populated()
    retarget(gw, path, **change)
    assert (subject, kind) in issues(gw)


@pytest.mark.parametrize(
    ("path", "subject"),
    [
        (CONFIG, "config"),
        (DROPINS, "dropins"),
        (f"{DROPINS}/a.yaml", "dropin"),
        (CONFIG_DIR, "directory"),
        (f"{HOME}/.config", "directory"),
    ],
)
def test_a_symlink_anywhere_in_the_chain_is_an_issue_and_is_not_read(
    path: str, subject: str
) -> None:
    gw = populated()
    gw.add_file("/elsewhere/real.yaml", "limits: {retros_per_day: 500}\n")
    entry = gw.files[path]
    gw.files[path] = type(entry)("symlink", target="/elsewhere/real.yaml", uid=entry.uid)
    loaded = load(gw)
    assert (subject, "symlink") in {(i.subject, i.kind) for i in loaded.issues}
    assert "/elsewhere/real.yaml" not in gw.reads
    if path in (CONFIG, f"{DROPINS}/a.yaml", DROPINS):
        assert loaded.error is None and loaded.settings.limits.retros_per_day != 500


def test_a_non_regular_file_is_an_issue_and_is_not_read() -> None:
    gw = tree()
    gw.add_dir(CONFIG, mode=0o700)
    assert ("config", "not_regular") in issues(gw)


def test_a_root_owned_ancestor_above_home_is_not_checked() -> None:
    gw = populated()
    retarget(gw, "/home", mode=0o777)
    retarget(gw, "/", uid=0, mode=0o777)
    assert issues(gw) == set()


def test_home_itself_may_be_a_symlink_but_not_group_writable() -> None:
    gw = tree()
    gw.files["/real-home"] = gw.files.pop(HOME)
    gw.add_symlink(HOME, "/real-home")
    write(gw, "{}\n")
    assert issues(gw) == set()
    retarget(gw, "/real-home", mode=0o775)
    assert ("directory", "writable") in issues(gw)


def test_a_config_outside_home_is_checked_up_to_the_first_root_owned_directory() -> None:
    gw = tree()
    gw.add_dir("/srv", uid=0, mode=0o1777)
    gw.add_dir("/srv/mine", mode=0o700)
    gw.add_file("/srv/mine/c.yaml", "{}\n")
    assert load(gw, config="/srv/mine/c.yaml").issues == ()
    retarget(gw, "/srv/mine", mode=0o770)
    assert ("directory", "writable") in {
        (i.subject, i.kind) for i in load(gw, config="/srv/mine/c.yaml").issues
    }
    gw.add_dir("/srv/mine/sub", uid=4242, mode=0o700)
    gw.add_file("/srv/mine/sub/c.yaml", "{}\n", uid=4242)
    kinds = {(i.subject, i.kind) for i in load(gw, config="/srv/mine/sub/c.yaml").issues}
    assert ("config", "not_owned") in kinds and ("directory", "not_owned") in kinds


def test_a_missing_config_directory_is_not_an_issue() -> None:
    gw = FakeGateway()
    gw.add_dir(HOME, mode=0o700)
    loaded = load(gw)
    assert loaded.error is None and loaded.issues == ()


def test_an_unsafe_file_is_still_applied_but_reported() -> None:
    gw = populated()
    retarget(gw, CONFIG, mode=0o666)
    loaded = load(gw)
    assert loaded.settings.limits.retros_per_day == 3 and not loaded.safe


def test_issue_texts_are_fixed_and_hold_no_path() -> None:
    gw = populated()
    retarget(gw, CONFIG, uid=7)
    (issue,) = load(gw).issues
    assert issue.describe() == "the config file is not owned by you"


def test_the_profile_file_follows_the_same_rule() -> None:
    gw = tree()
    gw.add_file(f"{HOME}/p.json", "{}", mode=0o600)
    assert profile_issues(gw, f"{HOME}/p.json", HOME) == []
    retarget(gw, f"{HOME}/p.json", mode=0o664)
    assert [(i.subject, i.kind) for i in profile_issues(gw, f"{HOME}/p.json", HOME)] == [
        ("profile", "writable")
    ]
    retarget(gw, f"{HOME}/p.json", uid=9, mode=0o600)
    assert [i.kind for i in profile_issues(gw, f"{HOME}/p.json", HOME)] == ["not_owned"]
    gw.add_symlink(f"{HOME}/l.json", f"{HOME}/p.json")
    assert [i.kind for i in profile_issues(gw, f"{HOME}/l.json", HOME)] == ["symlink"]
    gw.add_dir(f"{HOME}/d", mode=0o770)
    gw.add_file(f"{HOME}/d/p.json", "{}")
    assert [(i.subject, i.kind) for i in profile_issues(gw, f"{HOME}/d/p.json", HOME)] == [
        ("directory", "writable")
    ]
    assert profile_issues(gw, f"{HOME}/missing.json", HOME) == []


# --- the real gateway ----------------------------------------------------------------------------


@pytest.fixture
def real_home(_private_environment: Path) -> Path:
    config = _private_environment / ".config" / "session-audit"
    config.mkdir(mode=0o700)
    return config


def real_load(config: str | None = None) -> LoadedConfig:
    return load_config(SystemGateway(), os.environ, config)


def test_real_files_with_a_private_mode_load_clean(real_home: Path) -> None:
    (real_home / "config.yaml").write_text("limits: {retros_per_day: 8}\n", encoding="utf-8")
    os.chmod(real_home / "config.yaml", 0o600)
    loaded = real_load()
    assert loaded.error is None and loaded.issues == ()
    assert loaded.settings.limits.retros_per_day == 8


def test_real_group_writable_file_and_symlink_are_found(real_home: Path) -> None:
    target = real_home.parent / "other.yaml"
    target.write_text("limits: {retros_per_day: 500}\n", encoding="utf-8")
    os.chmod(target, 0o600)
    os.symlink(target, real_home / "config.yaml")
    loaded = real_load()
    assert {(i.subject, i.kind) for i in loaded.issues} == {("config", "symlink")}
    assert loaded.settings.limits.retros_per_day == 5
    (real_home / "config.yaml").unlink()
    (real_home / "config.yaml").write_text("{}\n", encoding="utf-8")
    os.chmod(real_home / "config.yaml", 0o660)
    assert {(i.subject, i.kind) for i in real_load().issues} == {("config", "writable")}


def test_real_fifo_is_not_read_and_does_not_block(real_home: Path) -> None:
    os.mkfifo(real_home / "config.yaml", 0o600)
    loaded = real_load()
    assert {(i.subject, i.kind) for i in loaded.issues} == {("config", "not_regular")}


def test_real_drop_in_directory_symlink_is_not_followed(real_home: Path) -> None:
    other = real_home.parent / "elsewhere"
    other.mkdir(mode=0o700)
    (other / "a.yaml").write_text("limits: {retros_per_day: 500}\n", encoding="utf-8")
    os.chmod(other / "a.yaml", 0o600)
    os.symlink(other, real_home / "config.d")
    loaded = real_load()
    assert {(i.subject, i.kind) for i in loaded.issues} == {("dropins", "symlink")}
    assert loaded.settings.limits.retros_per_day == 5


def test_real_read_is_capped(real_home: Path) -> None:
    path = real_home / "config.yaml"
    path.write_bytes(b"#" * (MAX_FILE_BYTES + 1))
    os.chmod(path, 0o600)
    assert real_load().error is not None
    content = SystemGateway().read_file(str(path), 10)
    assert len(content.data) == 10 and content.truncated


def test_real_explicit_relative_config_path(
    real_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (real_home / "x.yaml").write_text("limits: {retros_per_day: 6}\n", encoding="utf-8")
    os.chmod(real_home / "x.yaml", 0o600)
    monkeypatch.chdir(real_home)
    assert real_load("x.yaml").settings.limits.retros_per_day == 6
    assert real_load("./../session-audit/x.yaml").settings.limits.retros_per_day == 6


def test_yaml_loader_is_safe_on_a_real_python_object_tag(real_home: Path) -> None:
    path = real_home / "config.yaml"
    marker = real_home / "pwned"
    path.write_text(f"a: !!python/object/apply:os.system ['touch {marker}']\n", encoding="utf-8")
    os.chmod(path, 0o600)
    assert real_load().error is not None and not marker.exists()
    assert yaml.safe_load("a: 1") == {"a": 1}
