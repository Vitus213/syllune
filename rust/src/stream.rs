//! Production wiring: config -> coordinator with pw-record capture,
//! cloud/local transports, stdout/stderr event sink and wtype injection.

use std::io::{self, Write};
use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;

use serde::Serialize;
use tokio::process::Command;
use tokio::time::timeout;

use crate::capture::RawCapture;
use crate::config::{AppConfig, ConfigError};
use crate::coordinator::{
    AudioCapture, BackendTransport, ControlCommand, EventSink, HistoryEntry, HistoryRecorder,
    InjectionResult, OutputEvent, SessionPlan, TextInjector, TextProcessor,
};
use crate::realtime::{RealtimeEvent, RealtimeSession};
use crate::session::TranscriptSnapshot;

#[derive(Debug, Clone)]
pub struct StreamOptions {
    pub config_path: Option<PathBuf>,
    pub backend: Option<String>,
    pub json: bool,
    pub inject: bool,
    pub mode: String,
}

#[derive(Debug, thiserror::Error)]
pub enum StreamError {
    #[error(transparent)]
    Config(#[from] ConfigError),
    #[error("stream backend {0:?} is not implemented yet")]
    UnsupportedBackend(String),
    #[error("cloud realtime API key is missing")]
    MissingApiKey,
    #[error("realtime session: {0}")]
    Realtime(#[from] io::Error),
    #[error("capture: {0}")]
    Capture(String),
}

#[derive(Debug, Serialize)]
struct JsonEvent<'a> {
    #[serde(rename = "type")]
    kind: &'a str,
    sequence: u64,
    transcript: Option<&'a TranscriptSnapshot>,
    message: Option<&'a str>,
    injection: Option<&'a InjectionResult>,
}

pub async fn run(options: StreamOptions) -> Result<i32, StreamError> {
    let mut config = AppConfig::load_optional(options.config_path.as_deref())?;
    if let Some(backend) = options.backend.as_deref() {
        config.asr.streaming_backend = backend.to_owned();
        config = AppConfig::from_toml(&toml::to_string(&config).map_err(|error| {
            ConfigError::Invalid(format!("cannot apply backend override: {error}"))
        })?)?;
    }
    match config.asr.streaming_backend.as_str() {
        "cloud-realtime" => run_cloud(config, options).await,
        "local-streaming" => run_local(config, options).await,
        value => Err(StreamError::UnsupportedBackend(value.to_owned())),
    }
}

async fn run_cloud(config: AppConfig, options: StreamOptions) -> Result<i32, StreamError> {
    if config.cloud.api_key.trim().is_empty() {
        return Err(StreamError::MissingApiKey);
    }
    let transport = RealtimeSession::connect(
        &config.cloud.realtime_endpoint,
        &config.cloud.api_key,
        &config.cloud.realtime_model,
    )
    .await?;
    let plan = session_plan(&options, "cloud-realtime");
    let code = crate::coordinator::run_session(
        plan,
        PwCapture::default(),
        CloudTransport(transport),
        NoopProcessor,
        WtypeInjector,
        DiscardHistory,
        control_receiver(),
        &mut StdoutSink::new(options.json),
    )
    .await;
    Ok(code)
}

async fn run_local(config: AppConfig, options: StreamOptions) -> Result<i32, StreamError> {
    let model_dir = match config.asr.local_model_dir {
        Some(path) => path,
        None => {
            let mut sink = StdoutSink::new(options.json);
            let _ = sink.emit(OutputEvent::Error(
                "local-streaming requires asr.local_model_dir for an installed online model"
                    .to_owned(),
            ));
            let _ = sink.emit(OutputEvent::Completed);
            return Ok(1);
        }
    };
    let recognizer = match crate::local_asr::LocalStreamingRecognizer::new(&model_dir) {
        Ok(recognizer) => recognizer,
        Err(error) => {
            let mut sink = StdoutSink::new(options.json);
            let _ = sink.emit(OutputEvent::Error(error.to_string()));
            let _ = sink.emit(OutputEvent::Completed);
            return Ok(1);
        }
    };
    let plan = session_plan(&options, "local-streaming");
    let code = crate::coordinator::run_session(
        plan,
        PwCapture::default(),
        LocalTransport::new(recognizer),
        NoopProcessor,
        WtypeInjector,
        DiscardHistory,
        control_receiver(),
        &mut StdoutSink::new(options.json),
    )
    .await;
    Ok(code)
}

fn session_plan(options: &StreamOptions, backend: &str) -> SessionPlan {
    let mut plan = SessionPlan::new(backend, options.inject);
    plan.mode_id = options.mode.clone();
    plan
}

fn control_receiver() -> tokio::sync::mpsc::Receiver<ControlCommand> {
    let (tx, rx) = tokio::sync::mpsc::channel(4);
    tokio::spawn(async move {
        let mut first_stop = false;
        loop {
            let mut sigint = tokio::signal::unix::signal(
                tokio::signal::unix::SignalKind::interrupt(),
            )
            .expect("install SIGINT handler");
            let mut sigterm = tokio::signal::unix::signal(
                tokio::signal::unix::SignalKind::terminate(),
            )
            .expect("install SIGTERM handler");
            tokio::select! {
                _ = sigint.recv() => {
                    let command = if first_stop {
                        ControlCommand::Cancel
                    } else {
                        first_stop = true;
                        ControlCommand::Stop
                    };
                    if tx.send(command).await.is_err() {
                        break;
                    }
                }
                _ = sigterm.recv() => {
                    let _ = tx.send(ControlCommand::Cancel).await;
                    break;
                }
            }
        }
    });
    rx
}

/// pw-record capture adapted to the coordinator boundary. Construction is
/// deferred to `start` so the ready gate can fail before any capture exists.
#[derive(Default)]
struct PwCapture {
    inner: Option<RawCapture>,
}

impl AudioCapture for PwCapture {
    async fn start(&mut self) -> io::Result<()> {
        let capture = RawCapture::start()?;
        self.inner = Some(capture);
        Ok(())
    }

    async fn next_chunk(&mut self) -> io::Result<Option<Vec<u8>>> {
        match &mut self.inner {
            Some(capture) => capture.next_chunk().await,
            None => Err(io::Error::new(
                io::ErrorKind::NotConnected,
                "capture not started",
            )),
        }
    }

    async fn stop_capture(&mut self) -> io::Result<Option<Vec<u8>>> {
        match &mut self.inner {
            Some(capture) => capture.stop().await,
            None => Ok(None),
        }
    }

    fn abort(&mut self) {
        // Dropping the child kills it (kill_on_drop) without draining.
        self.inner = None;
    }
}

struct CloudTransport(RealtimeSession);

impl BackendTransport for CloudTransport {
    async fn send_audio(&self, pcm: &[u8]) -> io::Result<()> {
        self.0.send_audio(pcm).await
    }

    async fn next_event(&self) -> io::Result<RealtimeEvent> {
        self.0.next_event().await
    }

    async fn finish(&mut self) -> io::Result<()> {
        self.0.finish().await
    }

    async fn cancel(&mut self) -> io::Result<()> {
        self.0.cancel().await
    }
}

/// Local streaming backend exposed through the same event boundary. The
/// recognizer is synchronous; it reports Ready immediately and drains
/// decoded events before each await.
struct LocalTransport {
    state: parking_lot::Mutex<LocalTransportState>,
}

struct LocalTransportState {
    recognizer: crate::local_asr::LocalStreamingRecognizer,
    pending: std::collections::VecDeque<io::Result<RealtimeEvent>>,
    ready_sent: bool,
    finished: bool,
}

impl LocalTransport {
    fn new(recognizer: crate::local_asr::LocalStreamingRecognizer) -> Self {
        Self {
            state: parking_lot::Mutex::new(LocalTransportState {
                recognizer,
                pending: std::collections::VecDeque::new(),
                ready_sent: false,
                finished: false,
            }),
        }
    }
}

impl BackendTransport for LocalTransport {
    async fn send_audio(&self, pcm: &[u8]) -> io::Result<()> {
        let mut state = self.state.lock();
        if state.finished {
            return Err(io::Error::new(
                io::ErrorKind::BrokenPipe,
                "local session already finished",
            ));
        }
        match state.recognizer.accept_pcm(pcm) {
            Ok(events) => {
                for event in events {
                    state.pending.push_back(Ok(event));
                }
                Ok(())
            }
            Err(error) => Err(io::Error::new(io::ErrorKind::InvalidData, error.to_string())),
        }
    }

    async fn next_event(&self) -> io::Result<RealtimeEvent> {
        loop {
            {
                let mut state = self.state.lock();
                if !state.ready_sent {
                    state.ready_sent = true;
                    return Ok(RealtimeEvent::Ready);
                }
                if let Some(event) = state.pending.pop_front() {
                    return event;
                }
                if state.finished {
                    return Err(io::Error::new(
                        io::ErrorKind::UnexpectedEof,
                        "local session drained",
                    ));
                }
            }
            tokio::time::sleep(Duration::from_millis(2)).await;
        }
    }

    async fn finish(&mut self) -> io::Result<()> {
        let mut state = self.state.lock();
        if state.finished {
            return Ok(());
        }
        state.finished = true;
        match state.recognizer.finish() {
            Ok(events) => {
                for event in events {
                    state.pending.push_back(Ok(event));
                }
                Ok(())
            }
            Err(error) => Err(io::Error::new(io::ErrorKind::Other, error.to_string())),
        }
    }

    async fn cancel(&mut self) -> io::Result<()> {
        self.state.lock().finished = true;
        Ok(())
    }
}

struct StdoutSink {
    json: bool,
    sequence: u64,
}

impl StdoutSink {
    fn new(json: bool) -> Self {
        Self { json, sequence: 0 }
    }
}

impl EventSink for StdoutSink {
    fn emit(&mut self, event: OutputEvent) -> io::Result<()> {
        self.sequence += 1;
        let (kind, transcript, message, injection): (
            &str,
            Option<&TranscriptSnapshot>,
            Option<&str>,
            Option<&InjectionResult>,
        ) = match &event {
            OutputEvent::Ready => ("ready", None, None, None),
            OutputEvent::Transcript(snapshot) => ("transcript", Some(snapshot), None, None),
            OutputEvent::Finalized { injection } => {
                ("finalized", None, None, injection.as_ref())
            }
            OutputEvent::Warning(message) => ("warning", None, Some(message.as_str()), None),
            OutputEvent::Error(message) => ("error", None, Some(message.as_str()), None),
            OutputEvent::Cancelled => ("cancelled", None, None, None),
            OutputEvent::Completed => ("completed", None, None, None),
        };
        if self.json {
            let payload = JsonEvent {
                kind,
                sequence: self.sequence,
                transcript,
                message,
                injection,
            };
            println!("{}", serde_json::to_string(&payload)?);
            return Ok(());
        }
        match &event {
            OutputEvent::Transcript(snapshot) => {
                if snapshot.is_final {
                    println!("{}", snapshot.authoritative_text);
                } else {
                    eprint!("\r{}", snapshot.authoritative_text);
                    io::stderr().flush().ok();
                }
            }
            OutputEvent::Warning(message) => eprintln!("warning: {message}"),
            OutputEvent::Error(message) => eprintln!("{message}"),
            _ => {}
        }
        Ok(())
    }
}

struct WtypeInjector;

impl TextInjector for WtypeInjector {
    async fn inject(&mut self, text: &str) -> InjectionResult {
        let child = Command::new("wtype")
            .arg("--")
            .arg(text)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn();
        let child = match child {
            Ok(child) => child,
            Err(error) => return wtype_result(false, &error.to_string()),
        };
        match timeout(Duration::from_secs(1), child.wait_with_output()).await {
            Ok(Ok(output)) if output.status.success() => wtype_result(true, ""),
            Ok(Ok(output)) => wtype_result(
                false,
                &String::from_utf8_lossy(&output.stderr).trim().to_owned(),
            ),
            Ok(Err(error)) => wtype_result(false, &error.to_string()),
            Err(_) => wtype_result(false, "wtype timed out"),
        }
    }
}

fn wtype_result(ok: bool, message: &str) -> InjectionResult {
    InjectionResult {
        ok,
        method: "wtype".to_owned(),
        message: message.to_owned(),
    }
}

/// Processing is not wired in this build: quick mode passes text through and
/// other modes receive a processing failure warning that keeps the
/// recognized text, matching the coordinator contract.
struct NoopProcessor;

impl TextProcessor for NoopProcessor {
    async fn process(&mut self, _mode_id: &str, _text: &str) -> Result<String, String> {
        Err("no text processing provider configured".to_owned())
    }
}

struct DiscardHistory;

impl HistoryRecorder for DiscardHistory {
    fn record(&mut self, _entry: HistoryEntry) {}
}
