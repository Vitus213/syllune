use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::io;
use tokio::sync::mpsc;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::header::{HeaderValue, AUTHORIZATION};
use tokio_tungstenite::tungstenite::Message;

#[derive(Debug, PartialEq, Eq)]
pub enum RealtimeEvent {
    Ready,
    Partial { text: String, stash: String },
    Completed { transcript: String },
    Finished { transcript: String },
    Error(String),
}

enum Command {
    Text(String),
    Close,
}

pub struct RealtimeSession {
    commands: mpsc::Sender<Command>,
    events: mpsc::Receiver<io::Result<RealtimeEvent>>,
    finish_sent: bool,
}

impl RealtimeSession {
    pub async fn connect(endpoint: &str, api_key: &str, model: &str) -> io::Result<Self> {
        let separator = if endpoint.contains('?') { '&' } else { '?' };
        let url = format!("{endpoint}{separator}model={model}");
        let mut request = url
            .into_client_request()
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error.to_string()))?;
        let authorization = format!("Bearer {api_key}");
        request.headers_mut().insert(
            AUTHORIZATION,
            HeaderValue::from_str(&authorization)
                .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error.to_string()))?,
        );
        request
            .headers_mut()
            .insert("OpenAI-Beta", HeaderValue::from_static("realtime=v1"));

        let (socket, _) = connect_async(request)
            .await
            .map_err(|error| io::Error::new(io::ErrorKind::ConnectionRefused, error.to_string()))?;
        let (mut writer, mut reader) = socket.split();
        let (command_tx, mut command_rx) = mpsc::channel::<Command>(16);
        let (event_tx, event_rx) = mpsc::channel::<io::Result<RealtimeEvent>>(32);

        tokio::spawn(async move {
            while let Some(command) = command_rx.recv().await {
                let should_close = matches!(&command, Command::Close);
                let message = match command {
                    Command::Text(text) => Message::Text(text.into()),
                    Command::Close => Message::Close(None),
                };
                if writer.send(message).await.is_err() {
                    break;
                }
                if should_close {
                    break;
                }
            }
            let _ = writer.close().await;
        });

        tokio::spawn(async move {
            loop {
                let message = match reader.next().await {
                    Some(Ok(message)) => message,
                    Some(Err(error)) => {
                        let _ = event_tx.send(Err(socket_error(error))).await;
                        break;
                    }
                    None => {
                        let _ = event_tx
                            .send(Err(io::Error::new(
                                io::ErrorKind::UnexpectedEof,
                                "realtime socket closed",
                            )))
                            .await;
                        break;
                    }
                };
                match message {
                    Message::Text(text) => match parse_event(text.as_ref()) {
                        Ok(Some(event)) => {
                            if event_tx.send(Ok(event)).await.is_err() {
                                break;
                            }
                        }
                        Ok(None) => {}
                        Err(error) => {
                            let _ = event_tx.send(Err(error)).await;
                            break;
                        }
                    },
                    Message::Ping(payload) => {
                        let _ = event_tx.send(Ok(RealtimeEvent::Ready)).await;
                        let _ = payload;
                    }
                    Message::Close(frame) => {
                        let reason = frame
                            .map(|value| value.reason.to_string())
                            .unwrap_or_else(|| "realtime socket closed".to_owned());
                        let _ = event_tx
                            .send(Err(io::Error::new(io::ErrorKind::UnexpectedEof, reason)))
                            .await;
                        break;
                    }
                    Message::Binary(_) | Message::Pong(_) | Message::Frame(_) => {}
                }
            }
        });

        let session = Self {
            commands: command_tx,
            events: event_rx,
            finish_sent: false,
        };
        session
            .send_text(json!({
                "type": "session.update",
                "session": {
                    "modalities": ["text"],
                    "input_audio_format": "pcm",
                    "sample_rate": 16000,
                    "input_audio_transcription": {"language": "zh"},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.3,
                        "silence_duration_ms": 600
                    }
                }
            }))
            .await?;
        Ok(session)
    }

    pub async fn send_audio(&self, pcm: &[u8]) -> io::Result<()> {
        let audio = base64::Engine::encode(&base64::engine::general_purpose::STANDARD, pcm);
        self.send_text(json!({
            "type": "input_audio_buffer.append",
            "audio": audio
        }))
        .await
    }

    pub async fn next_event(&mut self) -> io::Result<RealtimeEvent> {
        self.events.recv().await.ok_or_else(|| {
            io::Error::new(io::ErrorKind::UnexpectedEof, "realtime event task closed")
        })?
    }

    pub async fn finish(&mut self) -> io::Result<()> {
        if self.finish_sent {
            return Ok(());
        }
        self.finish_sent = true;
        self.send_text(json!({"type": "session.finish"})).await
    }

    pub async fn cancel(&self) -> io::Result<()> {
        self.commands
            .send(Command::Close)
            .await
            .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "realtime writer closed"))
    }

    async fn send_text(&self, value: Value) -> io::Result<()> {
        self.commands
            .send(Command::Text(value.to_string()))
            .await
            .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "realtime writer closed"))
    }
}

fn parse_event(text: &str) -> io::Result<Option<RealtimeEvent>> {
    let value: Value = serde_json::from_str(text)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error.to_string()))?;
    let event = match value
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or_default()
    {
        "session.created" | "session.updated" => Some(RealtimeEvent::Ready),
        "conversation.item.input_audio_transcription.text" => Some(RealtimeEvent::Partial {
            text: string_field(&value, "text"),
            stash: string_field(&value, "stash"),
        }),
        "conversation.item.input_audio_transcription.completed" => Some(RealtimeEvent::Completed {
            transcript: string_field(&value, "transcript"),
        }),
        "session.finished" => Some(RealtimeEvent::Finished {
            transcript: string_field(&value, "transcript"),
        }),
        "error" => {
            return Ok(Some(RealtimeEvent::Error(
                value
                    .get("error")
                    .and_then(|error| error.get("message"))
                    .and_then(Value::as_str)
                    .or_else(|| value.get("message").and_then(Value::as_str))
                    .unwrap_or("realtime ASR request failed")
                    .to_owned(),
            )))
        }
        _ => None,
    };
    Ok(event)
}

fn string_field(value: &Value, name: &str) -> String {
    value
        .get(name)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned()
}

fn socket_error(error: tokio_tungstenite::tungstenite::Error) -> io::Error {
    io::Error::new(io::ErrorKind::ConnectionAborted, error.to_string())
}
