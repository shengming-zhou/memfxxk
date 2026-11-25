import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def test_health_requires_bearer_key(api_client: TestClient) -> None:
    api_client.headers.pop("Authorization")

    response = api_client.get("/v1/healthz")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert_error_shape(response.json(), "unauthorized")


def test_health_rejects_wrong_key(api_client: TestClient) -> None:
    response = api_client.get("/v1/healthz", headers={"Authorization": "Bearer invalid"})

    assert response.status_code == 401
    assert_error_shape(response.json(), "unauthorized")


def test_health_accepts_valid_key(api_client: TestClient) -> None:
    response = api_client.get("/v1/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def assert_error_shape(body: dict[str, object], code: str) -> None:
    assert set(body) == {"error"}
    error = body["error"]
    assert isinstance(error, dict)
    assert set(error) == {"code", "message", "details"}
    assert error["code"] == code
    assert isinstance(error["message"], str)
