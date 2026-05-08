"""Mock downloader: write `file_size` random bytes + compute sha256.

Real HuggingFace Hub fetch comes in Week 4 plan. The interface is designed
so the only swap needed in Week 4 is replacing the random-bytes generator
with the actual HF download stream.

W3-C: blocking I/O (file write + randbytes) is wrapped in asyncio.to_thread
so the executor's event loop stays responsive (heartbeat / poll / shutdown
keep working during a multi-second download).
"""
from __future__ import annotations

import asyncio
import hashlib
import random
from dataclasses import dataclass
from pathlib import Path

# Default chunk size for streaming write — keep small enough to test memory bounds
_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class DownloadResult:
    bytes_written: int
    actual_sha256: str
    file_path: Path


class MockDownloader:
    """Generates a file of `file_size` random bytes; computes sha256 on the fly."""

    def __init__(self, download_dir: Path, seed: int | None = None) -> None:
        self._download_dir = Path(download_dir)
        self._rng = random.Random(seed) if seed is not None else random.Random()

    async def download(
        self, *, task_id: str, filename: str, file_size: int
    ) -> DownloadResult:
        """Write `file_size` random bytes; computes sha256 on the fly.

        File I/O + RNG run inside asyncio.to_thread so event loop is not
        blocked during long downloads (W3-C). Memory is O(1) — chunked write.
        """
        return await asyncio.to_thread(
            self._download_sync, task_id=task_id, filename=filename, file_size=file_size
        )

    def _download_sync(
        self, *, task_id: str, filename: str, file_size: int
    ) -> DownloadResult:
        target = self._download_dir / task_id / filename
        target.parent.mkdir(parents=True, exist_ok=True)

        sha = hashlib.sha256()
        bytes_remaining = file_size
        bytes_written = 0
        with target.open("wb") as f:
            while bytes_remaining > 0:
                chunk_size = min(_CHUNK_SIZE, bytes_remaining)
                chunk = self._rng.randbytes(chunk_size)
                sha.update(chunk)
                f.write(chunk)
                bytes_written += chunk_size
                bytes_remaining -= chunk_size

        return DownloadResult(
            bytes_written=bytes_written,
            actual_sha256=sha.hexdigest(),
            file_path=target,
        )
