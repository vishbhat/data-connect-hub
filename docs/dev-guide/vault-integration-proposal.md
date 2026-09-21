# Vault Integration

## Scope

- HashiCorp Vault KV v2 static secrets.
- Vault Kubernetes authentication.
- Existing Kubernetes Secret references remaining supported.

## HashiCorp Vault Overview

- HashiCorp Vault is a secrets management tool.
- It centralizes the storage and distribution of credentials such as database passwords, API keys, and certificates.
- It controls access to those credentials through authentication and policies.

### Examples

Writing a secret with the Vault CLI creates a new version at the path. Each write to the same path increments the version number:

```console
$ vault kv put -mount=secret db/postgres \
    URI='postgresql://user:password@postgres.example:5432/database?sslmode=require'

Key              Value
---              ----
created_time     2025-01-15T10:30:00Z
destruction_time n/a
version          1
```

Reading the path returns the latest version by default. When a credential is rotated, another `vault kv put` to the same path creates version 2, and subsequent reads return the new value:

```console
$ vault kv get -mount=secret db/postgres

====== Data ======
Key     Value
---     -----
URI     postgresql://user:password@postgres.example:5432/database?sslmode=require
```

A policy is a named set of path rules. For example, the `db-read` policy below grants read access only to paths under `db/`:

```hcl
path "db/*" {
  capabilities = ["read"]
}
```

A policy is attached to a workload through a Kubernetes auth role, which binds it to one or more service accounts:

```console
$ vault write auth/kubernetes/role/db-reader \
    bound_service_account_names=my-app \
    bound_service_account_namespaces=my-namespace \
    policies=db-read \
    ttl=1h

Success! Data written to: auth/kubernetes/role/db-reader
```

When the workload authenticates, it sends its service-account JWT to the Kubernetes authentication endpoint and receives a short-lived Vault token carrying the attached policies:

```console
$ vault write auth/kubernetes/login role=db-reader jwt="$JWT"

Key                                  Value
---                                  ----
token                                hvs.XXXXXXXXXXXXXXXX
token_duration                       1h
token_policies                       ["default" "db-read"]
```

The returned token can then be used to read only the paths that `db-read` allows, and it expires when its lease ends.

The Kubernetes authentication method is what allows DCH to authenticate its services with their existing workload identities.

## Possible Solution to integrate with DCH

### Grant vault secret access to DCH

1. Create a Vault read policy for DCH

```console
vault policy write dch-read - <<'EOF'
path "db/postgres/*" {
  capabilities = ["read"]
}
EOF
```

2. Bind DCH service accounts to the Vault policy

```console
vault write auth/kubernetes/role/dch \
  bound_service_account_names=dch-rest-service-sa,dch-default-dataconnectservice-flight-sa \
  bound_service_account_namespaces=<namespace> \
  audience=vault \
  policies=dch-read
```

3. Create a connection

```json
{
  "name": "vault-postgres",
  "data_connection_type_id": "<postgres-connection-type-id>",
  "format": "tabular",
  "credentials_ref": {
    "vault": {
      "path": "db/postgres"
    }
  },
  "properties": {}
}
```

With Kubernetes Secrets, a tenant in one namespace cannot refer to a secret in another namespace, because DCH verifies that the tenant has access to the secret. With Vault, however, DCH cannot verify that a secret belongs to a tenant.

### Solution

DCH can verify that a tenant has access to a secret by using path-based access control, where each secret lives under its tenant's path, e.g. `dch/{tenant_id}/postgres`.

### Steps To Achieve This

1. Vault Admin recreates the existing secret, e.g. `db/postgres`, under `dch/{tenant_id}/postgres`.
2. Vault Admin adds a Vault policy granting DCH access to secrets under `dch/`. This is a one-time step.
3. The DCH service composes this path when resolving a connection's reference.

### Drawbacks

1. One-time overhead of recreating existing secrets.
