# Integrate Vault with External Secrets Operator on OpenShift

This guide continues [Getting started on OpenShift](getting-started-ocp.md). It uses the same tenant namespace (`dch-tenant-a`), PostgreSQL cluster (`dch-tenant-postgres`), DCH namespace (`dch-services`), DCH user (`dch-test-user`), and PostgreSQL connection type (`test-postgres-1`). Instead of copying a database URI into `tenant-database-secret`, External Secrets Operator (ESO) syncs a URI from Vault into a **new** tenant Secret named `eso-postgres-creds`. DCH uses its existing `credentials_ref.secret` field; it does not talk to Vault.

# Prerequisites

* Complete [Getting started on OpenShift](getting-started-ocp.md) through tenant setup and connection type creation. The `dch-tenant-a` namespace, CloudNativePG Secret `dch-tenant-postgres-app` with a `uri` key, and DCH services must exist. If you already created `tenant-database-secret` and a connection using it, leave them in place; this guide creates a separate connection.
* Vault is installed, unsealed, and reachable over TLS from the cluster. This example uses `https://vault-internal.vault.svc:8200`, with a certificate signed by the OpenShift service CA, and a KV v2 engine mounted at `secret/`. Change the address, CA setup, and mount to match your installation.
* The platform administrator needs `oc`, `helm`, and an authenticated Vault CLI with permission to manage Vault auth, policies, and roles. The tenant needs permission to create an `ExternalSecret` in `dch-tenant-a` and a DCH token with `dch-read-write` access. Do not give a Vault administrator token to the tenant.

Use the following names throughout the platform-administrator steps (keep the variables in the same shell):

```sh
export TENANT_NAMESPACE=dch-tenant-a
export VAULT_NS=vault
export VAULT_KEY=dch/db/postgres
export DCH_NAMESPACE=dch-services
```

# Install ESO (platform administrator; once per cluster)

ESO runs in its **own** namespace, not in `dch-tenant-a`. If ESO is already installed and serves `external-secrets.io/v1`, do not upgrade the shared installation just for this tenant.

```sh
helm upgrade --install external-secrets external-secrets \
  --repo https://charts.external-secrets.io --version 2.15.0 \
  -n external-secrets --create-namespace \
  --set installCRDs=true --wait --timeout 5m
oc get deployment -n external-secrets
oc get crd secretstores.external-secrets.io externalsecrets.external-secrets.io
```

You should see the ESO controller running and both CRDs installed. `SecretStore` and `ExternalSecret` below belong to the tenant namespace; installing ESO does not create a database credential or a DCH connection.

# Set up Vault access for the tenant (platform administrator)

## Connect to Vault securely

Forward Vault in one terminal:

```sh
oc port-forward -n "$VAULT_NS" svc/vault-internal 8200:8200
```

In another terminal, configure the Vault CLI to verify the certificate. The OpenShift service CA ConfigMap is expected in the Vault namespace; use your Vault issuer's CA instead if it differs. Authenticate with your organization's approved Vault admin method: `oc login` is not Vault login, and `vault status` does not prove you are authenticated. If using the disposable Vault installation in the [E2E runbook](../dev-guide/vault-eso-e2e-cluster-test.md#4-enable-kv-v2-and-kubernetes-authentication), follow its example for loading the saved root token; otherwise use your organization's admin credentials. Do not paste tokens into a manifest, the guide, or a command argument.

```sh
export TENANT_NAMESPACE=dch-tenant-a
export VAULT_NS=vault
export VAULT_KEY=dch/db/postgres
export VAULT_ADDR=https://127.0.0.1:8200
export VAULT_TLS_SERVER_NAME=vault-internal.vault.svc
export VAULT_CACERT="$(mktemp)"
chmod 600 "$VAULT_CACERT"
oc get configmap openshift-service-ca.crt -n "$VAULT_NS" \
  -o jsonpath='{.data.service-ca\.crt}' > "$VAULT_CACERT"
vault status  # TLS must verify and Vault must be unsealed
```

If Vault is sealed, ask its administrator to unseal it before continuing. Do not disable TLS verification.

## Configure Kubernetes auth if it is not already enabled

Vault must be able to use Kubernetes TokenReview, and the Vault pod's ServiceAccount must have `system:auth-delegator`. Check for an existing `kubernetes/` auth mount; do **not** overwrite a shared Vault's existing auth configuration.

```sh
# Disposable E2E Vault only; skip for an existing/shared Vault.
unset VAULT_NAMESPACE  # Vault Enterprise namespace header, not the OpenShift namespace
VAULT_TOKEN="$(jq -er '.root_token' "$HOME/.vault-init.json")" || exit 1
export VAULT_TOKEN
vault token lookup >/dev/null
vault auth list
# Only if kubernetes/ is absent:
vault auth enable kubernetes
KUBE_CA="$(mktemp)"
chmod 600 "$KUBE_CA"
oc get configmap kube-root-ca.crt -n "$VAULT_NS" \
  -o jsonpath='{.data.ca\.crt}' > "$KUBE_CA"
vault write auth/kubernetes/config \
  kubernetes_host=https://kubernetes.default.svc \
  kubernetes_ca_cert="@$KUBE_CA"
rm -f "$KUBE_CA"
```

Run the enable and config commands **only** for a new auth mount. The Vault deployment must have TokenReview RBAC (the Helm chart's `server.authDelegator.enabled: true` provides it in the [development setup](../dev-guide/vault-eso-e2e-cluster-test.md#platform-administrator-setup)).

## Use an existing Vault credential or add one

The Vault value must live at the relative key `dch/db/postgres` in the `secret/` KV v2 mount (API/policy path `secret/data/dch/db/postgres`) and contain a `URI` field. If you already have that key, verify its field without printing the value and **skip the write**:

```sh
vault kv get -mount=secret -field=URI "$VAULT_KEY" >/dev/null
```

If the key is absent, the platform administrator or credential owner can copy the URI from the CloudNativePG application Secret created in the getting-started guide. Check that the source key exists before writing; the URI is never printed or saved in a manifest. If the verification above failed for a reason other than a missing key (for example, access denied or a missing `URI` field), resolve that error instead of overwriting the Vault key.

```sh
URI_B64=$(oc get secret dch-tenant-postgres-app -n "$TENANT_NAMESPACE" \
  -o jsonpath='{.data.uri}')
test -n "$URI_B64" || { echo 'CloudNativePG URI is missing' >&2; exit 1; }
set -o pipefail
printf '%s' "$URI_B64" | base64 -d | vault kv put -mount=secret "$VAULT_KEY" URI=-
unset URI_B64
```

If your Vault key uses another path, adjust `VAULT_KEY`, the policy, and `ExternalSecret.remoteRef.key` together. Use a separate key and policy for any additional tenant; do not share this credential across tenants. Do not place a password or URI in Git.

## Allow ESO to read only this tenant's Vault key

Create a Vault policy on the KV v2 **data** path, then bind it to a dedicated tenant ServiceAccount through a Kubernetes auth role. The `vault` audience must match the store configuration below. DCH's ServiceAccounts do not need Vault access.

```sh
vault policy write eso-dch-tenant-a-postgres-read - <<EOF
path "secret/data/${VAULT_KEY}" {
  capabilities = ["read"]
}
EOF
vault write auth/kubernetes/role/eso-dch-tenant-a-postgres \
  bound_service_account_names=eso-vault-postgres \
  bound_service_account_namespaces="$TENANT_NAMESPACE" \
  audience=vault policies=eso-dch-tenant-a-postgres-read ttl=15m
```

When finished with Vault administration, stop the port-forward and remove the temporary CA file:

```sh
rm -f "$VAULT_CACERT"
unset VAULT_CACERT VAULT_ADDR VAULT_TLS_SERVER_NAME VAULT_TOKEN
```

# Set up ESO in the tenant namespace (platform administrator)

## Create the tenant identity and trusted CA

Create a tenant ServiceAccount for Vault authentication and a ConfigMap that receives the OpenShift service CA. Only the ESO controller uses this ServiceAccount to request a `vault`-audience token; it is **not** DCH's ServiceAccount. For a different Vault certificate issuer, provision that issuer's CA in a ConfigMap instead.

```sh
oc apply -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: eso-vault-postgres
  namespace: $TENANT_NAMESPACE
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: eso-vault-ca
  namespace: $TENANT_NAMESPACE
  annotations:
    service.beta.openshift.io/inject-cabundle: "true"
EOF
oc get configmap eso-vault-ca -n "$TENANT_NAMESPACE" \
  -o jsonpath='{.data.service-ca\.crt}' | wc -c  # wait for nonzero
```

The ESO controller must be allowed to request a token for `eso-vault-postgres`. As a cluster administrator, check the controller's ServiceAccount; if the check says `no`, add only this tenant-local permission:

```sh
ESO_SA=$(oc get deployment external-secrets -n external-secrets \
  -o jsonpath='{.spec.template.spec.serviceAccountName}')
oc auth can-i create serviceaccounts/token -n "$TENANT_NAMESPACE" \
  --as="system:serviceaccount:external-secrets:${ESO_SA}"
# Only if the check says no:
oc create role eso-vault-token -n "$TENANT_NAMESPACE" \
  --verb=create --resource=serviceaccounts/token --resource-name=eso-vault-postgres \
  --dry-run=client -o yaml | oc apply -f -
oc create rolebinding eso-vault-token -n "$TENANT_NAMESPACE" \
  --role=eso-vault-token --serviceaccount="external-secrets:${ESO_SA}" \
  --dry-run=client -o yaml | oc apply -f -
```

## Create a namespaced SecretStore

A `SecretStore` tells ESO how to reach Vault, which KV mount to use, which CA to trust, and which tenant identity/Vault role to authenticate with. It **does not** hold a URI or sync anything on its own. The platform administrator should own this store and restrict tenant permission to edit it; use a namespaced store instead of a broadly privileged `ClusterSecretStore`.

```sh
oc apply -f - <<EOF
apiVersion: external-secrets.io/v1
kind: SecretStore
metadata:
  name: eso-vault-postgres
  namespace: $TENANT_NAMESPACE
spec:
  provider:
    vault:
      server: https://vault-internal.$VAULT_NS.svc:8200
      path: secret
      version: v2
      caProvider:
        type: ConfigMap
        name: eso-vault-ca
        key: service-ca.crt
      auth:
        kubernetes:
          mountPath: kubernetes
          role: eso-dch-tenant-a-postgres
          serviceAccountRef:
            name: eso-vault-postgres
            audiences: [vault]
EOF
oc wait -n "$TENANT_NAMESPACE" --for=condition=Ready \
  secretstore/eso-vault-postgres --timeout=120s
```

You should see `condition met`. If the store is not Ready, check `oc describe secretstore eso-vault-postgres -n "$TENANT_NAMESPACE"` and verify Vault TLS, its Kubernetes role/audience, TokenReview RBAC, and ESO token-request permission.

# Sync the credential (tenant admin)

The platform administrator hands the tenant admin the Ready store name and the relative key `dch/db/postgres` — **not** a Vault administrator token or URI. A tenant admin with Kubernetes permission to create `ExternalSecret` resources can apply the following manifest; the `dch-read-write` DCH API role alone does not grant that permission. If the tenant admin lacks it, ask the platform administrator to apply this tenant manifest on their behalf. The remote key is relative to the `secret` KV mount, without `secret/data/`.

```sh
export TENANT_NAMESPACE=dch-tenant-a
oc apply -f - <<EOF
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: eso-postgres
  namespace: $TENANT_NAMESPACE
spec:
  refreshInterval: 30s
  secretStoreRef:
    kind: SecretStore
    name: eso-vault-postgres
  target:
    name: eso-postgres-creds
    creationPolicy: Owner
  data:
    - secretKey: URI
      remoteRef:
        key: dch/db/postgres
        property: URI
EOF
oc wait -n "$TENANT_NAMESPACE" --for=condition=Ready \
  externalsecret/eso-postgres --timeout=120s
oc get secret eso-postgres-creds -n "$TENANT_NAMESPACE" \
  -o json | jq -r '.data | keys[]'  # expect URI; do not print its value
```

You should see `condition met` and the key `URI`. If the `ExternalSecret` is not Ready, inspect `oc describe externalsecret eso-postgres -n "$TENANT_NAMESPACE"` and ask the platform administrator to review the store and Vault access.

# Allow DCH to read the synced Secret (platform administrator)

DCH REST and Flight run as separate identities in `dch-services`. Check whether **both** can read `eso-postgres-creds` in `dch-tenant-a`; the `dch-flight-secret-reader` Role created in the getting-started guide covers `tenant-database-secret`, **not** this new Secret. These checks are for DCH's Kubernetes access, not the tenant's DCH API role.

```sh
export TENANT_NAMESPACE=dch-tenant-a
export DCH_NAMESPACE=dch-services
REST_SA=$(oc get deployment dch-rest-service -n "$DCH_NAMESPACE" \
  -o jsonpath='{.spec.template.spec.serviceAccountName}')
FLIGHT_SA=$(oc get deployment dch-default-dataconnectservice-flight -n "$DCH_NAMESPACE" \
  -o jsonpath='{.spec.template.spec.serviceAccountName}')
oc auth can-i get secret/eso-postgres-creds -n "$TENANT_NAMESPACE" \
  --as="system:serviceaccount:${DCH_NAMESPACE}:${REST_SA}"
oc auth can-i get secret/eso-postgres-creds -n "$TENANT_NAMESPACE" \
  --as="system:serviceaccount:${DCH_NAMESPACE}:${FLIGHT_SA}"
```

If either check says `no`, a platform administrator can grant access to this Secret only. The example binds both accounts; omit any subject whose account already has access.

```sh
oc apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: dch-eso-postgres-read
  namespace: $TENANT_NAMESPACE
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["get"]
    resourceNames: ["eso-postgres-creds"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: dch-eso-postgres-read
  namespace: $TENANT_NAMESPACE
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: dch-eso-postgres-read
subjects:
  - kind: ServiceAccount
    name: $REST_SA
    namespace: $DCH_NAMESPACE
  - kind: ServiceAccount
    name: $FLIGHT_SA
    namespace: $DCH_NAMESPACE
EOF
```

# Create and test a DCH connection (tenant admin)

Use the `user_token` and `GW_URL` from [Use DCH Service](getting-started-ocp.md#use-dch-service); obtain a fresh token from [Get User Token](getting-started-ocp.md#get-user-token) if the previous one expired. Choose the PostgreSQL connection type `test-postgres-1` created in the getting-started guide. This creates a **new** connection, leaving `test-pg-conn-1` and `tenant-database-secret` unchanged. `-k` is for the DCH gateway certificate in the getting-started example; ESO verifies Vault's TLS separately.

```sh
export TENANT_NAMESPACE=dch-tenant-a
GW_HOST=$(oc get route data-science-gateway -n openshift-ingress \
  -o jsonpath='{.spec.host}')
GW_URL="https://${GW_HOST}"
type_id=$(curl -fsSk -H "Authorization: Bearer $user_token" \
  -H "x-tenant-id: $TENANT_NAMESPACE" \
  "${GW_URL}/api/v1alpha1/data/connection-types" |
  jq -r '.items[] | select(.resource.name == "test-postgres-1") | .metadata.id' | head -n 1)
test -n "$type_id" || { echo 'PostgreSQL connection type not found'; exit 1; }

connection_id=$(curl -fsSk -X POST \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $user_token" \
  -H "x-tenant-id: $TENANT_NAMESPACE" \
  --data @- "${GW_URL}/api/v1alpha1/data/connections" <<EOF | jq -r '.metadata.id'
{
  "name": "vault-pg-conn-1",
  "data_connection_type_id": "$type_id",
  "format": "tabular",
  "credentials_ref": { "secret": "eso-postgres-creds" },
  "properties": {}
}
EOF
)
test -n "$connection_id" && test "$connection_id" != null
curl -ksS -o /dev/null -w 'Readiness: HTTP %{http_code}\n' -X POST \
  -H "Authorization: Bearer $user_token" \
  -H "x-tenant-id: $TENANT_NAMESPACE" \
  "${GW_URL}/api/v1alpha1/data/connections/${connection_id}/readiness"
```

You should see `Readiness: HTTP 204`. To query the `test_prompts` table through Flight, follow [Get Data from the table](getting-started-ocp.md#get-data-from-the-table) with `CONN_NAME=vault-pg-conn-1` instead of `test-pg-conn-1`.

# Add another tenant

Install ESO only once. For another namespace such as `dch-tenant-b`, repeat the Vault key check, tenant-specific Vault policy and Kubernetes auth role, tenant ServiceAccount and CA, namespaced `SecretStore` and `ExternalSecret`, DCH Secret-read RoleBinding, and connection steps with `TENANT_NAMESPACE=dch-tenant-b` and Vault key `dch/dch-tenant-b/postgres`. Do not share `dch-tenant-a`'s Vault role or target Secret across tenants.

# Rotation and limitations

When the credential owner changes the `URI` at the **same** Vault key, ESO updates `eso-postgres-creds` on its refresh interval; DCH can use the new value after its existing connection/pool cache expires. The DCH connection does not need to be recreated. ESO **does** materialize the URI in a Kubernetes Secret: do not use this method if credentials must never exist in Kubernetes, and restrict access to the generated Secret. For a deeper rotation test and troubleshooting, see the [end-to-end test runbook](../dev-guide/vault-eso-e2e-cluster-test.md).
