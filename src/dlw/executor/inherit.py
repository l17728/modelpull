"""Executor-side inherit materialization (Phase 3 SP3; doc §2 step 4).
S3 server-side copy_object (in-region, ≈ free) or local hardlink — no
HF/source bytes. The executor holds the storage creds (INVARIANT 3)."""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

from dlw.executor._io import make_s3_client
from dlw.executor.types import DownloadResult


def _s3_client(settings: Any, cfg: Any) -> Any:
    """Test seam — monkeypatched to inject a fake S3 client."""
    return make_s3_client(settings, cfg)


async def materialize_inherit(
    *, settings: Any, storage_config: Any, src_key: str, dst_key: str,
    sha256: str, size: int,
) -> DownloadResult:
    backend = getattr(storage_config, "backend_type", "s3")
    if backend == "s3":
        s3 = _s3_client(settings, storage_config)
        bucket = storage_config.bucket
        await asyncio.to_thread(
            lambda: s3.copy_object(
                Bucket=bucket,
                CopySource={"Bucket": bucket, "Key": src_key},
                Key=dst_key))
    else:
        base = Path(getattr(storage_config, "base_path", "."))
        src = base / src_key
        dst = base / dst_key
        dst.parent.mkdir(parents=True, exist_ok=True)

        def _link() -> None:
            try:
                os.link(src, dst)
            except OSError:                 # EXDEV / unsupported → copy
                shutil.copy2(src, dst)
        await asyncio.to_thread(_link)
    return DownloadResult(bytes_written=size, actual_sha256=sha256,
                          s3_key=dst_key)
