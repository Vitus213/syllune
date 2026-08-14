#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct TranscriptSnapshot {
    pub confirmed_segments: Vec<String>,
    pub partial_text: String,
    pub authoritative_text: String,
    pub is_final: bool,
    pub backend: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionState {
    Recording,
    Stopping,
    Completed,
    Cancelled,
    Failed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionAction {
    Finish,
    Cancel,
    Ignore,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SessionUpdate {
    Transcript(TranscriptSnapshot),
    Final(TranscriptSnapshot),
    Error(String),
    Ignored,
}

pub struct RecognitionSession {
    backend: String,
    state: SessionState,
    confirmed_segments: Vec<String>,
    partial_text: String,
    injection_text: Option<String>,
}

impl RecognitionSession {
    pub fn new(backend: impl Into<String>) -> Self {
        Self {
            backend: backend.into(),
            state: SessionState::Recording,
            confirmed_segments: Vec::new(),
            partial_text: String::new(),
            injection_text: None,
        }
    }

    pub fn state(&self) -> SessionState {
        self.state
    }

    pub fn request_stop(&mut self) -> SessionAction {
        match self.state {
            SessionState::Recording => {
                self.state = SessionState::Stopping;
                SessionAction::Finish
            }
            SessionState::Stopping => {
                self.state = SessionState::Cancelled;
                self.injection_text = None;
                SessionAction::Cancel
            }
            SessionState::Completed | SessionState::Cancelled | SessionState::Failed => {
                SessionAction::Ignore
            }
        }
    }

    pub fn apply(&mut self, event: crate::realtime::RealtimeEvent) -> SessionUpdate {
        if matches!(self.state, SessionState::Cancelled | SessionState::Failed) {
            return SessionUpdate::Ignored;
        }
        match event {
            crate::realtime::RealtimeEvent::Partial { text, stash } => {
                self.partial_text = format!("{text}{stash}");
                SessionUpdate::Transcript(self.snapshot(false))
            }
            crate::realtime::RealtimeEvent::Completed { transcript } => {
                if !transcript.is_empty() && self.confirmed_segments.last() != Some(&transcript) {
                    self.confirmed_segments.push(transcript);
                }
                self.partial_text.clear();
                SessionUpdate::Transcript(self.snapshot(false))
            }
            crate::realtime::RealtimeEvent::Finished { transcript } => {
                if self.state != SessionState::Stopping {
                    return SessionUpdate::Ignored;
                }
                let authoritative_text = if transcript.is_empty() {
                    self.confirmed_segments.join("")
                } else {
                    transcript
                };
                self.state = SessionState::Completed;
                self.partial_text.clear();
                self.injection_text =
                    (!authoritative_text.is_empty()).then(|| authoritative_text.clone());
                SessionUpdate::Final(TranscriptSnapshot {
                    confirmed_segments: self.confirmed_segments.clone(),
                    partial_text: String::new(),
                    authoritative_text,
                    is_final: true,
                    backend: self.backend.clone(),
                })
            }
            crate::realtime::RealtimeEvent::Error(message) => {
                self.state = SessionState::Failed;
                self.injection_text = None;
                SessionUpdate::Error(message)
            }
            crate::realtime::RealtimeEvent::Ready => SessionUpdate::Ignored,
        }
    }

    pub fn take_injection_text(&mut self) -> Option<String> {
        self.injection_text.take()
    }

    fn snapshot(&self, is_final: bool) -> TranscriptSnapshot {
        TranscriptSnapshot {
            confirmed_segments: self.confirmed_segments.clone(),
            partial_text: self.partial_text.clone(),
            authoritative_text: self.confirmed_segments.join("") + &self.partial_text,
            is_final,
            backend: self.backend.clone(),
        }
    }
}
