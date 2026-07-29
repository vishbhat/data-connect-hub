# Data Connect Hub Python SDK

Python client library for the [Data Connect Hub](https://github.com/opendatahub-io/data-connect-hub) service.

## Installation

```bash
pip install data-connect-hub
```

For development:

```bash
pip install -e ".[dev]"
```

## Quick Start

```python
from data_connect_hub import DataConnectClient

client = DataConnectClient(
    rest_url="https://dch.example.com",
    flight_url="grpc://dch.example.com:50051",
    token="Bearer <your-token>",
    tenant_id="my-tenant",
)

# List connections (REST)
connections = client.list_connections()

# Query tabular data (Flight SQL)
table = client.query("SELECT * FROM prompts", connection_id="conn-id")
df = table.to_pandas()

# Stream large results
for batch in client.query_batches("SELECT * FROM large_table", connection_id="conn-id"):
    process(batch)
```

## Async Usage

```python
async with DataConnectClient(
    rest_url="https://dch.example.com",
    flight_url="grpc://dch.example.com:50051",
    token="Bearer <your-token>",
    tenant_id="my-tenant",
) as client:
    connections = await client.list_connections_async()
    table = await client.query_async("SELECT * FROM prompts", connection_id="conn-id")
```

## API Reference

### Connection Management (REST)

```python
client.list_connections() -> list[DataConnection]
client.get_connection(connection_id) -> DataConnection
client.create_connection(name=..., namespace=..., provider=..., format=..., location_url=...) -> DataConnection
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

### Tabular Data (Flight SQL)

```python
client.query(sql, connection_id) -> pyarrow.Table
client.query_batches(sql, connection_id) -> Generator[pyarrow.RecordBatch]
```

### Unstructured Data Ingestion (REST)

```python
client.ingest(connection_id) -> bytes
```

## Development

```bash
make sdk-install     # install in editable mode with dev deps
make sdk-test        # run tests with coverage
make sdk-lint        # ruff check + format check
make sdk-fmt         # auto-format
make sdk-typecheck   # run mypy strict type checking
make sdk-all         # lint + typecheck + test
```

## Requirements

- Python 3.10+
- Dependencies: httpx, pydantic, pyarrow, adbc-driver-flightsql
