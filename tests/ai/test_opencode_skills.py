"""Tests for the opencode skills MANIFEST generator (SP4f)."""
from __future__ import annotations

from dlw.ai.opencode_skills import build_skills_manifest
from dlw.ai.tools import READONLY_TOOLS
from dlw.ai.write_tools import WRITE_TOOLS


def test_manifest_includes_all_read_tools():
    md = build_skills_manifest()
    for name in READONLY_TOOLS:
        assert f"`{name}`" in md, f"missing read tool {name}"


def test_manifest_includes_all_write_tools():
    md = build_skills_manifest()
    for name in WRITE_TOOLS:
        assert f"`{name}`" in md, f"missing write tool {name}"


def test_manifest_excludes_write_when_read_only():
    md = build_skills_manifest(include_write=False)
    for name in WRITE_TOOLS:
        assert f"`{name}`" not in md
    # but read tools still present
    assert "search_huggingface_models" in md


def test_manifest_has_invocation_blocks_for_main_tools():
    md = build_skills_manifest()
    # Spot-check a representative subset
    assert "dlw show {task_id}" in md
    assert "dlw submit" in md
    assert "huggingface.co/api/models?search=" in md
    assert "modelscope.cn/api/v1/models" in md


def test_manifest_tells_llm_about_priority_ordering():
    md = build_skills_manifest()
    # The intro should explicitly tell the LLM HF/MS beat web_search,
    # which beats model knowledge.
    low = md.lower()
    assert "hugging face" in low or "huggingface" in low
    assert "web_search" in low
    assert "memory" in low or "model knowledge" in low


def test_manifest_marks_write_tools_as_destructive():
    md = build_skills_manifest()
    # The write section header must warn about confirmation.
    assert "DESTRUCTIVE" in md or "confirm" in md.lower()


def test_manifest_uses_bearer_env_var():
    md = build_skills_manifest()
    # All curl-based invocations should reference the bearer env var
    # so callers know to set it; never hard-code a token in the recipe.
    assert "$DLW_BEARER_TOKEN" in md
    # Counter-check: no hex-token-shaped strings in the manifest
    import re
    assert not re.search(r"\beyJ[A-Za-z0-9_\-]{20,}", md), "looks like a real JWT leaked into manifest"


def test_manifest_is_self_contained_markdown():
    md = build_skills_manifest()
    # Has top-level heading, sections, code fences (balanced)
    assert md.startswith("# ")
    assert md.count("```") % 2 == 0, "unbalanced code fences"
    assert "### " in md  # has subsection per tool


def test_opencode_runner_injects_manifest_by_default():
    """OpenCodeRunner.__init__ should default to inject_skills=True."""
    from dlw.ai.runner import OpenCodeRunner

    class _Settings:
        ai_opencode_bin = "opencode"
        ai_model_name = "opencode"

    r = OpenCodeRunner(_Settings())
    assert r._inject_skills is True
    # The manifest should be lazily built
    assert r._skills_manifest is None
    m = r._get_skills_manifest()
    assert "modelpull AI assistant" in m
    # Cached on second call
    assert r._get_skills_manifest() is m


def test_opencode_runner_respects_disable_flag():
    from dlw.ai.runner import OpenCodeRunner

    class _Settings:
        ai_opencode_bin = "opencode"
        ai_model_name = "opencode"
        ai_opencode_inject_skills = False

    r = OpenCodeRunner(_Settings())
    assert r._inject_skills is False
