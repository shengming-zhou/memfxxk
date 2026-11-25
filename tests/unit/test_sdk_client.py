import httpx
import pytest

from memfxxk.client import MemfxxkClient, ServerError


def test_sdk_maps_http_and_transport_errors() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(503, text="unavailable"))
    with MemfxxkClient("http://testserver", "key", transport=transport) as client:
        with pytest.raises(ServerError) as unavailable:
            client.healthz()
    assert unavailable.value.status_code == 503
    assert unavailable.value.code == "http_503"

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("unavailable", request=request)

    with MemfxxkClient(
        "http://testserver", "key", transport=httpx.MockTransport(timeout)
    ) as client:
        with pytest.raises(ServerError, match="Could not connect"):
            client.healthz()
