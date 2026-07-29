"""Tests for the async REST client."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from data_connect_hub.exceptions import (
    DCHAuthenticationError,
    DCHConnectionError,
    DCHForbiddenError,
    DCHNotFoundError,
    DCHServerError,
    DCHTimeoutError,
    DCHValidationError,
)
from data_connect_hub.models import (
    CreateConnectionRequest,
    DataLocation,
    UpdateConnectionRequest,
)
from data_connect_hub.rest import RestClient

from .conftest import SAMPLE_CONNECTION_JSON, SAMPLE_CONNECTION_TYPE_JSON


def _make_transport(
    status: int = 200,
    body: Any = None,
    *,
    assert_method: str | None = None,
    assert_path: str | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if assert_method:
            assert request.method == assert_method
        if assert_path:
            assert request.url.path == assert_path
        return httpx.Response(
            status,
            json=body if body is not None else {},
            headers={"content-type": "application/json"},
        )

    return httpx.MockTransport(handler)


def _make_client(
    transport: httpx.MockTransport,
    api_base: str = "/api/v1/data",
) -> RestClient:
    http_client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return RestClient(
        base_url="http://test",
        token="test-token",
        tenant_id="test-tenant",
        api_base=api_base,
        http_client=http_client,
    )


class TestListConnections:
    async def test_returns_connections(self) -> None:
        transport = _make_transport(
            body=[SAMPLE_CONNECTION_JSON],
            assert_method="GET",
            assert_path="/api/v1/data/connections",
        )
        client = _make_client(transport)
        result = await client.list_connections()
        assert len(result) == 1
        assert result[0].id == "123"
        assert result[0].namespace == "test-ns"

    async def test_empty_list(self) -> None:
        transport = _make_transport(body=[])
        client = _make_client(transport)
        result = await client.list_connections()
        assert result == []


class TestGetConnection:
    async def test_returns_connection(self) -> None:
        transport = _make_transport(
            body=SAMPLE_CONNECTION_JSON,
            assert_method="GET",
            assert_path="/api/v1/data/connections/123",
        )
        client = _make_client(transport)
        result = await client.get_connection("123")
        assert result.id == "123"
        assert result.provider == "postgres"


class TestCreateConnection:
    async def test_sends_post(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/api/v1/data/connections"
            body = json.loads(request.content)
            assert body["name"] == "new-conn"
            assert body["provider"] == "postgres"
            return httpx.Response(201, json=SAMPLE_CONNECTION_JSON)

        transport = httpx.MockTransport(handler)
        client = _make_client(transport)
        req = CreateConnectionRequest(
            namespace="ns",
            name="new-conn",
            provider="postgres",
            format="arrow",
            location=DataLocation(url="pg://localhost"),
        )
        result = await client.create_connection(req)
        assert result.id == "123"


class TestUpdateConnection:
    async def test_sends_patch(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "PATCH"
            assert request.url.path == "/api/v1/data/connections/123"
            body = json.loads(request.content)
            assert body == {"name": "updated"}
            return httpx.Response(200, json=SAMPLE_CONNECTION_JSON)

        transport = httpx.MockTransport(handler)
        client = _make_client(transport)
        req = UpdateConnectionRequest(name="updated")
        await client.update_connection("123", req)


class TestDeleteConnection:
    async def test_sends_delete(self) -> None:
        transport = _make_transport(
            assert_method="DELETE",
            assert_path="/api/v1/data/connections/123",
        )
        client = _make_client(transport)
        await client.delete_connection("123")


class TestConnectionTypes:
    async def test_list(self) -> None:
        transport = _make_transport(
            body=[SAMPLE_CONNECTION_TYPE_JSON],
            assert_method="GET",
            assert_path="/api/v1/data/connection_types",
        )
        client = _make_client(transport)
        result = await client.list_connection_types()
        assert len(result) == 1
        assert result[0].name == "postgres"

    async def test_get(self) -> None:
        transport = _make_transport(
            body=SAMPLE_CONNECTION_TYPE_JSON,
            assert_path="/api/v1/data/connection_types/ct-1",
        )
        client = _make_client(transport)
        result = await client.get_connection_type("ct-1")
        assert result.id == "ct-1"


class TestIngest:
    async def test_returns_bytes(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v1/data/ingestion/conn-1"
            assert request.headers.get("x-dch-connection-id") == "conn-1"
            return httpx.Response(200, content=b"raw-data")

        transport = httpx.MockTransport(handler)
        client = _make_client(transport)
        result = await client.ingest("conn-1")
        assert result == b"raw-data"


class TestHeaders:
    async def test_auth_and_tenant_headers(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["authorization"] == "Bearer test-token"
            assert request.headers["x-tenant-id"] == "test-tenant"
            return httpx.Response(200, json=[])

        transport = httpx.MockTransport(handler)
        client = _make_client(transport)
        await client.list_connections()


class TestCustomApiBase:
    async def test_uses_custom_path(self) -> None:
        transport = _make_transport(
            body=[],
            assert_path="/v1/data/connections",
        )
        client = _make_client(transport, api_base="/v1/data")
        await client.list_connections()


class TestErrorMapping:
    async def test_400_raises_validation(self) -> None:
        transport = _make_transport(status=400, body={"error": "bad request"})
        client = _make_client(transport)
        with pytest.raises(DCHValidationError) as exc_info:
            await client.list_connections()
        assert exc_info.value.status_code == 400

    async def test_401_raises_authentication(self) -> None:
        transport = _make_transport(status=401, body={"error": "unauthorized"})
        client = _make_client(transport)
        with pytest.raises(DCHAuthenticationError):
            await client.list_connections()

    async def test_403_raises_forbidden(self) -> None:
        transport = _make_transport(status=403, body={"error": "forbidden"})
        client = _make_client(transport)
        with pytest.raises(DCHForbiddenError):
            await client.list_connections()

    async def test_404_raises_not_found(self) -> None:
        transport = _make_transport(status=404, body={"error": "not found"})
        client = _make_client(transport)
        with pytest.raises(DCHNotFoundError):
            await client.get_connection("missing")

    async def test_500_raises_server_error(self) -> None:
        transport = _make_transport(status=500, body={"error": "internal"})
        client = _make_client(transport)
        with pytest.raises(DCHServerError):
            await client.list_connections()


class TestTransportErrors:
    async def test_connect_error_raises_dch_connection_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        transport = httpx.MockTransport(handler)
        client = _make_client(transport)
        with pytest.raises(DCHConnectionError, match="connection refused"):
            await client.list_connections()

    async def test_timeout_raises_dch_timeout_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timed out")

        transport = httpx.MockTransport(handler)
        client = _make_client(transport)
        with pytest.raises(DCHTimeoutError, match="timed out"):
            await client.list_connections()
