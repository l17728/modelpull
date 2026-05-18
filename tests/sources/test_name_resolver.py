"""NameResolver 3-tier (Phase 3 SP2; doc §1.5)."""
from __future__ import annotations

from dlw.sources.name_resolver import NameResolver

_RULES = """
identity_organizations: [deepseek-ai, Qwen, THUDM]
aliases:
  - hf_org: meta-llama
    modelscope_org: LLM-Research
    transform: "Meta-{name}"
per_model_overrides:
  - hf: "weird-org/weird-model"
    modelscope: "diff-org/diff-name"
"""


def _r(tmp_path):
    p = tmp_path / "rr.yaml"
    p.write_text(_RULES, encoding="utf-8")
    return NameResolver.from_file(str(p))


def test_huggingface_is_always_identity(tmp_path):
    r = _r(tmp_path)
    assert r.resolve("huggingface", "any-org/any-model") == "any-org/any-model"


def test_identity_org(tmp_path):
    r = _r(tmp_path)
    assert r.resolve("modelscope", "deepseek-ai/DeepSeek-V3") == "deepseek-ai/DeepSeek-V3"


def test_alias_transform(tmp_path):
    r = _r(tmp_path)
    assert r.resolve("modelscope", "meta-llama/Llama-3.1-8B") == "LLM-Research/Meta-Llama-3.1-8B"


def test_per_model_override(tmp_path):
    r = _r(tmp_path)
    assert r.resolve("modelscope", "weird-org/weird-model") == "diff-org/diff-name"


def test_unknown_returns_none(tmp_path):
    r = _r(tmp_path)
    assert r.resolve("modelscope", "rando-org/rando-model") is None
