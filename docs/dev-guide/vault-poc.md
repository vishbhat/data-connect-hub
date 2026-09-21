# Direct Vault integration POC

## Objective

Prove that Data Connect Hub can resolve PostgreSQL credentials directly from
HashiCorp Vault without first materializing them as a Kubernetes Secret.

## Scope

- HashiCorp Vault KV v2 secrets
- Vault Kubernetes authentication
- PostgreSQL as the demonstration connector
- Existing Kubernetes Secret references remain supported
- Optional KV secret version selection
- Vault credentials are not exported to Kubernetes Secrets

Dynamic secrets, lease renewal for generated database credentials, additional
secret providers, and a first-class Vault controller API are outside this POC.

## Proposed API

Add an alternative Vault reference to `credentials_ref`:

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

Existing references remain valid:

```json
{
  "credentials_ref": {
    "secret": "postgres-credentials"
  }
}
```

Exactly one reference type must be supplied. Vault paths will be resolved under
a configured tenant prefix such as `dch/{tenant_id}/{path}`. The Vault address,
KV mount, authentication mount, role, and TLS configuration belong in service
configuration rather than individual connection records.

## Multitenancy

The tenant identifier must come from the authenticated request and the stored
connection metadata, not from the Vault reference supplied by the user. Before
resolving credentials, the service must verify that the requested connection
belongs to the authorized tenant.

Users provide only a relative path such as `postgres/demo`. The resolver
normalizes and combines it with the configured root and tenant identifier:

```text
dch/{tenant_id}/postgres/demo
```

Absolute paths, encoded traversal, and references that can escape the tenant
prefix must be rejected. Existing Kubernetes authentication and authorization,
including the Flight service's tenant-scoped SubjectAccessReview, remain the
first authorization layer.

For the POC, REST and Flight will each use a service-level Vault role permitted
to read the configured `dch/*` hierarchy. Tenant isolation inside that hierarchy
is therefore enforced by Data Connect Hub path construction and authorization.
This is sufficient to validate the integration but makes each service a trusted
multitenant security boundary.

For production, evaluate tenant-scoped Vault roles and policies, with Vault
tokens cached by tenant and role, or Vault Enterprise namespaces. A shared
service account that can authenticate to every tenant role still trusts Data
Connect Hub to select the correct role; strict Vault-enforced isolation requires
tenant-specific workload identities or an equivalent identity-brokering design.

## Implementation plan

1. Extend the shared credential model, API schema, and Python SDK with the Vault
   reference while preserving existing persisted Secret references. No metadata
   database migration should be necessary because connections are stored as
   JSON documents.
2. Add a small Vault client to `kube-utils` using the existing `reqwest`
   dependency. It will perform Kubernetes authentication and read KV v2 values.
3. Implement a composite credential resolver that routes Kubernetes references
   to the current Secret store and Vault references to the Vault client.
4. Cache Vault client tokens until shortly before expiry, but never cache secret
   payloads. Re-read the projected service-account token when authenticating and
   retry authentication once after an authorization failure.
5. Use the resolver in Flight when constructing connector clients and in REST
   readiness checks. Reject export requests for Vault-backed connections so
   externally managed credentials are not copied into Kubernetes Secrets.
6. Configure Vault independently for REST and Flight. Mount a projected
   service-account token with the Vault audience and the Vault CA certificate
   into both services.
7. Configure Vault roles and policies for the REST and Flight service accounts.
   Policies must restrict each service to the expected tenant path hierarchy.
8. Preserve the existing connector cache behavior for the POC. Unversioned
   secret rotations may take up to the configured connector cache TTL, currently
   approximately 30 seconds, before a new client resolves the updated value.

## Security requirements

- Use TLS for all Vault requests.
- Do not store Vault tokens or resolved credentials in connection metadata.
- Do not include service-account JWTs, Vault tokens, secret values, or Vault
  response bodies in logs or client-facing errors.
- Reject empty paths, path traversal, references containing both source types,
  and references containing neither source type.
- Enforce tenant path isolation in both the resolver and Vault policies.

## Validation

Add focused tests for:

- Existing Kubernetes Secret reference compatibility
- Vault reference serialization and validation
- Kubernetes login request and KV v2 response parsing
- Token reuse, expiry, and re-authentication
- Tenant path construction and traversal rejection
- Rejection of cross-tenant connection and credential access
- Unauthorized, missing, malformed, and non-string Vault values
- REST readiness and Flight connector creation through a fake resolver
- Redaction of sensitive values from errors and logs

Run an end-to-end demonstration that:

1. Stores PostgreSQL credentials at a tenant-scoped Vault KV v2 path.
2. Creates a data connection containing only the Vault reference.
3. Completes readiness validation and executes a query.
4. Rotates the PostgreSQL password and updates the Vault secret.
5. Confirms that queries reconnect with the new credentials after connector
   cache expiry without recreating the data connection.
6. Confirms that an unauthorized tenant or Vault path fails without exposing
   sensitive information.

## Success criteria

- PostgreSQL credentials are resolved directly from Vault.
- No Kubernetes Secret contains the PostgreSQL credentials.
- Existing Kubernetes Secret-backed connections continue to work.
- Credential rotation works without recreating the connection.
- Vault access is scoped by tenant and service identity.
- Authentication and resolution failures do not leak credentials.
