"""Flight SQL client for tabular data queries via ADBC."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import adbc_driver_flightsql.dbapi as flightsql_dbapi
import pyarrow as pa

from ._auth import build_flight_headers
from .exceptions import DCHConnectionError, DCHQueryError


class FlightClient:
    """ADBC-based Flight SQL client for DCH tabular data access."""

    def __init__(
        self,
        flight_url: str,
        token: str,
        tenant_id: str,
    ) -> None:
        self._flight_url = flight_url
        self._token = token
        self._tenant_id = tenant_id

    def _connect(self, connection_id: str) -> flightsql_dbapi.Connection:
        headers = build_flight_headers(
            token=self._token,
            tenant_id=self._tenant_id,
            connection_id=connection_id,
        )
        try:
            return flightsql_dbapi.connect(self._flight_url, db_kwargs=headers)
        except Exception as exc:
            raise DCHConnectionError(f"Failed to connect to Flight SQL at {self._flight_url}: {exc}") from exc

    def query(self, sql: str, connection_id: str) -> pa.Table:
        """Execute SQL and return the full result as a PyArrow Table."""
        conn = self._connect(connection_id)
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(sql)
                return cursor.fetch_arrow_table()
            except DCHConnectionError:
                raise
            except Exception as exc:
                raise DCHQueryError(f"Query failed: {exc}") from exc
            finally:
                cursor.close()
        finally:
            conn.close()

    def query_batches(self, sql: str, connection_id: str) -> Generator[pa.RecordBatch, None, None]:
        """Execute SQL and yield RecordBatches for streaming large results."""
        conn = self._connect(connection_id)
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(sql)
                reader = cursor.fetch_record_batch()
                while True:
                    try:
                        batch = reader.read_next_batch()
                        yield batch
                    except StopIteration:
                        break
            except (DCHConnectionError, DCHQueryError):
                raise
            except Exception as exc:
                raise DCHQueryError(f"Query failed: {exc}") from exc
            finally:
                cursor.close()
        finally:
            conn.close()

    def server_info(self, connection_id: str) -> dict[str | int, Any]:
        """Retrieve Flight SQL server metadata via GetSqlInfo."""
        conn = self._connect(connection_id)
        try:
            return conn.adbc_get_info()
        finally:
            conn.close()
