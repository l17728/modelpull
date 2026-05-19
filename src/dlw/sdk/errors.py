"""dlw SDK typed errors + POSIX exit-code mapping (SP4; spec §4-§6)."""
from __future__ import annotations


class DlwError(Exception):
    def __init__(self, message: str, *, code: str | None = None,
                 status: int | None = None, trace_id: str | None = None,
                 details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.trace_id = trace_id
        self.details = details or {}


class UsageError(DlwError):
    """Bad CLI args / missing token (pre-flight)."""


class NotFound(DlwError):
    """HTTP 404."""


class AuthError(DlwError):
    """HTTP 401 / 403."""


class QuotaExceeded(DlwError):
    """HTTP 429 or code QUOTA_EXCEEDED."""


class Conflict(DlwError):
    """HTTP 409 (e.g. TASK_NOT_TERMINAL, duplicate)."""


class Timeout(DlwError):
    """wait/watch exceeded the deadline."""


class ApiError(DlwError):
    """Any other non-2xx."""


# Most-specific first; first isinstance match wins.
_ORDER: list[tuple[type, int]] = [
    (UsageError, 2), (NotFound, 3), (AuthError, 4), (QuotaExceeded, 5),
    (Conflict, 6), (Timeout, 9), (ApiError, 1), (DlwError, 1),
]


def exit_code_for(exc: BaseException) -> int:
    for cls, code in _ORDER:
        if isinstance(exc, cls):
            return code
    return 1
