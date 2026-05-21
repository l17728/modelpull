"""StubAgentRunner + build_runner (UI-SP4a)."""
from __future__ import annotations

import pytest

from dlw.ai.runner import (AgentContext, AIBackendUnavailable, StubAgentRunner,
                           build_runner)


class _Settings:
    def __init__(self, backend="stub"):
        self.ai_backend = backend
        self.ai_model_name = "stub-model"
        self.ai_opencode_bin = "opencode"


async def _collect(runner, ctx, call_tool):
    return [ev async for ev in runner.run(ctx, call_tool=call_tool)]


async def test_stub_task_query_emits_tool_sequence():
    seen_calls = []

    async def call_tool(name, tool_input):
        seen_calls.append((name, tool_input))
        return {"items": [{"id": "x"}, {"id": "y"}]}

    evs = await _collect(StubAgentRunner(),
                         AgentContext(user_message="list my tasks"),
                         call_tool)
    kinds = [e.event for e in evs]
    assert kinds == ["assistant.thinking", "tool_call", "tool_result",
                     "assistant.message_delta"]
    assert seen_calls == [("dlw_list_tasks", {"limit": 20})]
    assert "2 task" in evs[-1].data["text"]


async def test_stub_non_task_message_echoes():
    async def call_tool(name, tool_input):  # should not be called
        raise AssertionError("tool should not run for echo path")

    evs = await _collect(StubAgentRunner(),
                         AgentContext(user_message="hello there"),
                         call_tool)
    assert [e.event for e in evs] == ["assistant.message_delta"]
    assert "hello there" in evs[0].data["text"]


def test_build_runner_stub_and_unavailable():
    assert isinstance(build_runner(_Settings("stub")), StubAgentRunner)
    with pytest.raises(AIBackendUnavailable):
        build_runner(_Settings("claude_code"))
    with pytest.raises(AIBackendUnavailable):
        build_runner(_Settings("openai_compat"))
