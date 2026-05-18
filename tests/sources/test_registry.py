"""Source registry from sources.yaml (Phase 3 SP2)."""
from __future__ import annotations

from dlw.sources.registry import load_registry

_YAML = """
sources:
  - id: huggingface
    enabled: true
    driver: huggingface
    config: {base_url: https://huggingface.co}
  - id: hf_mirror
    enabled: true
    driver: hf_mirror
    config: {base_url: https://hf-mirror.com}
  - id: modelscope
    enabled: false
    driver: modelscope
    config: {base_url: https://www.modelscope.cn}
  - id: corp
    enabled: true
    driver: s3_mirror
    config: {}
regional_defaults:
  cn-north: [hf_mirror, modelscope, huggingface]
"""


def test_only_enabled_supported(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(_YAML, encoding="utf-8")
    reg = load_registry(str(p), hf_token="tk")
    assert set(reg.enabled_ids()) == {"huggingface", "hf_mirror"}
    assert reg.get("huggingface").id == "huggingface"
    assert reg.get("missing") is None
    assert reg.regional_defaults["cn-north"][0] == "hf_mirror"


def test_modelscope_enabled(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(_YAML.replace("id: modelscope\n    enabled: false",
                               "id: modelscope\n    enabled: true"),
                 encoding="utf-8")
    reg = load_registry(str(p), hf_token=None)
    assert "modelscope" in reg.enabled_ids()
