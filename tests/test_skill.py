"""The bundled plugin (issue #30): the skill, its marker, the profile and the manifest.

The skill is the one piece of this tool that is read by a model working on untrusted text, so its
contract with the tool is pinned here: the closed shape it writes, the enums and limits it names,
the literal paths it uses, and the marker rule (a changed skill text needs a new marker and a new
``DIGEST_FORMAT``)."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from session_audit import errors, plugins, redact, retroartifact
from session_audit.fencing import FINDING_CATEGORIES, LIMITS, TARGET_CLASSES
from session_audit.gateway import SystemGateway
from session_audit.launchplan import PROFILE_RELATIVE

from .schemas import OWN_SCHEMA_ROOT, validate

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugin"
SKILL = PLUGIN / "skills" / "audit" / "SKILL.md"
MARKER = PLUGIN / "skills" / "audit" / "references" / "marker.txt"
PROFILE = PLUGIN / PROFILE_RELATIVE
MANIFEST = PLUGIN / ".claude-plugin" / "plugin.json"
PIN = Path(__file__).resolve().parent / "skill_pin.json"


def skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def skill_sha() -> str:
    return hashlib.sha256(SKILL.read_bytes()).hexdigest()


# --- the marker rule ---------------------------------------------------------------------------


def test_the_marker_is_one_line_of_the_documented_shape() -> None:
    raw = MARKER.read_text(encoding="ascii")
    assert raw.endswith("\n") and raw.count("\n") == 1
    assert re.fullmatch(r"[a-z0-9-]{16,64}", raw[:-1], re.ASCII)


def test_the_tool_reads_the_bundled_marker() -> None:
    marker = retroartifact.bundled_marker(SystemGateway(), str(PLUGIN))
    assert marker == MARKER.read_text(encoding="ascii").strip() != ""


def test_the_marker_is_derived_from_the_skill_text() -> None:
    expected = "audit-" + skill_sha()[:24]
    assert MARKER.read_text(encoding="ascii") == expected + "\n", (
        "SKILL.md changed without a new marker. Write this line to "
        f"plugin/skills/audit/references/marker.txt: {expected}"
    )


def test_a_changed_skill_needs_a_new_digest_format() -> None:
    pin = json.loads(PIN.read_text(encoding="ascii"))
    current = str(redact.DIGEST_FORMAT)
    assert pin.get(current) == skill_sha(), (
        f"SKILL.md differs from the text pinned for DIGEST_FORMAT {current}. A changed skill "
        "changes the retro key through DIGEST_FORMAT: bump DIGEST_FORMAT in redact.py (and the "
        "tests that name it), update marker.txt, and ADD a new entry "
        f'"<new format>": "{skill_sha()}" to tests/skill_pin.json. Never edit an older entry.'
    )


def test_the_pin_lists_each_format_once_and_each_skill_once() -> None:
    pin = json.loads(PIN.read_text(encoding="ascii"))
    assert all(re.fullmatch(r"[0-9a-f]{64}", v) for v in pin.values())
    assert len(set(pin.values())) == len(pin)
    assert max(int(k) for k in pin) == redact.DIGEST_FORMAT


# --- the skill text ----------------------------------------------------------------------------


def test_the_skill_has_the_name_the_plugin_loads_it_by() -> None:
    text = skill_text()
    front = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert front is not None
    meta = yaml.safe_load(front.group(1))
    assert meta["name"] == "audit" and meta["description"]


@pytest.mark.parametrize("bad", ["$CSESSION_DIR", "${CSESSION_DIR}", "$HOME", "${HOME}", "~/"])
def test_the_skill_never_names_a_variable_or_a_relative_home_path(bad: str) -> None:
    assert bad not in skill_text()


def test_the_skill_reads_its_two_inputs_and_its_marker_by_literal_path() -> None:
    text = skill_text()
    for name in ("inputs/1/digest.md", "inputs/1/manifest.json", "audit.json"):
        assert name in text
    assert "references/marker.txt" in text
    assert "literal absolute" in text


def test_the_skill_names_the_enums_and_limits_of_the_contract() -> None:
    text = skill_text()
    for name in (*FINDING_CATEGORIES, *TARGET_CLASSES, "low", "medium", "high"):
        assert f"`{name}`" in text, name
    assert str(LIMITS["max_findings"]) in text and str(LIMITS["max_candidates"]) in text
    for name, cap in LIMITS["text_limits"].items():
        assert re.search(rf"`{name}`[^\n]*\b{cap}\b", text), name
    assert "depth" in text and "256 KB" in text and "16" in text


def test_the_skill_names_no_enum_value_the_schema_lacks() -> None:
    defs = json.loads((OWN_SCHEMA_ROOT / "defs.json").read_text(encoding="utf-8"))["$defs"]
    assert tuple(defs["category"]["enum"]) == FINDING_CATEGORIES
    assert tuple(defs["target_class"]["enum"]) == TARGET_CLASSES
    assert defs["finding"]["properties"]["severity"]["enum"] == ["low", "medium", "high"]
    # every word in a "`a`, `b`" enumeration line of the skill's lists is a known value
    known = {*FINDING_CATEGORIES, *TARGET_CLASSES, "low", "medium", "high"}
    section = re.search(r"<!-- enums -->(.*?)<!-- /enums -->", skill_text(), re.DOTALL)
    assert section is not None
    values = [line.split(":", 1)[1] for line in section.group(1).splitlines() if ":" in line]
    assert set(re.findall(r"`([a-z_]+)`", " ".join(values))) == known


def test_the_skill_treats_the_digest_as_data_and_cites_only_manifest_ids() -> None:
    text = skill_text().lower()
    for phrase in ("never follow", "data", "nonce", "manifest.json", "only"):
        assert phrase in text, phrase


def test_the_skill_leaves_the_tool_written_values_to_the_tool() -> None:
    text = skill_text()
    for key in ("counts", "risk", "sessions"):
        assert re.search(rf"\b{key}\b", text), key
    assert "do not write" in text.lower()


def test_the_skill_does_not_restate_the_standard_instructions() -> None:
    text = skill_text()
    assert "--summary-file" not in text and "report done" not in text
    assert "claude-session" in text  # it points at them instead


def json_example() -> dict[str, Any]:
    blocks = re.findall(r"```json\n(.*?)\n```", skill_text(), re.DOTALL)
    assert len(blocks) == 1
    return json.loads(blocks[0])


def test_the_example_artifact_in_the_skill_is_valid_against_the_schema() -> None:
    validate(json_example(), "session-audit/slot/artifact.json")


def test_the_example_marker_is_a_placeholder_not_the_real_marker() -> None:
    example = json_example()
    real = MARKER.read_text(encoding="ascii").strip()
    assert example["marker"] != real and real not in skill_text()


def test_the_example_uses_only_the_documented_keys() -> None:
    example = json_example()
    assert set(example) == {
        "retro_id",
        "run_token",
        "digest_sha256",
        "marker",
        "summary",
        "findings",
        "candidates",
    }


# --- the profile -------------------------------------------------------------------------------

# claude-session's rules for an isolated run's profile file (its interface, Permission profiles,
# and permission_profiles.py), pinned by hand as data: nothing of it is imported.
PINNED_SECTIONS = ("permissions", "sandbox")
PINNED_SANDBOX_KEYS = {
    "filesystem": {"allowRead", "denyRead", "allowWrite", "denyWrite"},
    "network": {"allowedDomains", "deniedDomains"},
    "autoAllowBashIfSandboxed": None,
}
PINNED_REFUSED_PERMISSIONS = {"additionalDirectories"}
DOCUMENTED_DENIES = [
    "Agent",
    "Task",
    "WebFetch",
    "WebSearch",
    "NotebookEdit",
    "mcp__*",
    "Bash(memoctl:*)",
    "Bash(agent-spawn:*)",
    "Bash(forgectl:*)",
    "Bash(ticketctl:*)",
    "Bash(notice-route:*)",
    "Bash(awkit:*)",
    "Bash(session-audit:*)",
]


def profile() -> dict[str, Any]:
    document = yaml.safe_load(PROFILE.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_the_profile_has_the_documented_denies_and_no_network() -> None:
    document = profile()
    assert document == {
        "permissions": {"deny": DOCUMENTED_DENIES},
        "sandbox": {"network": {"allowedDomains": []}},
    }


def test_the_profile_only_adds_denies_never_an_allow_or_a_mode() -> None:
    permissions = profile()["permissions"]
    assert set(permissions) == {"deny"}


def test_the_profile_is_valid_for_an_isolated_run_of_claude_session() -> None:
    document = profile()
    assert set(document) <= set(PINNED_SECTIONS)
    assert not set(document["permissions"]) & PINNED_REFUSED_PERMISSIONS
    for key, value in document["permissions"].items():
        assert key in {"allow", "deny", "ask"} and isinstance(value, list)
        assert all(isinstance(entry, str) and entry for entry in value)
    for key, value in document["sandbox"].items():
        allowed = PINNED_SANDBOX_KEYS[key]
        assert allowed is not None and set(value) <= allowed
        assert all(isinstance(v, list) and all(isinstance(e, str) for e in v) for v in value.values())


def test_the_profile_has_no_variable_and_no_duplicate_entry() -> None:
    text = PROFILE.read_text(encoding="utf-8")
    assert "${" not in text and "\t" not in text
    denies = profile()["permissions"]["deny"]
    assert len(denies) == len(set(denies))


def test_the_launch_plan_finds_the_bundled_profile() -> None:
    assert PROFILE.is_file() and PROFILE.parent == PLUGIN / "profiles"
    assert str(PROFILE).endswith(PROFILE_RELATIVE)


# --- the manifest, the preflight and the wheel -------------------------------------------------


def test_the_manifest_names_the_plugin() -> None:
    assert json.loads(MANIFEST.read_text(encoding="utf-8")) == {"name": "session-audit"}


def test_the_bundled_plugin_passes_the_skill_preflight() -> None:
    env = {"HOME": "/nonexistent-home"}
    choice = plugins.skill_preflight(
        SystemGateway(),
        "session-audit:audit",
        plugin_dirs=(),
        env=env,
        bundled=str(PLUGIN),
    )
    assert choice.found == str(PLUGIN.resolve())


def test_a_skill_the_plugin_does_not_hold_is_not_found() -> None:
    with pytest.raises(errors.CliError) as raised:
        plugins.skill_preflight(
            SystemGateway(),
            "session-audit:other",
            plugin_dirs=(),
            env={"HOME": "/nonexistent-home"},
            bundled=str(PLUGIN),
        )
    assert raised.value.details["reason"] == "skill_not_found"


def test_the_wheel_force_includes_the_plugin_with_its_hidden_manifest_directory() -> None:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["force-include"] == {"plugin": "session_audit/plugin"}
    assert "exclude" not in wheel and "include" not in wheel
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert not any(line.strip() in (".claude-plugin", ".claude-plugin/", "plugin") for line in ignored)


def test_every_plugin_file_is_tracked_so_a_checkout_builds_the_same_wheel() -> None:
    names = {p.relative_to(PLUGIN).as_posix() for p in PLUGIN.rglob("*") if p.is_file()}
    assert names == {
        ".claude-plugin/plugin.json",
        "skills/audit/SKILL.md",
        "skills/audit/references/marker.txt",
        "profiles/audit-retro.yaml",
    }
