"""HF Hub metadata wrapper — controller-side file enumeration.

Phase 1 W4: replaces task_service._MOCK_FILES. Calls
huggingface_hub.HfApi.list_repo_tree (sync) inside asyncio.to_thread.

Filters out:
  - Folders (only files become FileSubTasks)
  - Root-level metadata files (.gitattributes, .gitignore, README.md, LICENSE,
    USAGE.md). Nested files with the same name (e.g., docs/README.md) are kept.

Translates huggingface_hub exceptions to project-local types:
  RepositoryNotFoundError       -> RepoNotFound
  GatedRepoError                -> HfPrivateOrAuthRequired
  HfHubHTTPError 401/403        -> HfPrivateOrAuthRequired
  ConnectionError / Timeout     -> HfNetworkError
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import requests.exceptions
from huggingface_hub import HfApi
from huggingface_hub.errors import (
    GatedRepoError,
    HfHubHTTPError,
    RepositoryNotFoundError,
)

_METADATA_ROOT_FILES: frozenset[str] = frozenset({
    ".gitattributes", ".gitignore",
    "README.md", "LICENSE", "USAGE.md",
})


@dataclass(frozen=True)
class RepoFile:
    """Phase-1 simplified view of an HF repo file."""
    path: str
    size: int
    sha256: str | None     # populated for LFS files; None for small non-LFS files


class RepoNotFound(Exception):
    """HF repo or revision does not exist."""


class HfPrivateOrAuthRequired(Exception):
    """HF returned 401/403 — private repo or invalid token."""


class HfNetworkError(Exception):
    """Network / DNS / timeout — transient infrastructure issue."""


def _is_metadata_file(path: str) -> bool:
    """True for root-level metadata files (filtered from subtask creation)."""
    return "/" not in path and path in _METADATA_ROOT_FILES


def _to_repo_file(item: object) -> RepoFile | None:
    """Convert huggingface_hub item to RepoFile; return None for folders."""
    # huggingface_hub returns RepoFolder (no `size` attr) for folders.
    if not hasattr(item, "size"):
        return None
    sha = None
    lfs = getattr(item, "lfs", None)
    if lfs is not None:
        sha = getattr(lfs, "sha256", None)
    return RepoFile(path=item.path, size=item.size, sha256=sha)  # type: ignore[union-attr]


def _list_sync(
    repo_id: str, revision: str, *, hf_endpoint: str, hf_token: str | None,
) -> list[RepoFile]:
    api = HfApi(endpoint=hf_endpoint)
    try:
        items = api.list_repo_tree(
            repo_id, revision=revision, recursive=True, token=hf_token,
        )
        out: list[RepoFile] = []
        for item in items:
            rf = _to_repo_file(item)
            if rf is None:
                continue
            if _is_metadata_file(rf.path):
                continue
            out.append(rf)
        return out
    except GatedRepoError as e:
        raise HfPrivateOrAuthRequired(str(e)) from e
    except RepositoryNotFoundError as e:
        raise RepoNotFound(str(e)) from e
    except HfHubHTTPError as e:
        # 401/403/404 may also reach here depending on hub version
        status = getattr(e.response, "status_code", None)
        if status in (401, 403):
            raise HfPrivateOrAuthRequired(str(e)) from e
        if status == 404:
            raise RepoNotFound(str(e)) from e
        raise HfNetworkError(str(e)) from e
    except (requests.exceptions.ConnectionError,
            requests.exceptions.Timeout) as e:
        raise HfNetworkError(str(e)) from e


async def list_repo_tree(
    repo_id: str, revision: str, *,
    hf_endpoint: str, hf_token: str | None,
) -> list[RepoFile]:
    """Async-friendly wrapper for HF list_repo_tree.

    Returns a list (not iterator) of files only, with metadata files filtered.
    The sync HF SDK call runs in asyncio.to_thread.
    """
    return await asyncio.to_thread(
        _list_sync, repo_id, revision,
        hf_endpoint=hf_endpoint, hf_token=hf_token,
    )


def _model_metadata_sync(
    repo_id: str, revision: str | None, *, hf_endpoint: str, hf_token: str | None,
) -> dict:
    api = HfApi(endpoint=hf_endpoint)
    try:
        info = api.model_info(repo_id, revision=revision, token=hf_token,
                              files_metadata=True)
        siblings = [
            {"path": s.rfilename, "size": getattr(s, "size", None)}
            for s in (info.siblings or [])
        ]
        last_mod = getattr(info, "last_modified", None)
        return {"sha": getattr(info, "sha", None),
                "last_modified": last_mod.isoformat() if last_mod else None,
                "siblings": siblings}
    except GatedRepoError as e:
        raise HfPrivateOrAuthRequired(str(e)) from e
    except RepositoryNotFoundError as e:
        raise RepoNotFound(str(e)) from e
    except HfHubHTTPError as e:
        status = getattr(e.response, "status_code", None)
        if status in (401, 403):
            raise HfPrivateOrAuthRequired(str(e)) from e
        if status == 404:
            raise RepoNotFound(str(e)) from e
        raise HfNetworkError(str(e)) from e
    except (requests.exceptions.ConnectionError,
            requests.exceptions.Timeout) as e:
        raise HfNetworkError(str(e)) from e


def _model_card_sync(
    repo_id: str, *, hf_endpoint: str, hf_token: str | None,
) -> str:
    from huggingface_hub import hf_hub_download
    try:
        path = hf_hub_download(
            repo_id, "README.md", revision=None, token=hf_token,
            endpoint=hf_endpoint, repo_type="model")
    except GatedRepoError as e:
        raise HfPrivateOrAuthRequired(str(e)) from e
    except RepositoryNotFoundError as e:
        raise RepoNotFound(str(e)) from e
    except HfHubHTTPError as e:
        status = getattr(e.response, "status_code", None)
        if status in (401, 403):
            raise HfPrivateOrAuthRequired(str(e)) from e
        if status == 404:
            return ""   # no model card published — not an error
        raise HfNetworkError(str(e)) from e
    except (requests.exceptions.ConnectionError,
            requests.exceptions.Timeout) as e:
        raise HfNetworkError(str(e)) from e
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError as e:
        raise HfNetworkError(str(e)) from e


async def fetch_model_metadata(
    repo_id: str, revision: str | None = None, *,
    hf_endpoint: str, hf_token: str | None,
) -> dict:
    """T1 structured HF metadata: sha + file list + last_modified. No readme."""
    return await asyncio.to_thread(
        _model_metadata_sync, repo_id, revision,
        hf_endpoint=hf_endpoint, hf_token=hf_token)


async def fetch_model_card(
    repo_id: str, *, hf_endpoint: str, hf_token: str | None,
) -> str:
    """T2 user content: the model-card / README markdown ('' if none)."""
    return await asyncio.to_thread(
        _model_card_sync, repo_id, hf_endpoint=hf_endpoint, hf_token=hf_token)
