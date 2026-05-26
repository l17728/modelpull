"""Parse opencode stdout for [[dlw_tool ...]] markers and yield synthetic
tool_call / tool_result events for the decision-chain UI (SP4f).

The skills MANIFEST (ai/opencode_skills.py) instructs the LLM to wrap each
tool invocation with two marker lines. We strip those lines from the user-
visible message stream and instead emit AgentEvent("tool_call"/"tool_result")
so the same chronological-steps panel that works for the stub backend lights
up for opencode too.

Design:
- Stateful per-turn parser (one instance per AgentRunner.run()).
- Each marker is a full line; the parser inspects each completed line.
- Robust to malformed markers: if input/output JSON fails to parse, the
  event is still emitted with empty payload + a `parse_error` flag.
- Markers are CONSUMED (not forwarded as text deltas) so the user sees a
  clean reply.

Marker grammar:
  [[dlw_tool name=<name> input=<json>]]
  [[dlw_tool_result name=<name> ok=<true|false> output=<json>?]]
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass

# Anchored regex: the marker must be the only thing on the line (other
# than surrounding whitespace). Anything else passes through untouched.
_OPEN_RE = re.compile(
    r"^\s*\[\[dlw_tool\s+name=([A-Za-z0-9_]+)(?:\s+input=(.*?))?\]\]\s*$")
_CLOSE_RE = re.compile(
    r"^\s*\[\[dlw_tool_result\s+name=([A-Za-z0-9_]+)\s+ok=(true|false)"
    r"(?:\s+output=(.*?))?\]\]\s*$")


@dataclass
class ParsedLine:
    """Result of feeding one line to the parser."""
    text: str | None = None                # forward this as a message delta
    tool_call: dict | None = None          # emit AgentEvent("tool_call", ...)
    tool_result: dict | None = None        # emit AgentEvent("tool_result", ...)


def _safe_json(blob: str | None) -> tuple[dict, bool]:
    """Best-effort JSON load. Returns (value, parse_ok)."""
    if not blob:
        return {}, True
    try:
        v = json.loads(blob)
        if isinstance(v, dict):
            return v, True
        return {"value": v}, True
    except (json.JSONDecodeError, ValueError):
        return {}, False


class MarkerParser:
    """One instance per OpenCodeRunner.run() call. Stateful: each open
    marker generates a fresh call_id used to match the close marker.
    Tracks open calls so we can emit tool_result with the right id."""

    def __init__(self) -> None:
        # Stack of {name: call_id}: most recent open of that name wins
        # if the LLM nests. opencode doesn't truly parallelize tool calls,
        # so a simple latest-wins map is sufficient.
        self._open: dict[str, str] = {}

    def feed(self, line: str) -> ParsedLine:
        m = _OPEN_RE.match(line)
        if m:
            name = m.group(1)
            input_json = m.group(2)
            inp, parse_ok = _safe_json(input_json)
            call_id = f"oc_{uuid.uuid4().hex[:8]}"
            self._open[name] = call_id
            payload = {"id": call_id, "tool": name, "input": inp,
                       "requires_confirmation": False}
            if not parse_ok:
                payload["parse_error"] = "input_json"
            return ParsedLine(tool_call=payload)
        m = _CLOSE_RE.match(line)
        if m:
            name = m.group(1)
            ok = m.group(2) == "true"
            out_json = m.group(3)
            out, parse_ok = _safe_json(out_json)
            call_id = self._open.pop(name, f"oc_orphan_{uuid.uuid4().hex[:8]}")
            payload = {"id": call_id, "ok": ok, "output": out}
            if not parse_ok:
                payload["parse_error"] = "output_json"
            return ParsedLine(tool_result=payload)
        # Pass-through: forward as a normal message delta.
        return ParsedLine(text=line)
