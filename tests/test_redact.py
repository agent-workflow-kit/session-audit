"""Redaction and the digest text pipeline (issue #20).

Every secret-shaped fixture is built at run time from obvious fakes, so no literal in this file
looks like a credential to a secret scanner.
"""

from __future__ import annotations

import base64
import copy
import importlib
import pickle
import re
import time
import unicodedata

import pytest

from session_audit import redact, textsafe
from session_audit.redact import (
    Redactor,
    build_masks,
    cut,
    digest_text,
    find_encoded,
    redact_encoded,
)

R = Redactor()


def red(text: str) -> str:
    return R.redact(text)


def kinds(text: str) -> dict[str, int]:
    return dict(R.redact_counted(text)[1])


# ---- positives ----------

TOKEN_CASES = {
    "aws": "AKIA" + "EXAMPLE" + "0" * 9,
    "aws_session": "ASIA" + "EXAMPLE" + "1" * 9,
    "github": "ghp_" + "A" * 36,
    "github_oauth": "gho_" + "b" * 40,
    "github_pat": "github_pat_" + "C" * 30,
    "gitlab": "glpat-" + "d" * 22,
    "slack": "xoxb-" + "1234567890-" + "e" * 12,
    "slack_app": "xapp-1-" + "f" * 14,
    "anthropic": "sk-ant-" + "g" * 30,
    "openai": "sk-" + "h" * 40,
    "openai_proj": "sk-proj-" + "i" * 40,
    "google": "AIza" + "j" * 35,
    "google_oauth": "ya29." + "k" * 30,
    "stripe": "sk_live_" + "l" * 24,
    "stripe_whsec": "whsec_" + "m" * 24,
    "npm": "npm_" + "n" * 36,
    "pypi": "pypi-" + "o" * 60,
    "hf": "hf_" + "p" * 34,
    "sendgrid": "SG." + "q" * 20 + "." + "r" * 20,
    "telegram": "123456789:" + "s" * 35,
}


@pytest.mark.parametrize("name", sorted(TOKEN_CASES))
def test_token_shapes_are_redacted(name: str) -> None:
    value = TOKEN_CASES[name]
    out, counts = R.redact_counted(f"before {value} after")
    assert out == "before [REDACTED:token] after"
    assert counts == {"token": 1}


def test_slack_webhook_keeps_host() -> None:
    url = "https://hooks.slack.com/services/" + "T" + "0" * 10 + "/B" + "1" * 10 + "/" + "x" * 24
    assert red(url) == "https://hooks.slack.com/services/[REDACTED:token]"


def make_jwt() -> str:
    return "eyJ" + "a" * 20 + "." + "eyJ" + "b" * 20 + "." + "c" * 30


def test_jwt() -> None:
    assert red(f"token is {make_jwt()}!") == "token is [REDACTED:jwt]!"
    assert red("eyJ" + "a" * 20 + "." + "b" * 20 + ".") == "[REDACTED:jwt]"


def test_private_key_block_through_end_line() -> None:
    body = "\n".join("QUJD" * 16 for _ in range(300))  # over 8 KiB of body
    block = f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----"
    assert red(f"a\n{block}\nb") == "a\n[REDACTED:private_key]\nb"
    assert red("x -----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY----- y") == (
        "x [REDACTED:private_key] y"
    )
    assert red("-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA") == "[REDACTED:private_key]"
    assert red("-----BEGIN PGP PRIVATE KEY BLOCK-----\nAAAA\nmore") == "[REDACTED:private_key]"


def test_private_key_two_blocks_and_mismatched_end() -> None:
    one = "-----BEGIN PRIVATE KEY-----\nA\n-----END PRIVATE KEY-----"
    assert red(f"{one} mid {one}") == "[REDACTED:private_key] mid [REDACTED:private_key]"
    # an END line of another label does not close the block: it runs to the end of the part
    assert red("-----BEGIN PRIVATE KEY-----\nA\n-----END RSA PRIVATE KEY-----\ntail") == (
        "[REDACTED:private_key]"
    )


def test_public_key_and_certificate_stay() -> None:
    text = "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----"
    assert red(text) == text
    text = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----"
    assert red(text) == text
    long_label = "-----BEGIN " + "A" * 41 + " PRIVATE KEY-----"
    assert red(long_label) == long_label


def test_url_userinfo() -> None:
    assert red("git clone https://alice:hunter2@example.com/r.git") == (
        "git clone https://[REDACTED:url_userinfo]@example.com/r.git"
    )
    assert red("postgres://:pw0rd@db/x") == "postgres://[REDACTED:url_userinfo]@db/x"


def test_url_without_password_stays() -> None:
    for text in (
        "https://example.com:8080/path",
        "ssh://git@github.com/o/r.git",
        "https://example.com/a?e=u@x",
        "see http://host:80@",
    ):
        if text == "see http://host:80@":
            assert "REDACTED" in red(text)  # host:80@ is user:password syntax
        else:
            assert red(text) == text


def test_cookie_headers() -> None:
    assert red("Cookie: sid=abc; theme=dark\nnext") == "Cookie: [REDACTED:cookie]\nnext"
    assert red("set-cookie: a=b; Path=/") == "set-cookie: [REDACTED:cookie]"
    assert red("curl -H 'Cookie: a=b' x") == "curl -H 'Cookie: [REDACTED:cookie]' x"
    assert red("no cookies here") == "no cookies here"


def test_authorization_headers() -> None:
    assert red("Authorization: Bearer abc.def") == "Authorization: [REDACTED:auth_header]"
    assert red("authorization: Basic dXNlcjpwdw==") == "authorization: [REDACTED:auth_header]"
    assert red('{"Authorization": "Token abc123"}') == '{"Authorization": "[REDACTED:auth_header]"}'
    assert red("X-Api-Key: abcdef") == "X-Api-Key: [REDACTED:auth_header]"
    assert red("Proxy-Authorization: Basic zzz") == "Proxy-Authorization: [REDACTED:auth_header]"


def test_bare_bearer() -> None:
    assert red("use Bearer " + "ab12" * 6 + " here") == "use Bearer [REDACTED:auth_header] here"
    assert red("bearer authentication-mechanism-here") == "bearer authentication-mechanism-here"
    assert red("Bearer short1") == "Bearer short1"


def test_cli_secrets() -> None:
    assert red("tool --password hunter2 run") == "tool --password [REDACTED:cli_secret] run"
    assert red("tool --token=abc123") == "tool --token=[REDACTED:cli_secret]"
    assert red('tool --client-secret "a b c"') == "tool --client-secret [REDACTED:cli_secret]"
    assert red("tool --api-key 'k k'") == "tool --api-key [REDACTED:cli_secret]"
    assert red("curl -u alice:pw1 x") == "curl -u [REDACTED:cli_secret] x"
    assert red("curl --user bob:pw2 x") == "curl --user [REDACTED:cli_secret] x"
    assert (
        red("docker login -u bob -p s3cret x") == "docker login -u bob -p [REDACTED:cli_secret] x"
    )
    assert red("tool --token 'unterminated") == "tool --token [REDACTED:cli_secret]"


def test_cli_lookalikes_stay() -> None:
    for text in (
        "tool --password-stdin --username x",
        "tool --password-file /etc/p",
        "git commit --author=Jane",
        "tool --tokenizer gpt",
        "docker run -u 1000:1000 img",
        "tool --token --next",
        "find . -print0 -perm 755",
        "ls -p 8080",
    ):
        assert red(text) == text


def test_json_and_yaml_keys() -> None:
    assert red('{"password": "x"}') == '{"password": "[REDACTED:json_secret]"}'
    assert red('{"db_password":"a\\"b"}') == '{"db_password":"[REDACTED:json_secret]"}'
    assert red('{"accessToken": "t0k"}') == '{"accessToken": "[REDACTED:json_secret]"}'
    assert red('{\\"api_key\\": \\"abc\\"}') == '{\\"api_key\\": \\"[REDACTED:json_secret]\\"}'
    assert red("{'secret': 'abc'}") == "{'secret': '[REDACTED:json_secret]'}"
    assert red("password: hunter22") == "password: [REDACTED:json_secret]"
    assert red("  - client_secret: 'abc'") == "  - client_secret: [REDACTED:json_secret]"
    assert red('db:\n  password: "x y"\nnext: 1') == (
        "db:\n  password: [REDACTED:json_secret]\nnext: 1"
    )


def test_json_lookalikes_stay() -> None:
    for text in (
        '{"max_tokens": 100, "token_type": "bearer", "tokenizer": "gpt2"}',
        '{"input_tokens": "12345678"}',
        '{"password": null}',
        '{"password": ""}',
        "the token: use it",
        "password: x",
    ):
        assert red(text) == text


def test_assignments() -> None:
    assert red("export GITHUB_TOKEN=abcdef123") == "export GITHUB_TOKEN=[REDACTED:assignment]"
    assert red("PGPASSWORD=hunter22 psql") == "PGPASSWORD=[REDACTED:assignment] psql"
    # the value runs to whitespace or a quote: a "," ";" "&" or ")" inside it leaks no tail
    assert red("curl 'https://h/x?access_token=abcdef12&y=1'") == (
        "curl 'https://h/x?access_token=[REDACTED:assignment]'"
    )
    assert red('aws_secret_access_key = "wJalr"') == "aws_secret_access_key = [REDACTED:assignment]"
    assert red("API_KEY='abc'") == "API_KEY=[REDACTED:assignment]"
    assert red("MY_AUTH=x1y2z3w4") == "MY_AUTH=[REDACTED:assignment]"


def test_assignment_lookalikes_stay() -> None:
    for text in (
        "MAX_TOKENS=100",
        "TOKEN=$OTHER_VAR_NAME",
        "TOKEN=$(cat file)",
        "token = get_token()",
        "if token == other:",
        "KEY=abc",
        "--author=Jane Smith",
    ):
        assert red(text) == text, text


def test_ordinary_hashes_and_uuids_stay() -> None:
    text = (
        "commit 5f2b8a0c9d1e4f3a2b1c0d9e8f7a6b5c4d3e2f1a "
        "uuid 123e4567-e89b-12d3-a456-426614174000 "
        "sha256 " + "ab" * 32 + " b64 " + base64.b64encode(b"x" * 40).decode() + " "
        "ordinary prose about sk-learn and risk-assessment-for-the-whole-department-team"
    )
    assert red(text) == text


def test_redaction_is_idempotent_and_deterministic() -> None:
    text = f"a {TOKEN_CASES['github']} TOKEN=abcdef123 --password x -----BEGIN PRIVATE KEY-----\nz"
    once = red(text)
    assert red(once) == once
    assert red(text) == once


def test_overlap_makes_one_marker() -> None:
    text = "Authorization: Bearer " + TOKEN_CASES["github"]
    assert red(text) == "Authorization: [REDACTED:auth_header]"
    assert red("GITHUB_TOKEN=" + TOKEN_CASES["github"]).count("REDACTED") == 1


def test_every_kind_is_documented() -> None:
    assert redact.KINDS == (
        "token",
        "private_key",
        "url_userinfo",
        "cookie",
        "auth_header",
        "cli_secret",
        "json_secret",
        "jwt",
        "assignment",
        "env_value",
        "prefix",
        "encoded",
    )
    used = {p.kind for p in redact.builtin_patterns()} | {"private_key"}
    assert used == {
        "token",
        "private_key",
        "url_userinfo",
        "cookie",
        "auth_header",
        "cli_secret",
        "json_secret",
        "jwt",
        "assignment",
    }


# ---- pattern data: structure and packaging -------------------------------------------------------


MAX_REPEAT_HI = 16384  # the longest repetition any pattern may have (the value length bound)
_UNIVERSE = frozenset(range(0x250)) | {0x2028, 0x3000, 0x4E00}


def _parser_module():
    try:
        return importlib.import_module("re._parser")
    except ImportError:  # pragma: no cover
        pytest.fail("re._parser is unavailable: update the walker for this Python")


def _category_set(parser, cat) -> frozenset[int]:
    tests = {
        parser.CATEGORY_DIGIT: lambda c: chr(c).isdigit(),
        parser.CATEGORY_SPACE: lambda c: chr(c).isspace(),
        parser.CATEGORY_WORD: lambda c: chr(c).isalnum() or c == 0x5F,
    }
    for base, neg in (
        (parser.CATEGORY_DIGIT, parser.CATEGORY_NOT_DIGIT),
        (parser.CATEGORY_SPACE, parser.CATEGORY_NOT_SPACE),
        (parser.CATEGORY_WORD, parser.CATEGORY_NOT_WORD),
    ):
        if cat == base:
            return frozenset(c for c in _UNIVERSE if tests[base](c))
        if cat == neg:
            return frozenset(c for c in _UNIVERSE if not tests[base](c))
    pytest.fail(f"unknown category {cat}: update the walker for this Python")


def _fold_case(chars: set[int], flags: int) -> frozenset[int]:
    if flags & re.IGNORECASE:
        for c in list(chars):
            if c < 128:
                chars.add(ord(chr(c).swapcase()))
    return frozenset(chars)


def _first(parser, seq, flags: int) -> tuple[frozenset[int], bool]:
    """The characters a match of ``seq`` can start with, and whether it can be empty (a
    conservative reading: assertions are empty, a back reference can start with anything)."""
    first: set[int] = set()
    for op, arg in seq:
        chars, nullable = _first_node(parser, op, arg, flags)
        first |= chars
        if not nullable:
            return frozenset(first), False
    return frozenset(first), True


def _first_node(parser, op, arg, flags: int) -> tuple[frozenset[int], bool]:
    if op == parser.LITERAL:
        return _fold_case({arg}, flags), False
    if op == parser.NOT_LITERAL:
        return _UNIVERSE - _fold_case({arg}, flags), False
    if op == parser.ANY:
        return _UNIVERSE - {10}, False
    if op == parser.IN:
        members: set[int] = set()
        negate = False
        for item_op, item in arg:
            if item_op == parser.NEGATE:
                negate = True
            elif item_op == parser.LITERAL:
                members.add(item)
            elif item_op == parser.RANGE:
                members |= {c for c in _UNIVERSE if item[0] <= c <= item[1]}
            elif item_op == parser.CATEGORY:
                members |= _category_set(parser, item)
            else:
                pytest.fail(f"unknown set item {item_op}: update the walker for this Python")
        folded = _fold_case(members, flags)
        return (_UNIVERSE - folded if negate else folded), False
    if op in (parser.AT, parser.ASSERT, parser.ASSERT_NOT):
        return frozenset(), True
    if op == parser.SUBPATTERN:
        return _first(parser, arg[3], flags)
    if op == parser.ATOMIC_GROUP:
        return _first(parser, arg, flags)
    if op == parser.BRANCH:
        parts = [_first(parser, alt, flags) for alt in arg[1]]
        return frozenset().union(*(p[0] for p in parts)), any(p[1] for p in parts)
    if op in _repeat_ops(parser):
        lo, _hi, sub = arg
        chars, nullable = _first(parser, sub, flags)
        return chars, nullable or lo == 0
    if op == parser.GROUPREF:
        return _UNIVERSE, True
    if op == parser.GROUPREF_EXISTS:
        yes, no = arg[1], arg[2]
        parts = [_first(parser, yes, flags)] + ([_first(parser, no, flags)] if no else [])
        return frozenset().union(*(p[0] for p in parts)), True
    pytest.fail(f"unknown opcode {op}: update the walker for this Python")


def _repeat_ops(parser) -> tuple:
    return (parser.MAX_REPEAT, parser.MIN_REPEAT, parser.POSSESSIVE_REPEAT)


def _walk(parser, seq, flags: int, in_repeat: bool, problems: list[str]) -> None:
    prev_repeat: tuple[frozenset[int], int] | None = None
    for op, arg in seq:
        is_repeat = op in _repeat_ops(parser)
        if is_repeat:
            lo, hi, sub = arg
            if hi == parser.MAXREPEAT:
                problems.append("unbounded repetition")
            elif hi > MAX_REPEAT_HI:
                problems.append(f"repetition bound {hi} over {MAX_REPEAT_HI}")
            if in_repeat:
                problems.append("nested repetition")
            chars = _first(parser, sub, flags)[0]
            if prev_repeat is not None and prev_repeat[0] & chars and hi > 1 and prev_repeat[1] > 1:
                problems.append("adjacent repetitions share a starting character")
            prev_repeat = (chars, hi)
            _walk(parser, sub, flags, in_repeat or hi > 1, problems)
            continue
        prev_repeat = None
        if op == parser.SUBPATTERN:
            _walk(parser, arg[3], flags, in_repeat, problems)
        elif op == parser.BRANCH:
            if in_repeat:
                firsts = [_first(parser, alt, flags) for alt in arg[1]]
                for i, (chars, nullable) in enumerate(firsts):
                    if nullable:
                        problems.append("empty alternative inside a repetition")
                    for other, _ in firsts[:i]:
                        if chars & other:
                            problems.append("overlapping alternatives inside a repetition")
            for alt in arg[1]:
                _walk(parser, alt, flags, in_repeat, problems)
        elif op in (parser.ASSERT, parser.ASSERT_NOT):
            _walk(parser, arg[1], flags, in_repeat, problems)
        elif op == parser.ATOMIC_GROUP:
            _walk(parser, arg, flags, in_repeat, problems)
        elif op == parser.GROUPREF_EXISTS:
            _walk(parser, arg[1], flags, in_repeat, problems)
            if arg[2]:
                _walk(parser, arg[2], flags, in_repeat, problems)
        elif op in (
            parser.LITERAL,
            parser.NOT_LITERAL,
            parser.IN,
            parser.ANY,
            parser.AT,
            parser.GROUPREF,
        ):
            pass
        else:
            problems.append(f"unknown opcode {op}: update the walker for this Python")


def _check(regex: str, flags: int = 0) -> list[str]:
    parser = _parser_module()
    problems: list[str] = []
    parsed = parser.parse(regex, flags)
    _walk(parser, parsed, parsed.state.flags | flags, False, problems)
    return problems


@pytest.mark.parametrize("pat", redact.builtin_patterns(), ids=lambda p: p.name)
def test_pattern_is_bounded_and_not_nested(pat) -> None:
    assert _check(pat.regex.pattern, pat.regex.flags) == []


@pytest.mark.parametrize(
    ("regex", "problem"),
    [
        ("(?:a|a){1,40}b", "alternative inside a repetition"),
        ("(?:a|aa){1,40}b", "alternative inside a repetition"),
        ("a{0,2000}a{0,2000}b", "adjacent repetitions"),
        ("(?:a{1,3}){1,3}b", "nested repetition"),
        ("a+b", "unbounded"),
        ("a{0,20000}b", "over 16384"),
        ("(?:a|b?){1,5}c", "empty alternative"),
        ("(?:a|b){1,5}(?:x(?=a{1,3}b))", ""),
    ],
)
def test_the_walker_rejects_what_it_should(regex: str, problem: str) -> None:
    found = _check(regex)
    if problem:
        assert any(problem in p for p in found), found
    else:
        assert found == []  # disjoint alternatives and an assertion: fine


def test_the_walker_fails_loudly_on_an_unknown_opcode(monkeypatch) -> None:
    parser = _parser_module()
    parsed = parser.parse("(a)(?(1)b|c)")
    monkeypatch.setattr(parser, "GROUPREF_EXISTS", object())  # as if this Python renamed it
    problems: list[str] = []
    _walk(parser, parsed, 0, False, problems)
    assert any("unknown opcode" in p and "update the walker" in p for p in problems)


def test_patterns_ship_as_package_data() -> None:
    from importlib import resources

    data = resources.files("session_audit").joinpath("redact_patterns.toml")
    assert data.is_file()
    names = [p.name for p in redact.builtin_patterns()]
    assert len(names) == len(set(names)) >= 30


def test_wheel_build_includes_the_pattern_file(tmp_path) -> None:
    # hatch's wheel target takes every file under the package directory; pin that configuration.
    import tomllib
    from pathlib import Path

    cfg = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    wheel = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/session_audit"]
    assert "exclude" not in wheel and "include" not in wheel


# ---- linear time ----------

KB = 1024


def adversarial_families() -> dict[str, str]:
    jwt_part = "A" * 50
    return {
        "key chars": "a" * 65536,
        "key chars dashes": "a-" * 32768,
        "env name run": "KEY" * 21845,
        "eyJ run": "eyJ" * 21845,
        "eyJ dots": "eyJaaaa." * 8192,
        "jwt almost": ("eyJ" + jwt_part + "." + jwt_part + " ") * 600,
        "bearer prefixes": "Bearer " * 9362,
        "bearer almost": "Bearer " + "a" * 65000,
        "bearer words": "bearer aaaaaaaaaaaaaaaaaaaaaaaa " * 2000,
        "unterminated begin": "-----BEGIN PRIVATE KEY-----" * 2400,
        "begin bad label": "-----BEGIN " * 5900,
        "begin long label": ("-----BEGIN " + "A" * 39 + " ") * 1300,
        "huge base64": "QUJD" * 16384,
        "near token": "ghp_" + "A" * 35 + " " + ("ghp_" + "A" * 35 + " ") * 1600,
        "sk dashes": "sk-" + "a-" * 31 + " ",
        "sk repeated": "sk-" * 21845,
        "aws near": ("AKIA" + "A" * 15 + " ") * 3200,
        "userinfo": "://" + "a" * 128 + ":" * 3 + "b" * 300 + " ",
        "userinfo repeated": "://a:" * 13000,
        "cookie": "cookie:" * 9000,
        "cookie long": "Cookie: " + "a" * 65000,
        "authorization": 'authorization: "' * 4000,
        "cli flags": "--token " * 8000,
        "cli dashes": "--" + "a-" * 32000,
        "cli user": "-u " * 21000,
        "json key": '"token": "' + "\\" * 60000,
        "json keys": '"password": "a' * 4000,
        "yaml": ("  - " + "k" * 40 + ": ") * 1500,
        "yaml lines": "password: \n" * 5000,
        "assignment run": "A" * 40 + "KEY" + "B" * 40 + "=",
        "assignment many": ("a" * 38 + "key=" + "\n") * 1500,
        "assignment equals": "TOKEN=" * 10000,
        "url sch": "http://" * 9000,
        "slack hook": "hooks.slack.com/services/" * 2500,
        "spaces": " " * 65536,
        "begin partial": "-----BEGIN PRIVATE KEY-----\n" + "A" * 65000,
        "dots": "a." * 32768,
        "dashes": "-" * 65536,
        "equals": "=" * 65536,
        "newlines": "\n" * 65536,
        "key dots": "KEY." * 16384,
        "key dashes equals": "key-" * 8000 + "=" * 8000,
        "name then space": ("password" + " " * 20) * 2300,
        "password colon": "password:" * 7000,
        "password words": "password hunter2xyz " * 3000,
        "flow keys": "{password: " * 5000,
        "flow commas": ",password" * 7000,
        "quoted keys": "'password': " * 5000,
        "xml open": "<password>" * 6500,
        "xml attrs": '<add key="password" ' * 3200,
        "xml nested": "<a><token>" * 6000,
        "sshpass": "sshpass -p " * 6000,
        "openssl": "-passin pass:" * 5000,
        "curl cookie": "-b a=" * 13000,
        "sql": "IDENTIFIED BY " * 4600 + "PASSWORD " * 100,
        "netrc": "login a password " * 3800,
        "connection string": ";Password=" * 6500,
        "typed spaced": "password: string " * 3800,
        "bare assignment lines": ("password = " + "a" * 30 + "\n") * 1500,
        "header newline": "Authorization:\n  " * 3600,
        "orphan end": "-----END PRIVATE KEY-----" * 2600,
        "orphan end text": ("a" * 100 + "-----END PRIVATE KEY-----") * 500,
        "userinfo at": "://a:" + "@" * 65000,
        "user at run": "://a:" + "b@" * 30000,
        "two part jwt": "eyJaaaa.eyJbbbb" * 4300,
        "combining": "ghp_" + "A" * 36 + "\u0301" * 30000,
        "dash flags": "--password -" * 5400,
        "cli attached": "-ua:" * 16000,
        "partial prefixes": "ghp_gho_ghu_AKIAxox-sk-ant-github_pat_" * 1700,
        "typed signatures": "def f(a: int, token: str = None, retries: int = 3): " * 1300,
        "userinfo long password": "https://a:" + "b" * 65000,
    }


def _best(fn, arg, repeats=3) -> float:
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn(arg)
        best = min(best, time.perf_counter() - t0)
    return best


@pytest.mark.parametrize("family", sorted(adversarial_families()))
def test_redaction_is_linear_on_adversarial_input(family: str) -> None:
    big = adversarial_families()[family]
    big = (big * 2)[: 64 * KB]
    small = big[: 16 * KB]
    t_small = max(_best(R.redact, small), 0.004)
    t_big = _best(R.redact, big)
    # linear is 4x; quadratic would be 16x or more. Generous: 12x, and an absolute ceiling.
    assert t_big < max(12 * t_small, 0.05), (family, t_small, t_big)
    assert t_big < 3.0, (family, t_big)


@pytest.mark.parametrize("pat", redact.builtin_patterns(), ids=lambda p: p.name)
def test_each_pattern_alone_finishes_fast_on_every_adversary(pat) -> None:
    # An absolute ceiling per regex (the ratio test above sees only the whole redaction): a
    # shared, loaded host gets 2 s for 64 KiB, three orders of magnitude over what a linear scan
    # needs and far under what a quadratic one would take.
    worst = 0.0
    for family, text in adversarial_families().items():
        big = (text * 2)[: 64 * KB]
        t0 = time.perf_counter()
        for _ in pat.regex.finditer(big):
            pass
        took = time.perf_counter() - t0
        worst = max(worst, took)
        assert took < 2.0, (pat.name, family, took)
    assert worst < 2.0


def test_masks_are_linear() -> None:
    masks = [("a" * 40) + str(i) + "b" for i in range(100)]
    rd = Redactor(masks)
    big = "a" * (64 * KB)
    small = big[: 16 * KB]
    t_small = max(_best(rd.redact, small), 0.004)
    t_big = _best(rd.redact, big)
    assert t_big < max(12 * t_small, 0.05)


def test_digest_text_is_linear_for_a_full_part() -> None:
    big = "TOKEN=" + "̈́" * 70000
    t0 = time.perf_counter()
    digest_text(big, 1500)
    assert time.perf_counter() - t0 < 5.0


# ---- literal masks ----------


def test_masks_redact_literal_values_with_metacharacters() -> None:
    value = "a.b*c+d?(e)[f]{g}|h^i$j\\k"
    rd = Redactor([value])
    assert rd.redact(f"x {value} y") == "x [REDACTED:env_value] y"
    assert rd.redact("a.b*c+d?(e)[f]{g}|h^i$j") == "a.b*c+d?(e)[f]{g}|h^i$j"


def test_masks_multi_line_and_lines() -> None:
    env = {"MY_SECRET": "first-line-value\nsecond-line-value\n"}
    masks = build_masks(env)
    rd = Redactor(masks)
    assert rd.redact("a first-line-value\nsecond-line-value b").count("REDACTED:env_value") == 1
    assert rd.redact("only second-line-value here") == "only [REDACTED:env_value] here"
    assert rd.redact("trailing first-line-value\n") == "trailing [REDACTED:env_value]\n"


def test_masks_longest_first_and_overlap() -> None:
    rd = Redactor(["abcdefgh", "abcdefghijkl", "ghijklmn_extra"])
    assert rd.redact("xx abcdefghijkl yy") == "xx [REDACTED:env_value] yy"
    assert rd.redact("abcdefghijklmn_extra!") == "[REDACTED:env_value]!"


def test_masks_are_case_sensitive_and_exact() -> None:
    rd = Redactor(["Secret-Value1"])
    assert rd.redact("secret-value1 SECRET-VALUE1") == "secret-value1 SECRET-VALUE1"
    assert rd.redact("Secret-Value1") == "[REDACTED:env_value]"


def test_masks_are_nfc_and_surrogate_normalized() -> None:
    value = "café-secret-1"  # decomposed
    rd = Redactor([value])
    assert rd.redact(unicodedata.normalize("NFC", f"x {value} y")) == "x [REDACTED:env_value] y"
    bad = Redactor(["abc\udc80defgh"])
    assert bad.redact("abc�defgh") == "[REDACTED:env_value]"


def test_short_masks_and_mask_cap() -> None:
    assert Redactor(["short"]).mask_count == 0
    assert Redactor(["1234567"]).mask_count == 0
    assert Redactor(["12345678"]).mask_count == 1
    many = [f"value-number-{i:05d}" for i in range(redact.MAX_MASKS + 50)]
    assert Redactor(many).mask_count == redact.MAX_MASKS
    assert Redactor(["x" * 65537]).mask_count == 0


def test_build_masks_names_and_warnings() -> None:
    warnings: list[str] = []
    secret_short = "s3cr3t"  # 6 characters
    env = {
        "GITHUB_TOKEN": "tok-value-123456",
        "my_password": "pw-value-123456",
        "SHORT_SECRET": secret_short,
        "PLAIN": "plain-value-123456",
        "EXTRA": "extra-value-123456",
        "EXPLICIT_SHORT": "abc",
        "SSH_AUTH_SOCK": "/tmp/agent.123",
    }
    masks = build_masks(env, ["EXTRA", "EXPLICIT_SHORT", "NOT_SET"], warnings.append)
    assert "tok-value-123456" in masks and "pw-value-123456" in masks
    assert "extra-value-123456" in masks
    assert "plain-value-123456" not in masks
    assert secret_short not in masks and "abc" not in masks
    assert masks == sorted(masks, key=lambda v: (-len(v), v))
    text = "\n".join(warnings)
    assert "EXPLICIT_SHORT" in text
    assert secret_short not in text and "abc" not in text.replace("abcd", "")
    assert "SHORT_SECRET" not in text  # an automatic name is counted, not shown
    assert "1 secret-named" in text


def test_build_masks_does_not_read_the_process_environment(monkeypatch) -> None:
    monkeypatch.setenv("SESSION_AUDIT_TEST_TOKEN", "from-the-process-env")
    assert build_masks({}) == []


def test_build_masks_default_warning_goes_through_the_sanitizer(capsys) -> None:
    build_masks({"A_TOKEN": "tiny"}, ["A_TOKEN"])
    err = capsys.readouterr().err
    assert "A_TOKEN" in err and "tiny" not in err


def test_build_masks_rejects_non_strings_without_echo() -> None:
    with pytest.raises(ValueError) as info:
        build_masks({"A_TOKEN": 12345678901}, ["A_TOKEN"])  # type: ignore[dict-item]
    assert "12345678901" not in str(info.value)


def test_masks_huge_value_ignored_with_count() -> None:
    warnings: list[str] = []
    assert build_masks({"A_TOKEN": "x" * 70000}, [], warnings.append) == []
    assert warnings and "x" * 10 not in warnings[0]


def test_build_masks_bounded() -> None:
    env = {f"T{i}_TOKEN": f"value-{i:06d}-padding" for i in range(500)}
    warnings: list[str] = []
    assert len(build_masks(env, [], warnings.append)) == redact.MAX_MASKS
    assert warnings


# ---- extra token prefixes ----------


def test_extra_token_prefixes() -> None:
    rd = Redactor(extra_token_prefixes=["acme_", "zz-"])
    assert rd.redact("t acme_" + "a1b2c3d4e5" + " x") == "t [REDACTED:prefix] x"
    assert rd.redact("zz-" + "A" * 300) == "[REDACTED:prefix]"  # the whole run, not 256
    assert rd.redact("acme_short") == "acme_short"  # fewer than 8 after the prefix
    assert rd.redact("acme_ and zz-12345678") == "acme_ and [REDACTED:prefix]"
    assert rd.redact("acme_12345678\nacme_abcdefgh").count("REDACTED:prefix") == 2


@pytest.mark.parametrize("bad", ["ab", "a" * 65, "has space", "dot.dot", "", "ünï"])
def test_extra_token_prefix_validation_does_not_echo(bad: str) -> None:
    with pytest.raises(ValueError) as info:
        Redactor(extra_token_prefixes=[bad])
    assert bad not in str(info.value) or bad == ""


def test_too_many_prefixes() -> None:
    with pytest.raises(ValueError):
        Redactor(extra_token_prefixes=[f"pre{i:03d}" for i in range(65)])


def test_prefix_scan_is_linear() -> None:
    rd = Redactor(extra_token_prefixes=["a" * 60 + "b", "aaa", "acme_"])
    big = "a" * (64 * KB) + "acme_" * 100
    small = big[: 16 * KB]
    t_small = max(_best(rd.redact, small), 0.004)
    assert _best(rd.redact, big) < max(12 * t_small, 0.05)


# ---- encoded values ----------

SECRET = "Sup3r-Secret/Value+42=="  # a made-up mask value, not a credential


def b64_at_alignment(value: bytes, lead: int) -> str:
    enc = base64.b64encode(b"\0" * lead + value).decode().rstrip("=")
    return enc[(0, 2, 3)[lead] :]


@pytest.mark.parametrize("lead", [0, 1, 2])
def test_find_encoded_base64_alignments(lead: int) -> None:
    prefix = b"x" * lead
    blob = base64.b64encode(prefix + SECRET.encode() + b"tail").decode()
    assert find_encoded([SECRET], f"data: {blob} end")
    urlsafe = base64.urlsafe_b64encode(prefix + SECRET.encode() + b"tail").decode()
    assert find_encoded([SECRET], f"data: {urlsafe} end")


@pytest.mark.parametrize("n", [0, 1, 2, 3])
def test_find_encoded_base64_padding_variants(n: int) -> None:
    value = "padding-test-value" + "z" * n
    for text in (
        base64.b64encode(value.encode()).decode(),
        base64.b64encode(value.encode()).decode().rstrip("="),
    ):
        assert find_encoded([value], f"[{text}]")


def test_find_encoded_hex_and_percent() -> None:
    value = "hex/value?with spaces&more"
    hexed = value.encode().hex()
    assert find_encoded([value], "h " + hexed + " e")
    assert find_encoded([value], "h " + hexed.upper() + " e")
    mixed = "".join(c.upper() if i % 2 else c for i, c in enumerate(hexed))
    assert find_encoded([value], mixed)
    from urllib.parse import quote, quote_plus

    assert find_encoded([value], quote(value, safe=""))
    assert find_encoded([value], quote(value, safe="").lower())
    assert find_encoded([value], quote_plus(value))
    assert find_encoded([value], "".join(f"%{b:02x}" for b in value.encode()))
    assert find_encoded([value], "".join(f"%{b:02X}" for b in value.encode()))


def test_find_encoded_negatives() -> None:
    assert not find_encoded(
        [SECRET], "nothing to see " + base64.b64encode(b"unrelated bytes here").decode()
    )
    assert not find_encoded([SECRET], "")
    assert not find_encoded([], "anything")
    assert not find_encoded(["short"], base64.b64encode(b"short").decode())
    assert not find_encoded([SECRET], SECRET)  # the literal form is the mask's job, not this one's


def test_find_encoded_tail_after_8_kib() -> None:
    value = "long-key-value-" + "k" * 40
    blob = base64.b64encode(value.encode()).decode()
    assert find_encoded([value], "n" * (9 * KB) + blob)
    assert find_encoded([value], value.encode().hex().join(["n" * (9 * KB), ""]))
    # a key over 1 KiB: the head and the tail are each checked
    key = "".join(chr(97 + (i * 7) % 26) for i in range(5000))
    enc = base64.b64encode(key.encode()).decode()
    assert find_encoded([key], enc)
    tail_only = base64.b64encode(b"." + key.encode()[-600:]).decode()
    assert find_encoded([key], "y" * (9 * KB) + tail_only)


def test_redact_encoded_replaces_hits() -> None:
    blob = base64.b64encode(SECRET.encode()).decode()
    out, n = redact_encoded([SECRET], f"a {blob} b {SECRET.encode().hex()} c")
    assert out == "a [REDACTED:encoded] b [REDACTED:encoded] c"
    assert n == 2
    assert redact_encoded([SECRET], "clean") == ("clean", 0)


def test_redactor_encoded_methods_use_its_masks() -> None:
    rd = Redactor([SECRET])
    blob = base64.b64encode(SECRET.encode()).decode()
    assert rd.find_encoded(blob)
    assert rd.redact_encoded(blob) == ("[REDACTED:encoded]", 1)
    assert not Redactor().find_encoded(blob)


def test_encoded_is_linear() -> None:
    masks = [f"mask-value-{i:04d}-" + "z" * 20 for i in range(redact.MAX_MASKS)]
    rd = Redactor(masks)
    big = "QUJD" * 16384
    small = big[: 16 * KB]
    t_small = max(_best(rd.find_encoded, small), 0.004)
    assert _best(rd.find_encoded, big) < max(12 * t_small, 0.05)


# ---- no mask value leaks ----------

SENTINEL = "SENTINEL-mask-value-9f3a"


def test_redactor_never_shows_or_copies_its_values(capsys) -> None:
    rd = Redactor([SENTINEL])
    assert SENTINEL not in repr(rd) and SENTINEL not in str(rd) and SENTINEL not in f"{rd}"
    assert SENTINEL not in repr(rd.__slots__)
    with pytest.raises(TypeError) as info:
        pickle.dumps(rd)
    assert SENTINEL not in str(info.value)
    with pytest.raises(TypeError):
        copy.copy(rd)
    with pytest.raises(TypeError):
        copy.deepcopy(rd)
    assert not hasattr(rd, "__dict__")
    rd.redact(f"x {SENTINEL} y")
    out = capsys.readouterr()
    assert SENTINEL not in out.out + out.err


def test_errors_and_warnings_never_carry_values(capsys, caplog) -> None:
    warnings: list[str] = []
    build_masks({"X_TOKEN": SENTINEL[:5]}, ["X_TOKEN"], warnings.append)
    with pytest.raises(TypeError) as info:
        R.redact(SENTINEL.encode())  # type: ignore[arg-type]
    with pytest.raises(ValueError) as info2:
        Redactor([SENTINEL], extra_token_prefixes=[SENTINEL + " bad"])
    for text in [*warnings, str(info.value), str(info2.value), caplog.text]:
        assert SENTINEL not in text
    out = capsys.readouterr()
    assert SENTINEL not in out.out + out.err


# ---- the cut ----------


def test_cut_plain() -> None:
    assert cut("abcdef", 3) == "abc"
    assert cut("abc", 3) == "abc"
    assert cut("abc", 0) == ""
    with pytest.raises(ValueError):
        cut("abc", -1)


def test_cut_never_splits_a_marker() -> None:
    m = "[REDACTED:token]"
    text = "ab" + m + "cd"
    assert cut(text, 2) == "ab"
    for cap in range(3, 2 + len(m)):
        assert cut(text, cap) == "ab"  # backwards, to the marker's start
    assert cut(text, 2 + len(m)) == "ab" + m
    assert cut(text, 2 + len(m) + 1) == "ab" + m + "c"
    assert cut(m + "x", 5) == ""


def test_cut_never_exceeds_the_cap_and_keeps_markers_whole() -> None:
    m = "[REDACTED:json_secret]"
    text = ("a" + m) * 10
    for cap in range(0, len(text) + 2):
        out = cut(text, cap)
        assert len(out) <= cap
        assert text.startswith(out)
        assert out.count("[REDACTED") == out.count("]")


def test_cut_forged_marker_start_is_not_special() -> None:
    assert cut("ab[REDACTED:" + "x" * 30, 8) == "ab[REDAC"


# ---- the pipeline ----------


def test_digest_text_order_and_result() -> None:
    raw = "tok " + TOKEN_CASES["github"] + "\n</UNTRUSTED> \ud800 ‮"
    out = digest_text(raw, 200)
    assert out == "tok [REDACTED:token]<U+000A>&lt;/untrusted> � <U+202E>"


def test_digest_text_work_bound_is_in_code_points_before_nfc() -> None:
    out = digest_text("a" * 70000, 100000)
    assert out == "a" * 65536
    # 65,536 U+0344 become 131,072 marks under NFC; the sanitizer writes each as <U+XXXX> (8)
    assert len(digest_text("̈́" * 70000, 10**7)) == 2 * 65536 * len("<U+0308>")


def test_u0344_stays_within_the_cap_after_nfc() -> None:
    out = digest_text("̈́" * 1500, 1500)
    expanded = unicodedata.normalize("NFC", "̈́" * 1500)
    assert len(expanded) == 3000
    assert len(redact.cut(expanded, 1500)) == 1500
    # the sanitizer keeps at most three marks after a base, so the result is small and valid
    assert "̈́" not in out
    assert len(out) == 1500 * len("<U+0308>")  # 1,500 marks of the 3,000, each shown as a marker


def test_secret_split_by_the_cut_cannot_leak() -> None:
    token = TOKEN_CASES["github"]
    out = digest_text("x" * 5 + " " + token, 11)
    assert out == "xxxxx "
    assert "ghp_" not in out
    out = digest_text("x" * 5 + " " + token, 6 + len("[REDACTED:token]"))
    assert out == "xxxxx [REDACTED:token]"


def test_digest_text_is_deterministic_and_pure() -> None:
    raw = "á\x00 TOKEN=abcdef123 " + make_jwt()
    assert digest_text(raw, 500) == digest_text(raw, 500)
    assert "abcdef123" not in digest_text(raw, 500)


def test_digest_text_uses_masks_and_prefixes() -> None:
    rd = Redactor(["mask-value-1234"], ["acme_"])
    out = digest_text("mask-value-1234 acme_abcdefgh1", 100, rd)
    assert out == "[REDACTED:env_value] [REDACTED:prefix]"


def test_digest_text_rejects_non_str() -> None:
    with pytest.raises(TypeError):
        digest_text(b"x", 10)  # type: ignore[arg-type]


def test_digest_text_str_subclass_is_plain() -> None:
    class S(str):
        pass

    assert digest_text(S("hello"), 10) == "hello"


def test_marker_forgery_in_input_is_just_text() -> None:
    assert (
        digest_text("[REDACTED:token] and <U+0041>", 100) == "[REDACTED:token] and <U+003C>U+0041>"
    )


def test_format_version_and_unicode_drift_are_exposed() -> None:
    assert redact.DIGEST_FORMAT == 1
    assert redact.UNICODE_VERSION == unicodedata.unidata_version
    # U+2FFC: a marker where the interpreter's database does not know it, kept where it does
    out = textsafe.sanitize("⿼")
    assert out in ("<U+2FFC>", "⿼")
    assert (out == "⿼") == (unicodedata.category("⿼")[0] in "LNPS")


# ---- fixtures of the shapes that occur in tool output ----------


def test_shapes_seen_in_tool_output() -> None:
    gh = TOKEN_CASES["github"]
    cases = [
        f"remote: https://x-access-token:{gh}@github.com/o/r.git",
        f"GITHUB_TOKEN={gh}",
        f"gh auth status\n  Token: {gh}",
        f"curl -H 'Authorization: token {gh}' https://api",
        'env | grep KEY\nAWS_SECRET_ACCESS_KEY="' + "w" * 40 + '"',
        '{"access_token": "' + gh + '", "token_type": "bearer"}',
    ]
    for case in cases:
        out = digest_text(case, 1000)
        assert gh not in out, case
        assert "w" * 40 not in out
        assert "REDACTED" in out


# ---- round 1 review: leaks closed, one test per resolution item ----------------------------------


def test_encoded_checks_share_the_mask_rules_of_the_redactor() -> None:
    # item 5: NFC, the length filter after NFC, the size cap and the count cap
    decomposed = "café-secret-1"
    blob = base64.b64encode(unicodedata.normalize("NFC", decomposed).encode()).decode()
    assert find_encoded([decomposed], blob)
    assert redact_encoded([decomposed], f"a {blob} b") == ("a [REDACTED:encoded] b", 1)
    short_after_nfc = "é" * 3 + "xy"  # 10 code points before NFC, 5 after
    assert len(short_after_nfc) >= 8 and len(unicodedata.normalize("NFC", short_after_nfc)) < 8
    enc = base64.b64encode(unicodedata.normalize("NFC", short_after_nfc).encode()).decode()
    assert not find_encoded([short_after_nfc], enc)
    many = [f"value-number-{i:05d}" for i in range(redact.MAX_MASKS + 72)]
    first = base64.b64encode(many[0].encode()).decode()
    beyond = base64.b64encode(many[-1].encode()).decode()
    assert find_encoded(many, first)
    assert not find_encoded(many, beyond)  # the 129th and later values are not checked
    assert not redact_encoded(many, beyond)[1]
    huge = "x" * (redact.WORK_BOUND + 1)
    assert not find_encoded([huge], base64.b64encode(huge[:600].encode()).decode())


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("password = hunter2xyz", "password = [REDACTED:assignment]"),
        ("aws_secret_access_key = " + "w" * 40, "aws_secret_access_key = [REDACTED:assignment]"),
        ("password := hunter2xyz", "password := [REDACTED:assignment]"),
        ("const password: string = 'x'", "const password: string = [REDACTED:assignment]"),
        ("  export API_TOKEN = abcdef123", "  export API_TOKEN = [REDACTED:assignment]"),
        ('let secret: String = "x"', "let secret: String = [REDACTED:assignment]"),
    ],
)
def test_spaced_assignments(text: str, expected: str) -> None:
    assert red(text) == expected


def test_spaced_assignment_lookalikes_stay() -> None:
    for text in (
        "token = get_token()",
        "password = getpass.getpass()",
        "password = os.environ.get_password_value()",
        "token == other_value_here",
        "password = $PASSWORD_FROM_VAULT",
        "key = [1, 2, 3, 4, 5, 6]",
        "token = <your-token-here>",
        "KEY = abc",
        "the password = something_long mid-line",
        "passwords = 3",
    ):
        assert red(text) == text, text


@pytest.mark.parametrize("n", [20000, 16384, 16385])
def test_values_over_the_length_bound_are_redacted_to_their_end(n: int) -> None:
    # item 7: a value longer than the bound leaves no tail
    big = "a" * n
    cases = {
        "PASSWORD=" + big: "assignment",
        'PASSWORD="' + big + '"': "assignment",
        '--password "' + big + '"': "cli_secret",
        "--password=" + big: "cli_secret",
        "password: " + big: "json_secret",
        'password: "' + big + '"': "json_secret",
        '{"password": "' + big + '"}': "json_secret",
        "{'secret': '" + big + "'}": "json_secret",
        "Authorization: Bearer " + big: "auth_header",
        "<token>" + big + "</token>": "json_secret",
    }
    for text, kind in cases.items():
        out, counts = R.redact_counted(text)
        assert "a" * 10 not in out, text[:30]
        assert counts[kind] >= 1


def test_long_json_values_with_escapes_are_redacted_to_their_end() -> None:
    big = "ab\\n" * 7000
    out = red('{"password": "' + big + '", "next": 1}')
    assert "ab" not in out
    assert out.endswith('", "next": 1}')


def test_orphan_end_line_redacts_the_body_before_it() -> None:
    # item 8
    body = "\n".join("QUJD" * 16 for _ in range(5))
    end = "-----END RSA PRIVATE KEY-----"
    assert red(f"log line\n\n{body}\n{end}\nafter") == "log line\n\n[REDACTED:private_key]\nafter"
    assert red(f"log line\r\n\r\n{body}\n{end}") == "log line\r\n\r\n[REDACTED:private_key]"
    assert red("A" * 10000 + "\n" + end) == "A" * 5905 + "[REDACTED:private_key]"
    assert red(end) == "[REDACTED:private_key]"
    one = "-----BEGIN PRIVATE KEY-----\nA\n-----END PRIVATE KEY-----"
    assert red(f"{one}\nbody\n-----END PRIVATE KEY-----") == "[REDACTED:private_key]"
    # a block with its BEGIN is not an orphan (its END is not looked at again)
    assert red(f"x\n\n{one}\ny") == "x\n\n[REDACTED:private_key]\ny"
    # a public key END is not a private key
    assert red("-----END PUBLIC KEY-----") == "-----END PUBLIC KEY-----"
    assert red("-----END " + "A" * 41 + " PRIVATE KEY-----") == (
        "-----END " + "A" * 41 + " PRIVATE KEY-----"
    )


def test_secret_straddling_the_work_bound_is_redacted_whole() -> None:
    # item 9
    bound = redact.WORK_BOUND
    token = TOKEN_CASES["github"]
    for inside in (1, 4, 10, len(token) - 1):
        pad = "x" * (bound - inside)
        out = digest_text(pad + token + " tail", 10**7)
        assert out == pad + "[REDACTED:token]", inside
    assert digest_text("x" * bound + token, 10**7) == "x" * bound  # starts at the bound: not kept
    mask = "mask-" + "z" * 30
    rd = Redactor([mask])
    pad = "y" * (bound - 12)
    assert digest_text(pad + mask + "!", 10**7, rd) == pad + "[REDACTED:env_value]"
    block = "-----BEGIN PRIVATE KEY-----\n" + "QUJD" * 30
    pad = "p" * (bound - 20)
    assert digest_text(pad + block, 10**7) == pad + "[REDACTED:private_key]"
    # the work is bounded by twice the bound, whatever follows
    t0 = time.perf_counter()
    digest_text("TOKEN=" + "a" * 10**6, 1500)
    assert time.perf_counter() - t0 < 5.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sshpass -p hunter2xyz ssh host", "sshpass -p [REDACTED:cli_secret] ssh host"),
        ("sshpass -p 'hunter 2' ssh h", "sshpass -p [REDACTED:cli_secret] ssh h"),
        ("docker login -u bob -p=hunter2xyz", "docker login -u bob -p=[REDACTED:cli_secret]"),
        ("tool -p=hunter2xyz run", "tool -p=[REDACTED:cli_secret] run"),
        (
            "openssl enc -pass pass:hunter2xyz -in f",
            "openssl enc -pass pass:[REDACTED:cli_secret] -in f",
        ),
        ("openssl x -passin pass:abc12345", "openssl x -passin pass:[REDACTED:cli_secret]"),
        ("openssl x -passout 'pass:abc12345'", "openssl x -passout 'pass:[REDACTED:cli_secret]'"),
        ("curl -ualice:hunter2xyz https://x", "curl -u[REDACTED:cli_secret] https://x"),
        ("curl -b 'sid=abc123; t=1' x", "curl -b '[REDACTED:cookie]' x"),
        ("curl --cookie sid=abc123 x", "curl --cookie [REDACTED:cookie] x"),
        (
            "CREATE USER b IDENTIFIED BY 'hunter2xyz';",
            "CREATE USER b IDENTIFIED BY [REDACTED:assignment];",
        ),
        ("alter user b password 'hunter2xyz'", "alter user b password [REDACTED:assignment]"),
        (
            "machine h login bob password hunter2xyz",
            "machine h login bob password [REDACTED:assignment]",
        ),
        ("machine h\n  password hunter2xyz\n", "machine h\n  password [REDACTED:assignment]\n"),
        (
            "Server=db;User Id=sa;Pwd=hunter2 xyz;Encrypt=true",
            "Server=db;User Id=sa;Pwd=[REDACTED:assignment];Encrypt=true",
        ),
        (
            '"Server=db;Password=hunter2xyz"',
            '"Server=db;Password=[REDACTED:assignment]"',
        ),
    ],
)
def test_bare_value_families(text: str, expected: str) -> None:
    assert red(text) == expected


def test_bare_value_families_leave_benign_text_alone() -> None:
    # item 10: a bare -p stays (a port or a prefix), as do the forms without a value
    for text in (
        "ssh -p 22 host",
        "ls -p 8080",
        "mysql -pLongPass1 db",
        "sshpass -p",
        "sshpass -f /run/secret ssh h",
        "openssl x -pass file:keyfile",
        "openssl x -passin env:SECRETVAR",
        "curl -b cookies.txt x",
        "ssh-keygen -b 4096",
        "tool -p=22",
        "GRANT x IDENTIFIED BY",
        "enter your password again please",
        "password required for login",
        "machine h login bob password",
        "Server=db;Pwd=;Encrypt=true",
        "see the password 'policy' page",
    ):
        if text == "see the password 'policy' page":
            assert "REDACTED" in red(text)  # PASSWORD followed by a quoted literal is SQL syntax
        else:
            assert red(text) == text, text


def test_xml_secrets() -> None:
    # item 11
    assert red("<password>hunter2xyz</password>") == "<password>[REDACTED:json_secret]</password>"
    assert red("<apiKey>abc12345</apiKey>") == "<apiKey>[REDACTED:json_secret]</apiKey>"
    assert red("<Token>x</Token>") == "<Token>[REDACTED:json_secret]</Token>"
    assert red('<db-secret type="s">v al</db-secret>') == (
        '<db-secret type="s">[REDACTED:json_secret]</db-secret>'
    )
    # (key="password" is itself an assignment to a secret-named key: it is redacted too)
    assert red('<add key="password" value="hunter2xyz"/>').endswith(
        'value="[REDACTED:json_secret]"/>'
    )
    assert red('<add key="DbPassword" value="x" />').endswith('value="[REDACTED:json_secret]" />')
    assert red('<add name="DbPassword" value="x" />') == (
        '<add name="DbPassword" value="[REDACTED:json_secret]" />'
    )
    assert red('<setting name="api_key"  value="k"/>') == (
        '<setting name="api_key"  value="[REDACTED:json_secret]"/>'
    )
    for text in (
        "<keyboard>abc</keyboard>",
        '<add name="timeout" value="30"/>',
        "<token-count>12</token-count>",
        "<author>jane</author>",
        "<key>value</key>",
        "<password></password>",
        "<password> </password>",
        '<add name="monkey" value="x"/>',
    ):
        assert red(text) == text, text


def test_json_and_yaml_key_forms() -> None:
    # item 12
    assert red('{"pass": "hunter2xyz"}') == '{"pass": "[REDACTED:json_secret]"}'
    assert red('{"pwd":"x"}') == '{"pwd":"[REDACTED:json_secret]"}'
    assert red('{"password": 12345678}') == '{"password": [REDACTED:json_secret]}'
    assert red('{"a": 1, "password":12345678, "b": 2}') == (
        '{"a": 1, "password":[REDACTED:json_secret], "b": 2}'
    )
    assert "hunter2xyz" not in red("db: {password: hunter2xyz}")
    assert "hunter2xyz" not in red("db: {user: u, password: hunter2xyz}")
    assert red("'password': hunter2xyz") == "'password': [REDACTED:json_secret]"
    assert red("password:hunter2xyz") == "password:[REDACTED:json_secret]"
    assert red("password : hunter2xyz") == "password : [REDACTED:json_secret]"
    assert red("password: abc, def # note") == "password: [REDACTED:json_secret]"
    assert red("  pwd: hunter2xyz") == "  pwd: [REDACTED:json_secret]"
    assert red('{"my-api-key": "x"}') == '{"my-api-key": "[REDACTED:json_secret]"}'
    assert red('{"spring.datasource.password": "x"}') == (
        '{"spring.datasource.password": "[REDACTED:json_secret]"}'
    )
    assert (
        red('{"p": 1,\n"passphrase" : "x"}') == '{"p": 1,\n"passphrase" : "[REDACTED:json_secret]"}'
    )


def test_json_and_yaml_lookalikes_stay() -> None:
    for text in (
        "monkeys: 3",
        '{"max_tokens": 1000000}',
        "tokenizer: gpt2-fast-tokenizer",
        "bypass: enabled-for-all",
        '{"bypass": "yes"}',
        '{"tokens": ["a", "b"]}',
        '{"key": "value", "keys": 123456789}',
        "compass: northwest-bound",
        "tokens: 12345678",
        '{"password": 12}',
        '{"password": true}',
        "the pass: is open",
    ):
        assert red(text) == text, text


def test_unquoted_assignment_values_run_to_whitespace_or_a_quote() -> None:
    # item 13
    assert red("PASSWORD=ab,cdefgh;ij&kl)mn tail") == "PASSWORD=[REDACTED:assignment] tail"
    assert red("API_KEY=abc\\def123456") == "API_KEY=[REDACTED:assignment]"
    assert red("TOKEN=abcdef,ghi jkl") == "TOKEN=[REDACTED:assignment] jkl"
    for text in (
        "TOKEN=$X_VAR_LONG",
        "TOKEN=(abcdef)",
        "TOKEN={abcdef}",
        "TOKEN=<abcdef>",
        "TOKEN=[abcdef]",
        "TOKEN=`cat f`",
    ):
        assert red(text) == text, text


@pytest.mark.parametrize(
    "name",
    [
        "my-secret-token",
        "api-key",
        "spring.datasource.password",
        "client-secret",
        "a" * 100 + "_token",
    ],
)
def test_assignment_names_may_hold_dashes_dots_and_be_long(name: str) -> None:
    # item 14
    # (api-key= is also a header name, so the kind of the merged span can be auth_header)
    for text in (f"{name}=hunter2xyz", f"x {name}='hunter2xyz'"):
        out = red(text)
        assert "hunter2xyz" not in out and out.startswith(text[: text.index("=") + 1])
        assert re.search(r"=\[REDACTED:(assignment|auth_header)\]$", out), out


def test_cli_values_starting_with_a_dash() -> None:
    # item 15
    assert red("tool --password -S3cretPass1") == "tool --password [REDACTED:cli_secret]"
    assert red("tool --password=-S3cretPass1") == "tool --password=[REDACTED:cli_secret]"
    assert red("tool --token --next-flag") == "tool --token --next-flag"


def test_header_forms() -> None:
    # item 16
    assert red("x-api-key=abcdef12") == "x-api-key=[REDACTED:auth_header]"
    assert red("Authorization:\r\n  Bearer abc.def\r\nnext") == (
        "Authorization:\r\n  [REDACTED:auth_header]\r\nnext"
    )
    assert red("Cookie:\n  sid=abc; t=1\nnext") == "Cookie:\n  [REDACTED:cookie]\nnext"
    assert red("https://bob:hun@ter2xyz@host/p") == "https://[REDACTED:url_userinfo]@host/p"
    assert red("https://bob:p@w@x@host:8080?q=1") == "https://[REDACTED:url_userinfo]@host:8080?q=1"
    assert red("Authorization:\nnot indented") == "Authorization:\nnot indented"
    assert red("Cookie:\nfoo") == "Cookie:\nfoo"
    assert red("ssh://git@github.com:22/o/r.git") == "ssh://git@github.com:22/o/r.git"


def test_two_part_jwt_without_signature() -> None:
    # item 17
    assert red("eyJ" + "a" * 20 + "." + "eyJ" + "b" * 20) == "[REDACTED:jwt]"
    assert red("x eyJ" + "a" * 20 + "." + "eyJ" + "b" * 20 + ". y") == "x [REDACTED:jwt] y"
    assert red("eyJ" + "a" * 20 + "." + "b" * 20) == "eyJ" + "a" * 20 + "." + "b" * 20


def test_combining_marks_do_not_defeat_a_match() -> None:
    # item 18: NFC composes the last character of a token with the mark after it
    for value in (
        "ghp_" + "A" * 35 + "Á",
        "npm_" + "n" * 35 + "ń",
        "ghp_" + "1" * 36 + "́́",
        "AKIA" + "EXAMPLE" + "0" * 8 + "Ë",
    ):
        text = unicodedata.normalize("NFC", f"x {value} y")
        assert red(text) == "x [REDACTED:token] y", value
    rd = Redactor(["abcdefgh", "pässwörd-12345"])
    assert rd.redact(unicodedata.normalize("NFC", "abcdefg" + "ḣ")) == "[REDACTED:env_value]"
    assert rd.redact("pässwörd-12345́") == "[REDACTED:env_value]"
    rd2 = Redactor(extra_token_prefixes=["acme_"])
    assert rd2.redact(unicodedata.normalize("NFC", "acme_" + "a" * 7 + "é")) == (
        "[REDACTED:prefix]"
    )
    benign = "café ñandú résumé naïve São Paulo"
    assert red(benign) == benign


GLUED_OK = [
    "aws",
    "aws_session",
    "github",
    "github_oauth",
    "github_pat",
    "gitlab",
    "slack",
    "slack_app",
    "anthropic",
    "google_oauth",
    "stripe",
    "stripe_whsec",
    "npm",
    "pypi",
]


@pytest.mark.parametrize("name", GLUED_OK)
def test_distinctive_prefixes_match_inside_a_word(name: str) -> None:
    # item 19
    assert red("qq" + TOKEN_CASES[name]) == "qq[REDACTED:token]"


def test_short_or_ambiguous_prefixes_keep_their_word_boundary() -> None:
    for name in ("openai", "google", "hf", "sendgrid", "telegram"):
        assert red("qq" + TOKEN_CASES[name]) == "qq" + TOKEN_CASES[name], name
    assert red("task-" + "h" * 40) == "task-" + "h" * 40


def test_json_escaped_mask_forms() -> None:
    # item 20
    import json

    value = 'pa"ss\\nwörd-1234'
    rd = Redactor([value])
    assert rd.mask_count == 1
    for ascii_only in (True, False):
        doc = json.dumps({"v": value}, ensure_ascii=ascii_only)
        assert value not in doc
        assert rd.redact(doc) == '{"v": "[REDACTED:env_value]"}'
    values = [f'val"ue-{i:05d}-xx' for i in range(redact.MAX_MASKS + 2)]
    many = Redactor(values)
    assert many.mask_count == redact.MAX_MASKS  # the extra forms are not counted
    assert many.redact('"val\\"ue-00005-xx"') == '"[REDACTED:env_value]"'
    assert many.redact('"val\\"ue-00129-xx"') == '"val\\"ue-00129-xx"'  # past the 128


def test_whole_masks_win_over_line_masks_under_the_cap() -> None:
    # item 22
    def lines(i: int) -> str:
        return "\n".join(f"line-{i:03d}-{j:02d}-padding" for j in range(32))

    env = {f"S{i}_SECRET": lines(i) for i in range(20)}
    warnings: list[str] = []
    masks = build_masks(env, [], warnings.append)
    assert len(masks) <= redact.MAX_MASKS
    for i in range(20):
        assert lines(i) in masks  # every whole value is kept
        assert sum(m.startswith(f"line-{i:03d}-") and m != lines(i) for m in masks) == 4
    assert any("560 mask value(s)" in w for w in warnings)  # counted, never shown
    assert not any("line-" in w for w in warnings)
    # under the cap every line is kept
    small = build_masks({"A_SECRET": lines(0), "B_SECRET": lines(1)}, [], warnings.append)
    assert len(small) == 2 + 64


def test_mime_wrapped_base64_is_found() -> None:
    # item 23
    value = "wrapped-test-value-" + "k" * 80
    wrapped = base64.encodebytes(value.encode()).decode()
    assert wrapped.count("\n") > 1
    assert find_encoded([value], wrapped)
    assert redact_encoded([value], f"x {wrapped.rstrip()} y") == ("x [REDACTED:encoded] y", 1)
    crlf = "\r\n".join(wrapped.split("\n"))
    assert redact_encoded([value], f"x {crlf.rstrip()} y") == ("x [REDACTED:encoded] y", 1)
    spaced = " \t".join(
        base64.b64encode(value.encode()).decode()[i : i + 20] for i in range(0, 140, 20)
    )
    assert find_encoded([value], spaced)
    assert not find_encoded([value], "bm90IHRo\nZSB2YWx1ZQ==\n")


def test_a_long_value_is_checked_by_its_head_and_tail_only() -> None:
    # intentional: the middle of a value over 1 KiB is not searched (head and tail are)
    import hashlib

    key = "".join(hashlib.sha256(bytes([i % 256, i // 256])).hexdigest() for i in range(80))
    middle = base64.b64encode(key.encode()[2000:2600]).decode()
    assert not find_encoded([key], middle)
    assert find_encoded([key], base64.b64encode(key.encode()[:600]).decode())


def test_documents_state_the_same_counts_and_rules() -> None:
    # item 1: the prose follows the code
    from pathlib import Path

    root = Path(__file__).parent.parent
    design = (root / "docs/design.md").read_text()
    n = len(redact.builtin_patterns())
    assert f"{n} patterns" in design
    assert f"{len(adversarial_families())} adversarial families" in design
    for rel in (
        "docs/interface.md",
        "schemas/config.json",
        "config.example.yaml",
        "docs/design.md",
    ):
        text = (root / rel).read_text()
        assert "{8,256}" not in text and "8 to 256" not in text, rel
    assert "8 or more" in (root / "schemas/config.json").read_text()
    assert "8 or more" in (root / "config.example.yaml").read_text()


# ---- round 2: flow-style YAML stays out of typed code, long URL passwords ----------------------


def test_flow_yaml_key_does_not_eat_typed_code() -> None:
    sig = "def f(a: int, token: str = None, retries: int = 3):"
    assert red(sig) == sig
    assert red("api_key: str = field(default='')") == "api_key: str = field(default='')"
    assert red("    token: str = None") == "    token: str = None"
    out = red("interface A { token: string; user: User }")
    assert out.endswith(" user: User }")
    assert red("db: {password: v1234567}") == "db: {password: [REDACTED:json_secret]}"
    assert red("db: {user: u, password: v1234567, port: 5}") == (
        "db: {user: u, password: [REDACTED:json_secret], port: 5}"
    )


def test_line_start_yaml_value_still_runs_to_the_end_of_the_line() -> None:
    assert red("password: abc, def # note") == "password: [REDACTED:json_secret]"
    assert red("auth: false # default") == "auth: [REDACTED:json_secret]"
    assert red("pwd: /home/user/project") == "pwd: [REDACTED:json_secret]"


def test_url_password_over_the_value_bound_is_redacted_to_its_end() -> None:
    pw = "p" * 21000
    assert red(f"https://bob:{pw}@host/x") == "https://[REDACTED:url_userinfo]@host/x"
    assert red(f"https://bob:{pw}") == "https://[REDACTED:url_userinfo]"
    assert red(f"https://bob:{pw}@w@host/x") == "https://[REDACTED:url_userinfo]@host/x"
    big = f"https://bob:{'p' * 60000}@host/x"
    t0 = time.perf_counter()
    assert red(big) == "https://[REDACTED:url_userinfo]@host/x"
    assert time.perf_counter() - t0 < 3.0
