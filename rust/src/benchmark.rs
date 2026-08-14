//! Quality benchmark: replay versioned corpus audio through a realtime
//! backend, measure content CER per sample and aggregate a versioned JSON
//! report. Dev and test splits stay disjoint; cloud and local reports are
//! never merged.

use std::io;
use std::path::Path;
use std::time::Duration;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CorpusEntry {
    pub id: String,
    pub reference: String,
    pub voice: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct SampleResult {
    pub id: String,
    pub reference: String,
    pub transcript: String,
    pub cer: f64,
    pub latency_seconds: f64,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct BenchmarkReport {
    pub backend: String,
    pub model: String,
    pub corpus_version: String,
    pub split: String,
    pub samples: Vec<SampleResult>,
    pub failures: usize,
    pub content_cer: f64,
    pub generated_at: String,
}

/// Backend boundary for benchmark replay: feed one sample's PCM and return
/// the final transcript.
pub trait BackendFactory {
    fn replay(
        &mut self,
        index: usize,
        pcm: &[u8],
        chunk_interval: Duration,
    ) -> Result<String, String>;
}

/// Content normalization: NFKC, lowercase, strip all punctuation and
/// whitespace while keeping CJK characters and digits.
pub fn normalize_content(text: &str) -> String {
    use unicode_normalization::UnicodeNormalization;
    text.nfkc()
        .flat_map(|ch| ch.to_lowercase())
        .filter(|ch| ch.is_alphanumeric())
        .collect()
}

/// Character error rate: edit distance over reference length, computed on
/// normalized text. An empty reference has nothing to miss (0.0).
pub fn cer(reference: &str, hypothesis: &str) -> f64 {
    let reference = normalize_content(reference);
    let hypothesis = normalize_content(hypothesis);
    if reference.is_empty() {
        return 0.0;
    }
    let distance = edit_distance(&reference, &hypothesis);
    distance as f64 / reference.chars().count() as f64
}

fn edit_distance(a: &str, b: &str) -> usize {
    let b_chars: Vec<char> = b.chars().collect();
    let mut previous: Vec<usize> = (0..=b_chars.len()).collect();
    for (i, char_a) in a.chars().enumerate() {
        let mut current = vec![i + 1];
        for (j, char_b) in b_chars.iter().enumerate() {
            let cost = if char_a == *char_b { 0 } else { 1 };
            current.push(
                (previous[j + 1] + 1)
                    .min(current[j] + 1)
                    .min(previous[j] + cost),
            );
        }
        previous = current;
    }
    previous[b_chars.len()]
}

pub fn load_corpus(path: &Path) -> Result<Vec<CorpusEntry>, io::Error> {
    let raw = std::fs::read_to_string(path)?;
    let mut entries = Vec::new();
    for (line_number, line) in raw.lines().enumerate() {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let entry: CorpusEntry = serde_json::from_str(trimmed).map_err(|error| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                format!("{}:{}: {error}", path.display(), line_number + 1),
            )
        })?;
        entries.push(entry);
    }
    Ok(entries)
}

/// Corpus version: content hash over entry ids and references so report
/// comparisons can detect corpus drift.
pub fn corpus_version(entries: &[CorpusEntry]) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    for entry in entries {
        hasher.update(entry.id.as_bytes());
        hasher.update(b"\x1f");
        hasher.update(entry.reference.as_bytes());
        hasher.update(b"\x1e");
    }
    let digest = hasher.finalize();
    let hex: String = digest.iter().take(8).map(|byte| format!("{byte:02x}")).collect();
    format!("corpus-{hex}")
}

#[allow(clippy::too_many_arguments)]
pub fn run_asr_benchmark<F: BackendFactory>(
    entries: &[CorpusEntry],
    pcm_by_id: &[(String, Vec<u8>)],
    factory: &mut F,
    backend: &str,
    model: &str,
    corpus_version: &str,
    chunk_interval: Duration,
) -> BenchmarkReport {
    run_asr_benchmark_split(
        entries,
        pcm_by_id,
        factory,
        backend,
        model,
        corpus_version,
        "unspecified",
        chunk_interval,
    )
}

#[allow(clippy::too_many_arguments)]
pub fn run_asr_benchmark_split<F: BackendFactory>(
    entries: &[CorpusEntry],
    pcm_by_id: &[(String, Vec<u8>)],
    factory: &mut F,
    backend: &str,
    model: &str,
    corpus_version: &str,
    split: &str,
    chunk_interval: Duration,
) -> BenchmarkReport {
    let mut samples = Vec::new();
    let mut failures = 0;
    for (index, entry) in entries.iter().enumerate() {
        let pcm = pcm_by_id
            .iter()
            .find(|(id, _)| id == &entry.id)
            .map(|(_, pcm)| pcm.clone())
            .unwrap_or_default();
        let started = std::time::Instant::now();
        let outcome = factory.replay(index, &pcm, chunk_interval);
        let latency = started.elapsed().as_secs_f64();
        match outcome {
            Ok(transcript) => {
                let sample_cer = cer(&entry.reference, &transcript);
                samples.push(SampleResult {
                    id: entry.id.clone(),
                    reference: entry.reference.clone(),
                    transcript,
                    cer: sample_cer,
                    latency_seconds: latency,
                    error: None,
                });
            }
            Err(message) => {
                failures += 1;
                samples.push(SampleResult {
                    id: entry.id.clone(),
                    reference: entry.reference.clone(),
                    transcript: String::new(),
                    cer: f64::NAN,
                    latency_seconds: latency,
                    error: Some(message),
                });
            }
        }
    }
    let scored: Vec<f64> = samples.iter().map(|sample| sample.cer).filter(|value| value.is_finite()).collect();
    let content_cer = if scored.is_empty() {
        0.0
    } else {
        scored.iter().sum::<f64>() / scored.len() as f64
    };
    BenchmarkReport {
        backend: backend.to_owned(),
        model: model.to_owned(),
        corpus_version: corpus_version.to_owned(),
        split: split.to_owned(),
        samples,
        failures,
        content_cer,
        generated_at: now_rfc3339(),
    }
}

fn now_rfc3339() -> String {
    time::OffsetDateTime::now_utc()
        .format(&time::format_description::well_known::Rfc3339)
        .unwrap_or_default()
}
