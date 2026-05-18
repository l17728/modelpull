"""SourceDriver Protocol + dataclasses (Phase 3 SP2)."""
from __future__ import annotations

from dlw.sources.base import (
    SourceFile,
    SourceHealth,
    SourceManifest,
    SourceToken,
)


def test_sourcefile_defaults():
    f = SourceFile(filename="model.safetensors", size=10, sha256=None,
                   download_ref="r")
    assert f.filename == "model.safetensors" and f.sha256 is None


def test_manifest_holds_files():
    m = SourceManifest(source_id="huggingface", repo_id_in_source="o/r",
                        revision_in_source="abc", files=[
                            SourceFile("a", 1, "x" * 64, "ref")],
                        has_lfs_sha256=True)
    assert m.source_id == "huggingface" and len(m.files) == 1


def test_health_and_token():
    assert SourceHealth(ok=True, latency_ms=12.0).ok is True
    t = SourceToken(scheme="bearer", value="secret")
    assert t.value == "secret" and "secret" not in repr(t)
