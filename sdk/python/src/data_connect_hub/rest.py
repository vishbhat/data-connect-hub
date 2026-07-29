"""Async REST client for DCH connection management APIs."""

from __future__ import annotations

import httpx

from ._auth import build_headers
from .exceptions import DCHConnectionError, DCHTimeoutError, map_http_error
from .models import (
    ConnectionType,
    CreateConnectionRequest,
    CreateConnectionTypeRequest,
    DataConnection,
    UpdateConnectionRequest,
    UpdateConnectionTypeRequest,
)

_DEFAULT_API_BASE = "/api/v1/data"


class RestClient:
    """httpx-based async REST client for DCH."""

    def __init__(
        self,
        base_url: str,
        token: str,
        tenant_id: str,
        *,
        api_base: str = _DEFAULT_API_BASE,
        timeout: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._tenant_id = tenant_id
        self._api_base = api_base
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self, connection_id: str | None = None) -> dict[str, str]:
        return build_headers(
            token=self._token,
            tenant_id=self._tenant_id,
            connection_id=connection_id,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        connection_id: str | None = None,
        json: dict[str, object] | None = None,
    ) -> httpx.Response:
        try:
            resp = await self._client.request(
                method,
                f"{self._api_base}{path}",
                headers=self._headers(connection_id),
                json=json,
            )
        except httpx.ConnectError as exc:
            raise DCHConnectionError(f"Failed to connect to {self._base_url}: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise DCHTimeoutError(f"Request timed out: {exc}") from exc
        if resp.status_code >= 400:
            raise map_http_error(resp)
        return resp

    # -- Connections CRUD --

    async def list_connections(self) -> list[DataConnection]:
        resp = await self._request("GET", "/connections")
        return [DataConnection.model_validate(c) for c in resp.json()]

    async def get_connection(self, connection_id: str) -> DataConnection:
        resp = await self._request("GET", f"/connections/{connection_id}")
        return DataConnection.model_validate(resp.json())

    async def create_connection(self, request: CreateConnectionRequest) -> DataConnection:
        resp = await self._request(
            "POST",
            "/connections",
            json=request.model_dump(exclude_none=True),
        )
        return DataConnection.model_validate(resp.json())

    async def update_connection(self, connection_id: str, request: UpdateConnectionRequest) -> DataConnection:
        resp = await self._request(
            "PATCH",
            f"/connections/{connection_id}",
            json=request.model_dump(exclude_none=True),
        )
        return DataConnection.model_validate(resp.json())

    async def delete_connection(self, connection_id: str) -> None:
        await self._request("DELETE", f"/connections/{connection_id}")

    # -- Connection Types CRUD --

    async def list_connection_types(self) -> list[ConnectionType]:
        resp = await self._request("GET", "/connection_types")
        return [ConnectionType.model_validate(ct) for ct in resp.json()]

    async def get_connection_type(self, type_id: str) -> ConnectionType:
        resp = await self._request("GET", f"/connection_types/{type_id}")
        return ConnectionType.model_validate(resp.json())

    async def create_connection_type(self, request: CreateConnectionTypeRequest) -> ConnectionType:
        resp = await self._request(
            "POST",
            "/connection_types",
            json=request.model_dump(exclude_none=True),
        )
        return ConnectionType.model_validate(resp.json())

    async def update_connection_type(self, type_id: str, request: UpdateConnectionTypeRequest) -> ConnectionType:
        resp = await self._request(
            "PATCH",
            f"/connection_types/{type_id}",
            json=request.model_dump(exclude_none=True),
        )
        return ConnectionType.model_validate(resp.json())

    async def delete_connection_type(self, type_id: str) -> None:
        await self._request("DELETE", f"/connection_types/{type_id}")

    # -- Unstructured ingestion --

    async def ingest(self, connection_id: str) -> bytes:
        resp = await self._request(
            "GET",
            f"/ingestion/{connection_id}",
            connection_id=connection_id,
        )
        return resp.content
