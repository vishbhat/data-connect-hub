use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use arrow::array::{
    ArrayRef, BooleanArray, Float32Array, Float64Array, Int8Array, Int16Array, Int32Array, Int64Array, StringArray,
    TimestampMillisecondArray,
};
use arrow::datatypes::{DataType as ArrowDataType, Field, Schema, TimeUnit};
use arrow::record_batch::RecordBatch;
use commons::api::connections::{Admin, DataConnectionResource};
use commons::api::errors::ConnectorError;
use commons::api::tabular::{FlightConnector, QueryOptions, QueryOutput, TabularReader, TabularState};
use moka::future::Cache;

const KEY_URI: &str = "URI";
const KEY_TOKEN: &str = "TOKEN";
const KEY_USERNAME: &str = "USERNAME";
const KEY_PASSWORD: &str = "PASSWORD";
const KEY_CA_CERT: &str = "CA_CERT";

#[derive(Clone)]
struct UriClient {
    http: reqwest::Client,
    base_url: String,
    auth: UriAuth,
}

#[derive(Clone)]
enum UriAuth {
    None,
    Token { token: String },
    Basic { username: String, password: String },
}

impl UriClient {
    fn request(&self, method: reqwest::Method, path: &str) -> reqwest::RequestBuilder {
        let url = format!("{}{}", self.base_url.trim_end_matches('/'), path);
        let mut req = self.http.request(method, &url);
        match &self.auth {
            UriAuth::None => {},
            UriAuth::Token { token } => {
                req = req.header("Authorization", format!("Bearer {token}"));
            },
            UriAuth::Basic { username, password } => {
                req = req.basic_auth(username, Some(password));
            },
        }
        req
    }

    fn get(&self, path: &str) -> reqwest::RequestBuilder {
        self.request(reqwest::Method::GET, path)
    }

    fn post(&self, path: &str) -> reqwest::RequestBuilder {
        self.request(reqwest::Method::POST, path)
    }
}

pub struct UriConnector {
    clients: Cache<String, UriClient>,
}

impl UriConnector {
    pub fn new(cache_ttl: Duration, cache_idle: Duration, cache_max_capacity: u64) -> Self {
        Self {
            clients: Cache::builder()
                .time_to_live(cache_ttl)
                .time_to_idle(cache_idle)
                .max_capacity(cache_max_capacity)
                .build(),
        }
    }
}

fn extract_credentials(
    data_connection: &DataConnectionResource,
) -> Result<Arc<HashMap<String, String>>, ConnectorError> {
    match &data_connection.resource.admin {
        Some(Admin::Secret { name: _, secret }) => Ok(secret.clone()),
        _ => Err(ConnectorError::ConnectionError(
            "URI connector credentials are required".to_string(),
        )),
    }
}

fn build_client(credentials: &HashMap<String, String>) -> Result<UriClient, ConnectorError> {
    let base_url = credentials
        .get(KEY_URI)
        .ok_or_else(|| ConnectorError::ConnectionError("URI is required".to_string()))?
        .clone();

    let mut builder = reqwest::Client::builder().no_proxy();

    if let Some(ca_pem) = credentials.get(KEY_CA_CERT) {
        let cert = reqwest::tls::Certificate::from_pem(ca_pem.as_bytes())
            .map_err(|e| ConnectorError::ConnectionError(format!("Invalid CA certificate: {e}")))?;
        builder = builder.add_root_certificate(cert);
    }

    let http = builder
        .build()
        .map_err(|e| ConnectorError::ConnectionError(format!("Failed to build HTTP client: {e}")))?;

    let auth = if let Some(token) = credentials.get(KEY_TOKEN) {
        UriAuth::Token { token: token.clone() }
    } else if let (Some(username), Some(password)) = (credentials.get(KEY_USERNAME), credentials.get(KEY_PASSWORD)) {
        UriAuth::Basic {
            username: username.clone(),
            password: password.clone(),
        }
    } else {
        UriAuth::None
    };

    Ok(UriClient { http, base_url, auth })
}

#[async_trait::async_trait]
impl FlightConnector for UriConnector {
    fn provider(&self) -> String {
        "uri".to_string()
    }

    fn description(&self) -> String {
        "URI connector".to_string()
    }

    async fn get_reader(
        &self,
        data_connection: &DataConnectionResource,
    ) -> Result<Arc<dyn TabularReader>, ConnectorError> {
        let credentials = extract_credentials(data_connection)?;
        let cache_key = data_connection.metadata.id.clone();
        let client = self
            .clients
            .try_get_with(cache_key, async { build_client(&credentials) })
            .await
            .map_err(|e| ConnectorError::ConnectionError(format!("Failed to get URI client: {e}")))?;

        Ok(Arc::new(UriReader { client }))
    }
}

pub struct UriReader {
    client: UriClient,
}

#[derive(serde::Deserialize)]
struct UriRequest {
    path: String,
    #[serde(default = "default_method")]
    method: String,
    #[serde(default)]
    body: Option<serde_json::Value>,
    #[serde(default)]
    data_path: Option<String>,
}

fn default_method() -> String {
    "GET".to_string()
}

impl UriRequest {
    fn parse(query: &str) -> Result<Self, ConnectorError> {
        serde_json::from_str(query).map_err(|e| {
            ConnectorError::InvalidRequest(format!(
                "Invalid URI query (expected JSON with 'path', optional 'method', 'body', 'data_path'): {e}"
            ))
        })
    }
}

async fn fetch_json(client: &UriClient, request: &UriRequest) -> Result<serde_json::Value, ConnectorError> {
    let method = request
        .method
        .parse::<reqwest::Method>()
        .map_err(|e| ConnectorError::InvalidRequest(format!("Invalid HTTP method '{}': {e}", request.method)))?;

    let req_builder = match method {
        reqwest::Method::GET => client.get(&request.path),
        reqwest::Method::POST => {
            let mut b = client.post(&request.path);
            if let Some(body) = &request.body {
                b = b.json(body);
            }
            b
        },
        _ => client.request(method, &request.path),
    };

    let response = req_builder
        .send()
        .await
        .map_err(|e| ConnectorError::ConnectionError(format!("HTTP request failed: {e}")))?;

    let status = response.status();
    if !status.is_success() {
        let body = response.text().await.unwrap_or_default();
        return Err(ConnectorError::ConnectionError(format!(
            "HTTP request failed (HTTP {status}): {body}"
        )));
    }

    response
        .json()
        .await
        .map_err(|e| ConnectorError::SQLError(format!("Failed to parse JSON response: {e}")))
}

fn extract_data_array<'a>(
    value: &'a serde_json::Value,
    data_path: Option<&str>,
) -> Result<&'a Vec<serde_json::Value>, ConnectorError> {
    let target = match data_path {
        Some(path) => {
            let mut current = value;
            for segment in path.split('.') {
                current = current
                    .get(segment)
                    .ok_or_else(|| ConnectorError::SQLError(format!("data_path segment '{segment}' not found")))?;
            }
            current
        },
        None => value,
    };

    target
        .as_array()
        .ok_or_else(|| ConnectorError::SQLError("Response is not a JSON array".to_string()))
}

fn infer_arrow_type(value: &serde_json::Value) -> ArrowDataType {
    match value {
        serde_json::Value::Bool(_) => ArrowDataType::Boolean,
        serde_json::Value::Number(n) => {
            if n.is_i64() {
                ArrowDataType::Int64
            } else {
                ArrowDataType::Float64
            }
        },
        _ => ArrowDataType::Utf8,
    }
}

fn infer_schema(rows: &[serde_json::Value]) -> Result<Schema, ConnectorError> {
    let first = rows
        .first()
        .and_then(|v| v.as_object())
        .ok_or(ConnectorError::NoDataError)?;

    let mut fields: Vec<Field> = first
        .iter()
        .map(|(key, value)| Field::new(key, infer_arrow_type(value), true))
        .collect();

    fields.sort_by(|a, b| a.name().cmp(b.name()));
    Ok(Schema::new(fields))
}

fn rows_to_record_batch(schema: &Arc<Schema>, rows: &[serde_json::Value]) -> Result<RecordBatch, ConnectorError> {
    let arrays: Vec<ArrayRef> = schema
        .fields()
        .iter()
        .map(|field| {
            let values: Vec<Option<&serde_json::Value>> = rows.iter().map(|row| row.get(field.name())).collect();
            json_values_to_array(field.data_type(), &values)
        })
        .collect::<Result<_, _>>()?;

    RecordBatch::try_new(Arc::clone(schema), arrays).map_err(|e| ConnectorError::SQLError(e.to_string()))
}

#[async_trait::async_trait]
impl TabularReader for UriReader {
    fn provider(&self) -> String {
        "uri".to_string()
    }

    async fn schema(&self, query: &str) -> Result<Arc<TabularState>, ConnectorError> {
        let request = UriRequest::parse(query)?;
        let response = fetch_json(&self.client, &request).await?;
        let rows = extract_data_array(&response, request.data_path.as_deref())?;

        let schema = infer_schema(rows)?;
        Ok(Arc::new(TabularState::new(query.to_owned(), Arc::new(schema))))
    }

    async fn read(&self, state: Arc<TabularState>, options: &QueryOptions) -> QueryOutput {
        let request = UriRequest::parse(&state.query)?;
        let schema = state.schema.clone();
        let batch_size = options.batch_size;
        let client = self.client.clone();

        let stream = async_stream::try_stream! {
            let response = fetch_json(&client, &request).await?;
            let rows = extract_data_array(&response, request.data_path.as_deref())?;

            for chunk in rows.chunks(batch_size) {
                let batch = rows_to_record_batch(&schema, chunk)?;
                yield batch;
            }
        };

        Ok(Box::pin(stream))
    }

    async fn test_connection(&self) -> Result<(), ConnectorError> {
        let response = self
            .client
            .get("/")
            .send()
            .await
            .map_err(|e| ConnectorError::ConnectionError(format!("Connection test failed: {e}")))?;

        if !response.status().is_success() {
            return Err(ConnectorError::ConnectionError(format!(
                "URI endpoint returned HTTP {}",
                response.status()
            )));
        }

        Ok(())
    }
}

fn json_values_to_array(
    data_type: &ArrowDataType,
    values: &[Option<&serde_json::Value>],
) -> Result<ArrayRef, ConnectorError> {
    match data_type {
        ArrowDataType::Boolean => {
            let arr: BooleanArray = values.iter().map(|v| v.and_then(|v| v.as_bool())).collect();
            Ok(Arc::new(arr))
        },
        ArrowDataType::Int8 => {
            let arr: Int8Array = values
                .iter()
                .map(|v| v.and_then(|v| v.as_i64()).map(|n| n as i8))
                .collect();
            Ok(Arc::new(arr))
        },
        ArrowDataType::Int16 => {
            let arr: Int16Array = values
                .iter()
                .map(|v| v.and_then(|v| v.as_i64()).map(|n| n as i16))
                .collect();
            Ok(Arc::new(arr))
        },
        ArrowDataType::Int32 => {
            let arr: Int32Array = values
                .iter()
                .map(|v| v.and_then(|v| v.as_i64()).map(|n| n as i32))
                .collect();
            Ok(Arc::new(arr))
        },
        ArrowDataType::Int64 => {
            let arr: Int64Array = values.iter().map(|v| v.and_then(|v| v.as_i64())).collect();
            Ok(Arc::new(arr))
        },
        ArrowDataType::Float32 => {
            let arr: Float32Array = values
                .iter()
                .map(|v| v.and_then(|v| v.as_f64()).map(|n| n as f32))
                .collect();
            Ok(Arc::new(arr))
        },
        ArrowDataType::Float64 => {
            let arr: Float64Array = values.iter().map(|v| v.and_then(|v| v.as_f64())).collect();
            Ok(Arc::new(arr))
        },
        ArrowDataType::Timestamp(TimeUnit::Millisecond, _) => {
            let arr: TimestampMillisecondArray = values
                .iter()
                .map(|v| {
                    v.and_then(|v| {
                        v.as_i64().or_else(|| {
                            v.as_str().and_then(|s| {
                                chrono::DateTime::parse_from_rfc3339(s)
                                    .ok()
                                    .map(|dt| dt.timestamp_millis())
                                    .or_else(|| {
                                        chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S%.f")
                                            .ok()
                                            .map(|dt| dt.and_utc().timestamp_millis())
                                    })
                            })
                        })
                    })
                })
                .collect();
            Ok(Arc::new(arr.with_timezone("UTC")))
        },
        _ => {
            let arr: StringArray = values
                .iter()
                .map(|v| {
                    v.map(|v| match v {
                        serde_json::Value::String(s) => s.clone(),
                        other => other.to_string(),
                    })
                })
                .collect();
            Ok(Arc::new(arr))
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use arrow::array::Array;

    #[test]
    fn test_connector_provider() {
        let connector = UriConnector::new(Duration::from_secs(300), Duration::from_secs(60), 100);
        assert_eq!(connector.provider(), "uri");
    }

    #[test]
    fn test_extract_credentials_success() {
        let conn = DataConnectionResource {
            metadata: commons::api::ResourceMetadata {
                id: "conn-1".to_string(),
                tenant_id: Some("t-1".to_string()),
                created_at: "2026-01-01T00:00:00Z".to_string(),
                updated_at: "2026-01-01T00:00:00Z".to_string(),
            },
            resource: commons::api::connections::DataConnection {
                name: "test-uri".to_string(),
                data_connection_type_id: "uri-type".to_string(),
                format: commons::api::connections::DataFormat::Tabular,
                admin: Some(Admin::Secret {
                    name: "test-uri".to_string(),
                    secret: Arc::new(HashMap::from([(
                        KEY_URI.to_string(),
                        "http://localhost:8080".to_string(),
                    )])),
                }),
                properties: HashMap::new(),
            },
            status: Default::default(),
        };
        let result = extract_credentials(&conn);
        assert!(result.is_ok());
        assert_eq!(result.unwrap().get(KEY_URI).unwrap(), "http://localhost:8080");
    }

    #[test]
    fn test_extract_credentials_missing() {
        let conn = DataConnectionResource {
            metadata: commons::api::ResourceMetadata {
                id: "conn-1".to_string(),
                tenant_id: Some("t-1".to_string()),
                created_at: "2026-01-01T00:00:00Z".to_string(),
                updated_at: "2026-01-01T00:00:00Z".to_string(),
            },
            resource: commons::api::connections::DataConnection {
                name: "test-uri".to_string(),
                data_connection_type_id: "uri-type".to_string(),
                format: commons::api::connections::DataFormat::Tabular,
                admin: None,
                properties: HashMap::new(),
            },
            status: Default::default(),
        };
        assert!(extract_credentials(&conn).is_err());
    }

    #[test]
    fn test_extract_credentials_secret_ref() {
        let conn = DataConnectionResource {
            metadata: commons::api::ResourceMetadata {
                id: "conn-1".to_string(),
                tenant_id: Some("t-1".to_string()),
                created_at: "2026-01-01T00:00:00Z".to_string(),
                updated_at: "2026-01-01T00:00:00Z".to_string(),
            },
            resource: commons::api::connections::DataConnection {
                name: "test-uri".to_string(),
                data_connection_type_id: "uri-type".to_string(),
                format: commons::api::connections::DataFormat::Tabular,
                admin: Some(Admin::SecretRef {
                    secret_ref: "secret/test".to_string(),
                }),
                properties: HashMap::new(),
            },
            status: Default::default(),
        };
        assert!(extract_credentials(&conn).is_err());
    }

    #[test]
    fn test_build_client_with_token() {
        let creds = HashMap::from([
            (KEY_URI.to_string(), "http://localhost:8080".to_string()),
            (KEY_TOKEN.to_string(), "my-token-123".to_string()),
        ]);
        let client = build_client(&creds).unwrap();
        assert_eq!(client.base_url, "http://localhost:8080");
        assert!(matches!(client.auth, UriAuth::Token { .. }));
    }

    #[test]
    fn test_build_client_with_basic() {
        let creds = HashMap::from([
            (KEY_URI.to_string(), "http://localhost:8080".to_string()),
            (KEY_USERNAME.to_string(), "admin".to_string()),
            (KEY_PASSWORD.to_string(), "secret".to_string()),
        ]);
        let client = build_client(&creds).unwrap();
        assert!(matches!(client.auth, UriAuth::Basic { .. }));
    }

    #[test]
    fn test_build_client_no_auth() {
        let creds = HashMap::from([(KEY_URI.to_string(), "http://localhost:8080".to_string())]);
        let client = build_client(&creds).unwrap();
        assert!(matches!(client.auth, UriAuth::None));
    }

    #[test]
    fn test_build_client_missing_uri() {
        let creds = HashMap::new();
        assert!(build_client(&creds).is_err());
    }

    #[test]
    fn test_build_client_token_takes_precedence() {
        let creds = HashMap::from([
            (KEY_URI.to_string(), "http://localhost:8080".to_string()),
            (KEY_TOKEN.to_string(), "my-token".to_string()),
            (KEY_USERNAME.to_string(), "admin".to_string()),
            (KEY_PASSWORD.to_string(), "secret".to_string()),
        ]);
        let client = build_client(&creds).unwrap();
        assert!(matches!(client.auth, UriAuth::Token { .. }));
    }

    #[test]
    fn test_parse_uri_request() {
        let query = r#"{"path": "/api/data", "method": "GET"}"#;
        let req = UriRequest::parse(query).unwrap();
        assert_eq!(req.path, "/api/data");
        assert_eq!(req.method, "GET");
        assert!(req.body.is_none());
        assert!(req.data_path.is_none());
    }

    #[test]
    fn test_parse_uri_request_with_body() {
        let query = r#"{"path": "/api/search", "method": "POST", "body": {"filter": "active"}}"#;
        let req = UriRequest::parse(query).unwrap();
        assert_eq!(req.path, "/api/search");
        assert_eq!(req.method, "POST");
        assert!(req.body.is_some());
    }

    #[test]
    fn test_parse_uri_request_with_data_path() {
        let query = r#"{"path": "/api/data", "data_path": "results.items"}"#;
        let req = UriRequest::parse(query).unwrap();
        assert_eq!(req.data_path.as_deref(), Some("results.items"));
    }

    #[test]
    fn test_parse_uri_request_defaults() {
        let query = r#"{"path": "/api/data"}"#;
        let req = UriRequest::parse(query).unwrap();
        assert_eq!(req.method, "GET");
    }

    #[test]
    fn test_parse_uri_request_invalid() {
        assert!(UriRequest::parse("not json").is_err());
        assert!(UriRequest::parse("{}").is_err());
    }

    #[test]
    fn test_infer_arrow_type() {
        assert_eq!(infer_arrow_type(&serde_json::json!(true)), ArrowDataType::Boolean);
        assert_eq!(infer_arrow_type(&serde_json::json!(42)), ArrowDataType::Int64);
        assert_eq!(infer_arrow_type(&serde_json::json!(1.5)), ArrowDataType::Float64);
        assert_eq!(infer_arrow_type(&serde_json::json!("hello")), ArrowDataType::Utf8);
        assert_eq!(infer_arrow_type(&serde_json::json!(null)), ArrowDataType::Utf8);
    }

    #[test]
    fn test_infer_schema() {
        let rows = vec![
            serde_json::json!({"name": "Alice", "age": 30, "active": true}),
            serde_json::json!({"name": "Bob", "age": 25, "active": false}),
        ];
        let schema = infer_schema(&rows).unwrap();
        assert_eq!(schema.fields().len(), 3);
        assert_eq!(schema.field(0).name(), "active");
        assert_eq!(*schema.field(0).data_type(), ArrowDataType::Boolean);
        assert_eq!(schema.field(1).name(), "age");
        assert_eq!(*schema.field(1).data_type(), ArrowDataType::Int64);
        assert_eq!(schema.field(2).name(), "name");
        assert_eq!(*schema.field(2).data_type(), ArrowDataType::Utf8);
    }

    #[test]
    fn test_infer_schema_empty() {
        let rows: Vec<serde_json::Value> = vec![];
        assert!(infer_schema(&rows).is_err());
    }

    #[test]
    fn test_extract_data_array_top_level() {
        let val = serde_json::json!([{"a": 1}, {"a": 2}]);
        let arr = extract_data_array(&val, None).unwrap();
        assert_eq!(arr.len(), 2);
    }

    #[test]
    fn test_extract_data_array_nested() {
        let val = serde_json::json!({"results": {"items": [{"a": 1}]}});
        let arr = extract_data_array(&val, Some("results.items")).unwrap();
        assert_eq!(arr.len(), 1);
    }

    #[test]
    fn test_extract_data_array_not_array() {
        let val = serde_json::json!({"data": "not_an_array"});
        assert!(extract_data_array(&val, Some("data")).is_err());
    }

    #[test]
    fn test_extract_data_array_missing_path() {
        let val = serde_json::json!({"data": [1]});
        assert!(extract_data_array(&val, Some("missing")).is_err());
    }

    #[test]
    fn test_rows_to_record_batch() {
        let schema = Arc::new(Schema::new(vec![
            Field::new("name", ArrowDataType::Utf8, true),
            Field::new("count", ArrowDataType::Int64, true),
        ]));
        let rows = vec![
            serde_json::json!({"name": "hello", "count": 10}),
            serde_json::json!({"name": "world", "count": 20}),
        ];
        let batch = rows_to_record_batch(&schema, &rows).unwrap();
        assert_eq!(batch.num_rows(), 2);
        assert_eq!(batch.num_columns(), 2);

        let name_arr = batch.column(0).as_any().downcast_ref::<StringArray>().unwrap();
        assert_eq!(name_arr.value(0), "hello");
        assert_eq!(name_arr.value(1), "world");

        let count_arr = batch.column(1).as_any().downcast_ref::<Int64Array>().unwrap();
        assert_eq!(count_arr.value(0), 10);
        assert_eq!(count_arr.value(1), 20);
    }

    #[test]
    fn test_rows_to_record_batch_null_fields() {
        let schema = Arc::new(Schema::new(vec![
            Field::new("name", ArrowDataType::Utf8, true),
            Field::new("missing", ArrowDataType::Utf8, true),
        ]));
        let rows = vec![serde_json::json!({"name": "hello"})];
        let batch = rows_to_record_batch(&schema, &rows).unwrap();
        assert_eq!(batch.num_rows(), 1);

        let missing_arr = batch.column(1).as_any().downcast_ref::<StringArray>().unwrap();
        assert!(missing_arr.is_null(0));
    }

    #[test]
    fn test_json_values_to_array_boolean() {
        let v_true = serde_json::json!(true);
        let v_false = serde_json::json!(false);
        let vals = vec![Some(&v_true), None, Some(&v_false)];
        let arr = json_values_to_array(&ArrowDataType::Boolean, &vals).unwrap();
        let bool_arr = arr.as_any().downcast_ref::<BooleanArray>().unwrap();
        assert_eq!(bool_arr.len(), 3);
        assert!(bool_arr.value(0));
        assert!(bool_arr.is_null(1));
        assert!(!bool_arr.value(2));
    }

    #[test]
    fn test_json_values_to_array_int64() {
        let v1 = serde_json::json!(42);
        let v2 = serde_json::json!(99);
        let vals = vec![Some(&v1), Some(&v2), None];
        let arr = json_values_to_array(&ArrowDataType::Int64, &vals).unwrap();
        let int_arr = arr.as_any().downcast_ref::<Int64Array>().unwrap();
        assert_eq!(int_arr.value(0), 42);
        assert_eq!(int_arr.value(1), 99);
        assert!(int_arr.is_null(2));
    }

    #[test]
    fn test_json_values_to_array_float64() {
        let v = serde_json::json!(1.23);
        let vals = vec![Some(&v), None];
        let arr = json_values_to_array(&ArrowDataType::Float64, &vals).unwrap();
        let f_arr = arr.as_any().downcast_ref::<Float64Array>().unwrap();
        assert!((f_arr.value(0) - 1.23).abs() < f64::EPSILON);
        assert!(f_arr.is_null(1));
    }

    #[test]
    fn test_json_values_to_array_utf8_fallback() {
        let v_str = serde_json::json!("hello");
        let v_obj = serde_json::json!({"nested": true});
        let vals = vec![Some(&v_str), Some(&v_obj), None];
        let arr = json_values_to_array(&ArrowDataType::Utf8, &vals).unwrap();
        let str_arr = arr.as_any().downcast_ref::<StringArray>().unwrap();
        assert_eq!(str_arr.value(0), "hello");
        assert_eq!(str_arr.value(1), r#"{"nested":true}"#);
        assert!(str_arr.is_null(2));
    }

    #[test]
    fn test_json_values_to_array_timestamp_epoch() {
        let v = serde_json::json!(1700000000000_i64);
        let vals = vec![Some(&v), None];
        let arr = json_values_to_array(
            &ArrowDataType::Timestamp(TimeUnit::Millisecond, Some("UTC".into())),
            &vals,
        )
        .unwrap();
        let ts_arr = arr.as_any().downcast_ref::<TimestampMillisecondArray>().unwrap();
        assert_eq!(ts_arr.value(0), 1700000000000);
        assert!(ts_arr.is_null(1));
    }

    #[test]
    fn test_json_values_to_array_timestamp_iso() {
        let v = serde_json::json!("2023-11-14T22:13:20.000Z");
        let vals = vec![Some(&v)];
        let arr = json_values_to_array(
            &ArrowDataType::Timestamp(TimeUnit::Millisecond, Some("UTC".into())),
            &vals,
        )
        .unwrap();
        let ts_arr = arr.as_any().downcast_ref::<TimestampMillisecondArray>().unwrap();
        assert_eq!(ts_arr.value(0), 1700000000000);
    }
}
