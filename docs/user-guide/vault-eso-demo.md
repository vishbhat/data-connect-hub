# Vault → ESO demo

## **What is already installed?**

- DCH in `dch-services` and tenant `dch-tenant-a` are ready. The PostgreSQL connection type `test-postgres-1` exists, but a connection is not created yet.
- Hashicorp Vault runs in `vault` and already holds the PostgreSQL `URI` at `secret/data/dch/db/postgres`.
  - In one terminal, forward the Vault pod:
    ```sh
    oc port-forward -n vault pod/vault-0 8200:8200
    ```
  - In another terminal, authenticate to this Vault using the saved init token, verify TLS, and display the Vault secret. This prints the PostgreSQL URI (including its password), so use demo credentials:
    ```sh
    set +x
    unset VAULT_NAMESPACE VAULT_TOKEN
    export VAULT_ADDR=https://127.0.0.1:8200
    export VAULT_TLS_SERVER_NAME=vault-internal.vault.svc
    export VAULT_CACERT="$(mktemp)"
    chmod 600 "$VAULT_CACERT"
    oc get configmap openshift-service-ca.crt -n vault \
      -o jsonpath='{.data.service-ca\.crt}' > "$VAULT_CACERT"
    VAULT_TOKEN="$(jq -er '.root_token' "$HOME/.vault-init.json")" &&
      export VAULT_TOKEN &&
      vault token lookup >/dev/null &&
      vault kv get -mount=secret dch/db/postgres
    ```
- ESO runs in `external-secrets`:
  ```sh
  oc get deployments -n external-secrets
  ```

## **What is not there?**

- The PostgreSQL credential Secret `eso-postgres-creds` does not yet exist in tenant namespace `dch-tenant-a`. ESO will create it.

## **Steps to integrate ESO**

- Create a dedicated ServiceAccount `eso-vault-postgres` in `dch-tenant-a`, a Vault policy, and a Kubernetes auth role. The policy allows reading only `secret/data/dch/db/postgres`, and the role binds it to this tenant ServiceAccount.
  ```sh
  oc get sa eso-vault-postgres -n dch-tenant-a
  vault policy list
  vault policy read eso-dch-tenant-a-postgres-read
  vault list auth/kubernetes/role
  vault read auth/kubernetes/role/eso-dch-tenant-a-postgres
  ```
- Create a `SecretStore` in tenant namespace `dch-tenant-a`. It tells ESO how to authenticate to Vault; it does not create a Kubernetes Secret by itself.
  ```sh
  oc get secretstore eso-vault-postgres -n dch-tenant-a -o yaml
  ```
- Create an `ExternalSecret` in tenant namespace `dch-tenant-a` to tell ESO which Vault secret to pull into the namespace:
  ```sh
  oc apply -f - <<'EOF'
  apiVersion: external-secrets.io/v1
  kind: ExternalSecret
  metadata:
    name: eso-postgres
    namespace: dch-tenant-a
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
  oc get externalsecret eso-postgres -n dch-tenant-a
  oc get secret eso-postgres-creds -n dch-tenant-a
  oc get secret eso-postgres-creds -n dch-tenant-a -o json | jq -r '.data '
  ```
- Grant DCH REST and Flight permission to `get` only `eso-postgres-creds` in `dch-tenant-a` through a tenant Role and RoleBinding. These are already configured for the demo; verify that both service accounts can read the Secret:
  ```sh
  oc get role dch-eso-postgres-read -n dch-tenant-a
  oc get rolebinding dch-eso-postgres-read -n dch-tenant-a
  REST_SA=$(oc get deployment dch-rest-service -n dch-services \
    -o jsonpath='{.spec.template.spec.serviceAccountName}')
  FLIGHT_SA=$(oc get deployment dch-default-dataconnectservice-flight -n dch-services \
    -o jsonpath='{.spec.template.spec.serviceAccountName}')
  oc auth can-i get secret/eso-postgres-creds -n dch-tenant-a \
    --as="system:serviceaccount:dch-services:${REST_SA}"
  oc auth can-i get secret/eso-postgres-creds -n dch-tenant-a \
    --as="system:serviceaccount:dch-services:${FLIGHT_SA}"
  ```
- Create a DCH connection using `eso-postgres-creds`, then get the connection. DCH must already have permission to read this Secret in `dch-tenant-a`.
  ```sh
  SA_ISSUER=$(oc get authentication cluster -o jsonpath='{.spec.serviceAccountIssuer}')
  SA_ISSUER=${SA_ISSUER:-https://kubernetes.default.svc}
  user_token=$(oc create token dch-test-user -n dch-tenant-a \
    --audience="$SA_ISSUER" --duration=1h)
  test -n "$user_token" && echo 'DCH token obtained'
  TENANT_NAMESPACE=dch-tenant-a
  GW_HOST=$(oc get route data-science-gateway -n openshift-ingress \
    -o jsonpath='{.spec.host}')
  GW_URL="https://${GW_HOST}"
  type_id=$(curl -fsSk -H "Authorization: Bearer $user_token" \
    -H "x-tenant-id: $TENANT_NAMESPACE" \
    "${GW_URL}/api/v1alpha1/data/connection-types" | \
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
  curl -fsSk -H "Authorization: Bearer $user_token" \
    -H "x-tenant-id: $TENANT_NAMESPACE" \
    "${GW_URL}/api/v1alpha1/data/connections/${connection_id}" | jq .
  ```
