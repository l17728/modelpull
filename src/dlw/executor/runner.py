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

from dlw.executor.client import ControllerClient
from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import HfS3StreamDownloader

logger = logging.getLogger(__name__)


class ExecutorRunner:
    def __init__(
        self,
        *,
        settings: ExecutorSettings,
        client: ControllerClient,
        downloader: HfS3StreamDownloader,
    ) -> None:
        self._s = settings
        self._client = client
        self._downloader = downloader
        self._shutdown = asyncio.Event()

    def request_shutdown(self) -> None:
        self._shutdown.set()

    async def run(self) -> None:
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
                    executor_id=self._s.id, health_score=100, parts_dir_bytes=0
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
            except Exception as e:
                logger.warning("poll failed: %s", e)
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(),
                    timeout=self._s.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

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
            result = await self._downloader.download(assignment=assignment)
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
