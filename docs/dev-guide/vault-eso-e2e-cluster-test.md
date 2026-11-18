# End-to-end cluster test: Vault → ESO → DCH

This test assumes Data Connect Hub (DCH) and the tenant PostgreSQL database are installed using [Getting started on OpenShift](../user-guide/getting-started-ocp.md). It installs and configures **Vault** on a disposable OpenShift cluster if necessary, then uses [Integrate Vault with ESO](../user-guide/integrate-vault-with-eso.md) to install ESO and configure the tenant. The final checks prove that a PostgreSQL credential flows from Vault through ESO to a DCH connection and Flight query. DCH does not authenticate to Vault.

This example uses `dch-services` for DCH, `dch-tenant-a` for the tenant and its PostgreSQL cluster, and `vault` for Vault. The Vault key is `dch/db/postgres` in the `secret/` KV v2 mount (API/policy path `secret/data/dch/db/postgres`) with a `URI` field. Vault's single-pod, single-unseal-key configuration below is **for development only**, not production. ESO materializes the URI in a tenant Kubernetes Secret; do not use this approach if credentials must never exist in Kubernetes.

## Prerequisites

* DCH, the `dch-tenant-a` namespace, the `dch-tenant-postgres` database, its `dch-tenant-postgres-app` Secret, the `test_prompts` table, the `test-postgres-1` connection type, and a DCH user with `dch-read-write` access from the [getting-started guide](../user-guide/getting-started-ocp.md).
* A cluster administrator with `oc`, Helm, and the Vault CLI. Use an approved procedure to handle Vault unseal keys and tokens. Never put them in this repository or a Kubernetes manifest.
* `jq` and the DCH Python SDK with Flight support for the optional query check. The [getting-started guide](../user-guide/getting-started-ocp.md#install-the-python-sdk) covers installation.

## Platform administrator setup: install and configure Vault

If Vault is already installed and unsealed, skip steps 1–3. Confirm that its TLS CA and KV v2 mount are available; use step 4 only if Kubernetes auth has not yet been configured. Never overwrite a shared Vault's existing listener, certificate, mounts, or auth configuration. Then proceed to [ESO and tenant setup](#install-eso-and-configure-the-tenant).

### 1. Request the OpenShift service CA

Create the `vault` namespace and a ConfigMap to receive the CA that signs Vault's OpenShift service certificate. Wait for `service-ca.crt` to be populated.

```sh
oc create namespace vault --dry-run=client -o yaml | oc apply -f -
oc apply -n vault -f - <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: openshift-service-ca.crt
  annotations:
    service.beta.openshift.io/inject-cabundle: "true"
EOF
oc get configmap openshift-service-ca.crt -n vault \
  -o jsonpath='{.data.service-ca\.crt}' | wc -c  # expect nonzero
```

### 2. Install Vault with TLS

Stream the Helm values directly to `helm` with `-f -`; no values file is needed. Chart 0.34.1 uses `server.standalone.config`; `server.extraConfig` is ignored. `global.tlsDisable: false` sets the container address and readiness probe to HTTPS. The fully qualified image avoids an `ErrImagePull` observed when an unqualified `hashicorp/vault` resolved to `registry.connect.redhat.com` on OpenShift.

Install the chart, request a serving certificate for `vault-internal`, and wait for the TLS Secret. **Do not use `helm --wait`** yet: Vault's readiness check fails until it is initialized and unsealed. At the end of this step, `vault-0` should show **`0/1 Running`**; it will not become `1/1 Ready` until step 3. A readiness probe reporting `Initialized: false` and `Sealed: true` is expected here, not an installation failure. This chart's StatefulSet uses `OnDelete`, so use `oc get pod` rather than `oc rollout status`.

```sh
helm upgrade --install vault vault \
  --repo https://helm.releases.hashicorp.com --version 0.34.1 \
  -n vault -f - <<'EOF'
global:
  openshift: true
  tlsDisable: false
injector:
  enabled: false
server:
  image:
    repository: docker.io/hashicorp/vault
    tag: "2.0.4"
  authDelegator:
    enabled: true
  extraVolumes:
    - type: secret
      name: vault-tls
  standalone:
    config: |
      ui = true
      disable_mlock = true
      api_addr = "https://vault-internal.vault.svc:8200"
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
EOF
oc annotate service vault-internal -n vault \
  service.beta.openshift.io/serving-cert-secret-name=vault-tls --overwrite
oc get secret vault-tls -n vault -o name  # wait for issuance
oc get pod vault-0 -n vault              # expect 0/1 Running; proceed to step 3
```

### 3. Initialize and unseal Vault

In one terminal, forward the Vault service:

```sh
oc port-forward -n vault pod/vault-0 8200:8200
```

In another terminal, verify Vault's TLS certificate using the service CA. `VAULT_TLS_SERVER_NAME` matches the certificate when connecting through localhost; **do not** use `VAULT_SKIP_VERIFY`.

```sh
export VAULT_ADDR=https://127.0.0.1:8200
export VAULT_TLS_SERVER_NAME=vault-internal.vault.svc
export VAULT_CACERT="$(mktemp)"
chmod 600 "$VAULT_CACERT"
oc get configmap openshift-service-ca.crt -n vault \
  -o jsonpath='{.data.service-ca\.crt}' > "$VAULT_CACERT"
vault status  # Initialized: false; Sealed: true on a fresh installation
```

Initialize **only once** on a new Vault. Store the unseal key and root token from the init output securely outside Git and Kubernetes; restrict access to the file. Unseal interactively: the CLI requires a TTY for its prompt (piping an unseal key into stdin does not work). Do not put the key in a command argument or paste it into a manifest.

```sh
umask 077
vault operator init -key-shares=1 -key-threshold=1 -format=json > "$HOME/.vault-init.json"
chmod 600 "$HOME/.vault-init.json"
vault operator unseal  # enter the key interactively from the saved init output
vault status           # Initialized: true; Sealed: false
oc get pod vault-0 -n vault  # expect 1/1 Ready
```

With this Shamir setup, **every pod restart seals Vault again**. Unseal with the saved key; never run `vault operator init` a second time. Move the root token and unseal key to your approved secure storage and remove the local file when appropriate.

### 4. Enable KV v2 and Kubernetes authentication

Authenticate the Vault CLI using your approved administrator method. `oc login` authenticates to Kubernetes, **not** Vault, and `vault status` works without a token; `vault auth list` requires a Vault token permitted to read `sys/auth`. If you set `VAULT_NAMESPACE` while following an earlier revision of this guide, unset it: it controls Vault Enterprise namespaces, not the OpenShift namespace. For this **disposable Vault installation only**, load the root token saved during initialization without printing it (do not enable shell tracing). For an existing/shared Vault, use your organization's approved admin credentials instead of a root token. The Helm setting `server.authDelegator.enabled: true` binds Vault's ServiceAccount to `system:auth-delegator` so it can perform Kubernetes TokenReview.

```sh
unset VAULT_NAMESPACE
VAULT_TOKEN="$(jq -er '.root_token' "$HOME/.vault-init.json")" || exit 1
export VAULT_TOKEN
vault token lookup >/dev/null  # confirm CLI authentication without printing token details
```

Check for existing mounts before enabling either one:

```sh
vault secrets list -detailed  # only enable secret/ if absent
vault auth list               # only enable kubernetes/ if absent
# On a fresh Vault only:
vault secrets enable -path=secret -version=2 kv
vault auth enable kubernetes
```

For a **new** Kubernetes auth mount, use the cluster API CA. Do not overwrite an existing Vault installation's Kubernetes auth configuration.

```sh
KUBE_CA="$(mktemp)"
chmod 600 "$KUBE_CA"
oc get configmap kube-root-ca.crt -n vault \
  -o jsonpath='{.data.ca\.crt}' > "$KUBE_CA"
vault write auth/kubernetes/config \
  kubernetes_host=https://kubernetes.default.svc \
  kubernetes_ca_cert="@$KUBE_CA"
rm -f "$KUBE_CA"
```

When finished with Vault administration, stop the port-forward and remove the temporary CA file. Keep the unseal key and token in approved secure storage.

```sh
rm -f "$VAULT_CACERT"
unset VAULT_CACERT VAULT_TLS_SERVER_NAME VAULT_ADDR VAULT_TOKEN
```

## Install ESO and configure the tenant

Follow [Integrate Vault with ESO](../user-guide/integrate-vault-with-eso.md) from **Install ESO** through **Create and test a DCH connection**. That guide covers ESO installation, using the existing Vault `URI` or copying it from `dch-tenant-postgres-app` if absent, a Vault policy and Kubernetes auth role scoped to `dch-tenant-a`, a namespaced `SecretStore` and `ExternalSecret`, DCH Secret-read RBAC, and the `vault-pg-conn-1` connection. Do not duplicate those resources here. The Vault setup above must be unsealed and reachable before beginning the ESO guide.

## End-to-end test

### 1. Verify Vault → ESO → Kubernetes Secret

As a cluster administrator, confirm both resources are Ready and the resulting Secret has a `URI` **key** (never print or decode the value). The key and Secret name match the user guide.

```sh
export TENANT_NAMESPACE=dch-tenant-a
oc wait -n "$TENANT_NAMESPACE" --for=condition=Ready \
  secretstore/eso-vault-postgres --timeout=120s
oc wait -n "$TENANT_NAMESPACE" --for=condition=Ready \
  externalsecret/eso-postgres --timeout=120s
oc get secret eso-postgres-creds -n "$TENANT_NAMESPACE" \
  -o json | jq -r '.data | keys[]'  # expect URI
```

If either resource is not Ready, inspect `oc describe secretstore eso-vault-postgres -n "$TENANT_NAMESPACE"` or `oc describe externalsecret eso-postgres -n "$TENANT_NAMESPACE"`. Check the Vault policy path (`secret/data/dch/db/postgres`), role audience, TLS CA, Kubernetes TokenReview, and ESO's permission to request the tenant ServiceAccount token before retrying.

### 2. Verify DCH readiness and query PostgreSQL

The user guide creates `vault-pg-conn-1` and checks readiness. For a repeat test, obtain a fresh DCH token using [Get User Token](../user-guide/getting-started-ocp.md#get-user-token), then find the connection ID and check readiness again. A 204 response confirms REST and Flight can resolve the ESO-managed Secret and connect to PostgreSQL. `-k` affects only the DCH gateway's certificate, not ESO's TLS connection to Vault.

```sh
export TENANT_NAMESPACE=dch-tenant-a
GW_HOST=$(oc get route data-science-gateway -n openshift-ingress \
  -o jsonpath='{.spec.host}')
GW_URL="https://${GW_HOST}"
connection_id=$(curl -fsSk \
  -H "Authorization: Bearer $user_token" -H "x-tenant-id: $TENANT_NAMESPACE" \
  "${GW_URL}/api/v1alpha1/data/connections" | \
  jq -r '.items[] | select(.resource.name == "vault-pg-conn-1") | .metadata.id' | head -n 1)
test -n "$connection_id" || { echo 'Connection not found'; exit 1; }
curl -ksS -o /dev/null -w 'Readiness: HTTP %{http_code}\n' -X POST \
  -H "Authorization: Bearer $user_token" -H "x-tenant-id: $TENANT_NAMESPACE" \
  "${GW_URL}/api/v1alpha1/data/connections/${connection_id}/readiness"
```

For a stronger end-to-end check, query the `test_prompts` table created in the getting-started guide using the [Flight SDK instructions](../user-guide/getting-started-ocp.md#get-data-from-the-table) with `CONN_NAME=vault-pg-conn-1`. Seeing its rows proves the DCH connection can query the tenant database through the ESO-synced URI without changing DCH.

### 3. Test rotation (optional; coordinate with the credential owner)

Ask the credential owner to update the `URI` at the **same** Vault key to a *different, valid test database URI*. Do not change the `ExternalSecret` or DCH connection. Record only the Kubernetes Secret's encoded value in a shell variable without printing it or enabling `set -x`, then wait for ESO to refresh:

```sh
BEFORE=$(oc get secret eso-postgres-creds -n "$TENANT_NAMESPACE" \
  -o jsonpath='{.data.URI}')
# Ask the owner to update the Vault URI now.
for i in $(seq 1 24); do
  AFTER=$(oc get secret eso-postgres-creds -n "$TENANT_NAMESPACE" \
    -o jsonpath='{.data.URI}')
  [ "$AFTER" != "$BEFORE" ] && break
  sleep 5
done
[ "$AFTER" != "$BEFORE" ] || { echo 'ESO did not sync within 120s'; false; }
unset BEFORE AFTER
```

Allow the existing DCH PostgreSQL pool cache to expire (about 35 seconds in this test), then rerun readiness and the Flight query against the **same** connection. To prove it switched databases rather than merely reconnected, query a marker table present only in the second test database. Do not rotate a shared production database password just for this test.

## Cleanup (optional)

If the connection and `ExternalSecret` were created solely for this test, remove them while ESO is running. Deleting the `ExternalSecret` also removes its `Owner` target Secret. Keep the DCH gateway URL, token, tenant namespace, and connection ID from the test above:

```sh
curl -fsSk -X DELETE -o /dev/null \
  -H "Authorization: Bearer $user_token" -H "x-tenant-id: $TENANT_NAMESPACE" \
  "${GW_URL}/api/v1alpha1/data/connections/${connection_id}"
oc delete externalsecret eso-postgres -n "$TENANT_NAMESPACE"
```

Leave Vault, ESO, and the administrator-managed `SecretStore` in place if they serve other tenants. Do not uninstall ESO while `ExternalSecret` or `SecretStore` resources remain, because their finalizers may block deletion. The [user guide](../user-guide/integrate-vault-with-eso.md) describes which resources are tenant-managed versus platform-managed.
