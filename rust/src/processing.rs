//! Mode text processing: OpenAI-compatible and Ollama chat endpoints.
//! `quick` mode never reaches a provider; failures surface as `Err` so the
//! coordinator keeps the recognized text with a warning.

use std::time::Duration;

use serde::{Deserialize, Serialize};

use crate::config::ProcessingConfig;

#[derive(Debug, thiserror::Error)]
pub enum ProcessingError {
    #[error("no text processing provider configured")]
    NoProvider,
    #[error("processing request failed: {0}")]
    Request(String),
    #[error("processing response malformed: {0}")]
    Response(String),
    #[error("processing provider rejected the request: HTTP {0}")]
    Status(u16),
}

/// HTTP boundary; production uses ureq, tests inject canned responses.
pub trait HttpPoster {
    fn post_json(
        &self,
        url: &str,
        body: &str,
        bearer: Option<&str>,
        timeout: Duration,
    ) -> Result<(u16, String), ProcessingError>;
}

#[derive(Clone)]
pub struct UreqPoster;

impl HttpPoster for UreqPoster {
    fn post_json(
        &self,
        url: &str,
        body: &str,
        bearer: Option<&str>,
        timeout: Duration,
    ) -> Result<(u16, String), ProcessingError> {
        let mut request = ureq::post(url)
            .timeout(timeout)
            .set("Content-Type", "application/json");
        if let Some(bearer) = bearer {
            request = request.set("Authorization", &format!("Bearer {bearer}"));
        }
        match request.send_string(body) {
            Ok(response) => {
                let status = response.status();
                let body = response
                    .into_string()
                    .map_err(|error| ProcessingError::Response(error.to_string()))?;
                Ok((status, body))
            }
            Err(ureq::Error::Status(status, response)) => {
                let body = response.into_string().unwrap_or_default();
                Ok((status, body))
            }
            Err(error) => Err(ProcessingError::Request(error.to_string())),
        }
    }
}

#[derive(Clone)]
pub struct ChatProcessor<H: HttpPoster> {
    pub provider: String,
    pub base_url: String,
    pub model: String,
    pub api_key: String,
    pub timeout: Duration,
    poster: H,
}

impl<H: HttpPoster> ChatProcessor<H> {
    pub fn new(
        provider: String,
        base_url: String,
        model: String,
        api_key: String,
        timeout: Duration,
        poster: H,
    ) -> Self {
        Self {
            provider,
            base_url,
            model,
            api_key,
            timeout,
            poster,
        }
    }

    pub fn poster_ref(&self) -> &H {
        &self.poster
    }

    pub fn process(&self, prompt: &str) -> Result<String, ProcessingError> {
        let (url, payload) = match self.provider.as_str() {
            "openai-compatible" => {
                let url = format!("{}/chat/completions", self.base_url.trim_end_matches('/'));
                let payload = serde_json::json!({
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                });
                (url, payload)
            }
            "ollama" => {
                let url = format!("{}/api/chat", self.base_url.trim_end_matches('/'));
                let payload = serde_json::json!({
                    "model": self.model,
                    "stream": false,
                    "messages": [{"role": "user", "content": prompt}],
                });
                (url, payload)
            }
            other => return Err(ProcessingError::Request(format!("unknown provider: {other}"))),
        };
        let bearer = (!self.api_key.is_empty()).then_some(self.api_key.as_str());
        let (status, body) = self.poster.post_json(
            &url,
            &payload.to_string(),
            bearer,
            self.timeout,
        )?;
        if status == 401 || status == 403 {
            return Err(ProcessingError::Status(status));
        }
        if status != 200 {
            return Err(ProcessingError::Status(status));
        }
        let parsed: serde_json::Value = serde_json::from_str(&body)
            .map_err(|error| ProcessingError::Response(error.to_string()))?;
        let content = match self.provider.as_str() {
            "openai-compatible" => parsed["choices"][0]["message"]["content"].as_str(),
            _ => parsed["message"]["content"].as_str(),
        };
        match content {
            Some(text) if !text.trim().is_empty() => Ok(text.to_owned()),
            _ => Err(ProcessingError::Response(
                "provider returned no text content".to_owned(),
            )),
        }
    }
}

/// Build the production processor from config; `none` maps to `None`.
pub fn from_config(config: &ProcessingConfig) -> Result<Option<ChatProcessor<UreqPoster>>, ProcessingError> {
    match config.provider.as_str() {
        "none" => Ok(None),
        "openai-compatible" | "ollama" => {
            if config.base_url.is_empty() || config.model.is_empty() {
                return Err(ProcessingError::Request(
                    "processing.base_url and processing.model are required".to_owned(),
                ));
            }
            let api_key = if config.api_key_env.is_empty() {
                String::new()
            } else {
                std::env::var(&config.api_key_env).unwrap_or_default()
            };
            Ok(Some(ChatProcessor::new(
                config.provider.clone(),
                config.base_url.clone(),
                config.model.clone(),
                api_key,
                Duration::from_secs_f64(config.timeout_seconds),
                UreqPoster,
            )))
        }
        other => Err(ProcessingError::Request(format!(
            "unknown processing provider: {other}"
        ))),
    }
}

#[derive(Debug, Serialize, Deserialize)]
pub struct ProcessedText {
    pub text: String,
    pub provider: String,
}
