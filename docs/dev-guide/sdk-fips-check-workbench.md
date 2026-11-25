# Dev guide: checking Python SDK FIPS compliance from an RHOAI workbench

This guide shows how to check that the Data Connect Hub Python SDK uses
FIPS-validated cryptography on a FIPS cluster. You start a pod from a Red Hat
OpenShift AI (RHOAI) workbench image, install the SDK, and run one script,
[`sdk/python/examples/fips_check.py`](../../sdk/python/examples/fips_check.py).
The script creates a data connection and runs Flight queries on it.

The SDK implements no cryptography. It makes two kinds of TLS connection:

| Traffic | Library | FIPS when |
|---|---|---|
| REST (connections, connection types) | `httpx` → Python `ssl` | Python links the system OpenSSL and the host is in FIPS mode. True for RHEL-based images |
| Flight (queries, tables, server info) | `pyarrow.flight` (gRPC) | `pyarrow` comes from the Red Hat Python index, which links the system OpenSSL. PyPI `pyarrow` bundles its own OpenSSL and isn't FIPS |

So the SDK is FIPS-compliant only on this install path: an RHOAI workbench or
AIPCC Python 3.12 image, with `pyarrow` from the Red Hat index, on a FIPS
cluster.

## What the script checks

| Check | Passes when |
|---|---|
| Host in FIPS mode | `/proc/sys/crypto/fips_enabled` is `1` |
| REST TLS | Python's `ssl` links the system `libssl.so.3`, OpenSSL lookups are restricted to the FIPS provider, and `hashlib.md5()` is blocked |
| Flight TLS | `pyarrow/libarrow_flight.so*` links the system `libssl.so.3` and `libcrypto.so.3`. A PyPI build fails this check |
| REST `create_connection`, `check_connection_readiness` | A Postgres connection is created from a Secret and reaches the `ready` state |
| Flight `server_info`, `get_tables`, `read`, `read_batches`, `read_pandas` | Each call succeeds against the connection |
| REST `delete_connection` | The connection is removed |

The script exits with status 1 if any check fails.

## Prerequisites

- **A FIPS cluster with DCH installed by the dc-controller.** All nodes run in
  FIPS mode, and the `DataConnectService` is `Ready`.
- **A tenant namespace** labelled `dataconnecthub.opendatahub.io/tenant=true`,
  so the controller grants the REST service access to its Secrets. Flight's
  access is granted by hand in step 1.
- **A PostgreSQL database** that the Flight service can reach.
- **An SDK version without the ADBC dependency.** The Red Hat index has no ADBC
  wheels, so older SDK versions fail to install their `[flight]` extra there.

## 1. Prepare the tenant

Run these commands from your laptop, logged in with `oc`. Set your values first:

```console
TENANT=<tenant-namespace>
SA=dch-fips-check
```

Create a Secret with the Postgres connection URI. The `postgres` connection type
reads the `URI` key, plus an optional `CA_CERT`:

```console
oc create secret generic dch-fips-check-pg -n "$TENANT" \
  --from-literal=URI='postgresql://<user>:<password>@<host>:5432/<db>?sslmode=verify-full'
```

Let the Flight service read that Secret. DCH doesn't grant this automatically
(see [Grant Flight Service permission to read connection secrets](../user-guide/auth.md#22-grant-flight-service-permission-to-read-connection-secrets)).
Find the Flight ServiceAccount with
`oc get deploy -n <dch-service-namespace> -o custom-columns=NAME:.metadata.name,SA:.spec.template.spec.serviceAccountName`:

```console
DCH_NS=<dch-service-namespace>
FLIGHT_SA=<flight-service-account>

oc create role dch-fips-check-secret-read -n "$TENANT" \
  --verb=get --resource=secrets --resource-name=dch-fips-check-pg
oc create rolebinding dch-fips-check-secret-read -n "$TENANT" \
  --role=dch-fips-check-secret-read --serviceaccount="$DCH_NS:$FLIGHT_SA"
```

Create a ServiceAccount for the check and give it the `dch-read-write` role in
the tenant. The role covers creating and deleting connections and reading data:

```console
oc create serviceaccount "$SA" -n "$TENANT"
oc create rolebinding "$SA" -n "$TENANT" \
  --clusterrole=dch-read-write --serviceaccount="$TENANT:$SA"
```

## 2. Start the workbench pod

Use either an RHOAI workbench or a standalone pod.

**RHOAI workbench:** in the RHOAI dashboard, create a workbench with the
Jupyter Data Science image for CPU and Python 3.12, and open a terminal in it.

**Standalone pod:** use the workbench image directly. The image below is the
RHOAI 3.7 datascience workbench this guide was tested with. On a cluster with
RHOAI installed, `oc get imagestream -n redhat-ods-applications` lists the
images for the installed version.

```console
oc run dch-fips-check -n "$TENANT" --restart=Never --command \
  --image=registry.redhat.io/rhoai/odh-workbench-jupyter-datascience-cpu-py312-rhel9@sha256:17e8930ada19cb1f744ad43e78c80c91c02c10d8622433f6f5b0ad734af3e13d \
  -- sleep 14400
oc wait pod/dch-fips-check -n "$TENANT" --for=condition=Ready --timeout=300s
```

The pod sleeps for 4 hours and then exits. Don't use a plain `ubi9/python-312`
image. It lacks system libraries that Red Hat `pyarrow` needs (`libthrift`,
`libre2`, `libsnappy`), so `import pyarrow` fails.

## 3. Install the SDK

Workbench images set `PIP_CONFIG_FILE=/opt/app-root/pip.conf` and
`UV_CONFIG_FILE=/opt/app-root/uv.toml`, which point pip and uv at the Red Hat
index instead of PyPI. Don't pass `--index-url` or `--extra-index-url`. With
PyPI added, pip can choose PyPI's `pyarrow`, which isn't FIPS.

From a release published on the Red Hat index, in the pod:

```console
pip install "data-connect-hub[flight]"
```

From a branch, build the wheel on your laptop and copy it in. Only the SDK
wheel comes from your laptop. pip still fetches `pyarrow` and the other
dependencies from the Red Hat index:

```console
pip wheel --no-deps ./sdk/python -w /tmp/dch-wheel
oc cp /tmp/dch-wheel/data_connect_hub-*.whl "$TENANT/dch-fips-check:/tmp/"
oc exec -n "$TENANT" dch-fips-check -- bash -c 'pip install "$(ls /tmp/data_connect_hub-*.whl)[flight]"'
```

The datascience image already has Red Hat `pyarrow`, so pip keeps it and
installs only the SDK.

## 4. Copy in the script and a token

From your laptop:

```console
oc cp sdk/python/examples/fips_check.py "$TENANT/dch-fips-check:/tmp/fips_check.py"
oc create token "$SA" -n "$TENANT" --duration=1h \
  | oc exec -i -n "$TENANT" dch-fips-check -- bash -c 'cat > /tmp/dch.token && chmod 600 /tmp/dch.token'
```

## 5. Run the check

Open a shell in the pod (`oc exec -it -n "$TENANT" dch-fips-check -- bash`)
and set the endpoint. REST and Flight are served through the platform gateway:

```console
# On your laptop: oc get route -n openshift-ingress data-science-gateway -o jsonpath='{.spec.host}'
export DCH_HOST=<gateway-host>
export DCH_TENANT_ID=<tenant-namespace>
export DCH_TOKEN_FILE=/tmp/dch.token
export DCH_SECRET_NAME=dch-fips-check-pg
```

If the gateway certificate isn't signed by a public CA, give the script the
cluster's ingress CA. Don't use `DCH_INSECURE=true` for this check: it skips
certificate verification.

```console
# On your laptop:
oc get configmap default-ingress-cert -n openshift-config-managed \
  -o jsonpath='{.data.ca-bundle\.crt}' > /tmp/ingress-ca.crt
oc cp /tmp/ingress-ca.crt "$TENANT/dch-fips-check:/tmp/ingress-ca.crt"

# In the pod:
export DCH_CA_CERT=/tmp/ingress-ca.crt
```

Run it:

```console
python3 /tmp/fips_check.py
```

The script reads these optional variables:

| Variable | Purpose |
|---|---|
| `DCH_FLIGHT_HOST` | A separate Flight endpoint, when REST and Flight aren't behind one gateway (for example, in-cluster service addresses) |
| `DCH_CONNECTION_ID` | Use an existing connection instead of creating one. Skips create, readiness and delete |
| `DCH_CONNECTION_TYPE_ID` | Connection type to use. Default: the type named `Postgres` |
| `DCH_KEEP_CONNECTION` | `true` keeps the created connection |
| `DCH_TOKEN` | The token itself, instead of `DCH_TOKEN_FILE` |

Example output when every check passes:

```text
data-connect-hub 0.3.0, Python 3.12.15

PASS  Host in FIPS mode                  kernel fips_enabled=1
PASS  REST TLS: Python ssl uses FIPS     OpenSSL 3.5.8 25 Aug 2026, FIPS properties on, MD5 blocked
PASS  Flight TLS: pyarrow links system   pyarrow 25.0.1: libarrow_flight.so.2500 links system libssl.so.3/libcrypto.so.3
PASS  REST create_connection             fips-check-4d8071d4 (938e5e39-...), secret=dch-fips-check-pg
PASS  REST check_connection_readiness    state=ready (Connection check successful)
PASS  Flight server_info                 Data Connect Hub 0.1.0, driver='Data Connect Hub Python SDK (pyarrow.flight)'
PASS  Flight get_tables                  1 table(s): ['test_prompts']
PASS  Flight read SELECT 1               result={'one': [1]}
PASS  Flight read table                  'SELECT * FROM public.test_prompts LIMIT 100' -> 3 rows x 3 cols
PASS  Flight read_batches                1 batch(es), 3 rows
PASS  Flight read_pandas                 DataFrame shape=(3, 3)
PASS  REST delete_connection             deleted 938e5e39-...

12/12 passed
```

For comparison, the same script in a pod with PyPI `pyarrow` fails the Flight
TLS check:

```text
FAIL  Flight TLS: pyarrow links system   RuntimeError: pyarrow 26.0.1 bundles its own TLS library (a PyPI build); install pyarrow from the Red Hat index
```

Record the image digest, `pip freeze`, and the script output with your results.

## Troubleshooting

| Failure | Cause | Fix |
|---|---|---|
| `Host in FIPS mode` | The node isn't in FIPS mode | Run on a FIPS cluster |
| `REST TLS` | Python doesn't use the system OpenSSL, or the host isn't in FIPS mode. A `uv`-managed or python.org Python bundles its own OpenSSL | Use the image's `python3` |
| `Flight TLS ... bundles its own TLS library` | `pyarrow` came from PyPI | `pip install --force-reinstall --no-deps pyarrow` with only the Red Hat index configured, or start from a fresh workbench |
| `Flight TLS ... ImportError: libthrift-0.24.0.so` | Red Hat `pyarrow` on an image without its system libraries | Use an RHOAI workbench or AIPCC image |
| `ResolutionImpossible` mentioning `adbc-driver-flightsql` | The SDK version still depends on ADBC | Use a version without ADBC |
| REST `HTTP 401` | The token is invalid or expired. Also returned when kube-rbac-proxy in front of rest-service can't create TokenReviews | Create a new token. Check the kube-rbac-proxy logs for `tokenreviews ... forbidden` |
| REST `HTTP 403`, or Flight `access denied for data-store` | The ServiceAccount lacks `dch-read-write` in the tenant, or the `dch-*` ClusterRoles are missing | Repeat [step 1](#1-prepare-the-tenant). Check that the dc-controller installed the ClusterRoles |
| `check_connection_readiness` not `ready` | REST or Flight can't read the Secret, or Flight can't reach the database | Check the Secret's `URI`, the tenant label, the Flight Secret access from [step 1](#1-prepare-the-tenant), and the database's network access |

## What this check doesn't cover

- **Certificate identity.** The script shows that TLS runs in the system
  OpenSSL with the FIPS provider active. Matching that provider build to a
  NIST CMVP certificate relies on Red Hat's RHEL FIPS documentation.
- **The server side.** REST, Flight and the database connection are covered
  by the services' own FIPS work.
- **Negotiated algorithms.** The script checks library linkage and provider
  state, not a captured TLS handshake.

## Cleanup

```console
oc delete pod dch-fips-check -n "$TENANT"
oc delete rolebinding,serviceaccount "$SA" -n "$TENANT"
oc delete role,rolebinding dch-fips-check-secret-read -n "$TENANT"
oc delete secret dch-fips-check-pg -n "$TENANT"
```

For a workbench, delete `/tmp/dch.token` and the wheel, then stop the workbench.
