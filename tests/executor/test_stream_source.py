"""ControllerClient.stream_source targets /source-proxy (Phase 3 SP2)."""
from __future__ import annotations

import uuid

import httpx

from dlw.executor.client import ControllerClient
from tests.conftest import make_fake_auth_state


async def test_stream_source_hits_source_proxy(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["range"] = request.headers.get("Range")
        return httpx.Response(200, content=b"DATA")

    c = ControllerClient(
        "http://ctrl",
        auth_state=make_fake_auth_state(tmp_path),
        _transport=httpx.MockTransport(handler))
    sid = uuid.uuid4()
    tok = uuid.uuid4()
    async with c.stream_source(subtask_id=sid, assignment_token=tok,
                               range_header="bytes=0-3") as resp:
        assert resp.status_code == 200
        body = b""
        async for b in resp.aiter_bytes():
            body += b
    assert body == b"DATA"
    assert seen["path"] == f"/api/v1/source-proxy/subtask/{sid}"
    assert seen["range"] == "bytes=0-3"
