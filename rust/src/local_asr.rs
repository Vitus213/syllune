use std::path::{Path, PathBuf};

use sherpa_onnx::{OnlineRecognizer, OnlineRecognizerConfig, OnlineStream};
use thiserror::Error;

use crate::realtime::RealtimeEvent;

const SAMPLE_RATE: i32 = 16_000;
const MODEL_ENCODER: &str = "encoder.int8.onnx";
const MODEL_DECODER: &str = "decoder.int8.onnx";
const MODEL_TOKENS: &str = "tokens.txt";

#[derive(Debug, Error)]
pub enum LocalAsrError {
    #[error("local-streaming model directory does not exist: {0}")]
    ModelDirectoryMissing(PathBuf),
    #[error("local-streaming model is missing required file: {0}")]
    MissingModelFile(PathBuf),
    #[error("local-streaming model path is not valid UTF-8: {0}")]
    InvalidModelPath(PathBuf),
    #[error("local-streaming recognizer could not be created from model directory: {0}")]
    RecognizerUnavailable(PathBuf),
    #[error("local-streaming received an incomplete PCM16 frame ({0} bytes)")]
    IncompletePcm(usize),
}

#[derive(Debug, Clone)]
struct ModelFiles {
    encoder: PathBuf,
    decoder: PathBuf,
    tokens: PathBuf,
}

pub struct LocalStreamingRecognizer {
    recognizer: OnlineRecognizer,
    stream: OnlineStream,
    samples: Vec<f32>,
    last_partial: String,
    confirmed_text: String,
}

impl LocalStreamingRecognizer {
    pub fn new(model_dir: impl AsRef<Path>) -> Result<Self, LocalAsrError> {
        let model_dir = model_dir.as_ref();
        let files = required_model_files(model_dir)?;
        let mut config = OnlineRecognizerConfig::default();
        config.model_config.paraformer.encoder = Some(path_string(&files.encoder)?);
        config.model_config.paraformer.decoder = Some(path_string(&files.decoder)?);
        config.model_config.tokens = Some(path_string(&files.tokens)?);
        config.model_config.provider = Some("cpu".to_owned());
        config.model_config.num_threads = 1;
        config.decoding_method = Some("greedy_search".to_owned());
        config.enable_endpoint = true;
        config.rule1_min_trailing_silence = 2.4;
        config.rule2_min_trailing_silence = 1.2;
        config.rule3_min_utterance_length = 20.0;

        let recognizer = OnlineRecognizer::create(&config)
            .ok_or_else(|| LocalAsrError::RecognizerUnavailable(model_dir.to_owned()))?;
        let stream = recognizer.create_stream();
        Ok(Self {
            recognizer,
            stream,
            samples: Vec::new(),
            last_partial: String::new(),
            confirmed_text: String::new(),
        })
    }

    pub fn accept_pcm(&mut self, pcm: &[u8]) -> Result<Vec<RealtimeEvent>, LocalAsrError> {
        if !pcm.len().is_multiple_of(2) {
            return Err(LocalAsrError::IncompletePcm(pcm.len()));
        }
        self.samples.clear();
        self.samples.reserve(pcm.len() / 2);
        for bytes in pcm.chunks_exact(2) {
            let sample = i16::from_le_bytes([bytes[0], bytes[1]]);
            self.samples.push(f32::from(sample) / f32::from(i16::MAX));
        }
        self.stream.accept_waveform(SAMPLE_RATE, &self.samples);
        Ok(self.decode_ready())
    }

    pub fn finish(&mut self) -> Result<Vec<RealtimeEvent>, LocalAsrError> {
        self.stream.input_finished();
        let mut events = self.decode_ready();
        let trailing = self.current_text();
        let transcript = if trailing.is_empty() {
            self.confirmed_text.clone()
        } else {
            format!("{}{}", self.confirmed_text, trailing)
        };
        events.push(RealtimeEvent::Finished { transcript });
        Ok(events)
    }

    fn decode_ready(&mut self) -> Vec<RealtimeEvent> {
        while self.recognizer.is_ready(&self.stream) {
            self.recognizer.decode(&self.stream);
        }

        let text = self.current_text();
        if self.recognizer.is_endpoint(&self.stream) {
            let mut events = Vec::new();
            if !text.is_empty() {
                self.confirmed_text.push_str(&text);
                events.push(RealtimeEvent::Completed { transcript: text });
            }
            self.last_partial.clear();
            self.recognizer.reset(&self.stream);
            events
        } else if !text.is_empty() && text != self.last_partial {
            self.last_partial = text.clone();
            vec![RealtimeEvent::Partial {
                text,
                stash: String::new(),
            }]
        } else {
            Vec::new()
        }
    }

    fn current_text(&self) -> String {
        self.recognizer
            .get_result(&self.stream)
            .map(|result| result.text)
            .unwrap_or_default()
    }
}

fn required_model_files(model_dir: &Path) -> Result<ModelFiles, LocalAsrError> {
    if !model_dir.is_dir() {
        return Err(LocalAsrError::ModelDirectoryMissing(model_dir.to_owned()));
    }
    let encoder = required_file(model_dir, MODEL_ENCODER)?;
    let decoder = required_file(model_dir, MODEL_DECODER)?;
    let tokens = required_file(model_dir, MODEL_TOKENS)?;
    Ok(ModelFiles {
        encoder,
        decoder,
        tokens,
    })
}

fn required_file(model_dir: &Path, name: &str) -> Result<PathBuf, LocalAsrError> {
    let path = model_dir.join(name);
    if path.is_file() {
        Ok(path)
    } else {
        Err(LocalAsrError::MissingModelFile(path))
    }
}

fn path_string(path: &Path) -> Result<String, LocalAsrError> {
    path.to_str()
        .map(ToOwned::to_owned)
        .ok_or_else(|| LocalAsrError::InvalidModelPath(path.to_owned()))
}
