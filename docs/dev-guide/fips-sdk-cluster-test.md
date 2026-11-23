# Test the Python SDK against FIPS-mode DCH

This runbook exercises the Data Connect Hub (DCH) **Python SDK** (REST and
Flight) from a pod on a **FIPS-enabled** OpenShift cluster against DCH
services that also run on that cluster.

It records a **functional** test in a FIPS environment. It does **not** by
itself prove that the SDK’s Flight TLS uses a FIPS-validated cryptographic
module. Stock PyPI `[flight]` wheels still use ADBC (Go `crypto/tls`) and
pyarrow’s bundled TLS; see the [SDK README FIPS section](../../sdk/python/README.md#fips-environments)
and RHOAIENG-102749.

## What you need

| Piece | Example used here |
| --- | --- |
| FIPS OpenShift cluster | nodes with `/proc/sys/crypto/fips_enabled = 1` |
| DCH REST + Flight | namespace `dch-fips-test` |
| Tenant namespace + Postgres data source | `dch-tenant-a`, Secret `tenant-database-secret` with key `URI` |
| Caller ServiceAccount | `dch-test-user` in `dch-tenant-a` bound to `dch-read-write` |
| Cluster-admin (one-time setup) | TokenReview binding for REST; optional Flight registration |

Confirm FIPS on a node:

```bash
oc debug node/$(oc get nodes -o name | head -1) --quiet -- \
  chroot /host cat /proc/sys/crypto/fips_enabled
# expect: 1
```

## 1. One-time cluster setup for the FIPS DCH install

Skip steps that are already done on your cluster.

### 1.1 Allow REST to validate bearer tokens

`kube-rbac-proxy` on REST must create `TokenReview`s. If API calls return
HTTP 401 and proxy logs show `cannot create resource "tokenreviews"`, bind
the existing ClusterRole to the FIPS REST ServiceAccount:

```bash
DCH_NS=dch-fips-test

oc create clusterrolebinding dch-fips-kube-rbac-proxy-auth-review \
  --clusterrole=dch-kube-rbac-proxy-auth-review \
  --serviceaccount=${DCH_NS}:rest-service-sa \
  --dry-run=client -o yaml | oc apply -f -
```

### 1.2 Temporarily allow the SDK pod to reach REST

The default REST NetworkPolicy only admits the gateway and controller. Same-
namespace pods (including the SDK test pod) are blocked and REST calls hang.

For this test only:

```bash
DCH_NS=dch-fips-test

oc patch networkpolicy rest-service -n "$DCH_NS" --type=merge -p 'spec:
  ingress:
  - {}'
```

Revert when finished (restore the original `from:` rules from your install
manifests). Flight’s NetworkPolicy may already allow same-namespace clients;
if Flight TCP to `:8443` times out, open it the same way.

### 1.3 Register Flight with REST (manual installs only)

A normal `DataConnectService` install registers Flight via the controller.
A hand-built FIPS stack may leave `flight_services` empty. Readiness then
fails with `no flight service supports connector 'postgres'`.

Register once (use a token that can `create` `flight-services` in the DCH
namespace — for example `oc whoami -t` as kubeadmin):

```bash
DCH_NS=dch-fips-test
TOKEN=$(oc whoami -t)

# Prefer running this curl from a pod that can reach the REST Service
# (for example dch-sdk-flight after section 2), or port-forward REST.

curl -sk \
  --cacert /certs/service-ca.pem \
  -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "x-tenant-id: ${DCH_NS}" \
  -H "Content-Type: application/json" \
  -d "{
    \"name\": \"flight-service\",
    \"namespace\": \"${DCH_NS}\",
    \"external_url\": \"https://flight-service.${DCH_NS}.svc:8443\",
    \"internal_url\": \"https://flight-service.${DCH_NS}.svc:8443\",
    \"status\": { \"ready\": true }
  }" \
  "https://rest-service.${DCH_NS}.svc.cluster.local:8443/api/v1alpha1/data/flights"
```

Expect HTTP 201 and `supported_connectors` including `postgres`.

### 1.4 Restart Flight after metadata DB TLS secrets change

Flight loads `/secrets/secret-config.toml` at process start and does not
hot-reload. If client certs for the DCH metadata Postgres were added after
Flight started, queries fail with metadata errors such as
`connection requires a valid client certificate`. Restart Flight:

```bash
oc rollout restart deploy/flight-service -n dch-fips-test
oc rollout status deploy/flight-service -n dch-fips-test --timeout=120s
```

## 2. Tenant access (safe to change in your test tenant)

Replace names if your tenant differs.

```bash
TENANT_NS=dch-tenant-a
DCH_NS=dch-fips-test
SECRET_NAME=tenant-database-secret

# Flight may read the connection Secret
oc apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: dch-flight-secret-reader
  namespace: ${TENANT_NS}
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["get"]
    resourceNames: ["${SECRET_NAME}"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: dch-flight-secret-reader
  namespace: ${TENANT_NS}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: dch-flight-secret-reader
subjects:
  - kind: ServiceAccount
    name: flight-service-sa
    namespace: ${DCH_NS}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: dch-rest-service-secret-access
  namespace: ${TENANT_NS}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: dch-rest-service-secret-access
subjects:
  - kind: ServiceAccount
    name: rest-service-sa
    namespace: ${DCH_NS}
EOF

# Caller SA for the SDK
oc create serviceaccount dch-test-user -n "$TENANT_NS" --dry-run=client -o yaml | oc apply -f -
oc create rolebinding dch-test-user-dch-read-write \
  -n "$TENANT_NS" \
  --clusterrole=dch-read-write \
  --serviceaccount="${TENANT_NS}:dch-test-user" \
  --dry-run=client -o yaml | oc apply -f -
```

Ensure the tenant Postgres is reachable from Flight and that
`tenant-database-secret` contains a working `URI` (for a non-TLS tenant DB,
`sslmode=disable` is typical). Seed a table such as `test_prompts` if you
will run the sample Flight query below.

## 3. Create the FIPS SDK test pod

```bash
DCH_NS=dch-fips-test

oc delete pod dch-sdk-flight -n "$DCH_NS" --ignore-not-found

oc run dch-sdk-flight -n "$DCH_NS" \
  --image=registry.access.redhat.com/ubi9/python-311:latest \
  --restart=Never \
  --overrides='{
    "spec": {
      "containers": [{
        "name": "dch-sdk-flight",
        "image": "registry.access.redhat.com/ubi9/python-311:latest",
        "command": ["sleep", "36000"],
        "volumeMounts": [{
          "name": "service-ca",
          "mountPath": "/certs",
          "readOnly": true
        }]
      }],
      "volumes": [{
        "name": "service-ca",
        "configMap": {
          "name": "openshift-service-ca.crt",
          "items": [{"key": "service-ca.crt", "path": "service-ca.pem"}]
        }
      }]
    }
  }'

oc wait -n "$DCH_NS" pod/dch-sdk-flight --for=condition=Ready --timeout=120s
```

Inside the pod, confirm FIPS and install the SDK (REST + Flight):

```bash
oc rsh -n dch-fips-test dch-sdk-flight

cat /proc/sys/crypto/fips_enabled    # expect 1
python3 -c "import ssl; print(ssl.OPENSSL_VERSION)"

python3 -m pip install 'data-connect-hub[flight]'
```

## 4. Supply a caller token

From your laptop (tokens expire; recreate when you see `UNAUTHENTICATED`):

```bash
TOK=$(oc create token dch-test-user -n dch-tenant-a --duration=3600s)
oc exec -n dch-fips-test dch-sdk-flight -- bash -lc "printf '%s' '$TOK' > /tmp/dch.token"
```

All scripts below run **inside** `dch-sdk-flight` and call in-cluster Service
DNS (not the shared gateway):

- REST: `rest-service.dch-fips-test.svc.cluster.local:8443`
- Flight: `flight-service.dch-fips-test.svc.cluster.local:8443`

## 5. REST: create connection type and connection

Write `/tmp/create_connection.py` in the pod:

```python
#!/usr/bin/env python3
"""Create connection type + connection via DCH REST (run in dch-sdk-flight)."""

from pathlib import Path

from data_connect_hub import CredentialField, CredentialsRef, DataConnectClient

TOKEN_FILE = Path("/tmp/dch.token")
CONN_ID_FILE = Path("/tmp/dch_connection_id")

REST_ENDPOINT = "rest-service.dch-fips-test.svc.cluster.local:8443"
TENANT_ID = "dch-tenant-a"
CA_CERT = "/certs/service-ca.pem"
SECRET_NAME = "tenant-database-secret"
TYPE_NAME = "postgres-tenant"
CONN_NAME = "tenant-pg"


def main() -> None:
    token = TOKEN_FILE.read_text().strip()
    if not token:
        raise SystemExit(f"missing token in {TOKEN_FILE}")

    client = DataConnectClient(
        endpoint=REST_ENDPOINT,
        token=token,
        tenant_id=TENANT_ID,
        ca_cert=CA_CERT,
    )

    types = client.list_connection_types()
    ct = next((t for t in types if t.name == TYPE_NAME), None)
    if ct is None:
        ct = client.create_connection_type(
            name=TYPE_NAME,
            provider="postgres",
            description="Tenant PostgreSQL",
            credentials_fields=[
                CredentialField(
                    name="URI",
                    label="Connection URI",
                    required=True,
                    type="string",
                    sensitive=True,
                ),
            ],
        )
    print(f"connection_type_id={ct.id}")

    conns = client.list_connections()
    conn = next((c for c in conns if c.name == CONN_NAME), None)
    if conn is None:
        conn = client.create_connection(
            name=CONN_NAME,
            connection_type_id=ct.id,
            data_format="tabular",
            credentials_ref=CredentialsRef(secret=SECRET_NAME),
        )
    print(f"connection_id={conn.id}")
    CONN_ID_FILE.write_text(conn.id)

    client.check_connection_readiness(conn.id)
    print("readiness=ok")


if __name__ == "__main__":
    main()
```

Run:

```bash
python3 /tmp/create_connection.py
```

**Pass:** prints `connection_id=…` and `readiness=ok`.

### Optional REST smoke (list only)

```python
from pathlib import Path
from data_connect_hub import DataConnectClient

client = DataConnectClient(
    endpoint="rest-service.dch-fips-test.svc.cluster.local:8443",
    token=Path("/tmp/dch.token").read_text().strip(),
    tenant_id="dch-tenant-a",
    ca_cert="/certs/service-ca.pem",
)
print([(c.name, c.id) for c in client.list_connections()])
print([(t.name, t.id, t.status.flight_ready) for t in client.list_connection_types()])
```

## 6. Flight: run a SQL query

Write `/tmp/flight_query.py` in the pod:

```python
#!/usr/bin/env python3
"""Run a Flight SQL query (run in dch-sdk-flight)."""

import warnings
from pathlib import Path

from data_connect_hub import DataConnectClient, DCHError

warnings.filterwarnings(
    "ignore",
    message=r"^Cannot disable autocommit; conn will not be DB-API 2\.0 compliant$",
    category=Warning,
    module=r"adbc_driver_manager\.dbapi",
)

TOKEN_FILE = Path("/tmp/dch.token")
CONN_ID_FILE = Path("/tmp/dch_connection_id")

FLIGHT_ENDPOINT = "flight-service.dch-fips-test.svc.cluster.local:8443"
TENANT_ID = "dch-tenant-a"
CA_CERT = "/certs/service-ca.pem"
SQL = "SELECT * FROM test_prompts ORDER BY id"


def main() -> None:
    token = TOKEN_FILE.read_text().strip()
    connection_id = CONN_ID_FILE.read_text().strip()
    if not token or not connection_id:
        raise SystemExit("need /tmp/dch.token and /tmp/dch_connection_id")

    try:
        with DataConnectClient(
            endpoint=FLIGHT_ENDPOINT,
            token=token,
            tenant_id=TENANT_ID,
            ca_cert=CA_CERT,
        ) as client:
            print("server_info:", client.server_info())
            table = client.read(SQL, connection_id=connection_id)
            print(table.to_pandas().to_string(index=False))
    except DCHError as exc:
        raise SystemExit(f"Flight query failed: {exc}") from exc


if __name__ == "__main__":
    main()
```

Run:

```bash
python3 /tmp/flight_query.py
```

**Pass:** `server_info` lists connectors including `postgres`, and the query
returns rows from `test_prompts`.

## 7. Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| REST call hangs from the SDK pod | REST NetworkPolicy (section 1.2) |
| HTTP 401 Unauthorized | REST SA missing TokenReview binding (section 1.1), or stale token |
| `CredentialField` validation error `type` required | Pass `type="string"` on the field |
| `no flight service supports connector 'postgres'` | Flight not registered (section 1.3) |
| `UNAUTHENTICATED` / token expired | Recreate token into `/tmp/dch.token` (section 4) |
| `INTERNAL` / metadata query / `client certificate` | Restart Flight after DB TLS secret update (section 1.4) |
| Flight `PERMISSION_DENIED` / access denied for `data-store` | Caller missing `dch-read` / `dch-read-write` in the **tenant** namespace |

## 8. What this test does and does not prove

**Does prove**

- Stock `data-connect-hub[flight]` can talk TLS to FIPS-mode DCH REST (via
`kube-rbac-proxy`) and Flight (direct rustls/OpenSSL server path) from a
FIPS UBI9 Python pod using in-cluster Service DNS.
- End-to-end create connection + Flight SQL against a tenant Postgres Secret.

**Does not prove**

- That ADBC/pyarrow Flight client TLS uses a host FIPS-validated module.
- Full product FIPS compliance for DCH services or the SDK.

## 9. Cleanup

```bash
# Revert REST NetworkPolicy to the install default when done.
# Remove the test pod:
oc delete pod dch-sdk-flight -n dch-fips-test --ignore-not-found

# Optionally delete tenant resources you created (connection, SA bindings, Postgres).
```
