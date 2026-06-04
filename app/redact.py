"""Secret redaction. Kept dependency-free so every layer (http, ratetest, jobs,
plugins) can scrub key-like tokens from arbitrary text before it is ever sent to
a client or written to a log.
"""

from __future__ import annotations

import re

# Token shapes that could be a live secret: provider key prefixes, long
# base64url/hex runs, and Bearer tokens.
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"[A-Za-z0-9_\-]{40,}"),  # generic long opaque token
]


def redact(text: str | None) -> str | None:
    """Scrub anything that looks like an API key/secret from arbitrary text."""
    if not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub("«redacted»", out)
    return out
