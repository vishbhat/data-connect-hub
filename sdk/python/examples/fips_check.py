"""Check that the SDK's REST and Flight TLS run in the host's FIPS-validated OpenSSL.

Run it in an RHOAI workbench (or another RHOAI/AIPCC Python 3.12 image) on a FIPS
cluster, after installing the SDK from the Red Hat Python index:

    pip install "data-connect-hub[flight]"
    python fips_check.py

The script:

1. checks the host and the installed libraries:
   - the kernel is in FIPS mode,
   - the OpenSSL that Python uses is restricted to the FIPS provider,
   - pyarrow's Flight library links the system OpenSSL instead of a bundled copy;
2. creates a Postgres data connection through the REST API and runs its
   readiness check;
3. runs Flight calls on it: server_info, get_tables, read, read_batches,
   read_pandas;
4. deletes the connection.

It exits with status 1 if any check or call fails.

Environment variables:
    DCH_HOST              Gateway host[:port] serving REST and Flight (required)
    DCH_FLIGHT_HOST       Separate Flight host[:port], when REST and Flight
                          aren't behind one gateway (optional)
    DCH_TENANT_ID         Tenant namespace (required)
    DCH_TOKEN             Bearer token; or DCH_TOKEN_FILE with a path to one
    DCH_CA_CERT           CA bundle that signed the server certificate
    DCH_INSECURE          "true" skips certificate verification (not valid for
                          a FIPS claim; for debugging only)
    DCH_SECRET_NAME       Secret in the tenant namespace with a Postgres "URI"
                          key, used to create the connection
    DCH_CONNECTION_TYPE_ID  Connection type to use (default: the type named
                          "Postgres")
    DCH_CONNECTION_ID     Use this existing connection instead of creating one
    DCH_KEEP_CONNECTION   "true" keeps the created connection
"""

from __future__ import annotations

import ctypes
import glob
import hashlib
import os
import ssl
import subprocess
import sys
import uuid
from collections.abc import Callable
from typing import Any

from data_connect_hub import CredentialsRef, DataConnectClient

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> bool:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name:<34} {detail}")
    return ok


def step(name: str, fn: Callable[[], str]) -> bool:
    try:
        return record(name, True, fn())
    except Exception as exc:  # report every failure and keep going
        return record(name, False, f"{type(exc).__name__}: {str(exc)[:300]}")


def env_flag(name: str) -> bool:
    return os.getenv(name, "").lower() in ("1", "true", "yes")


def elf_needed(path: str) -> list[str]:
    out = subprocess.run(["readelf", "-d", path], capture_output=True, text=True, check=True).stdout
    return [line.split("[", 1)[1].rstrip("]") for line in out.splitlines() if "(NEEDED)" in line]


# --- 1. Host and library checks -------------------------------------------------------


def check_host_fips() -> str:
    with open("/proc/sys/crypto/fips_enabled") as f:
        value = f.read().strip()
    if value != "1":
        raise RuntimeError(f"/proc/sys/crypto/fips_enabled={value}")
    return "kernel fips_enabled=1"


def check_python_openssl() -> str:
    # Python's ssl module (used by httpx for REST) and the system libcrypto must be
    # the same OpenSSL, with lookups restricted to the FIPS provider.
    import _ssl

    needed = elf_needed(_ssl.__file__)
    if "libssl.so.3" not in needed:
        raise RuntimeError(f"Python ssl does not link the system libssl.so.3: {needed}")
    libcrypto = ctypes.CDLL("libcrypto.so.3")
    libcrypto.EVP_default_properties_is_fips_enabled.argtypes = [ctypes.c_void_p]
    if libcrypto.EVP_default_properties_is_fips_enabled(None) != 1:
        raise RuntimeError("OpenSSL default properties are not restricted to FIPS")
    try:
        hashlib.md5()
    except ValueError:
        pass
    else:
        raise RuntimeError("hashlib.md5() is allowed, so the FIPS provider is not enforcing")
    return f"{ssl.OPENSSL_VERSION}, FIPS properties on, MD5 blocked"


def check_pyarrow_flight() -> str:
    # Importing pyarrow.flight fails here if Red Hat pyarrow's system libraries are missing.
    import pyarrow
    import pyarrow.flight

    libs = glob.glob(os.path.join(os.path.dirname(pyarrow.__file__), "libarrow_flight.so*"))
    if not libs:
        raise RuntimeError("libarrow_flight.so not found in the pyarrow package")
    needed = elf_needed(libs[0])
    if "libssl.so.3" not in needed or "libcrypto.so.3" not in needed:
        raise RuntimeError(
            f"pyarrow {pyarrow.__version__} bundles its own TLS library "
            "(a PyPI build); install pyarrow from the Red Hat index"
        )
    return f"pyarrow {pyarrow.__version__}: {os.path.basename(libs[0])} links system libssl.so.3/libcrypto.so.3"


# --- 2-4. SDK calls ---------------------------------------------------------------------


def make_client(endpoint: str) -> DataConnectClient:
    token = os.getenv("DCH_TOKEN", "")
    token_file = os.getenv("DCH_TOKEN_FILE", "")
    if not token and token_file:
        with open(token_file) as f:
            token = f.read().strip()
    if not token:
        sys.exit("Set DCH_TOKEN or DCH_TOKEN_FILE.")
    return DataConnectClient(
        endpoint,
        token=token,
        tenant_id=os.environ["DCH_TENANT_ID"],
        ca_cert=os.getenv("DCH_CA_CERT") or None,
        insecure=env_flag("DCH_INSECURE"),
    )


def pick_connection_type(rest: DataConnectClient) -> str:
    wanted = os.getenv("DCH_CONNECTION_TYPE_ID")
    if wanted:
        return rest.get_connection_type(wanted).id
    types = [t for t in rest.list_connection_types() if t.provider == "postgres"]
    for t in types:
        if t.name == "Postgres":
            return t.id
    if not types:
        raise RuntimeError("no connection type with provider 'postgres'; set DCH_CONNECTION_TYPE_ID")
    return types[0].id


def check_ready(rest: DataConnectClient, connection_id: str) -> str:
    # New connections start as not_ready; the readiness check has the Flight
    # service verify the secret and the data source and updates the state.
    rest.check_connection_readiness(connection_id)
    status = rest.get_connection(connection_id).status
    detail = f"state={status.state}" + (f" ({status.message})" if status.message else "")
    if status.state != "ready":
        raise RuntimeError(detail)
    return detail


def run_flight(flight: DataConnectClient, connection_id: str) -> None:
    def server_info() -> str:
        info = flight.server_info()
        return f"{info['vendor_name']} {info['vendor_version']}, driver={info['driver_name']!r}"

    step("Flight server_info", server_info)

    tables: dict[str, Any] = {}

    def get_tables() -> str:
        t = flight.get_tables(connection_id)
        rows = t.to_pylist()
        if rows:
            tables["first"] = rows[0]
        return f"{t.num_rows} table(s): {[r['table_name'] for r in rows][:5]}"

    step("Flight get_tables", get_tables)
    step("Flight read SELECT 1", lambda: f"result={flight.read('SELECT 1 AS one', connection_id).to_pydict()}")

    first = tables.get("first")
    if first:
        name = ".".join(p for p in (first.get("db_schema_name"), first["table_name"]) if p)
        sql = f"SELECT * FROM {name} LIMIT 100"

        def read_table() -> str:
            t = flight.read(sql, connection_id)
            return f"{sql!r} -> {t.num_rows} rows x {t.num_columns} cols"

        def read_batches() -> str:
            batches = list(flight.read_batches(sql, connection_id))
            return f"{len(batches)} batch(es), {sum(b.num_rows for b in batches)} rows"

        step("Flight read table", read_table)
        step("Flight read_batches", read_batches)
        step("Flight read_pandas", lambda: f"DataFrame shape={flight.read_pandas(sql, connection_id).shape}")


def main() -> int:
    host = os.getenv("DCH_HOST")
    if not host or not os.getenv("DCH_TENANT_ID"):
        sys.exit("Set DCH_HOST and DCH_TENANT_ID.")
    if env_flag("DCH_INSECURE"):
        print("WARNING: DCH_INSECURE skips certificate verification; don't use it for a FIPS claim.\n")

    import data_connect_hub

    print(f"data-connect-hub {data_connect_hub.__version__}, Python {sys.version.split()[0]}\n")
    step("Host in FIPS mode", check_host_fips)
    step("REST TLS: Python ssl uses FIPS", check_python_openssl)
    step("Flight TLS: pyarrow links system", check_pyarrow_flight)

    rest = make_client(host)
    flight = make_client(os.getenv("DCH_FLIGHT_HOST") or host)
    connection_id = os.getenv("DCH_CONNECTION_ID", "")
    created = False
    try:
        if not connection_id:
            secret = os.getenv("DCH_SECRET_NAME")
            if not secret:
                sys.exit("Set DCH_SECRET_NAME (or DCH_CONNECTION_ID to use an existing connection).")

            def create() -> str:
                nonlocal connection_id, created
                conn = rest.create_connection(
                    name=f"fips-check-{uuid.uuid4().hex[:8]}",
                    connection_type_id=pick_connection_type(rest),
                    data_format="tabular",
                    credentials_ref=CredentialsRef(secret=secret),
                )
                connection_id, created = conn.id, True
                return f"{conn.name} ({conn.id}), secret={secret}"

            if not step("REST create_connection", create):
                return report()
            step("REST check_connection_readiness", lambda: check_ready(rest, connection_id))
        else:
            step("REST get_connection", lambda: f"{rest.get_connection(connection_id).name} ({connection_id})")

        run_flight(flight, connection_id)
    finally:
        if created and not env_flag("DCH_KEEP_CONNECTION"):

            def delete() -> str:
                rest.delete_connection(connection_id)
                return f"deleted {connection_id}"

            step("REST delete_connection", delete)
        rest.close()
        flight.close()
    return report()


def report() -> int:
    failed = [name for name, ok, _ in results if not ok]
    print(
        f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; failed: {', '.join(failed)}" if failed else "")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
