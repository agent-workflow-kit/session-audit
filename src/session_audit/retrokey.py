"""The content key of a retro (interface, Retro lifecycle, Key; issue #23).

The key is computed over the DIGESTED sessions, never over the scope or the digest bytes::

    doc = {"v": DIGEST_FORMAT, "sessions": sorted([session_id, section_sha256]),
           "skill": skill, "model": model or ""}
    key = sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True))

``section_sha256`` is the hash of exactly the section text (``fencing.section_hashes``), which
holds no nonce, so the key is the same for every build of the same content and is known before
any retro row, retro id or fence exists (Order of checks step 9). A changed transcript, changed
digest settings (they change the section text) or a changed redaction list (the same) change
the key through the hashes; ``DIGEST_FORMAT``, the skill and the model are in the document
itself. The scope is not in it, and neither is the nonce. The Unicode database of the running
Python can change a section text (see ``textsafe``); that drift is known and documented.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from .fencing import SectionHash
from .redact import DIGEST_FORMAT

__all__ = ["retro_key"]


def retro_key(sections: Iterable[SectionHash], skill: str, model: str | None) -> str:
    """The key of the digested ``sections`` (every skip already applied), the skill name and the
    model (None or empty both mean "Claude Code's default": the key holds ``""``)."""
    parts = sorted((s.session_id, s.sha256) for s in sections)
    doc = {
        "v": DIGEST_FORMAT,
        "sessions": [[sid, digest] for sid, digest in parts],
        "skill": skill,
        "model": model or "",
    }
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
