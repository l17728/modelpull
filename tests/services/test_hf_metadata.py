"""Tests for hf_metadata.list_repo_tree — controller-side HF API wrapper."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from dlw.services.hf_metadata import (
    HfNetworkError,
    HfPrivateOrAuthRequired,
    RepoFile,
    RepoNotFound,
    list_repo_tree,
)


def _make_repo_file(*, path: str, size: int, sha: str | None = None):
    """Mimic huggingface_hub.RepoFile shape used by list_repo_tree."""
    lfs = SimpleNamespace(sha256=sha, size=size) if sha else None
    return SimpleNamespace(path=path, size=size, lfs=lfs, blob_id="dummy_blob")


def _make_repo_folder(*, path: str):
    """Mimic huggingface_hub.RepoFolder."""
    return SimpleNamespace(path=path, tree_id="dummy_tree")


@pytest.mark.slow
async def test_list_repo_tree_returns_files_with_size_and_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [
        _make_repo_file(path="config.json", size=4096, sha=None),
        _make_repo_file(
            path="model.safetensors",
            size=1_000_000_000,
            sha="a" * 64,
        ),
        _make_repo_folder(path="assets"),
    ]

    def fake_list_repo_tree(self, repo_id, *, revision, recursive, token):
        assert repo_id == "owner/repo"
        assert revision == "main"
        assert recursive is True
        return iter(items)

    monkeypatch.setattr(
        "huggingface_hub.HfApi.list_repo_tree", fake_list_repo_tree
    )

    files = await list_repo_tree(
        "owner/repo", "main",
        hf_endpoint="https://huggingface.co", hf_token=None,
    )

    assert len(files) == 2  # folder filtered
    assert all(isinstance(f, RepoFile) for f in files)
    assert files[0].path == "config.json"
    assert files[0].size == 4096
    assert files[0].sha256 is None  # non-LFS file: no sha
    assert files[1].path == "model.safetensors"
    assert files[1].sha256 == "a" * 64


@pytest.mark.slow
async def test_list_repo_tree_filters_metadata_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [
        _make_repo_file(path=".gitattributes", size=100),
        _make_repo_file(path=".gitignore", size=50),
        _make_repo_file(path="README.md", size=5000),
        _make_repo_file(path="LICENSE", size=1000),
        _make_repo_file(path="USAGE.md", size=2000),
        _make_repo_file(path="config.json", size=4096),
        _make_repo_file(path="docs/README.md", size=3000),  # nested README NOT filtered
    ]

    def fake(self, repo_id, *, revision, recursive, token):
        return iter(items)

    monkeypatch.setattr("huggingface_hub.HfApi.list_repo_tree", fake)

    files = await list_repo_tree(
        "owner/repo", "main",
        hf_endpoint="https://huggingface.co", hf_token=None,
    )
    paths = {f.path for f in files}
    assert paths == {"config.json", "docs/README.md"}


@pytest.mark.slow
async def test_list_repo_tree_404_raises_RepoNotFound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from huggingface_hub.errors import RepositoryNotFoundError

    def fake(self, repo_id, *, revision, recursive, token):
        raise RepositoryNotFoundError("not found")

    monkeypatch.setattr("huggingface_hub.HfApi.list_repo_tree", fake)

    with pytest.raises(RepoNotFound):
        await list_repo_tree(
            "owner/missing", "main",
            hf_endpoint="https://huggingface.co", hf_token=None,
        )


@pytest.mark.slow
async def test_list_repo_tree_401_raises_HfPrivateOrAuthRequired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from huggingface_hub.errors import GatedRepoError

    def fake(self, repo_id, *, revision, recursive, token):
        raise GatedRepoError("private")

    monkeypatch.setattr("huggingface_hub.HfApi.list_repo_tree", fake)

    with pytest.raises(HfPrivateOrAuthRequired):
        await list_repo_tree(
            "owner/private", "main",
            hf_endpoint="https://huggingface.co", hf_token=None,
        )


@pytest.mark.slow
async def test_list_repo_tree_network_error_raises_HfNetworkError(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import requests

    def fake(self, repo_id, *, revision, recursive, token):
        raise requests.exceptions.ConnectionError("dns failed")

    monkeypatch.setattr("huggingface_hub.HfApi.list_repo_tree", fake)

    with pytest.raises(HfNetworkError):
        await list_repo_tree(
            "owner/x", "main",
            hf_endpoint="https://huggingface.co", hf_token=None,
        )
