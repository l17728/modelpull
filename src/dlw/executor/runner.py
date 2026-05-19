"""ExecutorRunner — async main loop: register, heartbeat, poll-and-execute, renew.

On startup: load-or-register via mTLS enrollment (W3a). Then runs three
concurrent loops:
  - Heartbeat every settings.heartbeat_interval_seconds (mTLS + JWT + HMAC)
  - Poll every settings.poll_interval_seconds; if assigned, download + report
  - Auth renew: refresh the JWT ~5min before expiry, the cert ~1h before expiry

W3-A: shutdown does NOT cancel loops mid-iteration. The pacing wait inside
each loop reacts to _shutdown.set() instantly. _execute_subtask completes its
download + report cycle before the loop exits — otherwise mid-flight subtasks
would be stuck in 'assigned' state forever.

W3a: a 401 on poll triggers a re-register (generalizes the W1 EPOCH_MISMATCH
re-join path) — the executor's identity may have been reset controller-side.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dlw.executor.auth_lifecycle import AuthState, load_or_register, renew
from dlw.executor.chunk_downloader import DirectOffsetDownloader, DiskFullError
from dlw.executor.client import ControllerClient
from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import HfS3StreamDownloader
from dlw.executor.parts_dir import startup_gc, total_parts_bytes

logger = logging.getLogger(__name__)


class ExecutorRunner:
    def __init__(
        self,
        *,
        settings: ExecutorSettings,
        client: ControllerClient,
        stream_downloader: HfS3StreamDownloader,
        chunk_downloader: DirectOffsetDownloader,
        auth_state: AuthState | None = None,
    ) -> None:
        self._s = settings
        self._client = client
        self._stream_downloader = stream_downloader
        self._chunk_downloader = chunk_downloader
        # When auth_state is provided (tests), run() skips load_or_register.
        self._auth = auth_state
        self._shutdown = asyncio.Event()

    def _choose_downloader(self, file_size: int | None):
        threshold = self._s.chunk_level_threshold_bytes
        if file_size is None or file_size >= threshold:
            return self._chunk_downloader
        return self._stream_downloader

    def request_shutdown(self) -> None:
        self._shutdown.set()

    def _capabilities(self) -> dict:
        return {"nic_speed_gbps": self._s.nic_speed_gbps, "region": self._s.region}

    async def run(self) -> None:
        # W2b1 §3.2: clean up any stale .parts/ dirs from a previous crash.
        removed = startup_gc(self._s.parts_dir_path, active_subtask_ids=set())
        if removed:
            logger.info("startup_gc removed %d stale parts dirs", removed)

        # W3a §3.13: auth bootstrap — load existing cert or register fresh.
        if self._auth is None:
            self._auth = await load_or_register(
                cert_dir=Path(self._s.executor_cert_dir),
                controller_url=self._s.controller_url,
                ca_bundle_path=self._s.executor_ca_bundle or None,
                enrollment_token=self._s.enrollment_token,
                executor_id=self._s.id,
                host_id=self._s.host_id,
                capabilities=self._capabilities(),
            )
        self._client.update_auth(self._auth)

        # Three concurrent loops — each checks self._shutdown.is_set().
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        poll_task = asyncio.create_task(self._poll_and_execute_loop())
        renew_task = asyncio.create_task(self._auth_renew_loop())

        await self._shutdown.wait()
        await asyncio.gather(
            heartbeat_task, poll_task, renew_task, return_exceptions=True,
        )

    async def _heartbeat_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                try:
                    import os as _os
                    _probe = self._s.parts_dir_path
                    if not _os.path.exists(_probe):
                        _probe = _os.path.dirname(_os.path.abspath(_probe)) or "."
                    _du = shutil.disk_usage(_probe)
                    _disk_free_gb = int(_du.free // (1024 ** 3))
                except OSError:
                    _disk_free_gb = None
                await self._client.heartbeat(
                    executor_id=self._s.id, health_score=100,
                    parts_dir_bytes=total_parts_bytes(self._s.parts_dir_path),
                    disk_free_gb=_disk_free_gb,
                )
            except Exception as e:
                logger.warning("heartbeat failed: %s", e)
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(),
                    timeout=self._s.heartbeat_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

    async def _poll_and_execute_loop(self) -> None:
        import httpx as _httpx
        while not self._shutdown.is_set():
            try:
                resp = await self._client.poll(executor_id=self._s.id)
                if resp.get("assigned"):
                    await self._execute_subtask(
                        subtask=resp["subtask"],
                        assignment_token=uuid.UUID(resp["assignment_token"]),
                        repo_id=resp["repo_id"],
                        revision=resp["revision"],
                        storage_config=resp["storage_config"],
                    )
                    continue  # immediately poll again — there may be more work
            except _httpx.HTTPStatusError as e:
                # W3a: any 401 on poll → re-register. The executor identity may
                # have been reset controller-side (epoch bump, cert rotation,
                # or the row cleared). load_or_register is idempotent.
                if e.response.status_code == 401:
                    logger.warning("poll 401 (%s); re-registering",
                                   _safe_detail(e))
                    await self._reregister()
                    continue
                logger.warning("poll failed: %s", e)
            except Exception as e:
                logger.warning("poll failed: %s", e)
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(),
                    timeout=self._s.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

    async def _auth_renew_loop(self) -> None:
        """W3a §3.13: refresh the JWT ~5min before expiry, the cert ~1h before."""
        while not self._shutdown.is_set():
            assert self._auth is not None
            now = datetime.now(UTC)
            jwt_due = self._auth.jwt_exp - timedelta(minutes=5)
            cert_due = self._auth.cert_exp - timedelta(hours=1)
            sleep_for = max(60, int((min(jwt_due, cert_due) - now).total_seconds()))
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=sleep_for)
                return  # shutdown
            except asyncio.TimeoutError:
                pass
            try:
                self._auth = await renew(
                    self._auth, controller_url=self._s.controller_url,
                )
                self._client.update_auth(self._auth)
            except Exception as e:
                logger.warning("auth renew failed: %s; retry next cycle", e)

    async def _reregister(self) -> None:
        """Discard in-flight auth state and re-register (fresh cert + JWT + epoch)."""
        try:
            self._auth = await load_or_register(
                cert_dir=Path(self._s.executor_cert_dir),
                controller_url=self._s.controller_url,
                ca_bundle_path=self._s.executor_ca_bundle or None,
                enrollment_token=self._s.enrollment_token,
                executor_id=self._s.id,
                host_id=self._s.host_id,
                capabilities=self._capabilities(),
            )
            self._client.update_auth(self._auth)
        except Exception as e:
            logger.warning("re-register failed: %s", e)

    async def _execute_subtask(
        self, *, subtask: dict, assignment_token: uuid.UUID,
        repo_id: str, revision: str, storage_config: dict,
    ) -> None:
        import httpx as _httpx
        from dlw.executor.downloader import Assignment
        from dlw.schemas.storage import StorageConfig

        sub_id = uuid.UUID(subtask["id"])
        try:
            assignment = Assignment(
                subtask_id=sub_id,
                task_id=uuid.UUID(subtask["task_id"]),
                assignment_token=assignment_token,
                repo_id=repo_id,
                revision=revision,
                filename=subtask["filename"],
                file_size=subtask.get("file_size"),
                expected_sha256=subtask.get("expected_sha256"),
                storage_config=StorageConfig(**storage_config),
            )
            inherit_key = subtask.get("inherit_from_key")
            if inherit_key:
                from dlw.executor._io import compose_key
                from dlw.executor.inherit import materialize_inherit
                result = await materialize_inherit(
                    settings=self._s,
                    storage_config=assignment.storage_config,
                    src_key=inherit_key,
                    dst_key=compose_key(assignment),
                    sha256=assignment.expected_sha256 or "",
                    size=assignment.file_size or 0)
                await self._client.report(
                    subtask_id=sub_id, status="succeeded",
                    assignment_token=assignment_token,
                    actual_sha256=result.actual_sha256,
                    bytes_downloaded=result.bytes_written,
                    s3_key=result.s3_key)
                return
            downloader = self._choose_downloader(assignment.file_size)
            try:
                result = await downloader.download(assignment=assignment)
            except DiskFullError as e:
                logger.warning("subtask %s paused_disk_full: %s", sub_id, e)
                await self._client.report(
                    subtask_id=sub_id,
                    status="paused_disk_full",
                    assignment_token=assignment_token,
                    actual_sha256=None,
                    bytes_downloaded=0,
                    error=str(e),
                )
                return
            except _httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code in (429, 503):
                    logger.warning(
                        "subtask %s paused_external: HF returned %d", sub_id, code,
                    )
                    await self._client.report(
                        subtask_id=sub_id,
                        status="paused_external",
                        assignment_token=assignment_token,
                        actual_sha256=None,
                        bytes_downloaded=0,
                        error=f"HTTP {code}",
                    )
                    return
                raise   # other 4xx/5xx → generic handler
            await self._client.report(
                subtask_id=sub_id,
                status="succeeded",
                assignment_token=assignment_token,
                actual_sha256=result.actual_sha256,
                bytes_downloaded=result.bytes_written,
                s3_key=result.s3_key,
            )
        except Exception as e:
            logger.exception("subtask %s failed", sub_id)
            try:
                await self._client.report(
                    subtask_id=sub_id,
                    status="failed",
                    assignment_token=assignment_token,
                    actual_sha256=None,
                    bytes_downloaded=0,
                    error=str(e),
                )
            except Exception:
                logger.exception("report failure also failed for %s", sub_id)


def _safe_detail(e) -> str:
    try:
        return str(e.response.json().get("detail"))
    except Exception:
        return "<no detail>"
