"""AgentRunner abstraction + backends (UI-SP4a).

stub      — deterministic, scripted; CI/tests; no secret, no subprocess.
opencode  — `opencode` CLI subprocess (live backend; binary must be on PATH).
claude_code / openai_compat — structural only in SP4a (raise AIBackendUnavailable).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field


class AIBackendUnavailable(RuntimeError):
    pass


@dataclass
class AgentEvent:
    event: str          # assistant.thinking | tool_call | tool_result
                        # | tool_error | assistant.message_delta | error | done
    data: dict


@dataclass
class AgentContext:
    history: list[dict] = field(default_factory=list)
    user_message: str = ""


CallTool = Callable[[str, dict], Awaitable[dict]]


class AgentRunner(ABC):
    backend_name: str
    model_name: str

    @abstractmethod
    def run(self, ctx: AgentContext, *,
            call_tool: CallTool) -> AsyncIterator[AgentEvent]:
        """Yield the assistant turn's events. `call_tool(name, input) -> dict`
        is supplied by the chat service (tenant-scoped + audited). The stub
        uses it; OpenCodeRunner accepts it but does not yet dispatch tools
        (plain Q&A via subprocess stdout; the MCP tool bridge is a follow-on)."""
        ...


_TASK_KEYWORDS = ("task", "任务", "download", "下载", "job", "失败", "fail")


class StubAgentRunner(AgentRunner):
    """Deterministic. If the message mentions tasks, calls dlw_list_tasks and
    summarizes; else echoes. Used for all CI/tests (no secret, no subprocess)."""

    def __init__(self, model_name: str = "stub-model"):
        self.backend_name = "stub"
        self.model_name = model_name

    async def run(self, ctx: AgentContext, *,
                  call_tool: CallTool) -> AsyncIterator[AgentEvent]:
        msg = ctx.user_message
        if any(k in msg.lower() for k in _TASK_KEYWORDS):
            yield AgentEvent("assistant.thinking",
                             {"text": "Looking up your tasks…"})
            yield AgentEvent("tool_call",
                             {"id": "call_1", "tool": "dlw_list_tasks",
                              "input": {"limit": 20},
                              "requires_confirmation": False})
            result = await call_tool("dlw_list_tasks", {"limit": 20})
            yield AgentEvent("tool_result",
                             {"id": "call_1", "ok": "error" not in result,
                              "output": result})
            n = len(result.get("items", []))
            yield AgentEvent("assistant.message_delta",
                             {"text": f"You have {n} task(s)."})
        else:
            yield AgentEvent("assistant.message_delta",
                             {"text": f"(stub) You said: {msg}"})


class OpenCodeRunner(AgentRunner):
    """`opencode` CLI subprocess. Live backend; binary must be on PATH.
    Exact flags resolved at deploy time against the installed version.
    Raises AIBackendUnavailable if the binary is missing; tool dispatch via
    MCP is a follow-on (SP4a streams stdout as message deltas for plain Q&A)."""

    def __init__(self, settings):
        self.backend_name = "opencode"
        self.model_name = getattr(settings, "ai_model_name", "opencode")
        self._bin = getattr(settings, "ai_opencode_bin", "opencode")

    async def run(self, ctx: AgentContext, *,
                  call_tool: CallTool) -> AsyncIterator[AgentEvent]:
        import asyncio
        import shutil
        if shutil.which(self._bin) is None:
            raise AIBackendUnavailable(
                f"opencode binary '{self._bin}' not found on PATH")
        proc = await asyncio.create_subprocess_exec(
            self._bin, "run", "--print", ctx.user_message,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        if proc.stdout is None:
            raise AIBackendUnavailable("opencode stdout pipe unavailable")
        # Drain stderr concurrently to avoid a pipe-buffer deadlock.
        stderr_buf: list[bytes] = []

        async def _drain_stderr() -> None:
            if proc.stderr is None:
                return
            async for line in proc.stderr:
                stderr_buf.append(line)

        drainer = asyncio.create_task(_drain_stderr())
        try:
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line:
                    yield AgentEvent("assistant.message_delta", {"text": line})
        finally:
            rc = await proc.wait()
            await drainer
            if rc != 0:
                err = b"".join(stderr_buf).decode("utf-8", "replace")
                yield AgentEvent("error",
                                 {"code": "opencode_failed",
                                  "message": err[:500] or f"exit {rc}"})


def build_runner(settings) -> AgentRunner:
    b = getattr(settings, "ai_backend", "stub")
    if b == "stub":
        return StubAgentRunner(getattr(settings, "ai_model_name", "stub-model"))
    if b == "opencode":
        return OpenCodeRunner(settings)
    raise AIBackendUnavailable(f"AI backend '{b}' not wired in SP4a")
