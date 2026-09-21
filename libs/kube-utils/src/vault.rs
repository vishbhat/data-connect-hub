use async_trait::async_trait;
use commons::api::connections::{DataConnectionResource, VaultCredentialsRef};
use commons::api::connector::CredentialsResolver;
use commons::api::errors::ConnectorError;
use commons::api::storage::SecretStore;
use reqwest::{Client, StatusCode};
use serde::Deserialize;
use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Mutex;

const DEFAULT_TOKEN_FILE: &str = "/var/run/secrets/vault/token";

#[derive(Clone, Debug, Deserialize)]
pub struct VaultConfig {
    pub address: String,
    #[serde(rename = "kv-mount", default = "default_kv_mount")]
    pub kv_mount: String,
    #[serde(rename = "auth-mount", default = "default_auth_mount")]
    pub auth_mount: String,
    pub role: String,
    #[serde(rename = "token-file", default = "default_token_file")]
    pub token_file: String,
    #[serde(rename = "ca-cert")]
    pub ca_cert: Option<String>,
    #[serde(rename = "tenant-prefix", default = "default_tenant_prefix")]
    pub tenant_prefix: String,
}

fn default_kv_mount() -> String {
    "secret".to_string()
}
fn default_auth_mount() -> String {
    "kubernetes".to_string()
}
fn default_token_file() -> String {
    DEFAULT_TOKEN_FILE.to_string()
}
fn default_tenant_prefix() -> String {
    "dch".to_string()
}

impl VaultConfig {
    pub fn validate(&self) -> Result<(), String> {
        if !self.address.starts_with("https://") {
            return Err("vault.address must use https".to_string());
        }
        if self.role.is_empty()
            || self.kv_mount.is_empty()
            || self.auth_mount.is_empty()
            || self.tenant_prefix.is_empty()
        {
            return Err("vault role, mounts, and tenant prefix must not be empty".to_string());
        }
        Ok(())
    }
}

#[derive(Clone)]
struct VaultToken {
    value: String,
    expires_at: Instant,
}

pub struct VaultClient {
    config: VaultConfig,
    client: Client,
    token: Mutex<Option<VaultToken>>,
}

impl VaultClient {
    pub fn new(config: VaultConfig) -> Result<Self, ConnectorError> {
        config.validate().map_err(ConnectorError::ConfigError)?;
        let mut builder = Client::builder();
        if let Some(path) = &config.ca_cert {
            let pem = std::fs::read(path)
                .map_err(|_| ConnectorError::ConfigError("unable to read Vault CA certificate".to_string()))?;
            let certificate = reqwest::Certificate::from_pem(&pem)
                .map_err(|_| ConnectorError::ConfigError("invalid Vault CA certificate".to_string()))?;
            builder = builder.add_root_certificate(certificate);
        }
        let client = builder
            .build()
            .map_err(|_| ConnectorError::ConfigError("unable to create Vault client".to_string()))?;
        Ok(Self {
            config,
            client,
            token: Mutex::new(None),
        })
    }

    async fn token(&self) -> Result<String, ConnectorError> {
        let mut cached = self.token.lock().await;
        if let Some(token) = cached
            .as_ref()
            .filter(|token| token.expires_at > Instant::now() + Duration::from_secs(30))
        {
            return Ok(token.value.clone());
        }

        let jwt = tokio::fs::read_to_string(&self.config.token_file)
            .await
            .map_err(|_| ConnectorError::ConnectionError("unable to read Vault service account token".to_string()))?;
        let url = format!(
            "{}/v1/auth/{}/login",
            self.config.address.trim_end_matches('/'),
            self.config.auth_mount
        );
        let response = self
            .client
            .post(url)
            .json(&serde_json::json!({"role": self.config.role, "jwt": jwt.trim()}))
            .send()
            .await
            .map_err(|_| ConnectorError::ConnectionError("Vault authentication request failed".to_string()))?;
        if !response.status().is_success() {
            return Err(ConnectorError::ConnectionError(
                "Vault authentication failed".to_string(),
            ));
        }
        let login: LoginResponse = response
            .json()
            .await
            .map_err(|_| ConnectorError::ConnectionError("invalid Vault authentication response".to_string()))?;
        let ttl = login.auth.lease_duration.max(60) as u64;
        *cached = Some(VaultToken {
            value: login.auth.client_token.clone(),
            expires_at: Instant::now() + Duration::from_secs(ttl),
        });
        Ok(login.auth.client_token)
    }

    async fn clear_token(&self) {
        *self.token.lock().await = None;
    }

    pub async fn read_secret(
        &self,
        tenant_id: &str,
        reference: &VaultCredentialsRef,
    ) -> Result<HashMap<String, String>, ConnectorError> {
        let path = vault_path(&self.config.tenant_prefix, tenant_id, reference)?;
        for attempt in 0..2 {
            let token = self.token().await?;
            let url = format!(
                "{}/v1/{}/data/{}",
                self.config.address.trim_end_matches('/'),
                self.config.kv_mount,
                path
            );
            let mut request = self.client.get(url).header("X-Vault-Token", token);
            if let Some(version) = reference.version {
                request = request.query(&[("version", version)]);
            }
            let response = request
                .send()
                .await
                .map_err(|_| ConnectorError::ConnectionError("Vault secret request failed".to_string()))?;
            if matches!(response.status(), StatusCode::FORBIDDEN | StatusCode::UNAUTHORIZED) && attempt == 0 {
                self.clear_token().await;
                continue;
            }
            if response.status() == StatusCode::NOT_FOUND {
                return Err(ConnectorError::NotFound("Vault secret not found".to_string()));
            }
            if !response.status().is_success() {
                return Err(ConnectorError::ConnectionError(
                    "Vault secret cannot be read".to_string(),
                ));
            }
            let secret: KvV2Response = response
                .json()
                .await
                .map_err(|_| ConnectorError::ConnectionError("invalid Vault secret response".to_string()))?;
            return secret
                .data
                .data
                .into_iter()
                .map(|(key, value)| {
                    value.as_str().map(|value| (key, value.to_string())).ok_or_else(|| {
                        ConnectorError::ConnectionError("Vault secret contains a non-string value".to_string())
                    })
                })
                .collect();
        }
        Err(ConnectorError::ConnectionError(
            "Vault secret cannot be read".to_string(),
        ))
    }
}

pub struct CompositeCredentialsResolver {
    secret_store: Arc<dyn SecretStore + Send + Sync>,
    vault: Option<Arc<VaultClient>>,
}

impl CompositeCredentialsResolver {
    pub fn new(
        secret_store: Arc<dyn SecretStore + Send + Sync>,
        vault: Option<VaultConfig>,
    ) -> Result<Self, ConnectorError> {
        let vault = vault.map(VaultClient::new).transpose()?.map(Arc::new);
        Ok(Self { secret_store, vault })
    }

    pub async fn resolve(
        &self,
        connection: &DataConnectionResource,
    ) -> Result<HashMap<String, String>, ConnectorError> {
        connection
            .resource
            .credentials_ref
            .validate()
            .map_err(ConnectorError::InvalidRequest)?;
        let tenant_id = connection
            .metadata
            .tenant_id
            .as_deref()
            .ok_or_else(|| ConnectorError::ConnectionError("connection tenant is required".to_string()))?;
        match &connection.resource.credentials_ref {
            reference if reference.secret.is_some() => {
                let name = reference.secret.as_deref().expect("validated secret reference");
                self.secret_store
                    .get_secret(tenant_id, name)
                    .await
                    .map(|secret| secret.properties)
                    .map_err(|_| ConnectorError::ConnectionError("Kubernetes secret cannot be read".to_string()))
            },
            reference if reference.vault.is_some() => {
                let vault = self
                    .vault
                    .as_ref()
                    .ok_or_else(|| ConnectorError::ConfigError("Vault is not configured".to_string()))?;
                vault
                    .read_secret(tenant_id, reference.vault.as_ref().expect("validated Vault reference"))
                    .await
            },
            _ => Err(ConnectorError::ConnectionError(
                "credentials reference is invalid".to_string(),
            )),
        }
    }
}

#[async_trait]
impl CredentialsResolver for CompositeCredentialsResolver {
    async fn resolve(&self, connection: &DataConnectionResource) -> Result<HashMap<String, String>, ConnectorError> {
        Self::resolve(self, connection).await
    }
}

fn vault_path(prefix: &str, tenant_id: &str, reference: &VaultCredentialsRef) -> Result<String, ConnectorError> {
    if tenant_id.is_empty() || tenant_id.contains('/') || tenant_id.contains('%') {
        return Err(ConnectorError::ConnectionError("invalid connection tenant".to_string()));
    }
    if prefix
        .split('/')
        .any(|segment| segment.is_empty() || matches!(segment, "." | ".."))
    {
        return Err(ConnectorError::ConfigError("invalid Vault tenant prefix".to_string()));
    }
    // CredentialsRef validates relative paths before they can reach the resolver.
    if reference
        .path
        .split('/')
        .any(|segment| segment.is_empty() || matches!(segment, "." | "..") || segment.contains('%'))
    {
        return Err(ConnectorError::ConnectionError(
            "invalid Vault credential path".to_string(),
        ));
    }
    Ok(format!("{prefix}/{tenant_id}/{}", reference.path))
}

#[derive(Deserialize)]
struct LoginResponse {
    auth: LoginAuth,
}
#[derive(Deserialize)]
struct LoginAuth {
    client_token: String,
    #[serde(default)]
    lease_duration: i64,
}
#[derive(Deserialize)]
struct KvV2Response {
    data: KvV2Data,
}
#[derive(Deserialize)]
struct KvV2Data {
    data: HashMap<String, serde_json::Value>,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn vault_ref(path: &str) -> VaultCredentialsRef {
        VaultCredentialsRef {
            path: path.to_string(),
            version: None,
        }
    }

    #[test]
    fn tenant_path_is_scoped_and_rejects_traversal() {
        assert_eq!(
            vault_path("dch", "tenant-a", &vault_ref("postgres/demo")).unwrap(),
            "dch/tenant-a/postgres/demo"
        );
        assert!(vault_path("dch", "tenant-a", &vault_ref("../postgres")).is_err());
        assert!(vault_path("dch", "tenant/a", &vault_ref("postgres/demo")).is_err());
    }

    #[test]
    fn vault_requires_tls() {
        let config = VaultConfig {
            address: "http://vault".to_string(),
            kv_mount: "secret".to_string(),
            auth_mount: "kubernetes".to_string(),
            role: "dch".to_string(),
            token_file: "token".to_string(),
            ca_cert: None,
            tenant_prefix: "dch".to_string(),
        };
        assert!(config.validate().is_err());
    }
}
