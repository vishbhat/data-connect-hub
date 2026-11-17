# Python SDK FIPS status with Data Connect Hub

The [Data Connect Hub Python SDK](../../sdk/python/README.md) is installed in the client's environment, not in the DCH service image. The SDK is **not a FIPS-validated cryptographic module**. Its suitability for a FIPS-restricted deployment depends on the TLS implementations actually used by the client and the services, as well as their configuration.

## REST client

The SDK's REST client has been reported working against TLS-enabled DCH REST services deployed on both FIPS-mode and non-FIPS OpenShift clusters, from FIPS-enabled and non-FIPS client environments ([RHOAIENG-102742](https://redhat.atlassian.net/browse/RHOAIENG-102742)). This is a **functional interoperability result**, not proof that the client or service uses FIPS-validated cryptography.

REST HTTPS uses `httpx` and Python's `ssl` module. A client that requires FIPS-approved cryptography must use a Python runtime backed by an applicable validated module in its approved mode; the client environment and its TLS library must be assessed for the specific platform and package versions. Running DCH on a FIPS-mode cluster does **not** configure remote SDK clients for FIPS. Likewise, running a UBI container on a FIPS-mode node does not, by itself, prove approved-mode operation of every library used in that container.

REST verifies server certificates by default. `httpx` normally trusts the `certifi` CA bundle; if the DCH endpoint uses an OpenShift service certificate or other private CA, supply the issuing CA's PEM file with `DataConnectClient(ca_cert="/path/to/cluster-ca.pem")`, or configure an appropriate trust store. The CA must be available to the SDK client, and the endpoint hostname must match the certificate. `insecure=True` disables peer certificate verification and should not be used to address a FIPS or CA-trust issue. Choosing a CA bundle affects trust, **not** which cryptographic module performs TLS.

## Flight SQL client

The reported REST result does **not** cover Flight SQL. The standard PyPI `[flight]` dependencies inspected for the SDK bundle their own TLS implementations rather than delegating Flight TLS to the host's validated OpenSSL module. Consequently, those wheels are **not suitable where Flight TLS must use a FIPS-validated module**. See the [SDK README](../../sdk/python/README.md#fips-environments) for the inspected versions and scope of this finding.

## End-to-end posture

A functional SDK-to-DCH connection, including one made from a FIPS-enabled client, does **not** establish end-to-end FIPS compliance. Client-side TLS and the DCH REST service, Flight service, controller, and their dependencies have separate cryptographic requirements. The currently shipped DCH service images do not claim that all cryptography uses FIPS 140-3 validated modules; a FIPS-mode cluster alone does not change that ([connector status](connectors.md)). Evaluate the complete deployment before making a compliance claim.
