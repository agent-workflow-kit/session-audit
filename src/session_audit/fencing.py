"""Fencing, section hashes and the manifest of a digest (issue #22).

``finish_digest(result, retro_id, run_token, gateway, csession_ids=...)`` turns the sections of a
``DigestResult`` (``digest.build_digest``) into the two files of the retro's inputs:

* each section is rendered WITHOUT the fence first; ``section_sha256`` is the SHA-256 of exactly
  ``SessionSection.text`` (the lines between the fences, each ended by LF);
* the per-digest nonce (``gateway.token_hex(16)``: 32 lowercase hex characters, random, drawn after
  the text exists so no content can know it) is then put into the two fence lines of every
  section. The nonce never enters a section hash, so the hashes (and the retro key built from
  them) are the same for every build of the same content; ``digest_sha256`` is over the final
  bytes of ``digest.md`` and differs between builds, an identity echo only;
* ``manifest.json`` (``schemas/slot/manifest.json``) holds ids, counts and enums only: no
  transcript-derived string and no project.

Text never carries a fence tag of its own: ``redact.digest_text`` neutralizes ``</untrusted`` and
``<untrusted`` in every part, and ``finish_digest`` checks every section again (a section that
holds a fence tag, a line break inside a line, or the nonce is refused or, for the nonce, drawn
again, at most ``NONCE_TRIES`` times). The function writes no file: the retro launch writes
``digest.md`` and ``manifest.json`` (TODO(#24)).
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from . import grammar, textsafe
from .digest import FENCE_CLOSE, FENCE_OPEN, NONCE_HEX_LEN, DigestResult, SessionSection
from .gateway import Gateway

__all__ = [
    "FINDING_CATEGORIES",
    "LIMITS",
    "NONCE_TRIES",
    "TARGET_CLASSES",
    "FencedDigest",
    "SectionHash",
    "finish_digest",
]

NONCE_BYTES = NONCE_HEX_LEN // 2
NONCE_TRIES = 8  # a nonce found in the content is drawn again; a real digest never needs a second
_NONCE = re.compile(rf"[0-9a-f]{{{NONCE_HEX_LEN}}}", re.ASCII)
_RUN_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,128}", re.ASCII)
_FENCE_TAG = re.compile(r"(?i)</?untrusted")
# schemas/defs.json ref, schemas/slot/manifest.json item id
_REF = re.compile(r"s([1-9][0-9]{0,1}|[1-4][0-9]{2}|500)", re.ASCII)
_ITEM_REF = re.compile(r"s([1-9][0-9]{0,1}|[1-4][0-9]{2}|500)\.i[0-9]{3,6}", re.ASCII)

# The limits the retro session is given (Slot contract, Inputs; Retro artifact).
LIMITS: dict[str, Any] = {
    "max_findings": 50,
    "max_candidates": 20,
    "text_limits": {
        "summary": 2000,
        "title": 120,
        "detail": 1500,
        "target_hint": 200,
        "proposal": 1500,
    },
}
FINDING_CATEGORIES = ("went_well", "went_badly", "friction", "cost")
TARGET_CLASSES = ("skill", "instructions_file", "config", "permission_profile", "workflow")


@dataclass(frozen=True, slots=True)
class SectionHash:
    """The hash of one kept session: what the retro key is built from."""

    ref: str
    session_id: str
    sha256: str


@dataclass(frozen=True, slots=True)
class FencedDigest:
    """The finished digest. ``digest_bytes`` is ``digest.md`` and ``manifest_bytes`` is
    ``manifest.json`` (``manifest`` is the same document as a dict). ``unicode_version`` and
    ``digest_format`` are the facts the section hashes depend on (the manifest schema is closed
    and has no place for them: they are for the caller's log and key)."""

    digest_bytes: bytes = field(repr=False)
    digest_sha256: str
    manifest: dict[str, Any] = field(repr=False)
    manifest_bytes: bytes = field(repr=False)
    section_hashes: tuple[SectionHash, ...]
    nonce: str
    unicode_version: str
    digest_format: int


def _check_run_token(run_token: object) -> str:
    if (
        not isinstance(run_token, str)
        or len(run_token) > 128
        or not _RUN_TOKEN.fullmatch(run_token)
    ):
        raise ValueError("run_token must be 16 to 128 characters of A-Z a-z 0-9 _ -")
    return str.__str__(run_token)


def _check_section(section: SessionSection) -> None:
    """Refuse a section that could not stay inside its fence (the pipeline makes this impossible:
    this is the second wall, not the first)."""
    lines = section.lines
    for line in lines:
        # the sentinel makes a trailing or lone terminator count as a second line
        if len(f"{line}x".splitlines()) != 1:
            raise ValueError(f"section {section.ref} holds a line break inside a line")
        if _FENCE_TAG.search(line):
            raise ValueError(f"section {section.ref} holds a fence tag")


def _check_refs(section: SessionSection) -> None:
    """The refs and the session id go into the manifest: check their shape, never echo them."""
    if not _REF.fullmatch(section.ref):
        raise ValueError("a section ref is not s1 to s500")
    if not grammar.is_session_id(section.session_id):
        raise ValueError(f"the session id of {section.ref} is not a Claude Code session id")
    for item in section.items:
        if not _ITEM_REF.fullmatch(item.ref) or not item.ref.startswith(f"{section.ref}."):
            raise ValueError(f"an item ref of {section.ref} is not s<N>.i<NNN> of its section")


def _nonce(gateway: Gateway, plain: tuple[str, ...]) -> str:
    """A fresh nonce that no part of the digest text contains."""
    for _ in range(NONCE_TRIES):
        nonce = gateway.token_hex(NONCE_BYTES)
        if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
            raise ValueError("the gateway's token_hex must give 32 lowercase hex characters")
        if not any(nonce in part for part in plain):
            return nonce
    raise RuntimeError(f"no nonce free of the digest text after {NONCE_TRIES} tries")


def _manifest(
    result: DigestResult,
    retro_id: str,
    run_token: str,
    digest_sha256: str,
    digest_bytes: int,
    csession_ids: Mapping[str, str | None],
) -> dict[str, Any]:
    sessions = []
    for section in result.sections:
        csession = csession_ids.get(section.session_id)
        if csession is not None and not grammar.is_csession_id(csession):
            raise ValueError(f"csession id of {section.ref} is not a claude-session id")
        sessions.append(
            {"ref": section.ref, "csession_id": csession, "items": [i.ref for i in section.items]}
        )
    return {
        "retro_id": retro_id,
        "run_token": run_token,
        "digest_sha256": digest_sha256,
        "sessions": sessions,
        "coverage": result.coverage(digest_bytes),
        "limits": copy.deepcopy(LIMITS),
        "finding_categories": list(FINDING_CATEGORIES),
        "target_classes": list(TARGET_CLASSES),
    }


def finish_digest(
    result: DigestResult,
    retro_id: str,
    run_token: str,
    gateway: Gateway,
    csession_ids: Mapping[str, str | None] | None = None,
) -> FencedDigest:
    """Fence the sections of ``result``, hash them and describe the digest.

    ``csession_ids`` maps a Claude Code session id to the claude-session id of its harness run
    (absent or None: no match, ``null`` in the manifest). ``ValueError`` for a retro id, run token
    or section that breaks its shape; ``RuntimeError`` when no nonce is free of the text."""
    retro = grammar.parse_retro_id(retro_id, field="retro_id")
    token = _check_run_token(run_token)
    if not result.sections:
        raise ValueError("a digest needs at least one session")
    for section in result.sections:
        _check_refs(section)
        _check_section(section)
    plain = (*result.preamble(retro), *(section.text for section in result.sections))
    nonce = _nonce(gateway, plain)
    hashes = tuple(
        SectionHash(s.ref, s.session_id, hashlib.sha256(s.text.encode("utf-8")).hexdigest())
        for s in result.sections
    )
    data = result.render(
        retro, lambda _s: (FENCE_OPEN.format(nonce=nonce), FENCE_CLOSE.format(nonce=nonce))
    )
    digest_sha256 = hashlib.sha256(data).hexdigest()
    manifest = _manifest(result, retro, token, digest_sha256, len(data), csession_ids or {})
    manifest_bytes = (textsafe.dumps_json(manifest) + "\n").encode("ascii")
    return FencedDigest(
        data,
        digest_sha256,
        manifest,
        manifest_bytes,
        hashes,
        nonce,
        result.unicode_version,
        result.digest_format,
    )
