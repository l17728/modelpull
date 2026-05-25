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


import re

_TASK_KEYWORDS = ("task", "任务", "download", "下载", "job", "失败", "fail")
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_REPO_RE = re.compile(r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+")


class StubAgentRunner(AgentRunner):
    """Deterministic. If the message mentions tasks, calls dlw_list_tasks and
    summarizes; else echoes. Used for all CI/tests (no secret, no subprocess)."""

    def __init__(self, model_name: str = "stub-model"):
        self.backend_name = "stub"
        self.model_name = model_name

    async def run(self, ctx: AgentContext, *,
                  call_tool: CallTool) -> AsyncIterator[AgentEvent]:
        msg = ctx.user_message
        low = msg.lower()
        # SP4b: write-tool PROPOSAL (never executes here — the chat service
        # persists a pending call + the user confirms in phase 2).
        m_uuid = _UUID_RE.search(msg)
        if ("cancel" in low or "取消" in low) and m_uuid:
            tid = m_uuid.group(0)
            yield AgentEvent("tool_call_pending_confirm", {
                "id": "", "tool": "dlw_cancel_task",
                "input": {"task_id": tid},
                "rationale": f"Cancel task {tid}.",
                "estimated_quota_impact": {}})
            yield AgentEvent("assistant.message_delta",
                             {"text": "Please confirm the cancellation."})
            return
        if any(k in low for k in ("web_search", "search the web", "搜索")):
            # Strip leading trigger words to leave the bare query.
            q = msg
            for prefix in ("web_search", "search the web for",
                           "search the web", "搜索"):
                if prefix in low:
                    idx = low.find(prefix)
                    q = (msg[:idx] + msg[idx + len(prefix):]).strip(" :")
                    break
            q = q.strip() or msg
            yield AgentEvent("tool_call",
                             {"id": "call_ws", "tool": "web_search",
                              "input": {"query": q},
                              "requires_confirmation": False})
            result = await call_tool("web_search", {"query": q})
            yield AgentEvent("tool_result",
                             {"id": "call_ws", "ok": "error" not in result,
                              "output": result})
            n = len(result.get("results", []))
            yield AgentEvent("assistant.message_delta",
                             {"text": f"Found {n} result(s) for '{q}'."})
            return
        m_repo = _REPO_RE.search(msg)
        if m_repo and any(k in low for k in ("card", "模型卡", "readme")):
            repo = m_repo.group(0)
            yield AgentEvent("tool_call",
                             {"id": "call_card", "tool": "hf_model_card",
                              "input": {"repo_id": repo},
                              "requires_confirmation": False})
            result = await call_tool("hf_model_card", {"repo_id": repo})
            yield AgentEvent("tool_result",
                             {"id": "call_card", "ok": "error" not in result,
                              "output": result})
            yield AgentEvent("assistant.message_delta",
                             {"text": f"Fetched & sanitized the model card "
                                      f"for {repo}."})
            return
        if m_repo and any(k in low for k in ("metadata", "元数据", "model info")):
            repo = m_repo.group(0)
            yield AgentEvent("tool_call",
                             {"id": "call_meta", "tool": "hf_api_metadata",
                              "input": {"repo_id": repo},
                              "requires_confirmation": False})
            result = await call_tool("hf_api_metadata", {"repo_id": repo})
            yield AgentEvent("tool_result",
                             {"id": "call_meta", "ok": "error" not in result,
                              "output": result})
            yield AgentEvent("assistant.message_delta",
                             {"text": f"Fetched HF metadata for {repo}."})
            return
        if ("create" in low or "download" in low or "下载" in low) and m_repo:
            repo = m_repo.group(0)
            yield AgentEvent("tool_call_pending_confirm", {
                "id": "", "tool": "dlw_create_task",
                "input": {"repo_id": repo, "revision": "0" * 40,
                          "storage_id": 1},
                "rationale": f"Create a download task for {repo}.",
                "estimated_quota_impact": {"bytes": 0}})
            yield AgentEvent("assistant.message_delta",
                             {"text": "Please confirm the new task."})
            return
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
        import sys
        resolved = shutil.which(self._bin)
        if resolved is None:
            raise AIBackendUnavailable(
                f"opencode binary '{self._bin}' not found on PATH")
        # Pass model override if configured (DLW_AI_MODEL_NAME=provider/model).
        # Plain text output (no --format flag): simpler and more reliable than
        # --format json which can stall on some providers.
        args = [resolved, "run"]
        if self.model_name and self.model_name != "opencode":
            args += ["--model", self.model_name]
        args.append(ctx.user_message)
        # On Windows, .cmd wrappers (npm-installed CLIs) cannot be launched
        # directly by asyncio.create_subprocess_exec — wrap with cmd.exe /c.
        if sys.platform == "win32" and resolved.lower().endswith(".cmd"):
            args = ["cmd.exe", "/c"] + args
        proc = await asyncio.create_subprocess_exec(
            *args,
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
        # Regex to strip ANSI escape sequences from opencode's colored output.
        _ANSI = re.compile(r"\x1b\[[0-9;]*[mGKHF]|\x1b\[[0-9;]*m")
        try:
            async for raw in proc.stdout:
                line = _ANSI.sub("", raw.decode("utf-8", "replace").rstrip("\n"))
                # Skip opencode UI header lines ("> build · <model>") and blank.
                if not line or line.lstrip().startswith(">"):
                    continue
                yield AgentEvent("assistant.message_delta", {"text": line + "\n"})
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
