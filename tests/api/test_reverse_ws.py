"""v2.1 Sprint 10 — Reverse-WSS unit tests.

WebSocket end-to-end coverage via TestClient hits a Windows-specific
asyncio proactor race during test teardown that's independent of our
code. We test core handler logic directly via the public helpers + the
registry contract. The full WS round-trip is exercised by the smoke
test in `_ws_smoke.py` (run on Linux CI)."""
from __future__ import annotations

import asyncio
import json

import jwt as _pyjwt
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.api.reverse_ws import _authenticate_executor
from dlw.auth.jwt_signing import sign as sign_jwt
from dlw.db.base import Base
from dlw.db.models.executor import Executor
from dlw.schemas.reverse_ws import (
    PROTOCOL_VERSION,
    ErrorFrame,
    HeartbeatAckFrame,
    HeartbeatFrame,
    HelloAckFrame,
    HelloFrame,
)
from dlw.services.reverse_ws_registry import (
    ReverseWSRegistry,
    get_registry,
)


_EX_ID = "ex-rev-ws-1"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Executor(id=_EX_ID, status="healthy", epoch=1,
                        host_id="h", tenant_id=None,
                        cert_fingerprint="dummy-fp-for-test"))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# ---------------------------------------------------------------------------
# Frame schema (pydantic models)

def test_hello_frame_round_trips_via_json():
    h = HelloFrame(executor_id="ex-1", executor_version="1.0.0-test")
    raw = h.model_dump_json()
    back = HelloFrame.model_validate(json.loads(raw))
    assert back.executor_id == "ex-1"
    assert back.protocol_version == PROTOCOL_VERSION


def test_heartbeat_frames_are_minimal():
    """Heartbeat + ack have no body fields — verify serialization is just
    the type discriminator."""
    raw = HeartbeatFrame().model_dump_json()
    assert json.loads(raw) == {"type": "heartbeat"}
    raw = HeartbeatAckFrame().model_dump_json()
    assert json.loads(raw) == {"type": "heartbeat_ack"}


def test_error_frame_carries_code_and_message():
    raw = ErrorFrame(code="X", message="m").model_dump_json()
    obj = json.loads(raw)
    assert obj == {"type": "error", "code": "X", "message": "m"}


def test_hello_ack_defaults_heartbeat_interval():
    """Server tells client how often to heartbeat — default 30s."""
    a = HelloAckFrame(session_id="sess-1")
    assert a.heartbeat_interval_seconds == 30.0


# ---------------------------------------------------------------------------
# JWT authentication helper

async def test_authenticate_executor_happy_path(ephemeral_ca):
    token = sign_jwt(
        ephemeral_ca["jwt_keypair"],
        executor_id=_EX_ID, epoch=1,
        scopes=["executor"], ttl_seconds=300)
    ex = await _authenticate_executor(
        token, ephemeral_ca["jwt_keypair"], _EX_ID)
    assert ex is not None
    assert ex.id == _EX_ID


async def test_authenticate_executor_bad_token_returns_none(ephemeral_ca):
    ex = await _authenticate_executor(
        "garbage", ephemeral_ca["jwt_keypair"], _EX_ID)
    assert ex is None


async def test_authenticate_executor_sub_mismatch_returns_none(ephemeral_ca):
    """JWT issued for one executor cannot be used to authenticate another."""
    token = sign_jwt(
        ephemeral_ca["jwt_keypair"],
        executor_id="some-other-id", epoch=1,
        scopes=["executor"], ttl_seconds=300)
    ex = await _authenticate_executor(
        token, ephemeral_ca["jwt_keypair"], _EX_ID)
    assert ex is None


async def test_authenticate_executor_unknown_id_returns_none(ephemeral_ca):
    """JWT verifies but the Executor row doesn't exist."""
    token = sign_jwt(
        ephemeral_ca["jwt_keypair"],
        executor_id="never-registered", epoch=1,
        scopes=["executor"], ttl_seconds=300)
    ex = await _authenticate_executor(
        token, ephemeral_ca["jwt_keypair"], "never-registered")
    assert ex is None


# ---------------------------------------------------------------------------
# Registry contract

async def test_registry_register_returns_unique_session_id():
    r = ReverseWSRegistry()
    a = await r.register(executor_id="ex-A", websocket=object(),
                          protocol_version="1.0")
    b = await r.register(executor_id="ex-B", websocket=object(),
                          protocol_version="1.0")
    assert a.session_id != b.session_id


async def test_registry_reconnect_evicts_old_session():
    """Second register() for the same executor_id closes the old socket
    and replaces it. unregister() with the OLD session_id must NOT touch
    the new one (race during reconnect)."""

    class _FakeWS:
        def __init__(self) -> None:
            self.closed = False
            self.close_code: int | None = None

        async def close(self, code: int) -> None:
            self.closed = True
            self.close_code = code

    r = ReverseWSRegistry()
    ws_old = _FakeWS()
    ws_new = _FakeWS()
    old_session = await r.register(
        executor_id="ex-X", websocket=ws_old, protocol_version="1.0")
    new_session = await r.register(
        executor_id="ex-X", websocket=ws_new, protocol_version="1.0")
    assert ws_old.closed is True
    assert ws_new.closed is False
    # Late-arriving unregister of OLD session_id is a no-op
    removed = await r.unregister(
        executor_id="ex-X", session_id=old_session.session_id)
    assert removed is False
    cur = await r.get("ex-X")
    assert cur is not None
    assert cur.session_id == new_session.session_id


async def test_registry_unregister_matching_session():
    r = ReverseWSRegistry()
    s = await r.register(executor_id="ex-Y", websocket=object(),
                          protocol_version="1.0")
    ok = await r.unregister(executor_id="ex-Y", session_id=s.session_id)
    assert ok is True
    assert await r.get("ex-Y") is None


async def test_registry_touch_heartbeat_updates_timestamp():
    r = ReverseWSRegistry()
    s = await r.register(executor_id="ex-Z", websocket=object(),
                          protocol_version="1.0")
    before = s.last_heartbeat_at
    # Sleep ε so the new timestamp is strictly greater
    await asyncio.sleep(0.01)
    ok = await r.touch_heartbeat(
        executor_id="ex-Z", session_id=s.session_id)
    assert ok is True
    after = (await r.get("ex-Z")).last_heartbeat_at
    assert after > before


async def test_registry_touch_heartbeat_wrong_session_rejected():
    """An old session_id whose connection was evicted must not be able
    to bump the heartbeat clock on the new one."""
    r = ReverseWSRegistry()
    s1 = await r.register(executor_id="ex-Q", websocket=object(),
                           protocol_version="1.0")
    # Reconnect with new session
    _ = await r.register(executor_id="ex-Q", websocket=object(),
                          protocol_version="1.0")
    ok = await r.touch_heartbeat(
        executor_id="ex-Q", session_id=s1.session_id)
    assert ok is False


async def test_registry_singleton_is_shared():
    """get_registry() returns the same module-level instance — both the
    WS endpoint and any future REST inspection read the same map."""
    assert get_registry() is get_registry()


# ---------------------------------------------------------------------------
# Sprint-10 protocol invariants (encoded in PROTOCOL_VERSION)

def test_protocol_version_is_pinned_string():
    """PROTOCOL_VERSION is the single source of truth for the wire
    format. Tests pin its value so a future schema change can't sneak
    past code review without flipping the constant."""
    assert PROTOCOL_VERSION == "1.0"
