"""dlw Python SDK (Phase 3 SP4) — thin client over the controller REST API.

Monorepo note: the controller owns the `dlw` package, so the SDK lives at
`dlw.sdk` (not top-level `dlw`) to avoid heavy-importing FastAPI/SQLAlchemy.
See docs/superpowers/specs/2026-05-19-phase-3-sp4-cli-sdk-design.md."""
from __future__ import annotations

from dlw.sdk import errors
from dlw.sdk.aclient import AsyncClient, AsyncDownloadTask
from dlw.sdk.client import Client
from dlw.sdk.device import device_authorize, device_token, poll_for_token
from dlw.sdk.models import DownloadTask

__all__ = ["Client", "AsyncClient", "AsyncDownloadTask",
           "DownloadTask", "errors",
           "device_authorize", "device_token", "poll_for_token"]
