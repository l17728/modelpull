"""Executor inherit materialization: S3 copy / local hardlink (SP3)."""
from __future__ import annotations

from dlw.executor.inherit import materialize_inherit
from dlw.executor.types import DownloadResult


class _FakeS3:
    def __init__(self):
        self.calls = []

    def copy_object(self, **kw):
        self.calls.append(kw)
        return {}


async def test_s3_copy_object(monkeypatch):
    fake = _FakeS3()
    import dlw.executor.inherit as inh
    monkeypatch.setattr(inh, "_s3_client", lambda settings, cfg: fake)

    class _Cfg:
        bucket = "b"
        backend_type = "s3"
    r = await materialize_inherit(
        settings=object(), storage_config=_Cfg(),
        src_key="old/k", dst_key="new/k", sha256="a" * 64, size=42)
    assert isinstance(r, DownloadResult)
    assert r.actual_sha256 == "a" * 64 and r.s3_key == "new/k"
    assert fake.calls[0]["CopySource"] == {"Bucket": "b", "Key": "old/k"}
    assert fake.calls[0]["Key"] == "new/k"


async def test_local_hardlink(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"x" * 8)
    dst = tmp_path / "sub" / "dst.bin"

    class _Cfg:
        backend_type = "local"
        base_path = str(tmp_path)
    r = await materialize_inherit(
        settings=object(), storage_config=_Cfg(),
        src_key="src.bin", dst_key="sub/dst.bin",
        sha256="z" * 64, size=8)
    assert dst.read_bytes() == b"x" * 8
    assert r.s3_key == "sub/dst.bin" and r.actual_sha256 == "z" * 64
