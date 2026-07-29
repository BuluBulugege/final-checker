"""Input parsing: turn a blob of pasted text into a list of credential strings.

The core splitter is provider-agnostic: it pulls out every balanced top-level
JSON object ``{...}`` as ONE credential and treats the remaining non-empty,
non-comment lines as one credential per line, preserving original order.

Provider-specific knowledge lives in the plugins, consulted via two optional
hooks (see app.plugins.base.CheckerPlugin):

- ``extract_candidates(text)`` — whole-input formats such as the provider
  arrays inside an ``all_combos.json`` aggregate export.
- ``stitch(lines, i)`` — multi-line formats such as env-var pairs or
  URL-followed-by-key pastes, folded into single-line credentials before the
  generic splitter runs.
"""

from __future__ import annotations

from app.plugins.registry import all_plugins


def _plugin_extract(raw: str) -> list[str] | None:
    """Ask each registered plugin whether the whole input is one of its
    provider-specific formats (e.g. an aggregate export). Returns the merged,
    de-duplicated credential list when at least one plugin claims the input,
    else None — including the recognized-but-empty case, which yields ``[]``.
    """
    if not raw.lstrip().startswith("{"):
        return None
    claimed = False
    credentials: list[str] = []
    seen: set[str] = set()
    for plugin in all_plugins():
        try:
            candidates = plugin.extract_candidates(raw)
        except Exception:
            continue
        if candidates is None:
            continue
        claimed = True
        for value in candidates:
            value = value.strip()
            if value and value not in seen:
                seen.add(value)
                credentials.append(value)
    return credentials if claimed else None


def _preprocess_raw(raw: str) -> str:
    """Fold multi-line credentials into single lines before the main splitter.

    The core pass handles comments, blank lines, and consumed-index bookkeeping;
    recognizing and joining provider-specific multi-line formats is delegated
    to the plugins' stitch() hooks, consulted in dispatch (priority) order.
    """
    plugins = all_plugins()
    lines = raw.split("\n")
    out: list[str] = []
    i = 0
    consumed: set[int] = set()  # line indices already folded into a previous entry

    while i < len(lines):
        if i in consumed:
            i += 1
            continue
        line = lines[i].strip()

        # skip comments and blank lines
        if line.startswith("#") or not line:
            i += 1
            continue

        stitched: tuple[str | None, set[int]] | None = None
        for plugin in plugins:
            try:
                stitched = plugin.stitch(lines, i)
            except Exception:
                stitched = None
            if stitched is not None:
                break

        if stitched is not None:
            credential, extra = stitched
            if credential:
                out.append(credential)
            consumed.update(extra)
        else:
            out.append(line)
        i += 1

    return "\n".join(out)


def _extract_json_blocks(text: str) -> list[tuple[int, int, str]]:
    """Find balanced top-level ``{...}`` blocks. Returns (start, end, block) spans.

    Brace counting is string-aware so braces inside JSON string values (and the
    private_key PEM) don't throw off the balance.
    """
    blocks: list[tuple[int, int, str]] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "{":
            depth = 0
            in_str = False
            escape = False
            j = i
            while j < n:
                c = text[j]
                if in_str:
                    if escape:
                        escape = False
                    elif c == "\\":
                        escape = True
                    elif c == '"':
                        in_str = False
                else:
                    if c == '"':
                        in_str = True
                    elif c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            blocks.append((i, j + 1, text[i : j + 1]))
                            break
                j += 1
            if depth != 0:
                # unbalanced — no closing brace; stop scanning from here
                break
            i = j + 1
        else:
            i += 1
    return blocks


def parse_credentials(raw: str) -> list[str]:
    """Split pasted text into individual credential strings.

    - Gives plugins first claim on whole-input provider formats (aggregates).
    - Pre-processes multi-line credentials via the plugins' stitch() hooks.
    - Each balanced top-level ``{...}`` JSON object becomes one credential.
    - Text outside JSON blocks is split by line, one credential per non-empty line.
    Positions are interleaved so the output order matches the input order.
    """
    from app.config import settings

    if len(raw) > settings.max_input_chars:
        raise ValueError(f"input too large (max {settings.max_input_chars} characters)")

    # Strip a UTF-8 BOM / zero-width junk that editors and web copies often
    # prepend; an invisible char before "{" would otherwise hide the JSON block
    # and the whole key would be split line-by-line ("no plugin matched").
    raw = raw.replace("﻿", "").replace("​", "")

    # Whole-input plugin formats (e.g. all_combos aggregate exports) take
    # precedence over line preprocessing, which is meant for pasted pairs
    # rather than structured JSON.
    plugin_credentials = _plugin_extract(raw)
    if plugin_credentials is not None:
        return plugin_credentials

    raw = _preprocess_raw(raw)
    blocks = _extract_json_blocks(raw)
    creds: list[tuple[int, str]] = []

    # JSON blocks, anchored at their start offset.
    for start, _end, block in blocks:
        creds.append((start, block.strip()))

    # Non-JSON regions: everything not covered by a block, split into lines.
    covered = [(s, e) for s, e, _ in blocks]
    cursor = 0
    for s, e in covered + [(len(raw), len(raw))]:
        gap = raw[cursor:s]
        offset = cursor
        for line in gap.splitlines(keepends=True):
            stripped = line.strip()
            if stripped:
                creds.append((offset, stripped))
            offset += len(line)
        cursor = e

    creds.sort(key=lambda t: t[0])
    return [c for _, c in creds]
