"""v2.1 Sprint 13 — Live Console dispatch tests (dispatcher side)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from dlw.schemas.reverse_ws import WHITELIST_COMMANDS, CommandFrame
from dlw.services.reverse_dispatcher import (
    CommandNotWhitelisted,
    CommandUnknownExecutor,
    get_dispatcher,
)
from dlw.services.reverse_ws_registry import get_registry


@dataclass
class _StubWS:
    sent: list[str] = field(default_factory=list)

    async def send_text(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self, code: int) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_singletons():
    get_dispatcher()._reset_for_tests()
    get_registry()._reset_for_tests()
    yield
    get_dispatcher()._reset_for_tests()
    get_registry()._reset_for_tests()


# ---------------------------------------------------------------------------
# Whitelist enforcement

@pytest.mark.parametrize("cmd", WHITELIST_COMMANDS)
async def test_each_whitelisted_command_dispatches(cmd):
    """Every command in WHITELIST_COMMANDS must succeed on a live session."""
    ws = _StubWS()
    await get_registry().register(
        executor_id="ex-cmd", websocket=ws, protocol_version="1.0")
    cid = await get_dispatcher().send_command(
        executor_id="ex-cmd", command=cmd)
    assert cid
    assert len(ws.sent) == 1
    frame = CommandFrame.model_validate(json.loads(ws.sent[0]))
    assert frame.command == cmd
    assert frame.command_id == cid


async def test_non_whitelisted_command_rejected():
    """A command not in WHITELIST_COMMANDS must raise before reaching
    the websocket — defense in depth (the admin REST layer also rejects)."""
    ws = _StubWS()
    await get_registry().register(
        executor_id="ex-x", websocket=ws, protocol_version="1.0")
    with pytest.raises(CommandNotWhitelisted):
        await get_dispatcher().send_command(
            executor_id="ex-x", command="rm-rf-everything")
    assert ws.sent == []  # nothing sent


async def test_command_to_offline_executor_raises():
    """No live session → CommandUnknownExecutor (REST layer maps to 404)."""
    with pytest.raises(CommandUnknownExecutor):
        await get_dispatcher().send_command(
            executor_id="ex-offline", command="status")


async def test_explicit_command_id_is_preserved():
    """Caller may pass command_id (e.g. for idempotency or correlation)."""
    ws = _StubWS()
    await get_registry().register(
        executor_id="ex-y", websocket=ws, protocol_version="1.0")
    cid = await get_dispatcher().send_command(
        executor_id="ex-y", command="drain", command_id="my-fixed-id")
    assert cid == "my-fixed-id"
    frame = CommandFrame.model_validate(json.loads(ws.sent[0]))
    assert frame.command_id == "my-fixed-id"


async def test_frame_validate_command_helper():
    """CommandFrame.validate_command is the shared whitelist check;
    must reject anything outside the tuple."""
    CommandFrame.validate_command("status")
    CommandFrame.validate_command("drain")
    CommandFrame.validate_command("restart")
    with pytest.raises(ValueError):
        CommandFrame.validate_command("evil")
