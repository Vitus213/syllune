use std::fs;
use std::path::Path;

use serde::Deserialize;
use thiserror::Error;

const DEFAULT_REALTIME_ENDPOINT: &str = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime";
const DEFAULT_REALTIME_MODEL: &str = "qwen3-asr-flash-realtime";

#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("unknown configuration field: {field}")]
    UnknownField { field: String },
    #[error("invalid configuration: {0}")]
    Invalid(String),
    #[error("configuration parse error: {0}")]
    Parse(String),
    #[error("cannot read configuration: {0}")]
    Io(#[from] std::io::Error),
    #[error(
        "configuration file containing a cloud API key must have permissions 0600 or stricter"
    )]
    InsecurePermissions,
}

#[derive(Debug, Clone, Deserialize, serde::Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct AppConfig {
    pub asr: AsrConfig,
    pub cloud: CloudConfig,
    pub inject: InjectConfig,
    pub processing: ProcessingConfig,
    pub history: HistoryConfig,
}

impl Default for AppConfig {
    fn default() -> Self {
        Self {
            asr: AsrConfig::default(),
            cloud: CloudConfig::default(),
            inject: InjectConfig::default(),
            processing: ProcessingConfig::default(),
            history: HistoryConfig::default(),
        }
    }
}

#[derive(Debug, Clone, Deserialize, serde::Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct AsrConfig {
    pub streaming_backend: String,
    pub batch_backend: String,
    pub final_backend: Option<String>,
    pub local_model_dir: Option<std::path::PathBuf>,
    pub batch_model_dir: Option<std::path::PathBuf>,
}

impl Default for AsrConfig {
    fn default() -> Self {
        Self {
            streaming_backend: "cloud-realtime".to_owned(),
            batch_backend: "cloud".to_owned(),
            final_backend: None,
            local_model_dir: None,
            batch_model_dir: None,
        }
    }
}

#[derive(Debug, Clone, Deserialize, serde::Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct CloudConfig {
    pub api_key: String,
    pub base_url: String,
    pub model: String,
    pub timeout_seconds: f64,
    pub realtime_endpoint: String,
    pub realtime_model: String,
}

impl Default for CloudConfig {
    fn default() -> Self {
        Self {
            api_key: String::new(),
            base_url: "https://dashscope.aliyuncs.com".to_owned(),
            model: "qwen3-asr-flash-2026-02-10".to_owned(),
            timeout_seconds: 60.0,
            realtime_endpoint: DEFAULT_REALTIME_ENDPOINT.to_owned(),
            realtime_model: DEFAULT_REALTIME_MODEL.to_owned(),
        }
    }
}

#[derive(Debug, Clone, Deserialize, serde::Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct InjectConfig {
    pub prefer: String,
    pub wtype_command: String,
    pub wl_copy_command: String,
    pub clipboard_fallback: bool,
    pub timeout_seconds: f64,
}

impl Default for InjectConfig {
    fn default() -> Self {
        Self {
            prefer: "wtype".to_owned(),
            wtype_command: "wtype".to_owned(),
            wl_copy_command: "wl-copy".to_owned(),
            clipboard_fallback: true,
            timeout_seconds: 10.0,
        }
    }
}

#[derive(Debug, Clone, Deserialize, serde::Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct ProcessingConfig {
    pub provider: String,
    pub base_url: String,
    pub model: String,
    pub api_key_env: String,
    pub timeout_seconds: f64,
}

impl Default for ProcessingConfig {
    fn default() -> Self {
        Self {
            provider: "none".to_owned(),
            base_url: String::new(),
            model: String::new(),
            api_key_env: String::new(),
            timeout_seconds: 30.0,
        }
    }
}

#[derive(Debug, Clone, Deserialize, serde::Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct HistoryConfig {
    pub enabled: bool,
}

impl Default for HistoryConfig {
    fn default() -> Self {
        Self { enabled: true }
    }
}

impl AppConfig {
    pub fn from_toml(input: &str) -> Result<Self, ConfigError> {
        let config = toml::from_str::<Self>(input).map_err(|error| {
            let message = error.to_string();
            if message.contains("unknown field") {
                ConfigError::UnknownField { field: message }
            } else {
                ConfigError::Parse(message)
            }
        })?;
        config.validate()
    }
    pub fn load_optional(path: Option<&Path>) -> Result<Self, ConfigError> {
        match path {
            Some(path) => Self::load(path),
            None => Ok(Self::default()),
        }
    }

    pub fn load(path: &Path) -> Result<Self, ConfigError> {
        let input = fs::read_to_string(path)?;
        #[cfg(unix)]
        if !input.is_empty() && input.contains("api_key") {
            use std::os::unix::fs::PermissionsExt;
            let mode = fs::metadata(path)?.permissions().mode();
            if mode & 0o077 != 0 {
                return Err(ConfigError::InsecurePermissions);
            }
        }
        Self::from_toml(&input)
    }

    fn validate(self) -> Result<Self, ConfigError> {
        match self.asr.streaming_backend.as_str() {
            "cloud-realtime" | "local-streaming" => {}
            value => {
                return Err(ConfigError::Invalid(format!(
                    "asr.streaming_backend={value:?}; expected cloud-realtime or local-streaming"
                )))
            }
        }
        if self.asr.final_backend.is_some() {
            return Err(ConfigError::Invalid(
                "asr.final_backend was removed; choose cloud-realtime or local-streaming"
                    .to_owned(),
            ));
        }
        if !self.cloud.realtime_endpoint.starts_with("wss://") {
            return Err(ConfigError::Invalid(
                "cloud.realtime_endpoint must use wss://".to_owned(),
            ));
        }
        if self.cloud.realtime_model.trim().is_empty() {
            return Err(ConfigError::Invalid(
                "cloud.realtime_model must not be empty".to_owned(),
            ));
        }
        Ok(self)
    }
}

/// Load the Syllune XDG config file when present, otherwise defaults.
/// Missing files are valid; unreadable or invalid files are errors.
pub fn load_default_config() -> Result<AppConfig, ConfigError> {
    let root = std::env::var("XDG_CONFIG_HOME")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| {
            std::path::PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| ".".to_owned()))
                .join(".config")
        });
    let path = root.join("syllune").join("config.toml");
    if !path.exists() {
        return Ok(AppConfig::default());
    }
    AppConfig::load(&path)
}
