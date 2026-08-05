"""Keeping secrets out of logs and error responses.

Two habits cause most credential leaks in a system like this: logging a whole request object
because it was convenient, and putting the rejected value into a validation error because it
was helpful. Both are addressed here, and both are addressed by *defaulting* to redaction
rather than by remembering to redact.

Header redaction uses a deny-list of sensitive names plus a catch-all for anything that
looks like a credential. A deny-list is the wrong default in general — but headers are an
open namespace, and an allow-list would silently drop the operational headers that make logs
useful. The tradeoff is made explicit here rather than left to each call site.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Final

__all__ = ["REDACTED", "redact_headers", "redact_mapping", "redact_url", "safe_repr"]

REDACTED: Final = "[redacted]"

_SENSITIVE_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "x-toollayer-admin-token",
        "x-toollayer-service-token",
    }
)

_SENSITIVE_NAME: Final = re.compile(
    r"(?:^|[_\-.])(?:secret|token|password|passwd|credential|api[_\-.]?key|private[_\-.]?key)"
    r"(?:$|[_\-.])",
    re.IGNORECASE,
)

_SENSITIVE_QUERY: Final = frozenset(
    {"access_token", "api_key", "apikey", "code", "id_token", "password", "secret", "token"}
)


def _is_sensitive(name: str) -> bool:
    lowered = name.lower()
    return lowered in _SENSITIVE_HEADERS or _SENSITIVE_NAME.search(lowered) is not None


def redact_headers(headers: Iterable[tuple[str, str]] | Mapping[str, str]) -> dict[str, str]:
    """Return headers with every sensitive value replaced."""
    items = headers.items() if isinstance(headers, Mapping) else headers
    return {name: (REDACTED if _is_sensitive(name) else value) for name, value in items}


def redact_mapping(payload: Mapping[str, Any], *, depth: int = 0) -> dict[str, Any]:
    """Return a mapping with every sensitively named value replaced, recursively."""
    if depth > 6:
        return {"...": REDACTED}
    result: dict[str, Any] = {}
    for key, value in payload.items():
        name = str(key)
        if _is_sensitive(name):
            result[name] = REDACTED
        elif isinstance(value, Mapping):
            result[name] = redact_mapping(value, depth=depth + 1)
        elif isinstance(value, list):
            result[name] = [
                redact_mapping(item, depth=depth + 1) if isinstance(item, Mapping) else item
                for item in value[:50]
            ]
        else:
            result[name] = value
    return result


def redact_url(url: str) -> str:
    """Return a URL with credentials and sensitive query values removed.

    Query parameter *names* are kept because they are useful in a log line and are not
    themselves secret; only the values are replaced.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    try:
        parsed = urlsplit(url)
    except ValueError:
        return REDACTED

    netloc = parsed.hostname or ""
    if parsed.username or parsed.password:
        netloc = f"{REDACTED}@{netloc}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"

    query = urlencode(
        [
            (key, REDACTED if key.lower() in _SENSITIVE_QUERY or _is_sensitive(key) else value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))


def safe_repr(value: object, *, limit: int = 120) -> str:
    """Describe a value by shape rather than by content.

    Used where a message needs to say *something* about an offending value without
    reproducing it. ``str`` becomes ``string(len=42)``, never the string itself.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return f"string(len={len(value)})"
    if isinstance(value, Mapping):
        return f"object(keys={len(value)})"
    if isinstance(value, (list, tuple)):
        return f"array(len={len(value)})"
    return type(value).__name__[:limit]
