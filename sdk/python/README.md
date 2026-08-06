# Data Connect Hub Python SDK

Python client library for the [Data Connect Hub](https://github.com/opendatahub-io/data-connect-hub) service.

## Installation

> **Note:** This package is not yet published to PyPI. Install from source for now.

```bash
# Full install (REST + Flight SQL)
pip install -e "sdk/python[dev]"

# REST connection management only
pip install "sdk/python[connections]"

# Flight SQL data querying only
pip install "sdk/python[ingestion]"
```

## Quick Start

```python
from data_connect_hub import DataConnectClient

client = DataConnectClient(
    rest_url="https://dch.example.com",
    flight_url="grpc://dch.example.com:50051",
    token="<your-token>",  # raw token value, "Bearer" prefix added automatically
    tenant_id="my-tenant",
)

# List connections (REST)
connections = client.list_connections()

# Query data via Flight SQL
table = client.query("SELECT * FROM prompts", connection_id="conn-uuid")
df = table.to_pandas()
```

## API Reference

### Connection Management (REST)

```python
client.list_connections() -> list[DataConnection]
client.get_connection(connection_id) -> DataConnection
client.create_connection(name=..., namespace=..., provider=..., data_format=..., location_url=...) -> DataConnection
client.update_connection(connection_id, name=...) -> DataConnection
client.delete_connection(connection_id) -> None
```

### Connection Types (REST)

```python
client.list_connection_types() -> list[ConnectionType]
client.get_connection_type(type_id) -> ConnectionType
client.create_connection_type(name=..., description=...) -> ConnectionType
client.update_connection_type(type_id, name=...) -> ConnectionType
client.delete_connection_type(type_id) -> None
```

### Unstructured Data Access (REST)

```python
client.read_bytes(connection_id) -> bytes
client.read_pandas(connection_id) -> pd.DataFrame  # requires: pip install data-connect-hub[pandas]
```

### Tabular Data Queries (Flight SQL)

```python
client.query(sql, connection_id) -> pyarrow.Table                   # full materialization
client.query_batches(sql, connection_id) -> Generator[RecordBatch]  # sync, streaming
client.server_info() -> dict                                   # sync, server metadata
```

## Development

A virtual environment at `sdk/python/.venv` is created automatically on first run.
If `VIRTUAL_ENV` is already set (e.g. a manually activated venv), the Makefile uses the system Python directly.

```bash
make sdk-install     # install in editable mode with dev deps
make sdk-test        # run tests with coverage
make sdk-lint        # ruff check + format check
make sdk-fmt         # auto-format
make sdk-typecheck   # run mypy strict type checking
make sdk-all         # lint + typecheck + test
```

## Requirements

- Python 3.11+
- Dependencies: httpx, pydantic, adbc-driver-flightsql (includes pyarrow)
