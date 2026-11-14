"""Shared fixtures."""

from __future__ import annotations

import pytest

from custom_components.openfeed.crypto import generate_private_key_pem, load_private_key

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ in every test."""
    yield


@pytest.fixture(scope="session")
def key_pems() -> tuple[str, str]:
    """One app and one DPoP key for the whole run (RSA generation is slow)."""
    return generate_private_key_pem(), generate_private_key_pem()


@pytest.fixture
def app_key(key_pems):
    return load_private_key(key_pems[0])


@pytest.fixture
def dpop_key(key_pems):
    return load_private_key(key_pems[1])


class FakeServer:
    """A local aiohttp server with queued responses per (method, path)."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list] = {}
        self.requests: list[dict] = []
        self.url = ""

    def add(self, method: str, path: str, status: int = 200, json=None, body: str | None = None,
            headers: dict | None = None, repeat: bool = False) -> None:
        self.routes.setdefault((method, path), []).append((status, json, body, headers or {}, repeat))

    async def handler(self, request):
        from aiohttp import web
        form = dict(await request.post()) if request.method == "POST" else {}
        self.requests.append({"method": request.method, "path": request.path, "raw_path": request.raw_path,
                              "query": dict(request.query), "headers": dict(request.headers), "form": form})
        queue = self.routes.get((request.method, request.path))
        if not queue:
            return web.json_response({"error": "unmocked", "path": request.path}, status=599)
        status, json_body, body, headers, repeat = queue[0] if queue[0][4] else queue.pop(0)
        if json_body is not None:
            return web.json_response(json_body, status=status, headers=headers)
        return web.Response(status=status, text=body or "", headers=headers)


@pytest.fixture
async def fake_server(socket_enabled):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    fake = FakeServer()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handler)
    server = TestServer(app)
    await server.start_server()
    fake.url = str(server.make_url("")).rstrip("/")
    yield fake
    await server.close()
