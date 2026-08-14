use std::io::{self, Write};
use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;

use serde::Serialize;
use tokio::process::Command;
use tokio::time::{timeout, timeout_at};

use crate::capture::RawCapture;
use crate::config::{AppConfig, ConfigError};
use crate::realtime::{RealtimeEvent, RealtimeSession};
use crate::session::{
    RecognitionSession, SessionAction, SessionState, SessionUpdate, TranscriptSnapshot,
};

#[derive(Debug, Clone)]
pub struct StreamOptions {
    pub config_path: Option<PathBuf>,
    pub backend: Option<String>,
    pub json: bool,
    pub inject: bool,
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
    #[error("injection: {0}")]
    Injection(String),
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

#[derive(Debug, Serialize)]
pub struct InjectionResult {
    pub ok: bool,
    pub method: String,
    pub message: String,
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

async fn run_local(config: AppConfig, options: StreamOptions) -> Result<i32, StreamError> {
    const CAPTURE_STOP_TIMEOUT: Duration = Duration::from_secs(2);

    let model_dir = match config.asr.local_model_dir {
        Some(path) => path,
        None => {
            return fail_local(
                &options,
                0,
                "local-streaming requires asr.local_model_dir for an installed online model",
            )
            .await;
        }
    };
    let mut recognizer = match crate::local_asr::LocalStreamingRecognizer::new(&model_dir) {
        Ok(recognizer) => recognizer,
        Err(error) => return fail_local(&options, 0, &error.to_string()).await,
    };
    let mut session = RecognitionSession::new("local-streaming");
    let mut sequence = 0_u64;
    let mut capture = match RawCapture::start() {
        Ok(capture) => capture,
        Err(error) => return fail_local(&options, sequence, &format!("capture: {error}")).await,
    };
    emit(&options, &mut sequence, "ready", None, None, None)?;
    let mut stop_signal = Box::pin(tokio::signal::ctrl_c());

    loop {
        tokio::select! {
            signal = &mut stop_signal => {
                signal.map_err(|error| StreamError::Capture(format!("stop signal: {error}")))?;
                if session.request_stop() == SessionAction::Finish {
                    break;
                }
            }
            chunk = capture.next_chunk() => {
                let chunk = match chunk {
                    Ok(chunk) => chunk,
                    Err(error) => return fail_local(&options, sequence, &format!("capture: {error}")).await,
                };
                match chunk {
                    Some(pcm) => {
                        let events = match recognizer.accept_pcm(&pcm) {
                            Ok(events) => events,
                            Err(error) => return fail_local(&options, sequence, &error.to_string()).await,
                        };
                        for event in events {
                            let _ = handle_event(&mut session, event, &options, &mut sequence)?;
                        }
                        if session.state() == SessionState::Failed {
                            return fail_local(&options, sequence, "local recognizer failed").await;
                        }
                    }
                    None => {
                        if session.request_stop() == SessionAction::Finish {
                            break;
                        }
                    }
                }
            }
        }
    }

    let mut cancel_signal = Box::pin(tokio::signal::ctrl_c());
    let tail = tokio::select! {
        signal = &mut cancel_signal => {
            signal.map_err(|error| StreamError::Capture(format!("cancel signal: {error}")))?;
            return cancel_local(&options, &mut sequence).await;
        }
        result = timeout(CAPTURE_STOP_TIMEOUT, capture.stop()) => {
            match result {
                Ok(Ok(tail)) => tail,
                Ok(Err(error)) => return fail_local(&options, sequence, &format!("capture stop: {error}")).await,
                Err(_) => return fail_local(&options, sequence, "capture stop timed out").await,
            }
        }
    };

    if let Some(tail) = tail {
        let events = match recognizer.accept_pcm(&tail) {
            Ok(events) => events,
            Err(error) => return fail_local(&options, sequence, &error.to_string()).await,
        };
        for event in events {
            let _ = handle_event(&mut session, event, &options, &mut sequence)?;
        }
    }

    let events = match recognizer.finish() {
        Ok(events) => events,
        Err(error) => return fail_local(&options, sequence, &error.to_string()).await,
    };
    for event in events {
        let _ = handle_event(&mut session, event, &options, &mut sequence)?;
    }
    if session.state() == SessionState::Failed {
        return fail_local(&options, sequence, "local recognizer failed").await;
    }
    finalize_session(&mut session, &options, &mut sequence).await
}

async fn run_cloud(config: AppConfig, options: StreamOptions) -> Result<i32, StreamError> {
    const CAPTURE_STOP_TIMEOUT: Duration = Duration::from_secs(2);
    const FINISH_TIMEOUT: Duration = Duration::from_secs(2);

    if config.cloud.api_key.trim().is_empty() {
        return Err(StreamError::MissingApiKey);
    }

    let mut realtime = RealtimeSession::connect(
        &config.cloud.realtime_endpoint,
        &config.cloud.api_key,
        &config.cloud.realtime_model,
    )
    .await?;
    let mut session = RecognitionSession::new("cloud-realtime");
    let mut sequence = 0_u64;

    loop {
        match realtime.next_event().await {
            Ok(RealtimeEvent::Ready) => {
                emit(&options, &mut sequence, "ready", None, None, None)?;
                break;
            }
            Ok(RealtimeEvent::Error(message)) => {
                return fail_stream(&realtime, &options, &mut sequence, &message).await;
            }
            Ok(_) => {}
            Err(error) => {
                let message = format!("realtime session: {error}");
                return fail_stream(&realtime, &options, &mut sequence, &message).await;
            }
        }
    }

    let mut capture = match RawCapture::start() {
        Ok(capture) => capture,
        Err(error) => {
            let message = format!("capture: {error}");
            return fail_stream(&realtime, &options, &mut sequence, &message).await;
        }
    };
    let mut stop_signal = Box::pin(tokio::signal::ctrl_c());

    loop {
        tokio::select! {
            signal = &mut stop_signal => {
                signal.map_err(|error| StreamError::Capture(format!("stop signal: {error}")))?;
                if session.request_stop() == SessionAction::Finish {
                    break;
                }
            }
            chunk = capture.next_chunk() => {
                let chunk = match chunk {
                    Ok(chunk) => chunk,
                    Err(error) => {
                        let message = format!("capture: {error}");
                        return fail_stream(&realtime, &options, &mut sequence, &message).await;
                    }
                };
                match chunk {
                    Some(pcm) => {
                        if let Err(error) = realtime.send_audio(&pcm).await {
                            let message = format!("realtime send: {error}");
                            return fail_stream(&realtime, &options, &mut sequence, &message).await;
                        }
                    }
                    None => {
                        if session.request_stop() == SessionAction::Finish {
                            break;
                        }
                    }
                }
            }
            event = realtime.next_event() => {
                let event = match event {
                    Ok(event) => event,
                    Err(error) => {
                        let message = format!("realtime receive: {error}");
                        return fail_stream(&realtime, &options, &mut sequence, &message).await;
                    }
                };
                let _ = handle_event(&mut session, event, &options, &mut sequence)?;
                if session.state() == SessionState::Failed {
                    return finish_failed(&realtime, &options, &mut sequence).await;
                }
            }
        }
    }

    let mut cancel_signal = Box::pin(tokio::signal::ctrl_c());
    let tail = tokio::select! {
        signal = &mut cancel_signal => {
            signal.map_err(|error| StreamError::Capture(format!("cancel signal: {error}")))?;
            return cancel_stream(&realtime, &options, &mut sequence).await;
        }
        result = timeout(CAPTURE_STOP_TIMEOUT, capture.stop()) => {
            match result {
                Ok(Ok(tail)) => tail,
                Ok(Err(error)) => {
                    let message = format!("capture stop: {error}");
                    return fail_stream(&realtime, &options, &mut sequence, &message).await;
                }
                Err(_) => {
                    return fail_stream(
                        &realtime,
                        &options,
                        &mut sequence,
                        "capture stop timed out",
                    )
                    .await;
                }
            }
        }
    };

    if let Some(tail) = tail {
        let result = tokio::select! {
            signal = &mut cancel_signal => {
                signal.map_err(|error| StreamError::Capture(format!("cancel signal: {error}")))?;
                return cancel_stream(&realtime, &options, &mut sequence).await;
            }
            result = timeout(CAPTURE_STOP_TIMEOUT, realtime.send_audio(&tail)) => result,
        };
        match result {
            Ok(Ok(())) => {}
            Ok(Err(error)) => {
                let message = format!("realtime tail send: {error}");
                return fail_stream(&realtime, &options, &mut sequence, &message).await;
            }
            Err(_) => {
                return fail_stream(
                    &realtime,
                    &options,
                    &mut sequence,
                    "realtime tail send timed out",
                )
                .await;
            }
        }
    }

    let finish_deadline = tokio::time::Instant::now() + FINISH_TIMEOUT;
    let finish_result = tokio::select! {
        signal = &mut cancel_signal => {
            signal.map_err(|error| StreamError::Capture(format!("cancel signal: {error}")))?;
            return cancel_stream(&realtime, &options, &mut sequence).await;
        }
        result = timeout_at(finish_deadline, realtime.finish()) => result,
    };
    match finish_result {
        Ok(Ok(())) => {}
        Ok(Err(error)) => {
            let message = format!("realtime finish: {error}");
            return fail_stream(&realtime, &options, &mut sequence, &message).await;
        }
        Err(_) => {
            return fail_stream(
                &realtime,
                &options,
                &mut sequence,
                "realtime finish timed out",
            )
            .await;
        }
    }

    loop {
        let event = tokio::select! {
            signal = &mut cancel_signal => {
                signal.map_err(|error| StreamError::Capture(format!("cancel signal: {error}")))?;
                return cancel_stream(&realtime, &options, &mut sequence).await;
            }
            result = timeout_at(finish_deadline, realtime.next_event()) => {
                match result {
                    Ok(Ok(event)) => event,
                    Ok(Err(error)) => {
                        let message = format!("realtime final receive: {error}");
                        return fail_stream(&realtime, &options, &mut sequence, &message).await;
                    }
                    Err(_) => {
                        return fail_stream(
                            &realtime,
                            &options,
                            &mut sequence,
                            "realtime final event timed out",
                        )
                        .await;
                    }
                }
            }
        };
        let _ = handle_event(&mut session, event, &options, &mut sequence)?;
        if session.state() == SessionState::Failed {
            return finish_failed(&realtime, &options, &mut sequence).await;
        }
        if session.state() == SessionState::Completed {
            break;
        }
    }

    finalize_session(&mut session, &options, &mut sequence).await
}

async fn cancel_local(options: &StreamOptions, sequence: &mut u64) -> Result<i32, StreamError> {
    emit(options, sequence, "cancelled", None, None, None)?;
    emit(options, sequence, "completed", None, None, None)?;
    Ok(130)
}

async fn fail_local(
    options: &StreamOptions,
    sequence: u64,
    message: &str,
) -> Result<i32, StreamError> {
    let mut sequence = sequence;
    emit(options, &mut sequence, "error", None, Some(message), None)?;
    emit(options, &mut sequence, "completed", None, None, None)?;
    Ok(1)
}

async fn finalize_session(
    session: &mut RecognitionSession,
    options: &StreamOptions,
    sequence: &mut u64,
) -> Result<i32, StreamError> {
    if let Some(text) = session.take_injection_text() {
        let injection = if options.inject {
            Some(inject_text(&text).await?)
        } else {
            None
        };
        emit(
            options,
            sequence,
            "finalized",
            None,
            None,
            injection.as_ref(),
        )?;
    }
    emit(options, sequence, "completed", None, None, None)?;
    Ok(0)
}

async fn cancel_stream(
    realtime: &RealtimeSession,
    options: &StreamOptions,
    sequence: &mut u64,
) -> Result<i32, StreamError> {
    let _ = realtime.cancel().await;
    emit(options, sequence, "cancelled", None, None, None)?;
    emit(options, sequence, "completed", None, None, None)?;
    Ok(130)
}

async fn fail_stream(
    realtime: &RealtimeSession,
    options: &StreamOptions,
    sequence: &mut u64,
    message: &str,
) -> Result<i32, StreamError> {
    let _ = realtime.cancel().await;
    emit(options, sequence, "error", None, Some(message), None)?;
    emit(options, sequence, "completed", None, None, None)?;
    Ok(1)
}

async fn finish_failed(
    realtime: &RealtimeSession,
    options: &StreamOptions,
    sequence: &mut u64,
) -> Result<i32, StreamError> {
    let _ = realtime.cancel().await;
    emit(options, sequence, "completed", None, None, None)?;
    Ok(1)
}

fn handle_event(
    session: &mut RecognitionSession,
    event: RealtimeEvent,
    options: &StreamOptions,
    sequence: &mut u64,
) -> Result<bool, StreamError> {
    let update = session.apply(event);
    match update {
        SessionUpdate::Transcript(snapshot) => {
            emit(options, sequence, "transcript", Some(&snapshot), None, None)?;
            Ok(false)
        }
        SessionUpdate::Final(snapshot) => {
            emit(options, sequence, "transcript", Some(&snapshot), None, None)?;
            Ok(true)
        }
        SessionUpdate::Error(message) => {
            emit(options, sequence, "error", None, Some(&message), None)?;
            Ok(true)
        }
        SessionUpdate::Ignored => Ok(false),
    }
}

fn emit(
    options: &StreamOptions,
    sequence: &mut u64,
    kind: &str,
    transcript: Option<&TranscriptSnapshot>,
    message: Option<&str>,
    injection: Option<&InjectionResult>,
) -> Result<(), StreamError> {
    *sequence += 1;
    if options.json {
        let event = JsonEvent {
            kind,
            sequence: *sequence,
            transcript,
            message,
            injection,
        };
        println!(
            "{}",
            serde_json::to_string(&event)
                .map_err(|error| StreamError::Capture(error.to_string()))?
        );
    } else if kind == "transcript" {
        if let Some(snapshot) = transcript {
            if snapshot.is_final {
                println!("{}", snapshot.authoritative_text);
            } else {
                eprint!("\r{}", snapshot.authoritative_text);
                io::stderr().flush().ok();
            }
        }
    } else if kind == "error" {
        if let Some(message) = message {
            eprintln!("{message}");
        }
    }
    Ok(())
}

async fn inject_text(text: &str) -> Result<InjectionResult, StreamError> {
    let child = Command::new("wtype")
        .arg("--")
        .arg(text)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| StreamError::Injection(error.to_string()))?;
    let output = timeout(Duration::from_secs(1), child.wait_with_output())
        .await
        .map_err(|_| StreamError::Injection("wtype timed out".to_owned()))?
        .map_err(|error| StreamError::Injection(error.to_string()))?;
    if output.status.success() {
        Ok(InjectionResult {
            ok: true,
            method: "wtype".to_owned(),
            message: String::new(),
        })
    } else {
        Ok(InjectionResult {
            ok: false,
            method: "wtype".to_owned(),
            message: String::from_utf8_lossy(&output.stderr).trim().to_owned(),
        })
    }
}
