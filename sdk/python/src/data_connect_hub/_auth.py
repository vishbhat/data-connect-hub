"""Header construction for REST and Flight SQL authentication."""

from __future__ import annotations

_FLIGHT_HEADER_PREFIX = "adbc.flight.sql.rpc.call_header"


def _normalize_token(token: str) -> str:
    if token and not token.startswith("Bearer "):
        return f"Bearer {token}"
    return token


def build_headers(
    *,
    token: str,
    tenant_id: str,
    connection_id: str | None = None,
) -> dict[str, str]:
    """Build HTTP headers for REST API calls."""
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = _normalize_token(token)
    if tenant_id:
        headers["x-tenant-id"] = tenant_id
    if connection_id:
        headers["x-dch-connection-id"] = connection_id
    return headers


def build_flight_headers(
    *,
    token: str,
    tenant_id: str,
    connection_id: str,
) -> dict[str, str]:
    """Build ADBC db_kwargs with Flight SQL call headers."""
    headers: dict[str, str] = {}
    if token:
        headers[f"{_FLIGHT_HEADER_PREFIX}.authorization"] = _normalize_token(token)
    if tenant_id:
        headers[f"{_FLIGHT_HEADER_PREFIX}.x-tenant-id"] = tenant_id
    if connection_id:
        headers[f"{_FLIGHT_HEADER_PREFIX}.x-dch-connection-id"] = connection_id
    return headers
