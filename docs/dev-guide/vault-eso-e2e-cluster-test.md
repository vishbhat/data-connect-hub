# End-to-end POC: Vault → ESO → DCH

External Secrets Operator (ESO) copies a Vault KV v2 value into a tenant
Kubernetes Secret; Data Connect Hub (DCH) reads that Secret through its
existing `credentials_ref.secret` field. **DCH does not authenticate to Vault
or require a change to its images, CRD, or configuration.**

This runbook separates the **tenant test** from the **one-time platform setup**.
A tenant user does not need a Vault administrator token, a Vault shell, or
permission to manage Vault policies. The platform administrator prepares a
namespaced `SecretStore` and grants ESO access to the tenant's Vault path;
the user creates an `ExternalSecret` and a normal DCH connection. If the
store is not ready yet, ask the platform administrator to complete the
[setup appendix](#platform-administrator-setup).

| Component | Location in this test |
| --- | --- |
| DCH REST and Flight | `redhat-ods-applications` |
| Tenant and PostgreSQL | `vish-eso-test`; CloudNativePG cluster `dch-tenant-postgres` |
| Vault | `vault-test`; `https://vault-internal.vault-test.svc:8200` |
| ESO controller | `external-secrets` (separate from the tenant) |
| Admin-provisioned tenant store | `vish-eso-test/eso-vault-postgres` |
| Tenant-managed sync and target Secret | `vish-eso-test/eso-postgres` → `eso-postgres-creds` |

The PostgreSQL application Secret `dch-tenant-postgres-app` has a `uri` key;
the platform administrator copies that URI into Vault KV v2 at
`secret/dch/vish-eso-test/postgres/demo` under the field `URI`. Do not put
the URI or a Vault token in an `ExternalSecret`, DCH connection, or Git.
ESO **does** materialize the URI in a Kubernetes Secret, so this approach
is not suitable if credentials must never exist in Kubernetes.

## Tenant test (start here)

Run these commands on your **local machine** with `oc`, `jq`, and a DCH API
token that has `dch-read-write` access to `vish-eso-test`. The existing
`dch-test-user` ServiceAccount already has that role; a cluster-admin token
also works. No `oc rsh` into Vault is necessary. A cluster admin may need
to grant the Flight ServiceAccount access to the synced Secret (step 3).

### 1. Verify the platform handoff

The platform administrator should confirm that ESO is running, the Vault
KV v2 value is present with field `URI`, and the tenant's `SecretStore`
`eso-vault-postgres` is **Ready**. You only need to verify the store's status:

```sh
export TENANT_NS=vish-eso-test
export DCH_NS=redhat-ods-applications
export SECRET_NAME=eso-postgres-creds

oc get secretstore eso-vault-postgres -n "$TENANT_NS"
oc wait -n "$TENANT_NS" --for=condition=Ready \
  secretstore/eso-vault-postgres --timeout=120s
oc get deployment dch-rest-service \
  dch-default-dataconnectservice-flight -n "$DCH_NS"
```

If the `SecretStore` is missing or not Ready, **stop** and request the
platform handoff above. Users should not create a Vault role, request a
Vault administrator token, or copy database credentials into a manifest.

### 2. Create the ExternalSecret

The store has already been configured by the platform administrator. Apply
only the tenant's sync specification; `remoteRef.key` is relative to the KV
mount `secret` (it must not include `secret/data/`). This command contains
**no credentials**:

```sh
oc apply -f - <<'EOF'
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: eso-postgres
  namespace: vish-eso-test
spec:
  refreshInterval: 30s
  secretStoreRef:
    kind: SecretStore
    name: eso-vault-postgres
  target:
    name: eso-postgres-creds
    creationPolicy: Owner
    template:
      type: Opaque
  data:
    - secretKey: URI
      remoteRef:
        key: dch/vish-eso-test/postgres/demo
        property: URI
EOF

oc wait -n "$TENANT_NS" --for=condition=Ready \
  externalsecret/eso-postgres --timeout=120s
oc get secret "$SECRET_NAME" -n "$TENANT_NS" \
  -o json | jq -r '.data | keys[]'  # expect URI; never print the value
```

If the `ExternalSecret` is not Ready, use
`oc describe externalsecret eso-postgres -n "$TENANT_NS"` to inspect its
conditions and ask the platform administrator to check ESO/Vault logs.

### 3. Check DCH access to the synced Secret

DCH resolves `credentials_ref.secret` in the `X-Tenant-Id` namespace. Its
REST and Flight **service accounts** need Kubernetes `get` access there;
this is different from your DCH API permissions. A cluster administrator
can run these impersonation checks:

```sh
REST_SA=$(oc get deployment dch-rest-service -n "$DCH_NS" \
  -o jsonpath='{.spec.template.spec.serviceAccountName}')
FLIGHT_SA=$(oc get deployment dch-default-dataconnectservice-flight -n "$DCH_NS" \
  -o jsonpath='{.spec.template.spec.serviceAccountName}')

oc auth can-i get "secret/$SECRET_NAME" -n "$TENANT_NS" \
  --as="system:serviceaccount:${DCH_NS}:${REST_SA}"
oc auth can-i get "secret/$SECRET_NAME" -n "$TENANT_NS" \
  --as="system:serviceaccount:${DCH_NS}:${FLIGHT_SA}"
```

On this dev cluster, **REST already says `yes`**: its
`dch-rest-service-data-access` ClusterRoleBinding grants Secret access.
The existing Flight role `dch-flight-secret-reader` only allows
`tenant-database-secret`, so **Flight says `no`** for the *new* Secret
`eso-postgres-creds` until an admin grants it access. Ask a cluster admin
to run the following, or use an equivalent tenant-local role/binding.
This does not alter the existing Flight role:

```sh
oc create role dch-flight-eso-postgres-read -n "$TENANT_NS" \
  --verb=get --resource=secrets --resource-name="$SECRET_NAME" \
  --dry-run=client -o yaml | oc apply -f -
oc create rolebinding dch-flight-eso-postgres-read -n "$TENANT_NS" \
  --role=dch-flight-eso-postgres-read \
  --serviceaccount="${DCH_NS}:${FLIGHT_SA}" \
  --dry-run=client -o yaml | oc apply -f -
oc auth can-i get "secret/$SECRET_NAME" -n "$TENANT_NS" \
  --as="system:serviceaccount:${DCH_NS}:${FLIGHT_SA}"  # now expect yes
```

Do not grant the Flight ServiceAccount access to every tenant Secret just
for this test. On other clusters, if REST also says `no`, the platform
administrator must arrange REST Secret read access before the readiness test.

### 4. Create and audit a normal DCH connection

In **terminal 1**, forward the DCH REST service (keep it running):

```sh
oc port-forward -n "$DCH_NS" svc/dch-rest-service 8443:8443
```

In **terminal 2**, list connection types and choose the PostgreSQL type ID.
`--insecure` here applies **only to the localhost DCH port-forward**, not
to Vault or ESO:

```sh
export API=https://localhost:8443
export TOKEN="$(oc whoami -t)"
curl --silent --show-error --insecure \
  -H "Authorization: Bearer $TOKEN" -H "X-Tenant-Id: $TENANT_NS" \
  "$API/api/v1alpha1/data/connection-types" | jq .
```

Substitute the returned PostgreSQL type ID, then create a connection with a
**bare Secret name**, not a Vault reference or an inline password:

```sh
export PG_TYPE_ID='<postgres-connection-type-id>'
CONNECTION_ID=$(
  curl --silent --show-error --fail --insecure \
    -H "Authorization: Bearer $TOKEN" -H "X-Tenant-Id: $TENANT_NS" \
    -H 'Content-Type: application/json' \
    --data @- "$API/api/v1alpha1/data/connections" <<EOF | jq -r '.metadata.id'
{
  "name": "eso-postgres-e2e",
  "data_connection_type_id": "$PG_TYPE_ID",
  "format": "tabular",
  "credentials_ref": { "secret": "$SECRET_NAME" },
  "properties": {}
}
EOF
)
test -n "$CONNECTION_ID" && test "$CONNECTION_ID" != null
curl --silent --show-error --insecure -o /dev/null \
  -w 'HTTP %{http_code}\n' -X POST \
  -H "Authorization: Bearer $TOKEN" -H "X-Tenant-Id: $TENANT_NS" \
  "$API/api/v1alpha1/data/connections/$CONNECTION_ID/readiness"
```

**Pass:** ESO is Ready, the target Secret has `URI`, and the DCH readiness
check returns **HTTP 204**. Readiness includes a check by Flight against
PostgreSQL. A missing Secret or a `no` in step 3 is an authorization/setup
issue, not a reason to configure Vault in DCH.

### 5. Query data through Flight (optional stronger test)

In **terminal 3**, forward the Flight service:

```sh
oc port-forward -n "$DCH_NS" \
  svc/dch-default-dataconnectservice-flight 8444:8443
```

From the repository root in **terminal 2**, install a temporary Flight SQL
client (Python 3.11+). TLS verification is bypassed **only** for the
localhost Flight port-forward:

```sh
python3 -m venv /tmp/dch-eso-flight-venv
/tmp/dch-eso-flight-venv/bin/python -m pip install -e './sdk/python[flight]'
export CONNECTION_ID
/tmp/dch-eso-flight-venv/bin/python - <<'PY'
import os
from adbc_driver_flightsql import DatabaseOptions, dbapi

with dbapi.connect(
    "grpc+tls://localhost:8444",
    db_kwargs={
        DatabaseOptions.TLS_SKIP_VERIFY.value: "true",
        "adbc.flight.sql.rpc.call_header.authorization": f"Bearer {os.environ['TOKEN']}",
        "adbc.flight.sql.rpc.call_header.x-tenant-id": os.environ["TENANT_NS"],
        "adbc.flight.sql.rpc.call_header.x-data-connection-id": os.environ["CONNECTION_ID"],
    },
) as connection:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 AS eso_ok")
        print(cursor.fetchone())  # (1,)
PY
```

### 6. Rotation (optional; coordinate with the secret owner)

Ask the platform administrator or database owner to change **only** the
Vault `URI` value at the **same** key to a *different, valid* test database
URI. If you want to prove that DCH switched databases, the administrator
can prepare `tenant_a_db_rotated` with a marker table in the disposable
CloudNativePG cluster (see [admin setup](#platform-administrator-setup)).
Do not change the `ExternalSecret` or DCH connection.

Before the Vault change, capture the current encoded Secret value in a
shell variable without printing it (do not enable `set -x`). After the
change, wait for ESO to update the target:

```sh
BEFORE=$(oc get secret "$SECRET_NAME" -n "$TENANT_NS" \
  -o jsonpath='{.data.URI}')
# Ask the administrator to update the Vault URI only after recording BEFORE.
for i in $(seq 1 24); do
  AFTER=$(oc get secret "$SECRET_NAME" -n "$TENANT_NS" \
    -o jsonpath='{.data.URI}')
  [ "$AFTER" != "$BEFORE" ] && break
  sleep 5
done
[ "$AFTER" != "$BEFORE" ] || { echo 'ESO did not sync within 120s'; false; }
```

Wait about **35 seconds** for DCH's existing PostgreSQL pool cache, then
repeat the readiness check and Flight query without recreating the
connection. If the second database contains a table `eso_rotated` with a
row `marker=42`, querying `SELECT marker FROM eso_rotated` through Flight
and receiving `(42,)` proves that DCH uses the new URI.

### 7. Clean up your test resources

Delete only what you created, while ESO is still running. Deleting the
`ExternalSecret` removes its `Owner` target Secret. Leave the platform's
`SecretStore`, ESO installation, Vault, and PostgreSQL intact:

```sh
# Only if you created a DCH connection (port-forward/API/TOKEN still needed):
curl --silent --show-error --insecure -o /dev/null \
  -w 'HTTP %{http_code}\n' -X DELETE \
  -H "Authorization: Bearer $TOKEN" -H "X-Tenant-Id: $TENANT_NS" \
  "$API/api/v1alpha1/data/connections/$CONNECTION_ID"

oc delete externalsecret eso-postgres -n "$TENANT_NS"
# Admin only, if the role/binding in step 3 was created for this test:
oc delete rolebinding dch-flight-eso-postgres-read -n "$TENANT_NS"
oc delete role dch-flight-eso-postgres-read -n "$TENANT_NS"
```

Remove the temporary Python environment when finished:
`rm -rf /tmp/dch-eso-flight-venv`. Do **not** uninstall ESO while tenant
`ExternalSecret` or `SecretStore` resources still exist: finalizers can
prevent deletion without a running controller.

## Platform administrator setup

**One-time per tenant, not a tenant-user procedure.** The admin owns the
Vault installation, Kubernetes authentication, narrow Vault policy/role,
SecretStore, TLS CA, and any required DCH service-account RBAC. The user
needs only the **Ready store name** and the **relative remote key/field**;
do not hand over an admin token. Avoid a cluster-wide `ClusterSecretStore`
with a broad Vault policy: a namespaced `SecretStore` plus a role bound to a
single tenant ServiceAccount keeps this POC scoped to `vish-eso-test`.

### Install/verify Vault and ESO

On the current dev cluster, Vault already runs in `vault-test`; verify it
without modifying it:

```sh
oc get pod vault-0 -n vault-test
oc get service vault-internal -n vault-test
oc get configmap openshift-service-ca.crt -n vault-test \
  -o jsonpath='{.data.service-ca\.crt}' | wc -c  # nonzero
```

On a new cluster where `openshift-service-ca.crt` does not yet exist in
`vault-test`, request a service CA ConfigMap there and use its
`service-ca.crt` key in the admin CLI setup below:

```sh
oc create namespace vault-test --dry-run=client -o yaml | oc apply -f -
oc apply -n vault-test -f - <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: openshift-service-ca.crt
  annotations:
    service.beta.openshift.io/inject-cabundle: "true"
EOF
```

Wait until the bundle is populated. For a **new disposable cluster** without Vault, install the official Helm
chart (version 0.34.1 in this example). The values below configure
standalone TLS using an OpenShift serving certificate for the headless
`vault-internal` Service. Do not use this single-pod, 1/1 unseal-key
configuration in production. If Vault already exists, **skip** these
installation steps. First, save the following as `vault-eso-values.yaml`:

```yaml
global:
  openshift: true
injector:
  enabled: false
server:
  authDelegator:
    enabled: true
  extraVolumes:
    - type: secret
      name: vault-tls
  extraConfig: |
    ui = true
    disable_mlock = true
    api_addr = "https://vault-internal.vault-test.svc:8200"
    listener "tcp" {
      address = "[::]:8200"
      cluster_address = "[::]:8201"
      tls_disable = 0
      tls_cert_file = "/vault/userconfig/vault-tls/tls.crt"
      tls_key_file = "/vault/userconfig/vault-tls/tls.key"
    }
    storage "file" {
      path = "/vault/data"
    }
```

Then install Vault and request the serving certificate for
`vault-internal`. **Do not use `helm --wait`** until Vault is initialized
and unsealed:

```sh
oc create namespace vault-test --dry-run=client -o yaml | oc apply -f -
helm upgrade --install vault vault \
  --repo https://helm.releases.hashicorp.com --version 0.34.1 \
  -n vault-test -f vault-eso-values.yaml
oc annotate service vault-internal -n vault-test \
  service.beta.openshift.io/serving-cert-secret-name=vault-tls
oc get secret vault-tls -n vault-test -o name  # wait for issuance
```

Use the TLS-verified Vault CLI setup below to initialize and unseal a fresh
Vault; securely store the unseal key and root token **outside this repo and
Kubernetes**. The chart's `server.authDelegator.enabled: true` binds the
Vault ServiceAccount to `system:auth-delegator` for TokenReview.

Install ESO in its **own** namespace if not already installed:

```sh
helm upgrade --install external-secrets external-secrets \
  --repo https://charts.external-secrets.io --version 2.15.0 \
  -n external-secrets --create-namespace \
  --set installCRDs=true --wait --timeout 5m
oc get deployments -n external-secrets
oc get crd secretstores.external-secrets.io externalsecrets.external-secrets.io
```

Use a supported ESO version that serves `external-secrets.io/v1`; do not
uninstall an operator shared by other tenants. Ensure its controller has
permission to request `serviceaccounts/token` in `vish-eso-test`.

### Configure Vault and the tenant SecretStore (admin only)

The admin can use the Vault UI or a Vault CLI **on their workstation**;
there is no need to `oc rsh` into the Vault pod. For the CLI, port-forward
`vault-internal` in one terminal:

```sh
oc port-forward -n vault-test svc/vault-internal 8200:8200
```

In another terminal, configure the CLI to verify the OpenShift service
certificate. `VAULT_TLS_SERVER_NAME` supplies the name on Vault's cert
while `VAULT_ADDR` points to the local tunnel; **do not use
`VAULT_SKIP_VERIFY`**. Obtain the service CA into a private temporary file,
then authenticate via your approved admin method (never paste the token into
a manifest, this doc, or a command argument):

```sh
export TENANT_NS=vish-eso-test
export VAULT_KEY="dch/${TENANT_NS}/postgres/demo"
export VAULT_ADDR=https://127.0.0.1:8200
export VAULT_TLS_SERVER_NAME=vault-internal.vault-test.svc
export VAULT_CACERT="$(mktemp)"
chmod 600 "$VAULT_CACERT"
oc get configmap openshift-service-ca.crt -n vault-test \
  -o jsonpath='{.data.service-ca\.crt}' > "$VAULT_CACERT"
vault status  # verify TLS; no token required (sealed Vault returns nonzero)
```

On **new, uninitialized Vault installations only**, initialize and unseal
interactively, then wait for `vault status` to report `Sealed: false`. The
1/1 unseal share is test-only; save the key and root token securely:

```sh
vault operator init -key-shares=1 -key-threshold=1
vault operator unseal  # prompts for the key; do not put it on the command line
vault status
```

Authenticate using your organization's admin procedure before the next
commands. If using a token interactively, keep it out of shell history and command
arguments (in a terminal with echo available):

```sh
printf 'Vault admin token: '
stty -echo
IFS= read -r VAULT_TOKEN
stty echo
printf '\n'
export VAULT_TOKEN
```

On **new** Vault installations, enable a `secret/` KV v2 engine and
`kubernetes/` auth only if absent (check `vault secrets list -detailed` and
`vault auth list` first). The Vault pod's ServiceAccount must have
`system:auth-delegator`. To configure Kubernetes auth from a workstation,
use the namespace's Kubernetes API CA:

```sh
vault secrets list -detailed
vault auth list
# Run the next two commands only for missing mounts:
vault secrets enable -path=secret -version=2 kv
vault auth enable kubernetes

KUBE_CA="$(mktemp)"
chmod 600 "$KUBE_CA"
oc get configmap kube-root-ca.crt -n vault-test \
  -o jsonpath='{.data.ca\.crt}' > "$KUBE_CA"
vault write auth/kubernetes/config \
  kubernetes_host=https://kubernetes.default.svc \
  kubernetes_ca_cert="@$KUBE_CA"
rm -f "$KUBE_CA"
```

Vault must be able to reach `kubernetes.default.svc` and reread its mounted
ServiceAccount token for TokenReview. If this Vault is already configured,
**do not overwrite** the existing auth configuration.

Create a read-only Vault policy for exactly one KV v2 path and a role bound
to ESO's tenant identity (the audience `vault` matches the requested
ServiceAccount token):

```sh
oc create serviceaccount eso-vault-postgres -n "$TENANT_NS" \
  --dry-run=client -o yaml | oc apply -f -
vault policy write eso-postgres-read - <<EOF
path "secret/data/dch/${TENANT_NS}/postgres/demo" {
  capabilities = ["read"]
}
EOF
vault write auth/kubernetes/role/eso-postgres \
  bound_service_account_names=eso-vault-postgres \
  bound_service_account_namespaces="$TENANT_NS" \
  audience=vault policies=eso-postgres-read ttl=15m
```

Ask the database owner for the approved test URI. In this dev tenant, the
CloudNativePG Secret `dch-tenant-postgres-app` has it under `uri`. **Only an
authorized admin** should run this pipe; it does not print the URI or store
it in a manifest:

```sh
oc get secret dch-tenant-postgres-app -n "$TENANT_NS" \
  -o jsonpath='{.data.uri}' | base64 -d | \
  vault kv put -mount=secret "$VAULT_KEY" URI=-
```

The Vault path above uses the relative KV key
`dch/vish-eso-test/postgres/demo`; ESO will read its `URI` field. The
CloudNativePG Secret and the ESO target Secret are distinct resources.
For a rotation proof, the database owner can prepare a *second, valid* URI
(e.g. a disposable database `tenant_a_db_rotated` with a table
`eso_rotated(marker)` containing `42`) and update only that Vault key.
Do not rotate a shared database user's password just for this test.

Finally, create a tenant CA ConfigMap and namespaced store. On this cluster,
the Vault serving cert is for `vault-internal.vault-test.svc`, signed by the
OpenShift service CA. The admin owns this `SecretStore`, not the tenant
`ExternalSecret`:

```sh
oc apply -n "$TENANT_NS" -f - <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: eso-vault-ca
  annotations:
    service.beta.openshift.io/inject-cabundle: "true"
EOF
oc get configmap eso-vault-ca -n "$TENANT_NS" \
  -o jsonpath='{.data.service-ca\.crt}' | wc -c  # wait for nonzero
oc apply -f - <<'EOF'
apiVersion: external-secrets.io/v1
kind: SecretStore
metadata:
  name: eso-vault-postgres
  namespace: vish-eso-test
spec:
  provider:
    vault:
      server: https://vault-internal.vault-test.svc:8200
      path: secret
      version: v2
      caProvider:
        type: ConfigMap
        name: eso-vault-ca
        key: service-ca.crt
      auth:
        kubernetes:
          mountPath: kubernetes
          role: eso-postgres
          serviceAccountRef:
            name: eso-vault-postgres
            audiences: [vault]
EOF
oc wait -n "$TENANT_NS" --for=condition=Ready \
  secretstore/eso-vault-postgres --timeout=120s
```

If that wait fails, inspect `oc describe secretstore eso-vault-postgres -n
"$TENANT_NS"`, ESO controller logs, Vault TLS, role audience, and
TokenReview configuration. After a successful handoff, the tenant user can
start at [step 1](#1-verify-the-platform-handoff).

When finished on the admin workstation:

```sh
unset VAULT_TOKEN VAULT_TLS_SERVER_NAME VAULT_ADDR
rm -f "$VAULT_CACERT"
unset VAULT_CACERT
```
