"""sources.yaml -> enabled SourceDriver registry (Phase 3 SP2)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

from dlw.sources.base import SourceDriver
from dlw.sources.hf_mirror import HfMirrorDriver
from dlw.sources.huggingface import HuggingFaceDriver
from dlw.sources.modelscope import ModelScopeDriver

_SUPPORTED = {"huggingface", "hf_mirror", "modelscope"}


@dataclass
class SourceRegistry:
    _drivers: dict[str, SourceDriver]
    regional_defaults: dict[str, list[str]] = field(default_factory=dict)

    def enabled_ids(self) -> list[str]:
        return list(self._drivers.keys())

    def get(self, source_id: str) -> SourceDriver | None:
        return self._drivers.get(source_id)


def _build(driver: str, cfg: dict[str, Any],
           hf_token: str | None) -> SourceDriver | None:
    if driver == "huggingface":
        return HuggingFaceDriver(
            base_url=cfg.get("base_url", "https://huggingface.co"),
            hf_token=hf_token)
    if driver == "hf_mirror":
        return HfMirrorDriver(
            base_url=cfg.get("base_url", "https://hf-mirror.com"))
    if driver == "modelscope":
        return ModelScopeDriver(
            base_url=cfg.get("base_url", "https://www.modelscope.cn"))
    return None


def load_registry(path: str, *, hf_token: str | None) -> SourceRegistry:
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    drivers: dict[str, SourceDriver] = {}
    for entry in doc.get("sources", []):
        if not entry.get("enabled"):
            continue
        if entry.get("driver") not in _SUPPORTED:
            continue
        d = _build(entry["driver"], entry.get("config") or {}, hf_token)
        if d is not None:
            drivers[entry["id"]] = d
    return SourceRegistry(_drivers=drivers,
                          regional_defaults=doc.get("regional_defaults", {}))
