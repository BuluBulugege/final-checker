"""Input parsing: turn a blob of pasted text into a list of credential strings.

Most providers are one-key-per-line. GCP service-account keys are instead a
multi-line JSON object. This splitter pulls out every balanced top-level JSON
object ``{...}`` as ONE credential, and treats the remaining non-JSON lines as
one credential per line. Order is preserved by position in the original text.
"""

from __future__ import annotations


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

    - Each balanced top-level ``{...}`` JSON object becomes one credential.
    - Text outside JSON blocks is split by line, one credential per non-empty line.
    Positions are interleaved so the output order matches the input order.
    """
    # Strip a UTF-8 BOM / zero-width junk that editors and web copies often
    # prepend; an invisible char before "{" would otherwise hide the JSON block
    # and the whole key would be split line-by-line ("no plugin matched").
    raw = raw.replace("﻿", "").replace("​", "")
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
