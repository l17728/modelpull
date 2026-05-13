"""ExecutorRunner — async main loop joining heartbeat + poll-and-execute.

On startup: register via /join. Then runs two concurrent loops:
  - Heartbeat every settings.heartbeat_interval_seconds
  - Poll every settings.poll_interval_seconds; if assigned, download + report

W3-A: shutdown does NOT cancel loops mid-iteration. The pacing wait inside
each loop reacts to _shutdown.set() instantly (asyncio.wait_for completes
immediately when the event is set). _execute_subtask completes its download
and report cycle before the loop exits — otherwise mid-flight subtasks would
be stuck in 'assigned' state forever.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

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
    ) -> None:
        self._s = settings
        self._client = client
        self._stream_downloader = stream_downloader
        self._chunk_downloader = chunk_downloader
        self._shutdown = asyncio.Event()

    def _choose_downloader(self, file_size: int | None):
        threshold = self._s.chunk_level_threshold_bytes
        if file_size is None or file_size >= threshold:
            return self._chunk_downloader
        return self._stream_downloader

    def request_shutdown(self) -> None:
        self._shutdown.set()

    async def run(self) -> None:
        # W2b1 §3.2: clean up any stale .parts/ dirs from a previous crash.
        # active_subtask_ids=set() removes everything — W2b1 has no resume.
        removed = startup_gc(self._s.parts_dir_path, active_subtask_ids=set())
        if removed:
            logger.info("startup_gc removed %d stale parts dirs", removed)

        # 1. Join (one-shot)
        await self._client.join(
            executor_id=self._s.id,
            host_id=self._s.host_id,
            capabilities={
                "nic_speed_gbps": self._s.nic_speed_gbps,
                "region": self._s.region,
            },
        )

        # 2. Concurrent loops — both check self._shutdown.is_set() each iteration
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        poll_task = asyncio.create_task(self._poll_and_execute_loop())

        # 3. Wait for shutdown signal then let loops exit naturally (W3-A)
        await self._shutdown.wait()
        await asyncio.gather(heartbeat_task, poll_task, return_exceptions=True)

    async def _heartbeat_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                await self._client.heartbeat(
                    executor_id=self._s.id, health_score=100,
                    parts_dir_bytes=total_parts_bytes(self._s.parts_dir_path),
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
                if e.response.status_code == 401:
                    detail = None
                    try:
                        detail = e.response.json().get("detail")
                    except Exception:
                        pass
                    if isinstance(detail, dict) and detail.get("code") == "EPOCH_MISMATCH":
                        logger.warning(
                            "EPOCH_MISMATCH (expected=%s got=%s); re-joining",
                            detail.get("expected"), detail.get("got"),
                        )
                        await self._rejoin()
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

    async def _rejoin(self) -> None:
        """Discard any in-flight state and re-issue /join (gets new epoch)."""
        try:
            await self._client.join(
                executor_id=self._s.id,
                host_id=self._s.host_id,
                capabilities={
                    "nic_speed_gbps": self._s.nic_speed_gbps,
                    "region": self._s.region,
                },
            )
        except Exception as e:
            logger.warning("rejoin failed: %s", e)

    async def _execute_subtask(
        self, *, subtask: dict, assignment_token: uuid.UUID,
        repo_id: str, revision: str, storage_config: dict,
    ) -> None:
        from dlw.executor.downloader import Assignment
        from dlw.schemas.storage import StorageConfig

        sub_id = uuid.UUID(subtask["id"])
        try:
            assignment = Assignment(
                subtask_id=sub_id,
                task_id=uuid.UUID(subtask["task_id"]),
                repo_id=repo_id,
                revision=revision,
                filename=subtask["filename"],
                file_size=subtask.get("file_size"),
                expected_sha256=subtask.get("expected_sha256"),
                storage_config=StorageConfig(**storage_config),
            )
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
