from collections.abc import Iterator
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from memfxxk.client import MemfxxkClient


class InProcessTransport(httpx.BaseTransport):
    """Send synchronous SDK requests to the real ASGI application."""

    def __init__(self, app_client: TestClient) -> None:
        self.app_client = app_client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        forwarded = self.app_client.build_request(
            request.method,
            str(request.url),
            content=request.read(),
        )
        forwarded.headers.clear()
        forwarded.headers.update(request.headers.raw)
        response = self.app_client.send(forwarded)
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content
        )


@pytest.fixture
def sdk(api_client: TestClient) -> Iterator["MemfxxkClient"]:
    from memfxxk.client import MemfxxkClient

    engine_key = api_client.headers["Authorization"].removeprefix("Bearer ")
    with MemfxxkClient(
        str(api_client.base_url),
        engine_key,
        user_id="u1",
        agent_id="dev-agent",
        project_id="p1",
        transport=InProcessTransport(api_client),
    ) as client:
        yield client
