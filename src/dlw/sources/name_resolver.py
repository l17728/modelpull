"""3-tier source name resolution (Phase 3 SP2; doc §1.5).

Tier 1 identity (HF, or org in identity_organizations); tier 2 alias /
per-model rules from resolver-rules.yaml; tier 3 source search-API (deferred
to a stub that returns None — wiring point for v2.1; cache scaffold present)."""
from __future__ import annotations

from dataclasses import dataclass

import yaml


@dataclass
class _Alias:
    hf_org: str
    ms_org: str
    transform: str   # e.g. "Meta-{name}"


class NameResolver:
    def __init__(self, *, identity_orgs: set[str], aliases: list[_Alias],
                 overrides: dict[str, str]) -> None:
        self._identity = identity_orgs
        self._aliases = {a.hf_org: a for a in aliases}
        self._overrides = overrides           # "hf_repo" -> "src_repo"
        self._search_cache: dict[tuple[str, str], str] = {}

    @classmethod
    def from_file(cls, path: str) -> NameResolver:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        aliases = [_Alias(a["hf_org"], a["modelscope_org"], a["transform"])
                   for a in doc.get("aliases", [])]
        overrides = {o["hf"]: o["modelscope"]
                     for o in doc.get("per_model_overrides", [])}
        return cls(identity_orgs=set(doc.get("identity_organizations", [])),
                   aliases=aliases, overrides=overrides)

    def resolve(self, source_id: str, hf_repo_id: str) -> str | None:
        if source_id == "huggingface" or source_id == "hf_mirror":
            return hf_repo_id
        if hf_repo_id in self._overrides:
            return self._overrides[hf_repo_id]
        org, _, name = hf_repo_id.partition("/")
        if org in self._identity:
            return hf_repo_id
        a = self._aliases.get(org)
        if a is not None:
            return f"{a.ms_org}/{a.transform.format(name=name)}"
        return self._search_cache.get((source_id, hf_repo_id))
