"""Text normalisation and content hashing.

Deduplication and citation verification both depend on these functions, and they depend
on them agreeing forever. ``content_hash`` is the document-level idempotency key, so a
change to :func:`normalise_text` changes what counts as a duplicate across the whole
corpus; ``clean_text`` is also the exact text that Phase 2 citation verification matches
quotes against, so loosening normalisation here silently loosens what counts as a
verified quote.

Treat any change to these functions as a migration, not a tweak.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

#: Runs of any whitespace collapse to a single space. Newlines included: a source that
#: re-flows its paragraphs must not read as a different document.
_WHITESPACE_RUN = re.compile(r"\s+")

#: Zero-width and bidirectional control characters. Publishers emit these by accident
#: (usually via a CMS copy-paste) and they would otherwise defeat both hashing and quote
#: matching while being invisible to a reviewer comparing the two strings.
#:
#: Built from explicit code point ranges rather than written as literals, because
#: literal invisible characters in source are unreviewable.
_INVISIBLE_RANGES = (
    (0x200B, 0x200F),  # zero-width space/non-joiner/joiner, LTR/RTL marks
    (0x202A, 0x202E),  # bidirectional embedding and override
    (0x2060, 0x2064),  # word joiner, invisible operators
    (0xFEFF, 0xFEFF),  # byte order mark appearing mid-document
)
_INVISIBLE = re.compile("[" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in _INVISIBLE_RANGES) + "]")


def normalise_text(text: str) -> str:
    """Canonical form of a document's text.

    NFKC folds compatibility forms (ligatures, full-width characters, non-breaking
    spaces) so that visually identical text hashes identically. Invisible control
    characters are dropped, then all whitespace runs collapse to single spaces.
    """
    normalised = unicodedata.normalize("NFKC", text)
    normalised = _INVISIBLE.sub("", normalised)
    normalised = normalised.replace("\r\n", "\n").replace("\r", "\n")
    normalised = _WHITESPACE_RUN.sub(" ", normalised)
    return normalised.strip()


def sha256_text(text: str) -> str:
    """SHA-256 of text, hashed over its UTF-8 encoding."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(payload: bytes) -> str:
    """SHA-256 of raw response bytes, before any decoding or normalisation."""
    return hashlib.sha256(payload).hexdigest()


def content_hash(text: str) -> str:
    """The deduplication key: SHA-256 of the normalised text."""
    return sha256_text(normalise_text(text))


def metadata_hash(title: str | None, url: str, published_at: str | None) -> str:
    """Deduplication key for sources we may not store the body of.

    Where a source's terms permit only headline, link, and publication time
    (``sources.store_full_text = false``), there is no body to hash, so identity is the
    normalised composite of what we are allowed to keep. Fields are joined with a
    separator that cannot occur in normalised text, so ("ab", "c") and ("a", "bc") cannot
    collide.
    """
    parts = [normalise_text(title or ""), normalise_text(url), normalise_text(published_at or "")]
    return sha256_text("\x1f".join(parts))
