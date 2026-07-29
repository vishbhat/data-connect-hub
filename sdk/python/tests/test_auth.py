"""Tests for header construction utilities."""

from __future__ import annotations

from data_connect_hub._auth import (
    _normalize_token,
    build_flight_headers,
    build_headers,
)


class TestNormalizeToken:
    def test_adds_bearer_prefix(self) -> None:
        assert _normalize_token("abc123") == "Bearer abc123"

    def test_preserves_existing_prefix(self) -> None:
        assert _normalize_token("Bearer abc123") == "Bearer abc123"

    def test_empty_token(self) -> None:
        assert _normalize_token("") == ""


class TestBuildHeaders:
    def test_all_headers(self) -> None:
        headers = build_headers(
            token="abc123",
            tenant_id="tenant-1",
            connection_id="conn-1",
        )
        assert headers == {
            "Authorization": "Bearer abc123",
            "x-tenant-id": "tenant-1",
            "x-dch-connection-id": "conn-1",
        }

    def test_without_connection_id(self) -> None:
        headers = build_headers(token="abc", tenant_id="t1")
        assert "x-dch-connection-id" not in headers
        assert headers["Authorization"] == "Bearer abc"
        assert headers["x-tenant-id"] == "t1"

    def test_empty_values_excluded(self) -> None:
        headers = build_headers(token="", tenant_id="", connection_id=None)
        assert headers == {}


class TestBuildFlightHeaders:
    def test_all_headers(self) -> None:
        headers = build_flight_headers(
            token="abc123",
            tenant_id="tenant-1",
            connection_id="conn-1",
        )
        prefix = "adbc.flight.sql.rpc.call_header"
        assert headers == {
            f"{prefix}.authorization": "Bearer abc123",
            f"{prefix}.x-tenant-id": "tenant-1",
            f"{prefix}.x-dch-connection-id": "conn-1",
        }

    def test_bearer_prefix_preserved(self) -> None:
        headers = build_flight_headers(
            token="Bearer existing",
            tenant_id="t",
            connection_id="c",
        )
        assert headers["adbc.flight.sql.rpc.call_header.authorization"] == "Bearer existing"
