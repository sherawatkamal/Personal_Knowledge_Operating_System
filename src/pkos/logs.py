"""Logging with secret scrubbing.

Every line that reaches a handler, including tracebacks, passes through `scrub`.
Two layers: exact values registered at runtime (everything loaded as a SecretStr),
and patterns for credential shapes we may receive from providers before they are
registered (bearer tokens, provider key prefixes, key=value pairs).
"""

import logging
import re
import sys
import threading
from types import TracebackType

REDACTED = "[REDACTED]"
# Shorter values are too likely to collide with ordinary words and redact unrelated text.
_MIN_SECRET_LEN = 8

_secrets: set[str] = set()
_lock = threading.Lock()

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), rf"\1 {REDACTED}"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),  # sk-... / sk-ant-... API keys
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), REDACTED),  # Slack tokens
    (re.compile(r"\bya29\.[A-Za-z0-9._-]{20,}"), REDACTED),  # Google access tokens
    (re.compile(r"\b1//[A-Za-z0-9._-]{20,}"), REDACTED),  # Google refresh tokens
    (re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{10,}"), REDACTED),  # Google OAuth client secrets
    (re.compile(r"\bgrn_[A-Za-z0-9_-]{16,}"), REDACTED),  # Granola API keys
    (
        re.compile(
            r"(?i)(\"?\b(?:password|passwd|pwd|secret|client_secret|token|access_token|"
            r"refresh_token|api[_-]?key|authorization)\b\"?\s*[:=]\s*\"?)[^\s\"',}&]+"
        ),
        rf"\1{REDACTED}",
    ),
]


def register_secret(value: str | None) -> None:
    """Redact this exact value from all log output from now on."""
    if value and len(value) >= _MIN_SECRET_LEN:
        with _lock:
            _secrets.add(value)


def scrub(text: str) -> str:
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)  # longest first: no partial leaks
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class ScrubbingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ScrubbingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)  # per-request lines are noise
    sys.excepthook = _scrubbed_excepthook


def _scrubbed_excepthook(
    exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
) -> None:
    """Uncaught exceptions go through the scrubbing formatter, never raw to stderr."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    logging.getLogger("pkos").critical("Unhandled exception", exc_info=(exc_type, exc, tb))
