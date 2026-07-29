"""Tests for the Flight SQL client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pyarrow as pa
import pytest

from data_connect_hub.exceptions import DCHConnectionError, DCHQueryError
from data_connect_hub.flight import FlightClient


@pytest.fixture()
def flight_client() -> FlightClient:
    return FlightClient(
        flight_url="grpc://localhost:50051",
        token="test-token",
        tenant_id="test-tenant",
    )


def _mock_cursor(table: pa.Table) -> MagicMock:
    cursor = MagicMock()
    cursor.fetch_arrow_table.return_value = table
    return cursor


def _mock_connection(cursor: MagicMock) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn


class TestQuery:
    @patch("data_connect_hub.flight.flightsql_dbapi.connect")
    def test_returns_table(self, mock_connect: MagicMock, flight_client: FlightClient) -> None:
        expected = pa.table({"col1": [1, 2, 3], "col2": ["a", "b", "c"]})
        cursor = _mock_cursor(expected)
        mock_connect.return_value = _mock_connection(cursor)

        result = flight_client.query("SELECT * FROM test", connection_id="conn-1")

        assert result.equals(expected)
        cursor.execute.assert_called_once_with("SELECT * FROM test")
        cursor.close.assert_called_once()
        mock_connect.return_value.close.assert_called_once()

    @patch("data_connect_hub.flight.flightsql_dbapi.connect")
    def test_passes_correct_headers(self, mock_connect: MagicMock, flight_client: FlightClient) -> None:
        cursor = _mock_cursor(pa.table({"x": [1]}))
        mock_connect.return_value = _mock_connection(cursor)

        flight_client.query("SELECT 1", connection_id="my-conn")

        call_kwargs = mock_connect.call_args
        db_kwargs = call_kwargs.kwargs.get("db_kwargs") or call_kwargs[1].get("db_kwargs")
        prefix = "adbc.flight.sql.rpc.call_header"
        assert db_kwargs[f"{prefix}.authorization"] == "Bearer test-token"
        assert db_kwargs[f"{prefix}.x-tenant-id"] == "test-tenant"
        assert db_kwargs[f"{prefix}.x-dch-connection-id"] == "my-conn"

    @patch("data_connect_hub.flight.flightsql_dbapi.connect")
    def test_connection_failure(self, mock_connect: MagicMock, flight_client: FlightClient) -> None:
        mock_connect.side_effect = RuntimeError("connection refused")

        with pytest.raises(DCHConnectionError, match="connection refused"):
            flight_client.query("SELECT 1", connection_id="conn-1")

    @patch("data_connect_hub.flight.flightsql_dbapi.connect")
    def test_query_failure(self, mock_connect: MagicMock, flight_client: FlightClient) -> None:
        cursor = MagicMock()
        cursor.execute.side_effect = RuntimeError("syntax error")
        mock_connect.return_value = _mock_connection(cursor)

        with pytest.raises(DCHQueryError, match="syntax error"):
            flight_client.query("INVALID SQL", connection_id="conn-1")

        cursor.close.assert_called_once()


class TestQueryBatches:
    @patch("data_connect_hub.flight.flightsql_dbapi.connect")
    def test_yields_batches(self, mock_connect: MagicMock, flight_client: FlightClient) -> None:
        batch1 = pa.record_batch({"col": [1, 2]})
        batch2 = pa.record_batch({"col": [3, 4]})

        reader = MagicMock()
        reader.read_next_batch.side_effect = [batch1, batch2, StopIteration()]

        cursor = MagicMock()
        cursor.fetch_record_batch.return_value = reader
        mock_connect.return_value = _mock_connection(cursor)

        batches = list(flight_client.query_batches("SELECT * FROM test", connection_id="conn-1"))

        assert len(batches) == 2
        assert batches[0].equals(batch1)
        assert batches[1].equals(batch2)
        cursor.close.assert_called_once()


class TestServerInfo:
    @patch("data_connect_hub.flight.flightsql_dbapi.connect")
    def test_returns_info(self, mock_connect: MagicMock, flight_client: FlightClient) -> None:
        expected_info = {"server_name": "Data Connect Hub", "version": "0.1.0"}
        conn = MagicMock()
        conn.adbc_get_info.return_value = expected_info
        mock_connect.return_value = conn

        result = flight_client.server_info(connection_id="conn-1")

        assert result == expected_info
        conn.close.assert_called_once()
