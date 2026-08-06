"""Unified DataConnectClient for REST and Flight SQL access."""

from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING, Any

from .exceptions import DCHConfigError
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

if TYPE_CHECKING:
    import pyarrow as pa

    from .flight import FlightClient


class DataConnectClient:
    """Single entry point for all DCH interactions.

    Parameters
    ----------
    rest_url : str, optional
        Base URL of the DCH REST service.
    flight_url : str, optional
        gRPC endpoint of the DCH Flight SQL service.
    token : str
        Raw Bearer token value (without the "Bearer " prefix).
    tenant_id : str
        Tenant identifier sent via ``x-tenant-id`` header.
    api_base : str
        API path prefix (default ``/api/v1/data``).
    timeout : float
        HTTP request timeout in seconds.
    max_retries : int
        Maximum retry attempts for transient errors (default 3, 0 to disable).
    backoff_base : float
        Base delay in seconds for exponential backoff (default 0.5).
    backoff_max : float
        Maximum backoff delay in seconds (default 30.0).
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
        max_retries: int = 3,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
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
                max_retries=max_retries,
                backoff_base=backoff_base,
                backoff_max=backoff_max,
            )

        if flight_url:
            from .flight import FlightClient as _FlightClient

            self._flight = _FlightClient(
                flight_url=flight_url,
                token=token,
                tenant_id=tenant_id,
            )

    # -- context manager --

    def __enter__(self) -> DataConnectClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close underlying clients."""
        if self._rest:
            self._rest.close()
        if self._flight:
            self._flight.close()

    # -- guards --

    def _require_rest(self) -> RestClient:
        if self._rest is None:
            raise DCHConfigError("rest_url is required for this operation")
        return self._rest

    def _require_flight(self) -> FlightClient:
        if self._flight is None:
            raise DCHConfigError("flight_url is required for this operation")
        return self._flight

    # -- Connections --

    def list_connections(self) -> list[DataConnection]:
        return self._require_rest().list_connections()

    def get_connection(self, connection_id: str) -> DataConnection:
        return self._require_rest().get_connection(connection_id)

    def create_connection(
        self,
        *,
        name: str,
        namespace: str,
        provider: str,
        data_format: str,
        location_url: str,
        properties: dict[str, str] | None = None,
    ) -> DataConnection:
        req = CreateConnectionRequest(
            name=name,
            namespace=namespace,
            provider=provider,
            format=data_format,
            location=DataLocation(url=location_url),
            properties=properties or {},
        )
        return self._require_rest().create_connection(req)

    def update_connection(
        self,
        connection_id: str,
        *,
        name: str | None = None,
        namespace: str | None = None,
        provider: str | None = None,
        data_format: str | None = None,
        location_url: str | None = None,
        properties: dict[str, str] | None = None,
    ) -> DataConnection:
        if all(v is None for v in (name, namespace, provider, data_format, location_url, properties)):
            raise DCHConfigError("at least one field must be provided for update")
        location = DataLocation(url=location_url) if location_url is not None else None
        req = UpdateConnectionRequest(
            name=name,
            namespace=namespace,
            provider=provider,
            format=data_format,
            location=location,
            properties=properties,
        )
        return self._require_rest().update_connection(connection_id, req)

    def delete_connection(self, connection_id: str) -> None:
        self._require_rest().delete_connection(connection_id)

    # -- Connection Types --

    def list_connection_types(self) -> list[ConnectionType]:
        return self._require_rest().list_connection_types()

    def get_connection_type(self, type_id: str) -> ConnectionType:
        return self._require_rest().get_connection_type(type_id)

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
        return self._require_rest().create_connection_type(req)

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
        return self._require_rest().update_connection_type(type_id, req)

    def delete_connection_type(self, type_id: str) -> None:
        self._require_rest().delete_connection_type(type_id)

    # -- Unstructured data access --

    def read_bytes(self, connection_id: str) -> bytes:
        return self._require_rest().read_bytes(connection_id)

    def read_pandas(self, connection_id: str) -> Any:
        """Fetch data for a connection and return it as a pandas DataFrame.

        Requires the ``pandas`` optional dependency::

            pip install data-connect-hub[pandas]
        """
        import io

        try:
            import pandas as pd  # type: ignore[import-untyped]
        except ImportError:
            raise DCHConfigError(
                "pandas is required for read_pandas — install with: pip install data-connect-hub[pandas]"
            ) from None
        raw = self.read_bytes(connection_id)
        return pd.read_json(io.BytesIO(raw))

    # -- Flight SQL queries --

    def query(self, sql: str, connection_id: str) -> pa.Table:
        """Execute *sql* via Flight SQL and return the full result as a PyArrow Table."""
        return self._require_flight().query(sql, connection_id)

    def query_batches(self, sql: str, connection_id: str) -> Generator[pa.RecordBatch, None, None]:
        """Execute *sql* via Flight SQL and yield record batches."""
        yield from self._require_flight().query_batches(sql, connection_id)

    def server_info(self) -> dict[str, Any]:
        """Return Flight SQL server metadata."""
        return self._require_flight().server_info()
