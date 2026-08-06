"""Tests for the FlightClient wrapper."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pytest

from data_connect_hub.exceptions import DCHConnectionError, DCHQueryError
from data_connect_hub.flight import FlightClient


class _OperationalError(Exception):
    pass


class _InterfaceError(Exception):
    pass


@pytest.fixture()
def flight_client() -> FlightClient:
    return FlightClient(flight_url="grpc://localhost:50051", token="tok", tenant_id="t1")


def _mock_cursor(table: pa.Table) -> MagicMock:
    cursor = MagicMock()
    cursor.fetch_arrow_table.return_value = table
    return cursor


class TestQuery:
    @patch("data_connect_hub.flight.flight_dbapi")
    def test_returns_table(self, mock_dbapi: MagicMock, flight_client: FlightClient) -> None:
        table = pa.table({"col": [1, 2, 3]})
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = _mock_cursor(table)
        mock_dbapi.connect.return_value = mock_conn

        result = flight_client.query("SELECT 1", "conn-1")
        assert result.equals(table)
        mock_conn.close.assert_called_once()

    @patch("data_connect_hub.flight.flight_dbapi")
    def test_empty_result_returns_empty_table(self, mock_dbapi: MagicMock, flight_client: FlightClient) -> None:
        empty = pa.table({"col": pa.array([], type=pa.int64())})
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = _mock_cursor(empty)
        mock_dbapi.connect.return_value = mock_conn

        result = flight_client.query("SELECT 1", "conn-1")
        assert result.num_rows == 0

    @patch("data_connect_hub.flight.flight_dbapi")
    def test_operational_error_mapped(self, mock_dbapi: MagicMock, flight_client: FlightClient) -> None:
        mock_dbapi.OperationalError = _OperationalError
        mock_conn = MagicMock()
        cursor = MagicMock()
        cursor.execute.side_effect = _OperationalError("bad sql")
        mock_conn.cursor.return_value = cursor
        mock_dbapi.connect.return_value = mock_conn

        with pytest.raises(DCHQueryError, match="bad sql"):
            flight_client.query("BAD SQL", "conn-1")


class TestQueryBatches:
    @patch("data_connect_hub.flight.flight_dbapi")
    def test_yields_batches(self, mock_dbapi: MagicMock, flight_client: FlightClient) -> None:
        batch = pa.record_batch({"col": [1, 2]})
        reader = MagicMock()
        reader.read_next_batch.side_effect = [batch, StopIteration()]

        mock_conn = MagicMock()
        cursor = MagicMock()
        cursor.fetch_record_batch.return_value = reader
        mock_conn.cursor.return_value = cursor
        mock_dbapi.connect.return_value = mock_conn

        batches = list(flight_client.query_batches("SELECT 1", "conn-1"))
        assert len(batches) == 1
        assert batches[0].equals(batch)


class TestServerInfo:
    @patch("data_connect_hub.flight.flight_dbapi")
    def test_returns_dict(self, mock_dbapi: MagicMock, flight_client: FlightClient) -> None:
        info: dict[str, Any] = {"vendor": "DCH", "version": "1.0"}
        mock_conn = MagicMock()
        mock_conn.adbc_get_info.return_value = info
        mock_dbapi.connect.return_value = mock_conn

        result = flight_client.server_info()
        assert result == info
        mock_conn.close.assert_called_once()


class TestConnectionError:
    @patch("data_connect_hub.flight.flight_dbapi")
    def test_interface_error_mapped(self, mock_dbapi: MagicMock, flight_client: FlightClient) -> None:
        mock_dbapi.InterfaceError = _InterfaceError
        mock_dbapi.connect.side_effect = _InterfaceError("unreachable")

        with pytest.raises(DCHConnectionError, match="unreachable"):
            flight_client.query("SELECT 1", "conn-1")


class TestHeaders:
    @patch("data_connect_hub.flight.flight_dbapi")
    def test_connection_id_injected(self, mock_dbapi: MagicMock, flight_client: FlightClient) -> None:
        table = pa.table({"col": [1]})
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = _mock_cursor(table)
        mock_dbapi.connect.return_value = mock_conn

        flight_client.query("SELECT 1", "my-conn")

        call_kwargs = mock_dbapi.connect.call_args
        db_kwargs = call_kwargs.kwargs.get("db_kwargs", call_kwargs[1].get("db_kwargs", {}))
        assert db_kwargs["adbc.flight.sql.rpc.call_header.x-data-connection-id"] == "my-conn"
        assert db_kwargs["adbc.flight.sql.rpc.call_header.authorization"] == "Bearer tok"
        assert db_kwargs["adbc.flight.sql.rpc.call_header.x-tenant-id"] == "t1"
