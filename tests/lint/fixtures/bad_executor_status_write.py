"""Self-test fixture: this file deliberately contains a violation.
The lint scans src/dlw/ only — this file lives under tests/ and is
NOT scanned in CI. The unit test points the lint at this file directly."""
from __future__ import annotations


def naughty(ex) -> None:
    ex.status = "faulty"     # noqa: violation we want the lint to catch
