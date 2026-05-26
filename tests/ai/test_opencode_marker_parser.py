"""Tests for the SP4f marker parser (opencode stdout → tool events)."""
from __future__ import annotations

from dlw.ai.opencode_marker_parser import MarkerParser


def test_plain_text_passes_through():
    p = MarkerParser()
    r = p.feed("hello world")
    assert r.text == "hello world"
    assert r.tool_call is None
    assert r.tool_result is None


def test_open_marker_emits_tool_call():
    p = MarkerParser()
    r = p.feed('[[dlw_tool name=search_huggingface_models input={"query":"deepseek"}]]')
    assert r.text is None
    assert r.tool_call is not None
    assert r.tool_call["tool"] == "search_huggingface_models"
    assert r.tool_call["input"] == {"query": "deepseek"}
    assert r.tool_call["id"].startswith("oc_")
    assert "parse_error" not in r.tool_call


def test_close_marker_emits_tool_result_with_matching_id():
    p = MarkerParser()
    open_r = p.feed('[[dlw_tool name=dlw_list_tasks input={"limit":10}]]')
    open_id = open_r.tool_call["id"]
    close_r = p.feed('[[dlw_tool_result name=dlw_list_tasks ok=true output={"count":3}]]')
    assert close_r.tool_result is not None
    assert close_r.tool_result["id"] == open_id
    assert close_r.tool_result["ok"] is True
    assert close_r.tool_result["output"] == {"count": 3}


def test_close_marker_ok_false():
    p = MarkerParser()
    p.feed('[[dlw_tool name=web_search input={"query":"x"}]]')
    r = p.feed('[[dlw_tool_result name=web_search ok=false]]')
    assert r.tool_result["ok"] is False
    assert r.tool_result["output"] == {}


def test_orphan_close_marker_gets_synthetic_id():
    """Close marker with no matching open shouldn't crash; it gets a
    synthetic orphan id so the UI can still render something."""
    p = MarkerParser()
    r = p.feed('[[dlw_tool_result name=ghost ok=true]]')
    assert r.tool_result is not None
    assert r.tool_result["id"].startswith("oc_orphan_")


def test_malformed_input_json_flags_parse_error_but_emits_event():
    p = MarkerParser()
    r = p.feed('[[dlw_tool name=foo input=not-json]]')
    assert r.tool_call is not None
    assert r.tool_call["input"] == {}
    assert r.tool_call.get("parse_error") == "input_json"


def test_malformed_output_json_flags_parse_error_but_emits_event():
    p = MarkerParser()
    p.feed('[[dlw_tool name=foo input={}]]')
    r = p.feed('[[dlw_tool_result name=foo ok=true output=junk]]')
    assert r.tool_result is not None
    assert r.tool_result["output"] == {}
    assert r.tool_result.get("parse_error") == "output_json"


def test_open_without_input_clause():
    """The input= clause is optional (some tools take no args)."""
    p = MarkerParser()
    r = p.feed('[[dlw_tool name=dlw_quota_current]]')
    assert r.tool_call is not None
    assert r.tool_call["tool"] == "dlw_quota_current"
    assert r.tool_call["input"] == {}


def test_marker_with_leading_whitespace_still_matches():
    p = MarkerParser()
    r = p.feed('   [[dlw_tool name=foo input={}]]')
    assert r.tool_call is not None


def test_text_containing_brackets_not_treated_as_marker():
    """The user reply might contain bracket text — only EXACT marker lines
    are consumed; arbitrary brackets pass through."""
    p = MarkerParser()
    cases = [
        "Here is [[brackets]] in text",
        "[[ not a tool marker",
        "[[dlw_tool but malformed]]",
        "prefix [[dlw_tool name=x input={}]] suffix",  # not anchored
    ]
    for line in cases:
        r = p.feed(line)
        assert r.text == line, f"line wrongly consumed: {line!r}"


def test_two_consecutive_calls_get_unique_ids():
    p = MarkerParser()
    a = p.feed('[[dlw_tool name=foo input={}]]')
    b = p.feed('[[dlw_tool name=foo input={}]]')
    # Same tool name twice — the second open overwrites the open-id map
    # for that name, but each emitted event still has a fresh unique id.
    assert a.tool_call["id"] != b.tool_call["id"]


def test_close_matches_most_recent_open_for_same_tool():
    p = MarkerParser()
    a = p.feed('[[dlw_tool name=foo input={"v":1}]]')
    b = p.feed('[[dlw_tool name=foo input={"v":2}]]')
    # First close should match the most recent open (= b)
    r = p.feed('[[dlw_tool_result name=foo ok=true]]')
    assert r.tool_result["id"] == b.tool_call["id"]
    # Second close (no more opens) → orphan
    r2 = p.feed('[[dlw_tool_result name=foo ok=true]]')
    assert r2.tool_result["id"].startswith("oc_orphan_")
