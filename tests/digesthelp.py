"""Helpers for the digest tests: a synthetic corpus of conversation lines, a driver that ingests it
through the real CLI and builds the digest from the stored bytes, and the fence of the byte
accounting (the fence itself is #22)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from session_audit import config
from session_audit.command import Context, Output
from session_audit.digest import (
    FENCE_CLOSE,
    FENCE_OPEN,
    NONCE_HEX_LEN,
    DigestResult,
    DigestScope,
    SessionSection,
    build_digest,
)

from .eventhelp import raw, ts
from .ingesthelp import Corpus, TickGateway, ingest, state_dir_of

# 2025-12-29 00:00 UTC: three days before the fake clock (2026-01-01), so no file is live.
OLD = 1_766_966_400
RETRO_ID = "ra-abcdefgh"
NONCE = "n" * NONCE_HEX_LEN


def fence(section: SessionSection) -> tuple[str, str]:
    return FENCE_OPEN.format(nonce=NONCE), FENCE_CLOSE.format(nonce=NONCE)


# --- lines -------------------------------------------------------------------------------------


def prompt(text: str, t: int, **extra: Any) -> bytes:
    obj: dict[str, Any] = {
        "type": "user",
        "uuid": f"u-p{t}",
        "timestamp": ts(t),
        "message": {"role": "user", "content": text},
    }
    obj.update(extra)
    return raw(obj)


def assistant(
    mid: str | None,
    content: list[dict[str, Any]],
    t: int | None,
    *,
    out: int = 10,
    inp: int = 1,
    model: str = "model-a",
    **extra: Any,
) -> bytes:
    message: dict[str, Any] = {
        "model": model,
        "usage": {"input_tokens": inp, "output_tokens": out},
        "content": content,
    }
    if mid is not None:
        message["id"] = mid
    obj: dict[str, Any] = {"type": "assistant", "message": message}
    if t is not None:
        obj["timestamp"] = ts(t)
    obj.update(extra)
    return raw(obj)


def say(mid: str | None, text: str, t: int | None, **kw: Any) -> bytes:
    return assistant(mid, [{"type": "text", "text": text}], t, **kw)


def call(mid: str, tid: str, name: str, tool_input: object, t: int | None, **kw: Any) -> bytes:
    block = {"type": "tool_use", "id": tid, "name": name, "input": tool_input}
    return assistant(mid, [block], t, **kw)


def result(tid: str, text: str, t: int, *, error: bool = False, **extra: Any) -> bytes:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tid, "content": text}
    if error:
        block["is_error"] = True
    obj: dict[str, Any] = {
        "type": "user",
        "uuid": f"u-r{tid}",
        "timestamp": ts(t),
        "message": {"role": "user", "content": [block]},
    }
    obj.update(extra)
    return raw(obj)


def denial(tid: str, text: str, t: int, kind: str = "auto") -> bytes:
    return result(tid, text, t, toolDenialKind=kind, uuid=f"u-d{t}")


def interruption(t: int, text: str = "[Request interrupted by user]") -> bytes:
    return raw(
        {
            "type": "user",
            "uuid": f"u-i{t}",
            "timestamp": ts(t),
            "interruptedMessageId": "m-x",
            "message": {"role": "user", "content": text},
        }
    )


def compact(t: int) -> bytes:
    return raw(
        {"type": "system", "subtype": "compact_boundary", "uuid": f"u-c{t}", "timestamp": ts(t)}
    )


def turn_line(ms: int, t: int) -> bytes:
    return raw({"type": "system", "subtype": "turn_duration", "durationMs": ms, "timestamp": ts(t)})


# --- the driver --------------------------------------------------------------------------------


def make_ctx(
    gw: TickGateway, home: Path, corpus: Corpus, **digest: int | str | tuple[str, ...]
) -> Context:
    ctx = Context(gateway=gw, out=Output(lambda: None), json=True)
    ctx.state_directory = str(state_dir_of(home))
    settings = config.Settings()
    update: dict[str, Any] = {"transcript_roots": (str(corpus.root),)}
    redaction = {k: v for k, v in digest.items() if k in ("mask_env", "extra_token_prefixes")}
    sizes = {k: v for k, v in digest.items() if k not in redaction}
    if sizes:
        update["digest"] = settings.digest.model_copy(update=sizes)
    if redaction:
        update["redaction"] = settings.redaction.model_copy(update=redaction)
    ctx.settings = settings.model_copy(update=update)
    return ctx


def build(
    capsys: pytest.CaptureFixture[str],
    gw: TickGateway,
    corpus: Corpus,
    scope: DigestScope | None = None,
    *,
    env: Mapping[str, str] | None = None,
    ingest_first: bool = True,
    **digest: Any,
) -> DigestResult:
    if ingest_first:
        run = ingest(capsys, gw)
        assert run.code == 0, run.err
    ctx = make_ctx(gw, corpus.home, corpus, **digest)
    return build_digest(ctx, scope or DigestScope(max_sessions=500), env=env or {})


def write(
    corpus: Corpus, session: str, lines: list[bytes], project: str = "p1", mtime: int = OLD
) -> Path:
    path = corpus.write_main(session, lines, project=project)
    os.utime(path, (mtime, mtime))
    return path


def render(result: DigestResult) -> str:
    return result.render(RETRO_ID, fence).decode("utf-8")
