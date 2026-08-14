//! Single owner of one recognition session: capture lifecycle, backend
//! transport, transcript accumulation and the one-shot final injection.
//!
//! The coordinator enforces the fixed stop order (capture stop -> tail
//! delivery -> backend finish -> final event -> optional single injection)
//! and guarantees that cancel/error paths never inject partial text.

use std::future::Future;
use std::io;
use std::time::Duration;

use serde::Serialize;
use tokio::sync::mpsc;
use tokio::time::timeout_at;

use crate::realtime::RealtimeEvent;
use crate::session::{
    RecognitionSession, SessionAction, SessionState, SessionUpdate, TranscriptSnapshot,
};

pub const DEFAULT_QUEUE_CAPACITY: usize = 16;
pub const DEFAULT_SEND_DEADLINE: Duration = Duration::from_millis(500);
pub const DEFAULT_FINISH_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ControlCommand {
    Stop,
    Cancel,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct InjectionResult {
    pub ok: bool,
    pub method: String,
    pub message: String,
}

#[derive(Debug, Clone)]
pub enum OutputEvent {
    Ready,
    Transcript(TranscriptSnapshot),
    Finalized { injection: Option<InjectionResult> },
    Warning(String),
    Error(String),
    Cancelled,
    Completed,
}

#[derive(Debug, Clone)]
pub struct SessionPlan {
    pub backend: String,
    pub inject: bool,
    pub queue_capacity: usize,
    pub send_deadline: Duration,
    pub finish_timeout: Duration,
}

impl SessionPlan {
    pub fn new(backend: impl Into<String>, inject: bool) -> Self {
        Self {
            backend: backend.into(),
            inject,
            queue_capacity: DEFAULT_QUEUE_CAPACITY,
            send_deadline: DEFAULT_SEND_DEADLINE,
            finish_timeout: DEFAULT_FINISH_TIMEOUT,
        }
    }
}

/// Audio source boundary. `start` MUST be called only after the backend is
/// ready; `stop_capture` performs a graceful stop and returns an optional
/// aligned tail frame; `abort` terminates capture without draining.
pub trait AudioCapture {
    async fn start(&mut self) -> io::Result<()>;
    async fn next_chunk(&mut self) -> io::Result<Option<Vec<u8>>>;
    async fn stop_capture(&mut self) -> io::Result<Option<Vec<u8>>>;
    fn abort(&mut self) {}
}

/// Realtime backend boundary. Events follow the same semantics for cloud and
/// local backends (`RealtimeEvent`).
pub trait BackendTransport {
    async fn send_audio(&mut self, pcm: &[u8]) -> io::Result<()>;
    async fn next_event(&mut self) -> io::Result<RealtimeEvent>;
    async fn finish(&mut self) -> io::Result<()>;
    async fn cancel(&mut self) -> io::Result<()>;
}

pub trait TextInjector {
    async fn inject(&mut self, text: &str) -> InjectionResult;
}

pub trait EventSink {
    fn emit(&mut self, event: OutputEvent) -> io::Result<()>;
}

/// Control input wrapper: a closed channel stops delivering commands but is
/// never treated as a cancellation itself.
pub struct ControlInput {
    receiver: mpsc::Receiver<ControlCommand>,
    closed: bool,
}

impl ControlInput {
    pub fn new(receiver: mpsc::Receiver<ControlCommand>) -> Self {
        Self {
            receiver,
            closed: false,
        }
    }
}

enum Step<T> {
    Value(T),
    TimedOut,
    Command(ControlCommand),
}

/// Run one recognition session to completion and return the process exit
/// code: `0` success, `1` failure, `130` cancellation.
pub async fn run_session<C, T, J, S>(
    plan: SessionPlan,
    mut capture: C,
    mut transport: T,
    mut injector: J,
    control: mpsc::Receiver<ControlCommand>,
    sink: &mut S,
) -> i32
where
    C: AudioCapture,
    T: BackendTransport,
    J: TextInjector,
    S: EventSink,
{
    let mut control = ControlInput::new(control);
    let mut session = RecognitionSession::new(plan.backend.clone());

    if let Err(error) = capture.start().await {
        return fail(sink, &format!("capture: {error}"));
    }
    if sink.emit(OutputEvent::Ready).is_err() {
        return 1;
    }

    loop {
        tokio::select! {
            biased;
            command = control.receiver.recv(), if !control.closed => {
                let action = match command {
                    Some(ControlCommand::Stop) => session.request_stop(),
                    Some(ControlCommand::Cancel) => SessionAction::Cancel,
                    None => {
                        control.closed = true;
                        continue;
                    }
                };
                match action {
                    SessionAction::Finish => break,
                    SessionAction::Cancel => {
                        return cancel_path(&mut capture, &mut transport, sink).await
                    }
                    SessionAction::Ignore => {}
                }
            }
            chunk = capture.next_chunk() => match chunk {
                Ok(Some(pcm)) => {
                    let send_deadline = tokio::time::Instant::now() + plan.send_deadline;
                    match timed_step(&mut control, send_deadline, transport.send_audio(&pcm))
                        .await
                    {
                        Step::Value(Ok(())) => {}
                        Step::Value(Err(error)) => {
                            return fail(sink, &format!("audio send: {error}"))
                        }
                        Step::TimedOut => return fail(sink, "audio send exceeded the deadline"),
                        Step::Command(command) => {
                            return handle_flush_command(
                                command, &mut session, &mut capture, &mut transport, sink,
                            )
                            .await
                        }
                    }
                }
                Ok(None) => {
                    if session.state() == SessionState::Recording {
                        let _ = session.request_stop();
                    }
                    break;
                }
                Err(error) => return fail(sink, &format!("capture: {error}")),
            },
            event = transport.next_event() => match event {
                Ok(event) => {
                    if let Err(code) = dispatch_event(&mut session, sink, event) {
                        let _ = sink.emit(OutputEvent::Completed);
                        return code;
                    }
                    if session.state() == SessionState::Failed {
                        let _ = sink.emit(OutputEvent::Completed);
                        return 1;
                    }
                }
                Err(error) => return fail(sink, &format!("realtime receive: {error}")),
            },
        }
    }

    let stop_deadline = tokio::time::Instant::now() + plan.finish_timeout;
    let tail = match timed_step(&mut control, stop_deadline, capture.stop_capture()).await {
        Step::Value(Ok(tail)) => tail,
        Step::Value(Err(error)) => return fail(sink, &format!("capture stop: {error}")),
        Step::TimedOut => return fail(sink, "capture stop timed out"),
        Step::Command(command) => {
            return handle_flush_command(
                command, &mut session, &mut capture, &mut transport, sink,
            )
            .await
        }
    };
    if let Some(tail) = tail {
        match timed_step(&mut control, stop_deadline, transport.send_audio(&tail)).await {
            Step::Value(Ok(())) => {}
            Step::Value(Err(error)) => return fail(sink, &format!("tail send: {error}")),
            Step::TimedOut => return fail(sink, "tail send timed out"),
            Step::Command(command) => {
                return handle_flush_command(
                    command, &mut session, &mut capture, &mut transport, sink,
                )
                .await
            }
        }
    }
    match timed_step(&mut control, stop_deadline, transport.finish()).await {
        Step::Value(Ok(())) => {}
        Step::Value(Err(error)) => return fail(sink, &format!("backend finish: {error}")),
        Step::TimedOut => return fail(sink, "backend finish timed out"),
        Step::Command(command) => {
            return handle_flush_command(
                command, &mut session, &mut capture, &mut transport, sink,
            )
            .await
        }
    }

    loop {
        let step = timed_step(&mut control, stop_deadline, transport.next_event()).await;
        let event = match step {
            Step::Value(Ok(event)) => event,
            Step::Value(Err(error)) => return fail(sink, &format!("final receive: {error}")),
            Step::TimedOut => return fail(sink, "final event timed out"),
            Step::Command(command) => {
                return handle_flush_command(
                    command, &mut session, &mut capture, &mut transport, sink,
                )
                .await
            }
        };
        if let Err(code) = dispatch_event(&mut session, sink, event) {
            let _ = sink.emit(OutputEvent::Completed);
            return code;
        }
        match session.state() {
            SessionState::Completed => break,
            SessionState::Failed | SessionState::Cancelled => {
                let _ = sink.emit(OutputEvent::Completed);
                return 1;
            }
            _ => {}
        }
    }

    match session.take_injection_text() {
        Some(text) => {
            let injection = if plan.inject {
                Some(injector.inject(&text).await)
            } else {
                None
            };
            let _ = sink.emit(OutputEvent::Finalized { injection });
        }
        None => {
            let _ = sink.emit(OutputEvent::Warning(
                "no speech text was recognized".to_owned(),
            ));
            let _ = sink.emit(OutputEvent::Finalized { injection: None });
        }
    }
    let _ = sink.emit(OutputEvent::Completed);
    0
}

/// Any control command arriving during the stop flush escalates to cancel:
/// a second stop request or an explicit cancel both abort the flush.
async fn handle_flush_command<C, T, S>(
    command: ControlCommand,
    session: &mut RecognitionSession,
    capture: &mut C,
    transport: &mut T,
    sink: &mut S,
) -> i32
where
    C: AudioCapture,
    T: BackendTransport,
    S: EventSink,
{
    let _ = command;
    let _ = session.request_stop();
    cancel_path(capture, transport, sink).await
}

async fn timed_step<F, T>(
    control: &mut ControlInput,
    deadline: tokio::time::Instant,
    future: F,
) -> Step<io::Result<T>>
where
    F: Future<Output = io::Result<T>>,
{
    tokio::pin!(future);
    loop {
        tokio::select! {
            biased;
            command = control.receiver.recv(), if !control.closed => match command {
                Some(command) => return Step::Command(command),
                None => control.closed = true,
            },
            result = timeout_at(deadline, &mut future) => {
                return match result {
                    Ok(value) => Step::Value(value),
                    Err(_) => Step::TimedOut,
                };
            }
        }
    }
}

async fn cancel_path<C, T, S>(capture: &mut C, transport: &mut T, sink: &mut S) -> i32
where
    C: AudioCapture,
    T: BackendTransport,
    S: EventSink,
{
    capture.abort();
    let _ = transport.cancel().await;
    let _ = sink.emit(OutputEvent::Cancelled);
    let _ = sink.emit(OutputEvent::Completed);
    130
}

fn fail<S>(sink: &mut S, message: &str) -> i32
where
    S: EventSink,
{
    let _ = sink.emit(OutputEvent::Error(message.to_owned()));
    let _ = sink.emit(OutputEvent::Completed);
    1
}

fn dispatch_event<S>(
    session: &mut RecognitionSession,
    sink: &mut S,
    event: RealtimeEvent,
) -> Result<(), i32>
where
    S: EventSink,
{
    let update = session.apply(event);
    match update {
        SessionUpdate::Transcript(snapshot) | SessionUpdate::Final(snapshot) => {
            if !(snapshot.is_final && snapshot.authoritative_text.is_empty()) {
                let _ = sink.emit(OutputEvent::Transcript(snapshot));
            }
        }
        SessionUpdate::Error(message) => {
            let _ = sink.emit(OutputEvent::Error(message));
            return Err(1);
        }
        SessionUpdate::Ignored => {}
    }
    Ok(())
}
