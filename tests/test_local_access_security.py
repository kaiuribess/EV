"""Local HTTP/WebSocket boundary checks; no application startup or account IO."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from evplatform.api import create_app, local_origin
from evplatform.core.config import Settings


@pytest.mark.parametrize("origin", [None, "http://localhost:8001", "https://127.0.0.1:8001",
    "http://[::1]:8001", "http://localhost:5173"])
def test_valid_local_origin(origin):
    assert local_origin(origin, 8001)


@pytest.mark.parametrize("origin", ["", "null", "http://localhost:bad", "http://localhost:99999",
    "http://[::1", "http://localhost:8001/path", "http://localhost:8001?x=1",
    "http://localhost:8001#fragment", "http://user@localhost:8001", "http://user:pass@localhost:8001",
    "http://localhost:8002", "http://localhost.evil:8001", "https://example.com:8001",
    "ftp://localhost:8001", " http://localhost:8001", "http://local\thost:8001",
    "http://localhost:8001\n", "http://localhost:8001 http://example.com"])
def test_malformed_or_untrusted_origins_fail_closed(origin):
    assert not local_origin(origin, 8001)


@pytest.fixture
def client(tmp_path):
    # Not entering a TestClient context deliberately leaves lifespan unstarted.
    return TestClient(create_app(Settings(database_path=str(tmp_path / "unused.db"), port=8001)))


@pytest.mark.parametrize("headers", [{"Origin": "http://localhost:bad"},
    {"Origin": "http://example.com:8001"}, {"Host": "example.com"},
    {"Host": "user@localhost"}])
def test_http_access_rejected_with_security_headers(client, headers):
    response = client.get("/api/health", headers=headers)
    assert response.status_code == 403
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_health_is_not_browser_cacheable(client):
    response = client.get("/api/health", headers={"Origin": "http://localhost:8001"})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_write_without_local_control_header_is_rejected(client):
    assert client.post("/api/nonexistent", json={}).status_code == 403


@pytest.mark.parametrize("headers", [{"Host": "example.com"}, {"Host": "user@localhost"},
    {"Origin": "http://example.com:8001"}, {"Origin": "http://localhost:bad"}])
def test_websocket_boundary_matches_http_boundary(client, headers):
    with pytest.raises(WebSocketDisconnect) as failure:
        with client.websocket_connect("/ws", headers=headers):
            pytest.fail("Untrusted WebSocket must not be accepted")
    assert failure.value.code == 1008
