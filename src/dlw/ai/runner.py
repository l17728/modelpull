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
    Raises AIBackendUnavailable if the binary is missing.

    Skills-style tool bridge (SP4f): prepends a generated MANIFEST.md
    listing all READONLY_TOOLS + WRITE_TOOLS with shell-command recipes
    so opencode/Claude can pick and invoke tools via bash, no MCP server
    needed. See ai/opencode_skills.py for the generator."""

    def __init__(self, settings):
        self.backend_name = "opencode"
        self.model_name = getattr(settings, "ai_model_name", "opencode")
        self._bin = getattr(settings, "ai_opencode_bin", "opencode")
        # Skills toggle — operator can disable the manifest if it bloats
        # the prompt or if a future opencode version supports MCP natively.
        self._inject_skills = getattr(
            settings, "ai_opencode_inject_skills", True)
        self._skills_manifest = None

    def _get_skills_manifest(self) -> str:
        if self._skills_manifest is None:
            from dlw.ai.opencode_skills import build_skills_manifest
            self._skills_manifest = build_skills_manifest()
        return self._skills_manifest

    async def run(self, ctx: AgentContext, *,
                  call_tool: CallTool) -> AsyncIterator[AgentEvent]:
        import asyncio
        import queue
        import shutil
        import subprocess
        import sys
        import threading

        resolved = shutil.which(self._bin)
        if resolved is None:
            raise AIBackendUnavailable(
                f"opencode binary '{self._bin}' not found on PATH")
        # Build command args.  On Windows, .cmd wrappers must be invoked via
        # cmd.exe /c because subprocess cannot execute .cmd files directly.
        args = [resolved, "run"]
        if self.model_name and self.model_name != "opencode":
            args += ["--model", self.model_name]
        # SP4f: prepend the tool catalog so the model can pick + shell-out
        # to dlw CLI / curl. Wrapped in a clear delimiter so the model
        # can distinguish "here's how to act" from "user's actual question".
        if self._inject_skills:
            manifest = self._get_skills_manifest()
            payload = (
                "<system_instructions>\n"
                f"{manifest}\n"
                "</system_instructions>\n\n"
                "<user_message>\n"
                f"{ctx.user_message}\n"
                "</user_message>"
            )
        else:
            payload = ctx.user_message
        args.append(payload)
        if sys.platform == "win32" and resolved.lower().endswith(".cmd"):
            args = ["cmd.exe", "/c"] + args

        # uvicorn on Windows intentionally uses SelectorEventLoop (for
        # multiprocessing compatibility), which does not support
        # asyncio.create_subprocess_exec.  Run the subprocess in a thread
        # and stream output through an asyncio.Queue to stay non-blocking.
        _SENTINEL = object()
        line_queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_event_loop()
        _ANSI = re.compile(r"\x1b\[[0-9;]*[mGKHF]|\x1b\[[0-9;]*m")

        def _run_in_thread() -> None:
            try:
                proc = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=False,
                )
                # Stream stdout line-by-line; stderr drained in a sub-thread.
                stderr_lines: list[bytes] = []

                def _drain() -> None:
                    if proc.stderr:
                        for ln in proc.stderr:
                            stderr_lines.append(ln)

                drain_t = threading.Thread(target=_drain, daemon=True)
                drain_t.start()
                if proc.stdout:
                    for raw in proc.stdout:
                        loop.call_soon_threadsafe(
                            line_queue.put_nowait,
                            ("line", raw))
                rc = proc.wait()
                drain_t.join()
                if rc != 0:
                    err = b"".join(stderr_lines).decode("utf-8", "replace")
                    loop.call_soon_threadsafe(
                        line_queue.put_nowait,
                        ("error", err[:500] or f"exit {rc}"))
            except Exception as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(
                    line_queue.put_nowait,
                    ("exc", exc))
            finally:
                loop.call_soon_threadsafe(line_queue.put_nowait, _SENTINEL)

        thread = threading.Thread(target=_run_in_thread, daemon=True)
        thread.start()

        # SP4f: per-turn marker parser that converts [[dlw_tool ...]] /
        # [[dlw_tool_result ...]] lines from the LLM's stdout into
        # tool_call / tool_result events so the decision-chain UI lights
        # up for opencode the same way as for the stub backend.
        from dlw.ai.opencode_marker_parser import MarkerParser
        marker_parser = MarkerParser()

        while True:
            item = await line_queue.get()
            if item is _SENTINEL:
                break
            kind, payload = item
            if kind == "line":
                line = _ANSI.sub("", payload.decode("utf-8", "replace").rstrip("\n"))
                if not line or line.lstrip().startswith(">"):
                    continue
                parsed = marker_parser.feed(line)
                if parsed.tool_call is not None:
                    yield AgentEvent("tool_call", parsed.tool_call)
                elif parsed.tool_result is not None:
                    yield AgentEvent("tool_result", parsed.tool_result)
                elif parsed.text is not None:
                    yield AgentEvent("assistant.message_delta",
                                     {"text": parsed.text + "\n"})
            elif kind == "error":
                yield AgentEvent("error",
                                 {"code": "opencode_failed", "message": payload})
            elif kind == "exc":
                raise AIBackendUnavailable(
                    f"opencode subprocess error: {payload}") from payload


def build_runner(settings) -> AgentRunner:
    b = getattr(settings, "ai_backend", "stub")
    if b == "stub":
        return StubAgentRunner(getattr(settings, "ai_model_name", "stub-model"))
    if b == "opencode":
        return OpenCodeRunner(settings)
    raise AIBackendUnavailable(f"AI backend '{b}' not wired in SP4a")
