# Tenant cluster test: Vault → ESO → DCH

Use this runbook for repeat tests after the platform administrator has
installed ESO and configured Vault access for tenant `vish-eso-test`.
For a first run (or admin setup), use the user-first
[end-to-end runbook](vault-eso-e2e-cluster-test.md). **Tenant users do not
need a Vault administrator token or a shell inside the Vault pod.**

The admin hands over:

- A Ready, namespaced `SecretStore` named `eso-vault-postgres` in
  `vish-eso-test`, backed by a Vault role bound to ESO's tenant ServiceAccount.
- A Vault KV v2 secret with field `URI` at
  `secret/dch/vish-eso-test/postgres/demo`, containing a valid URI for the
  tenant's CloudNativePG PostgreSQL (`dch-tenant-postgres-rw`).
- Permission for the DCH Flight ServiceAccount in
  `redhat-ods-applications` to `get` the ESO-managed Secret
  `vish-eso-test/eso-postgres-creds`. If this has not been granted, a
  cluster administrator can follow [step 3 in the e2e
  runbook](vault-eso-e2e-cluster-test.md#3-check-dch-access-to-the-synced-secret).

ESO runs in its own namespace, `external-secrets`; **only** the sync
resources and resulting Secret belong in `vish-eso-test`. DCH has no Vault
configuration and uses a normal Kubernetes Secret reference.

## 1. Check prerequisites

Commands run locally with `oc` and `jq`. You need DCH `dch-read-write`
access to the tenant for connection creation and readiness. The
`dch-test-user` ServiceAccount has that role on this cluster.

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

If the store is missing or not Ready, ask the platform admin to finish the
[one-time setup](vault-eso-e2e-cluster-test.md#platform-administrator-setup).
Do not try to create a Vault policy or role as a tenant user.

## 2. Sync the PostgreSQL URI

The following command does **not** include a password; ESO uses the
platform-managed store to read Vault and writes the `URI` into a Kubernetes
Secret in the same tenant namespace:

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

If ESO is not Ready, inspect
`oc describe externalsecret eso-postgres -n "$TENANT_NS"`; give the
platform administrator the conditions, not the Secret contents.

## 3. Check DCH access

REST and Flight are separate identities. The platform admin can run:

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

Both must say `yes`. REST already has Secret access on this dev cluster via
`dch-rest-service-data-access`. The old Flight role covers only
`tenant-database-secret`, not `eso-postgres-creds`; a cluster admin must
grant Flight `get` access to this new name if the second check says `no`:

```sh
oc create role dch-flight-eso-postgres-read -n "$TENANT_NS" \
  --verb=get --resource=secrets --resource-name="$SECRET_NAME" \
  --dry-run=client -o yaml | oc apply -f -
oc create rolebinding dch-flight-eso-postgres-read -n "$TENANT_NS" \
  --role=dch-flight-eso-postgres-read \
  --serviceaccount="${DCH_NS}:${FLIGHT_SA}" \
  --dry-run=client -o yaml | oc apply -f -
```

The `oc auth can-i --as=...` checks may require impersonation privileges;
ask a cluster administrator to run them if necessary.

## 4. Create and audit the DCH connection

Keep this port-forward running in **terminal 1**:

```sh
oc port-forward -n "$DCH_NS" svc/dch-rest-service 8443:8443
```

In **terminal 2**, list connection types and find the PostgreSQL type ID.
`--insecure` is only for the localhost DCH port-forward; ESO validates
Vault's TLS certificate:

```sh
export API=https://localhost:8443
export TOKEN="$(oc whoami -t)"
curl --silent --show-error --insecure \
  -H "Authorization: Bearer $TOKEN" -H "X-Tenant-Id: $TENANT_NS" \
  "$API/api/v1alpha1/data/connection-types" | jq .
```

Set the ID and create a connection containing the **bare** Kubernetes
Secret name (never a Vault token, URI, or namespace/name pair):

```sh
export PG_TYPE_ID='<postgres-connection-type-id>'
CONNECTION_ID=$(
  curl --silent --show-error --fail --insecure \
    -H "Authorization: Bearer $TOKEN" -H "X-Tenant-Id: $TENANT_NS" \
    -H 'Content-Type: application/json' \
    --data @- "$API/api/v1alpha1/data/connections" <<EOF | jq -r '.metadata.id'
{
  "name": "eso-postgres-test",
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

**Pass:** `ExternalSecret` is Ready, target Secret has a `URI` key, and DCH
readiness returns **HTTP 204**. For a Flight SQL query and an optional
rotation test, continue with
[steps 5–6 of the e2e runbook](vault-eso-e2e-cluster-test.md#5-query-data-through-flight-optional-stronger-test).

## Cleanup

Delete the connection and your tenant `ExternalSecret` while ESO is still
running. It owns its generated target Secret. Do **not** delete the admin's
`SecretStore`, ESO, Vault, or CloudNativePG database:

```sh
# Only if you created the connection (keep port-forward/API/TOKEN running):
curl --silent --show-error --insecure -o /dev/null \
  -w 'HTTP %{http_code}\n' -X DELETE \
  -H "Authorization: Bearer $TOKEN" -H "X-Tenant-Id: $TENANT_NS" \
  "$API/api/v1alpha1/data/connections/$CONNECTION_ID"
oc delete externalsecret eso-postgres -n "$TENANT_NS"
# Admin only, if the role/binding above was created for this test:
oc delete rolebinding dch-flight-eso-postgres-read -n "$TENANT_NS"
oc delete role dch-flight-eso-postgres-read -n "$TENANT_NS"
```
