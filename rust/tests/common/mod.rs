#![allow(dead_code)]

//! Shared fakes for coordinator lifecycle tests. Each fake records its calls
//! in a shared log so tests can assert ordering at the system boundaries.

use std::collections::VecDeque;
use std::io;
use std::sync::Arc;
use std::time::Duration;

use parking_lot::Mutex;
use syllune::coordinator::{
    AudioCapture, BackendTransport, ControlCommand, EventSink, InjectionResult, OutputEvent,
    TextInjector,
};
use syllune::realtime::RealtimeEvent;
use tokio::sync::mpsc;

pub type Log = Arc<Mutex<Vec<String>>>;

pub fn new_log() -> Log {
    Arc::new(Mutex::new(Vec::new()))
}

pub fn entries(log: &Log) -> Vec<String> {
    log.lock().clone()
}

pub fn ok_fake_injection() -> InjectionResult {
    InjectionResult {
        ok: true,
        method: "fake".to_owned(),
        message: String::new(),
    }
}

#[derive(Default)]
pub struct RecordingSink {
    pub events: Vec<OutputEvent>,
}

impl EventSink for RecordingSink {
    fn emit(&mut self, event: OutputEvent) -> io::Result<()> {
        self.events.push(event);
        Ok(())
    }
}

pub struct FakeCapture {
    pub log: Log,
    pub chunks: VecDeque<Vec<u8>>,
    pub tail: Option<Vec<u8>>,
    pub eof_on_empty: bool,
    pub start_error: Option<String>,
    pub stop_error: Option<String>,
}

impl FakeCapture {
    pub fn new(log: Log, chunks: Vec<Vec<u8>>, tail: Option<Vec<u8>>) -> Self {
        Self {
            log,
            chunks: chunks.into(),
            tail,
            eof_on_empty: false,
            start_error: None,
            stop_error: None,
        }
    }
}

impl AudioCapture for FakeCapture {
    async fn start(&mut self) -> io::Result<()> {
        self.log.lock().push("capture.start".to_owned());
        if let Some(message) = self.start_error.take() {
            return Err(io::Error::new(io::ErrorKind::Other, message));
        }
        Ok(())
    }

    async fn next_chunk(&mut self) -> io::Result<Option<Vec<u8>>> {
        if let Some(chunk) = self.chunks.pop_front() {
            self.log
                .lock()
                .push(format!("capture.chunk:{}", chunk.len()));
            return Ok(Some(chunk));
        }
        if self.eof_on_empty {
            return Ok(None);
        }
        std::future::pending::<()>().await;
        unreachable!("pending future resolved")
    }

    async fn stop_capture(&mut self) -> io::Result<Option<Vec<u8>>> {
        self.log.lock().push("capture.stop".to_owned());
        if let Some(message) = self.stop_error.take() {
            return Err(io::Error::new(io::ErrorKind::Other, message));
        }
        Ok(self.tail.take())
    }

    fn abort(&mut self) {
        self.log.lock().push("capture.abort".to_owned());
    }
}

pub struct ScriptedTransport {
    pub log: Log,
    pub before_finish: VecDeque<io::Result<RealtimeEvent>>,
    pub after_finish: VecDeque<io::Result<RealtimeEvent>>,
    pub finished: bool,
    pub appends: usize,
    /// After the Nth audio append, send the command to the coordinator.
    pub triggers: Vec<(usize, ControlCommand)>,
    pub control_tx: Option<mpsc::Sender<ControlCommand>>,
    pub append_delay: Duration,
    pub finish_error: Option<String>,
    pub fail_after_appends: Option<usize>,
}

impl ScriptedTransport {
    pub fn new(log: Log) -> Self {
        Self {
            log,
            before_finish: VecDeque::new(),
            after_finish: VecDeque::new(),
            finished: false,
            appends: 0,
            triggers: Vec::new(),
            control_tx: None,
            append_delay: Duration::ZERO,
            finish_error: None,
            fail_after_appends: None,
        }
    }
}

impl BackendTransport for ScriptedTransport {
    async fn send_audio(&mut self, pcm: &[u8]) -> io::Result<()> {
        if self.append_delay > Duration::ZERO {
            tokio::time::sleep(self.append_delay).await;
        }
        if self.fail_after_appends == Some(self.appends) {
            return Err(io::Error::new(
                io::ErrorKind::ConnectionAborted,
                "transport send failed",
            ));
        }
        self.log
            .lock()
            .push(format!("transport.append:{}", pcm.len()));
        self.appends += 1;
        let mut fired = Vec::new();
        self.triggers.retain(|(at, command)| {
            if *at == self.appends {
                fired.push(*command);
                false
            } else {
                true
            }
        });
        if let Some(tx) = &self.control_tx {
            for command in fired {
                let _ = tx.send(command).await;
            }
        }
        Ok(())
    }

    async fn next_event(&mut self) -> io::Result<RealtimeEvent> {
        if let Some(event) = self.before_finish.pop_front() {
            return event;
        }
        if self.finished {
            if let Some(event) = self.after_finish.pop_front() {
                return event;
            }
        }
        std::future::pending::<()>().await;
        unreachable!("pending future resolved")
    }

    async fn finish(&mut self) -> io::Result<()> {
        self.log.lock().push("transport.finish".to_owned());
        if let Some(message) = self.finish_error.take() {
            return Err(io::Error::new(io::ErrorKind::Other, message));
        }
        self.finished = true;
        Ok(())
    }

    async fn cancel(&mut self) -> io::Result<()> {
        self.log.lock().push("transport.cancel".to_owned());
        Ok(())
    }
}

pub struct RecordingInjector {
    pub log: Log,
    pub result_ok: bool,
}

impl RecordingInjector {
    pub fn new(log: Log) -> Self {
        Self {
            log,
            result_ok: true,
        }
    }
}

impl TextInjector for RecordingInjector {
    async fn inject(&mut self, text: &str) -> InjectionResult {
        self.log
            .lock()
            .push(format!("injector.inject:{text}"));
        InjectionResult {
            ok: self.result_ok,
            method: "fake".to_owned(),
            message: String::new(),
        }
    }
}
