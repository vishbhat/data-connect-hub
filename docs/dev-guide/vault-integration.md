# Vault Integration

## Background

Data Connect Hub (DCH) currently supports reading connector credentials only from Kubernetes
Secrets. While this works for credentials managed in Kubernetes, organizations may instead manage
credentials in an external secret manager and may not want to duplicate those credentials in
Kubernetes Secrets.

This feature extends DCH credential resolution to support external secret managers such as HashiCorp
Vault. DCH can resolve credentials from the external provider when they are needed while continuing
to support existing Kubernetes Secret-backed connections.

## Objective

Establish a secure, extensible approach for resolving connector credentials from an external secret
manager without first materializing them as Kubernetes Secrets.

## Scope

The general secret manager integration covers:

- Resolving connector credentials from an external secret manager at runtime.
- Preserving existing Kubernetes Secret references and behavior.
- Supporting provider-specific credential references and shared service configuration.
- Keeping credential payloads connector-agnostic so their fields can satisfy the selected connection
type schema.
- Preventing resolved credentials and provider authentication tokens from being persisted in
connection metadata.
- Enforcing tenant isolation, authorization, secure transport, and sensitive data redaction during
credential resolution.
- Avoiding the duplication or export of externally managed credentials into Kubernetes Secrets.

Specific providers can implement a subset of secret-manager capabilities and authentication methods.
Provider-specific behavior and limitations must be identified by each implementation.

## Vault Proof of Concept

As part of this feature, a proof of concept validates the secret manager design using HashiCorp
Vault. The Vault proof of concept is limited to:

- HashiCorp Vault KV v2 static secrets.
- Vault Kubernetes authentication.
- PostgreSQL as the demonstration connector.
- Existing Kubernetes Secret references remaining supported.
- Optional KV secret version selection.
- Preventing Vault credentials from being exported to Kubernetes Secrets.

Dynamic database secrets, lease renewal for generated database credentials, additional secret
providers, and a first-class Vault controller API are outside the scope of the Vault proof of
concept.

## HashiCorp Vault Overview

HashiCorp Vault is a secrets management tool. It centralizes the storage and distribution of
credentials such as database passwords, API keys, and certificates, and controls access to them
through authentication and policy. Applications retrieve a secret from Vault over an authenticated
TLS API at the moment it is needed instead of holding a long-lived copy in configuration files or
Kubernetes Secrets.

Vault exposes several concepts that DCH relies on:

- **Secrets engines**: pluggable components that store or generate secrets at a mount point. This
  proof of concept uses the key/value version 2 (KV v2) engine, which stores static secrets as
  key-value pairs under a path such as `secret/dch/opendatahub/postgres/demo`.
- **Versioning**: KV v2 keeps a history of every write to a secret path. Readers can request the
  latest version or pin a specific version, and older versions can be rolled back or soft-deleted.
  DCH maps the optional `version` field of a Vault reference to this capability.
- **Authentication methods**: pluggable components that verify a caller's identity at a mount
  point and return a Vault token. This proof of concept uses the Kubernetes authentication method,
  which accepts a pod's service-account JWT and exchanges it for a Vault token after validating it
  against the cluster.
- **Tokens and leases**: every authenticated caller receives a token with a time-to-live and an
  attached set of policies. Tokens expire and must be renewed or re-issued, so a compromised token
  is useful only for a limited window.
- **Policies**: named rule sets that grant capabilities, such as `read`, on Vault paths. A caller
  can only access the paths explicitly allowed by the policies attached to its token.

### Examples

Writing a secret with the Vault CLI creates a new version at the path. Each write to the same path
increments the version number:

```console
$ vault kv put -mount=secret dch/opendatahub/postgres/demo \
    URI='postgresql://user:password@postgres.example:5432/database?sslmode=require'

Key              Value
---              ----
created_time     2025-01-15T10:30:00Z
destruction_time n/a
version          1
```

Reading the path returns the latest version by default. When a credential is rotated, another
`vault kv put` to the same path creates version 2, and subsequent reads return the new value:

```console
$ vault kv get -mount=secret dch/opendatahub/postgres/demo

====== Data ======
Key     Value
---     -----
URI     postgresql://user:password@postgres.example:5432/database?sslmode=require
```

A reader can also pin an exact version, for example to verify the previous value after a rotation.
DCH exposes the same capability through the optional `version` field of a Vault reference:

```console
$ vault kv get -mount=secret -version=1 dch/opendatahub/postgres/demo
```

A policy is a named set of path rules. The `dch-read` policy used by this proof of concept grants
read access only to the DCH tenant hierarchy:

```hcl
path "secret/data/dch/*" {
  capabilities = ["read"]
}
```

When a DCH service authenticates, it sends its service-account JWT to the Kubernetes
authentication endpoint and receives a short-lived Vault token carrying the attached policies:

```console
$ vault write auth/kubernetes/login role=dch jwt="$JWT"

Key                                  Value
---                                  ----
token                                hvs.XXXXXXXXXXXXXXXX
token_duration                       1h
token_policies                       ["default" "dch-read"]
```

The returned token can then be used to read only the paths that `dch-read` allows, and it expires
when its lease ends.

The Kubernetes authentication method and KV v2 versioning are what allow DCH to authenticate its
services with their existing workload identities and to read the exact secret version a connection
requests.

## Design And Security

Before resolving credentials, DCH verifies that the requested connection belongs to the authorized
tenant. It then normalizes the relative path supplied by the connection's Vault reference and
combines it with the configured tenant prefix and the authenticated tenant:

```text
dch/{tenant_id}/postgres/demo
```

Absolute paths, traversal, encoded traversal, empty paths, and paths that can escape the tenant
prefix are rejected.

Existing Kubernetes authentication and authorization remain the first authorization layer,
including the Flight service tenant-scoped SubjectAccessReview. For this proof of concept, REST and
Flight use service-level Vault roles that can read the configured `dch/*` hierarchy. DCH enforces
tenant isolation through path construction and authorization, making it the trusted multitenant
security boundary.

Production deployments should evaluate tenant-scoped Vault roles and policies, tenant-specific
workload identities, or Vault Enterprise namespaces for Vault-enforced tenant isolation.

The implementation must:

- Use TLS for every Vault request.
- Never store Vault tokens or resolved credentials in connection metadata.
- Never log service-account JWTs, Vault tokens, secret values, or Vault response bodies.
- Reject references containing both source types or neither source type.
- Cache Vault client tokens until shortly before expiry, but never cache secret payloads.
- Re-read the projected service-account token during authentication and retry authentication once
after an authorization failure.
- Reject export requests for Vault-backed connections, preventing externally managed credentials
from being copied into Kubernetes Secrets.

The PostgreSQL connector cache remains unchanged for this proof of concept. An unversioned secret
rotation can take up to the configured connector cache TTL, currently about 30 seconds, before a
new client resolves the changed value.

## Vault Configuration

The Vault deployment must provide a KV v2 secrets engine and Kubernetes authentication. The KV and
authentication mount names must match the DCH configuration described in
[DCH Configuration](#dch-configuration). Vault
must also be reachable from the DCH REST and Flight services over TLS. When Vault uses Kubernetes
authentication, it must be able to access the cluster TokenReview API.

The examples below use a KV v2 mount named `secret`, a Kubernetes authentication mount named
`kubernetes`, a role named `dch`, and a tenant prefix named `dch`. Replace the placeholders with
values for the target cluster.

### Create The Vault Policy And Kubernetes Role

Create a policy scoped to the DCH tenant hierarchy. Because the proof of concept uses KV v2, the
policy path must include the `/data/` segment.

```console
vault policy write dch-read - <<'EOF'
path "secret/data/dch/*" {
  capabilities = ["read"]
}
EOF
```

Bind the policy to the REST and Flight service accounts through a Vault Kubernetes authentication
role:

```console
DCS_NAME=default-dataconnectservice

vault write auth/kubernetes/role/dch \
  bound_service_account_names=dch-rest-service-sa,dch-${DCS_NAME}-flight-sa \
  bound_service_account_namespaces=<namespace> \
  audience=vault \
  policies=dch-read
```

Controller-managed deployments use `dch-rest-service-sa` and
`dch-<DataConnectService-name>-flight-sa`. For the default resource, the Flight service account is
`dch-default-dataconnectservice-flight-sa`. Verify the rendered names in the target namespace before
creating the role.

### Store Connector Credentials

Store credentials under the configured tenant hierarchy. For the default configuration and tenant
`opendatahub`, the connection reference `postgres/demo` resolves to
`secret/data/dch/opendatahub/postgres/demo`.

```console
vault kv put -mount=secret dch/opendatahub/postgres/demo \
  URI='postgresql://user:password@postgres.example:5432/database?sslmode=require'
```

The secret fields must match the schema of the connection type that will use them. The PostgreSQL
example stores the database URL as `URI`.

## DCH Configuration

Configure `spec.vault` on the `DataConnectService`. This is shared service configuration used by
every Vault-backed connector; connector credentials remain only in Vault.

```yaml
apiVersion: dataconnecthub.opendatahub.io/v1alpha1
kind: DataConnectService
metadata:
  name: default-dataconnectservice
  namespace: <namespace>
spec:
  vault:
    address: https://vault.example:8200
    role: dch
    kvMount: secret
    authMount: kubernetes
    tenantPrefix: dch
    caConfigMap:
      name: vault-ca
      key: ca.crt
```

The configuration identifies the Vault address, Kubernetes authentication role, KV and
authentication mounts, tenant path prefix, and optional CA bundle. These values must correspond to
the policy, role, and mounts configured in Vault.

When Vault uses a private CA, create the referenced ConfigMap before applying the
`DataConnectService`:

```console
oc create configmap vault-ca \
  --from-file=ca.crt=vault-ca.crt \
  -n <namespace>
```

The controller propagates `spec.vault` to the REST and Flight ConfigMaps and updates both Deployment
pod templates. Each service receives a projected service-account token at
`/var/run/secrets/vault/token` with the `vault` audience. When a custom CA is configured, the
controller mounts it at `/etc/tls/vault/ca.crt`.

When `spec.vault` is omitted, both services continue to use Kubernetes Secret references. Restart or
wait for the REST and Flight deployments to roll out after changing the Vault configuration. If a
different projected token audience is used, update the Vault role and deployment configuration
together.

## Credential Model And Connection Usage

The `credentials_ref` field supports either a Kubernetes Secret reference or a Vault reference.
Exactly one reference type is required.

A Vault reference contains a relative secret path and can optionally select a KV secret version:

```json
{
  "credentials_ref": {
    "vault": {
      "path": "postgres/demo",
      "version": 3
    }
  }
}
```

Existing Kubernetes Secret references remain supported:

```json
{
  "credentials_ref": {
    "secret": "postgres-credentials"
  }
}
```

The Vault address, KV mount, authentication mount, role, tenant prefix, and TLS configuration come
from the `DataConnectService`; clients do not include them in a connection. The authenticated
request and stored connection metadata determine the tenant, so users also do not provide a tenant
in the Vault reference. DCH combines the configured prefix, authenticated tenant, and relative path
before reading the secret:

```text
dch/{tenant_id}/postgres/demo
```

The Vault secret is connector-agnostic, and its fields must match the selected connection type
schema. For PostgreSQL, store the database URL as `URI`. An S3 secret can instead contain
`ACCESS_KEY_ID`, `SECRET_ACCESS_KEY`, and `REGION`.

### Create And Use A Vault-Backed Connection

Create a connection containing only the Vault reference. Replace the connection type ID with the
PostgreSQL connection type installed in the cluster.

```json
{
  "name": "vault-postgres",
  "data_connection_type_id": "<postgres-connection-type-id>",
  "format": "tabular",
  "credentials_ref": {
    "vault": {
      "path": "postgres/demo"
    }
  },
  "properties": {}
}
```

Create the connection and run a readiness check:

```console
curl --fail-with-body \
  -H 'Content-Type: application/json' \
  -H 'X-Tenant-Id: opendatahub' \
  --data @vault-postgres.json \
  https://<dch-api>/api/v1alpha1/data/connections

curl --fail-with-body -X POST \
  -H 'X-Tenant-Id: opendatahub' \
  https://<dch-api>/api/v1alpha1/data/connections/<connection-id>/readiness
```

Use the normal Flight SQL client with the returned connection ID to execute a query.
[Vault cluster test](vault-cluster-test.md) describes deployment, validation, troubleshooting, and
rollback steps.

## Implementation

The proof of concept changes the shared credential model, API schema, and Python SDK while
preserving persisted Kubernetes Secret references. Connections are stored as JSON documents, so no
metadata database migration is required.

`kube-utils` provides a small Vault client using the existing `reqwest` dependency. It authenticates
with Vault's Kubernetes authentication endpoint and reads KV v2 values. A composite credential
resolver routes Kubernetes references to the existing Secret store and Vault references to this
client. Flight uses the resolver when constructing connector clients; REST uses it for readiness
checks.

## Validate Rotation And Isolation

Update the Vault `URI`, wait longer than the connector cache TTL, then run the readiness check and a
Flight query again. Confirm that the connection succeeds without being recreated. Also verify that:

- Vault-backed connections cannot be exported to Kubernetes Secrets.
- A malformed or traversal path is rejected.
- An unauthorized tenant or Vault path fails without exposing sensitive data.
- Existing Kubernetes Secret-backed connections continue to work.
