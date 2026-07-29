"""Input parsing: turn a blob of pasted text into a list of credential strings.

Most providers are one-key-per-line. GCP service-account keys are instead a
multi-line JSON object. This splitter pulls out every balanced top-level JSON
object ``{...}`` as ONE credential, and treats the remaining non-JSON lines as
one credential per line. Order is preserved by position in the original text.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

_AZURE_URL_RE = re.compile(
    r"^https?://[^\s]+\.(openai\.azure\.com|services\.ai\.azure\.com|cognitiveservices\.azure\.com)",
    re.IGNORECASE,
)
_GCP_SERVICE_ACCOUNT_DOMAIN = ".iam.gserviceaccount.com"
_GCP_TOKEN_URI = "https://oauth2.googleapis.com/token"
_AZURE_HOST_SUFFIXES = (
    ".openai.azure.com",
    ".services.ai.azure.com",
    ".cognitiveservices.azure.com",
)
_AWS_ACCESS_KEY_RE = re.compile(r"^AKIA[0-9A-Z]{16}$")
_COMBO_KEYS = {"aws_iam_pairs", "azure_openai_pairs", "gcp_service_accounts"}


def _combo_credentials(obj: object) -> list[str] | None:
    """Expand an ``all_combos.json`` aggregate into checker credentials.

    Returns ``None`` for ordinary JSON so the standard JSON-block parser remains
    unchanged. Invalid/incomplete aggregate rows are skipped; in particular ASIA
    temporary credentials require a session token, which the aggregate schema
    currently does not provide.
    """
    if not isinstance(obj, dict):
        return None
    # A standalone service-account key remains one credential even if it carries
    # an unrelated metadata field named like one of the aggregate arrays.
    if obj.get("type") == "service_account" or (
        obj.get("private_key") and obj.get("client_email")
    ):
        return None
    present = _COMBO_KEYS & obj.keys()
    if not present or any(not isinstance(obj[key], list) for key in present):
        return None

    credentials: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = value.strip()
        if value and value not in seen:
            seen.add(value)
            credentials.append(value)

    aws_rows = obj.get("aws_iam_pairs", [])
    if isinstance(aws_rows, list):
        for row in aws_rows:
            if not isinstance(row, dict):
                continue
            access_key = str(row.get("access_key_id") or "").strip()
            secret_key = str(row.get("secret_access_key") or "").strip()
            if not (_AWS_ACCESS_KEY_RE.fullmatch(access_key) and secret_key):
                continue
            add(f"{access_key}:{secret_key}")

    azure_rows = obj.get("azure_openai_pairs", [])
    if isinstance(azure_rows, list):
        for row in azure_rows:
            if not isinstance(row, dict):
                continue
            endpoint = str(row.get("endpoint") or "").strip()
            api_key = str(row.get("api_key") or "").strip()
            try:
                parsed = urlparse(endpoint)
                host = (parsed.hostname or "").lower()
                port = parsed.port
            except ValueError:
                continue
            if (
                parsed.scheme.lower() == "https"
                and parsed.netloc
                and parsed.username is None
                and parsed.password is None
                and port in {None, 443}
                and any(host.endswith(suffix) for suffix in _AZURE_HOST_SUFFIXES)
                and api_key
            ):
                # Provider plugins use only the resource origin; canonicalizing
                # here prevents duplicate rows that differ only by API path or
                # an explicit default HTTPS port.
                origin = f"https://{host}"
                add(f"{origin}|{api_key}")

    gcp_rows = obj.get("gcp_service_accounts", [])
    if isinstance(gcp_rows, list):
        for row in gcp_rows:
            if not isinstance(row, dict):
                continue
            email = str(row.get("client_email") or "").strip()
            private_key = str(row.get("private_key") or "")
            project_id = str(row.get("project_id") or "").strip()
            if not project_id and "@" in email:
                domain = email.rsplit("@", 1)[1]
                if domain.endswith(_GCP_SERVICE_ACCOUNT_DOMAIN):
                    project_id = domain[: -len(_GCP_SERVICE_ACCOUNT_DOMAIN)]
            if not (email and private_key):
                continue
            info = {
                "type": "service_account",
                "project_id": project_id,
                "private_key": private_key,
                "client_email": email,
                # Never trust an imported token endpoint: _mint_token POSTs a
                # signed JWT assertion here, so arbitrary URLs would be SSRF and
                # credential disclosure.
                "token_uri": _GCP_TOKEN_URI,
            }
            add(json.dumps(info, ensure_ascii=False, separators=(",", ":")))

    return credentials


def _preprocess_raw(raw: str) -> str:
    """Combine multi-line credentials before the main splitter runs.

    - ``AWS_ACCESS_KEY_ID=X`` + ``AWS_SECRET_ACCESS_KEY=Y`` → ``X:Y``
    - Azure URL line followed by an API-key line → ``URL|KEY``
    - Comment lines (``# ...``) are stripped.
    """
    lines = raw.split("\n")
    out: list[str] = []
    i = 0
    consumed: set[int] = set()  # line indices already folded into a previous entry

    while i < len(lines):
        if i in consumed:
            i += 1
            continue
        line = lines[i].strip()

        # skip comments
        if line.startswith("#") or not line:
            i += 1
            continue

        # ---- AWS env-var pair ----
        if line.upper().startswith("AWS_ACCESS_KEY_ID="):
            ak = line.split("=", 1)[1].strip()
            for j in range(i + 1, min(i + 6, len(lines))):
                nxt = lines[j].strip()
                if nxt.startswith("#") or not nxt:
                    continue
                if nxt.upper().startswith("AWS_SECRET_ACCESS_KEY="):
                    sk_val = nxt.split("=", 1)[1].strip()
                    out.append(f"{ak}:{sk_val}")
                    consumed.add(j)
                    break
            else:
                out.append(line)
            i += 1
            continue

        # skip orphan secret key lines (already consumed above)
        if line.upper().startswith("AWS_SECRET_ACCESS_KEY="):
            i += 1
            continue

        # ---- Azure URL + key on next non-blank line ----
        if _AZURE_URL_RE.match(line) and "|" not in line:
            for j in range(i + 1, min(i + 4, len(lines))):
                nxt = lines[j].strip()
                if not nxt or nxt.startswith("#"):
                    continue
                if not nxt.startswith("http") and len(nxt) > 16:
                    out.append(f"{line}|{nxt}")
                    consumed.add(j)
                    break
            else:
                out.append(line)
            i += 1
            continue

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

    - Pre-processes multi-line AWS / Azure credential pairs.
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

    # Aggregate export files are one JSON object containing provider-specific
    # arrays. Expand them before line preprocessing, which is intended for pasted
    # env-var/URL pairs rather than structured JSON.

    # Cheap marker check avoids decoding every ordinary service-account JSON
    # twice while still recognizing standalone aggregate exports.
    aggregate = None
    stripped = raw.lstrip()
    if stripped.startswith("{") and any(f'"{key}"' in raw for key in _COMBO_KEYS):
        try:
            aggregate = _combo_credentials(json.loads(raw))
        except (ValueError, TypeError, RecursionError):
            aggregate = None
    if aggregate is not None:
        return aggregate

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
