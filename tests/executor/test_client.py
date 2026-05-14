"""Tests for ControllerClient (W3a: mTLS + JWT + HMAC) using httpx MockTransport."""
from __future__ import annotations

import json
import uuid

import httpx
import pytest

from dlw.auth.hmac_nonce import verify_hmac
from dlw.executor.client import ControllerClient
from tests.conftest import make_fake_auth_state


_HMAC_SEED = b"\x05" * 32


def _mock_handler(request: httpx.Request) -> httpx.Response:
    """Routes requests to canned responses based on URL path."""
    path = request.url.path

    if path.endswith("/heartbeat") and request.method == "POST":
        body = json.loads(request.content) if request.content else {}
        return httpx.Response(200, json={
            "id": "x", "status": "healthy",
            "health_score": body.get("health_score", 100),
        })
    if path.endswith("/poll") and request.method == "POST":
        return httpx.Response(200, json={
            "assigned": True,
            "subtask": {
                "id": str(uuid.uuid4()),
                "task_id": str(uuid.uuid4()),
                "filename": "model.safetensors",
                "file_size": 1024,
                "expected_sha256": None,
                "status": "assigned",
            },
            "assignment_token": str(uuid.uuid4()),
        })
    if "/subtasks/" in path and path.endswith("/report"):
        return httpx.Response(200, json={
            "subtask_status": "succeeded", "task_status": "pending",
        })
    return httpx.Response(404)


@pytest.fixture
def transport() -> httpx.MockTransport:
    return httpx.MockTransport(_mock_handler)


@pytest.fixture
def auth_state(tmp_path):
    return make_fake_auth_state(
        tmp_path, executor_id="ex-1", epoch=1,
        jwt="header.payload.sig", hmac_seed=_HMAC_SEED,
    )


@pytest.mark.slow
async def test_heartbeat_returns_state(transport, auth_state) -> None:
    async with ControllerClient(
        base_url="http://test", auth_state=auth_state, _transport=transport,
    ) as c:
        r = await c.heartbeat(executor_id="ex-1", health_score=88, parts_dir_bytes=0)
    assert r["status"] == "healthy"
    assert r["health_score"] == 88


@pytest.mark.slow
async def test_poll_returns_assignment(transport, auth_state) -> None:
    async with ControllerClient(
        base_url="http://test", auth_state=auth_state, _transport=transport,
    ) as c:
        r = await c.poll(executor_id="ex-1")
    assert r["assigned"] is True
    assert "subtask" in r
    assert "assignment_token" in r


@pytest.mark.slow
async def test_report_propagates_token(transport, auth_state) -> None:
    async with ControllerClient(
        base_url="http://test", auth_state=auth_state, _transport=transport,
    ) as c:
        r = await c.report(
            subtask_id=uuid.uuid4(),
            status="succeeded",
            assignment_token=uuid.uuid4(),
            actual_sha256="a" * 64,
            bytes_downloaded=1024,
        )
    assert r["subtask_status"] == "succeeded"


@pytest.mark.slow
async def test_unauthenticated_returns_401(auth_state) -> None:
    """ControllerClient propagates 401 as an exception (caller decides retry)."""
    def unauth(_):
        return httpx.Response(401, json={"detail": "missing executor JWT"})
    t = httpx.MockTransport(unauth)
    async with ControllerClient(
        base_url="http://test", auth_state=auth_state, _transport=t,
    ) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await c.heartbeat(executor_id="ex-1", health_score=100, parts_dir_bytes=0)


@pytest.mark.slow
async def test_client_attaches_jwt_and_epoch_headers(tmp_path) -> None:
    """Every request carries Authorization: Bearer <jwt> + X-Executor-Epoch."""
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, json={"assigned": False})

    state = make_fake_auth_state(
        tmp_path, executor_id="h-w-1", epoch=7, jwt="jwt-token-7",
        hmac_seed=_HMAC_SEED,
    )
    async with ControllerClient(
        base_url="http://test", auth_state=state,
        _transport=httpx.MockTransport(handler),
    ) as c:
        await c.poll(executor_id="h-w-1")

    assert seen[0]["authorization"] == "Bearer jwt-token-7"
    assert seen[0]["x-executor-epoch"] == "7"


@pytest.mark.slow
async def test_client_signs_heartbeat_with_hmac(tmp_path) -> None:
    """heartbeat must carry X-HMAC-* headers and the signature must verify
    against the executor's hmac_seed over the exact request body."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        seen["body"] = bytes(request.content)
        return httpx.Response(200, json={"id": "h-w-1", "status": "healthy",
                                         "health_score": 100})

    state = make_fake_auth_state(
        tmp_path, executor_id="h-w-1", epoch=3, jwt="jwt-3",
        hmac_seed=_HMAC_SEED,
    )
    async with ControllerClient(
        base_url="http://test", auth_state=state,
        _transport=httpx.MockTransport(handler),
    ) as c:
        await c.heartbeat(executor_id="h-w-1", health_score=100, parts_dir_bytes=0)

    h = seen["headers"]
    assert "x-hmac-timestamp" in h
    assert "x-hmac-nonce" in h
    assert "x-hmac-signature" in h
    assert verify_hmac(
        _HMAC_SEED,
        ts=int(h["x-hmac-timestamp"]),
        nonce=h["x-hmac-nonce"],
        body=seen["body"],
        signature_hex=h["x-hmac-signature"],
    )


@pytest.mark.slow
async def test_current_epoch_reads_auth_state(tmp_path) -> None:
    state = make_fake_auth_state(tmp_path, epoch=42)
    c = ControllerClient(base_url="http://test", auth_state=state)
    assert c.current_epoch() == 42
    new = make_fake_auth_state(tmp_path, epoch=43)
    c.update_auth(new)
    assert c.current_epoch() == 43


@pytest.mark.slow
async def test_stream_hf_attaches_auth_and_token_headers(tmp_path) -> None:
    """stream_hf carries Authorization + X-Executor-Epoch + X-Assignment-Token,
    forwards Range when given, and hits /api/v1/hf-proxy/subtask/{id}."""
    seen: dict = {}
    body = b"hf-proxied-bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        seen["path"] = request.url.path
        return httpx.Response(200, content=body)

    state = make_fake_auth_state(
        tmp_path, executor_id="ex-stream", epoch=4, jwt="jwt-stream",
        hmac_seed=_HMAC_SEED,
    )
    sub_id = uuid.uuid4()
    tok = uuid.uuid4()
    async with ControllerClient(
        base_url="http://test", auth_state=state,
        _transport=httpx.MockTransport(handler),
    ) as c:
        async with c.stream_hf(
            subtask_id=sub_id, assignment_token=tok,
            range_header="bytes=0-1023",
        ) as resp:
            chunks = [chunk async for chunk in resp.aiter_bytes(8)]

    assert b"".join(chunks) == body
    assert seen["path"] == f"/api/v1/hf-proxy/subtask/{sub_id}"
    assert seen["headers"]["authorization"] == "Bearer jwt-stream"
    assert seen["headers"]["x-executor-epoch"] == "4"
    assert seen["headers"]["x-assignment-token"] == str(tok)
    assert seen["headers"]["range"] == "bytes=0-1023"
