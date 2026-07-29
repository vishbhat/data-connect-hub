"""Unified DataConnectClient combining REST and Flight SQL."""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Coroutine, Generator
from typing import Any, TypeVar

import pyarrow as pa

from .exceptions import DCHConfigError
from .flight import FlightClient
from .models import (
    ConnectionType,
    CreateConnectionRequest,
    CreateConnectionTypeRequest,
    DataConnection,
    DataLocation,
    UpdateConnectionRequest,
    UpdateConnectionTypeRequest,
)
from .rest import RestClient

_T = TypeVar("_T")


def _run_sync(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run an async coroutine synchronously.

    When called from within a running event loop (e.g. Jupyter), this falls
    back to executing the coroutine in a separate thread.  This avoids the
    ``RuntimeError: This event loop is already running`` but carries a small
    overhead from the thread-pool dispatch.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


class DataConnectClient:
    """Single entry point for all DCH interactions.

    Provides synchronous methods by default and ``*_async`` variants
    for async callers.
    """

    def __init__(
        self,
        rest_url: str | None = None,
        flight_url: str | None = None,
        token: str = "",
        tenant_id: str = "",
        *,
        api_base: str = "/api/v1/data",
        timeout: float = 30.0,
    ) -> None:
        self._rest: RestClient | None = None
        self._flight: FlightClient | None = None

        if rest_url:
            self._rest = RestClient(
                base_url=rest_url,
                token=token,
                tenant_id=tenant_id,
                api_base=api_base,
                timeout=timeout,
            )

        if flight_url:
            self._flight = FlightClient(
                flight_url=flight_url,
                token=token,
                tenant_id=tenant_id,
            )

    # -- context manager --

    def __enter__(self) -> DataConnectClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    async def __aenter__(self) -> DataConnectClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close_async()

    def close(self) -> None:
        if self._rest:
            _run_sync(self._rest.close())

    async def close_async(self) -> None:
        if self._rest:
            await self._rest.close()

    # -- guards --

    def _require_rest(self) -> RestClient:
        if self._rest is None:
            raise DCHConfigError("rest_url is required for this operation")
        return self._rest

    def _require_flight(self) -> FlightClient:
        if self._flight is None:
            raise DCHConfigError("flight_url is required for this operation")
        return self._flight

    # -- Connections (sync) --

    def list_connections(self) -> list[DataConnection]:
        return _run_sync(self._require_rest().list_connections())

    def get_connection(self, connection_id: str) -> DataConnection:
        return _run_sync(self._require_rest().get_connection(connection_id))

    def create_connection(
        self,
        *,
        name: str,
        namespace: str,
        provider: str,
        format: str,
        location_url: str,
        properties: dict[str, str] | None = None,
    ) -> DataConnection:
        req = CreateConnectionRequest(
            name=name,
            namespace=namespace,
            provider=provider,
            format=format,
            location=DataLocation(url=location_url),
            properties=properties or {},
        )
        return _run_sync(self._require_rest().create_connection(req))

    def update_connection(
        self,
        connection_id: str,
        *,
        name: str | None = None,
        namespace: str | None = None,
        provider: str | None = None,
        format: str | None = None,
        location_url: str | None = None,
        properties: dict[str, str] | None = None,
    ) -> DataConnection:
        if all(v is None for v in (name, namespace, provider, format, location_url, properties)):
            raise DCHConfigError("at least one field must be provided for update")
        location = DataLocation(url=location_url) if location_url else None
        req = UpdateConnectionRequest(
            name=name,
            namespace=namespace,
            provider=provider,
            format=format,
            location=location,
            properties=properties,
        )
        return _run_sync(self._require_rest().update_connection(connection_id, req))

    def delete_connection(self, connection_id: str) -> None:
        _run_sync(self._require_rest().delete_connection(connection_id))

    # -- Connections (async) --

    async def list_connections_async(self) -> list[DataConnection]:
        return await self._require_rest().list_connections()

    async def get_connection_async(self, connection_id: str) -> DataConnection:
        return await self._require_rest().get_connection(connection_id)

    async def create_connection_async(
        self,
        *,
        name: str,
        namespace: str,
        provider: str,
        format: str,
        location_url: str,
        properties: dict[str, str] | None = None,
    ) -> DataConnection:
        req = CreateConnectionRequest(
            name=name,
            namespace=namespace,
            provider=provider,
            format=format,
            location=DataLocation(url=location_url),
            properties=properties or {},
        )
        return await self._require_rest().create_connection(req)

    async def update_connection_async(
        self,
        connection_id: str,
        *,
        name: str | None = None,
        namespace: str | None = None,
        provider: str | None = None,
        format: str | None = None,
        location_url: str | None = None,
        properties: dict[str, str] | None = None,
    ) -> DataConnection:
        if all(v is None for v in (name, namespace, provider, format, location_url, properties)):
            raise DCHConfigError("at least one field must be provided for update")
        location = DataLocation(url=location_url) if location_url else None
        req = UpdateConnectionRequest(
            name=name,
            namespace=namespace,
            provider=provider,
            format=format,
            location=location,
            properties=properties,
        )
        return await self._require_rest().update_connection(connection_id, req)

    async def delete_connection_async(self, connection_id: str) -> None:
        await self._require_rest().delete_connection(connection_id)

    # -- Connection Types (sync) --

    def list_connection_types(self) -> list[ConnectionType]:
        return _run_sync(self._require_rest().list_connection_types())

    def get_connection_type(self, type_id: str) -> ConnectionType:
        return _run_sync(self._require_rest().get_connection_type(type_id))

    def create_connection_type(
        self,
        *,
        name: str,
        description: str = "",
        properties_schema: dict[str, Any] | None = None,
    ) -> ConnectionType:
        req = CreateConnectionTypeRequest(
            name=name,
            description=description,
            properties_schema=properties_schema or {},
        )
        return _run_sync(self._require_rest().create_connection_type(req))

    def update_connection_type(
        self,
        type_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        properties_schema: dict[str, Any] | None = None,
    ) -> ConnectionType:
        if all(v is None for v in (name, description, properties_schema)):
            raise DCHConfigError("at least one field must be provided for update")
        req = UpdateConnectionTypeRequest(
            name=name,
            description=description,
            properties_schema=properties_schema,
        )
        return _run_sync(self._require_rest().update_connection_type(type_id, req))

    def delete_connection_type(self, type_id: str) -> None:
        _run_sync(self._require_rest().delete_connection_type(type_id))

    # -- Connection Types (async) --

    async def list_connection_types_async(self) -> list[ConnectionType]:
        return await self._require_rest().list_connection_types()

    async def get_connection_type_async(self, type_id: str) -> ConnectionType:
        return await self._require_rest().get_connection_type(type_id)

    async def create_connection_type_async(
        self,
        *,
        name: str,
        description: str = "",
        properties_schema: dict[str, Any] | None = None,
    ) -> ConnectionType:
        req = CreateConnectionTypeRequest(
            name=name,
            description=description,
            properties_schema=properties_schema or {},
        )
        return await self._require_rest().create_connection_type(req)

    async def update_connection_type_async(
        self,
        type_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        properties_schema: dict[str, Any] | None = None,
    ) -> ConnectionType:
        if all(v is None for v in (name, description, properties_schema)):
            raise DCHConfigError("at least one field must be provided for update")
        req = UpdateConnectionTypeRequest(
            name=name,
            description=description,
            properties_schema=properties_schema,
        )
        return await self._require_rest().update_connection_type(type_id, req)

    async def delete_connection_type_async(self, type_id: str) -> None:
        await self._require_rest().delete_connection_type(type_id)

    # -- Flight SQL queries --

    def query(self, sql: str, connection_id: str) -> pa.Table:
        return self._require_flight().query(sql, connection_id)

    async def query_async(self, sql: str, connection_id: str) -> pa.Table:
        flight = self._require_flight()
        return await asyncio.to_thread(flight.query, sql, connection_id)

    def query_batches(self, sql: str, connection_id: str) -> Generator[pa.RecordBatch, None, None]:
        return self._require_flight().query_batches(sql, connection_id)

    # -- Unstructured ingestion --

    def ingest(self, connection_id: str) -> bytes:
        return _run_sync(self._require_rest().ingest(connection_id))

    async def ingest_async(self, connection_id: str) -> bytes:
        return await self._require_rest().ingest(connection_id)
