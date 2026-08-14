//! `syllune benchmark` subcommands. The ASR quality gate replays versioned
//! corpus audio through the selected realtime backend at the real 32 ms
//! pace and writes a versioned JSON report; `--enforce` applies the
//! cloud-realtime CER threshold. Missing credentials, corpus or audio skip
//! loudly (exit 2) instead of producing a pass.

use std::path::{Path, PathBuf};
use std::time::Duration;

use crate::benchmark::{
    corpus_version, load_corpus, run_asr_benchmark_split, BackendFactory, BenchmarkReport,
};
use crate::config::{load_default_config, AppConfig};
use crate::realtime::{RealtimeEvent, RealtimeSession};

const CHUNK_INTERVAL: Duration = Duration::from_millis(32);
pub const CLOUD_CER_THRESHOLD: f64 = 0.02;

#[derive(Debug, Clone)]
pub struct AsrBenchmarkArgs {
    pub split: String,
    pub backend: String,
    pub corpus_dir: PathBuf,
    pub audio_dir: PathBuf,
    pub report_path: PathBuf,
    pub enforce: bool,
}

impl AsrBenchmarkArgs {
    pub fn new(split: String, backend: String) -> Self {
        let root = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        let benchmark_root = root.join("scripts/asr_benchmark");
        Self {
            split,
            backend,
            corpus_dir: benchmark_root.join("corpus"),
            audio_dir: benchmark_root.join("audio"),
            report_path: benchmark_root.join("reports"),
            enforce: false,
        }
    }
}

pub async fn run_asr(args: AsrBenchmarkArgs) -> i32 {
    let corpus_path = args.corpus_dir.join(format!("{}.jsonl", args.split));
    let entries = match load_corpus(&corpus_path) {
        Ok(entries) if !entries.is_empty() => entries,
        Ok(_) => {
            eprintln!(
                "Syllune: corpus {} is empty; benchmark skipped (unverified)",
                corpus_path.display()
            );
            return 2;
        }
        Err(error) => {
            eprintln!(
                "Syllune: cannot read corpus {}: {error}; benchmark skipped (unverified)",
                corpus_path.display()
            );
            return 2;
        }
    };

    let mut pcm_by_id = Vec::new();
    let mut missing_audio = Vec::new();
    for entry in &entries {
        let wav_path = args.audio_dir.join(format!("{}.wav", entry.id));
        match crate::batch::load_wav_pcm16(&wav_path) {
            Ok(pcm) => pcm_by_id.push((entry.id.clone(), pcm)),
            Err(_) => missing_audio.push(entry.id.clone()),
        }
    }
    if !missing_audio.is_empty() {
        eprintln!(
            "Syllune: {} audio files missing from {} ({}); benchmark skipped (unverified)",
            missing_audio.len(),
            args.audio_dir.display(),
            missing_audio.join(", ")
        );
        return 2;
    }

    let config = match load_default_config() {
        Ok(config) => config,
        Err(error) => {
            eprintln!("Syllune: {error}; benchmark skipped (unverified)");
            return 2;
        }
    };

    let version = corpus_version(&entries);
    let factory = match args.backend.as_str() {
        "cloud-realtime" => {
            if config.cloud.api_key.trim().is_empty() {
                eprintln!("Syllune: cloud.api_key missing; benchmark skipped (unverified)");
                return 2;
            }
            Backend::Cloud {
                config: config.clone(),
            }
        }
        "local-streaming" => {
            eprintln!(
                "Syllune: local-streaming benchmark requires an installed online model and is reported separately; skipped (unverified)"
            );
            return 2;
        }
        other => {
            eprintln!("Syllune: unknown benchmark backend: {other}");
            return 2;
        }
    };

    let model = config.cloud.realtime_model.clone();
    let mut factory = factory;
    let report = run_asr_benchmark_split(
        &entries,
        &pcm_by_id,
        &mut factory,
        &args.backend,
        &model,
        &version,
        &args.split,
        CHUNK_INTERVAL,
    );

    if let Err(error) = write_report(&args.report_path, &args.split, &args.backend, &report) {
        eprintln!("Syllune: cannot write report: {error}");
        return 1;
    }

    println!(
        "backend={} split={} samples={} failures={} content_cer={:.4}",
        report.backend,
        report.split,
        report.samples.len(),
        report.failures,
        report.content_cer
    );

    if report.failures > 0 {
        eprintln!(
            "Syllune: {} samples failed; gate not passed",
            report.failures
        );
        return 1;
    }
    if args.enforce && args.backend == "cloud-realtime" && report.content_cer > CLOUD_CER_THRESHOLD
    {
        eprintln!(
            "Syllune: content CER {:.4} exceeds threshold {CLOUD_CER_THRESHOLD}; gate not passed",
            report.content_cer
        );
        return 1;
    }
    if args.enforce {
        println!("Syllune: quality gate passed");
    }
    0
}

fn write_report(
    report_dir: &Path,
    split: &str,
    backend: &str,
    report: &BenchmarkReport,
) -> Result<(), std::io::Error> {
    std::fs::create_dir_all(report_dir)?;
    let timestamp = time::OffsetDateTime::now_utc()
        .format(&time::format_description::well_known::Rfc3339)
        .unwrap_or_default()
        .replace([':', '+'], "-");
    let path = report_dir.join(format!("{backend}-{split}-{timestamp}.json"));
    let json = serde_json::to_string_pretty(report)?;
    std::fs::write(&path, json)?;
    println!("report: {}", path.display());
    Ok(())
}

/// Production replay backend.
enum Backend {
    Cloud { config: AppConfig },
}

impl BackendFactory for Backend {
    fn replay(
        &mut self,
        _index: usize,
        pcm: &[u8],
        chunk_interval: Duration,
    ) -> Result<String, String> {
        let Backend::Cloud { config } = self;
        let config = config.clone();
        let pcm = pcm.to_vec();
        tokio::task::block_in_place(|| {
            tokio::runtime::Handle::current()
                .block_on(async move { replay_cloud(&config, &pcm, chunk_interval).await })
        })
    }
}

async fn replay_cloud(
    config: &AppConfig,
    pcm: &[u8],
    chunk_interval: Duration,
) -> Result<String, String> {
    let mut session = RealtimeSession::connect(
        &config.cloud.realtime_endpoint,
        &config.cloud.api_key,
        &config.cloud.realtime_model,
    )
    .await
    .map_err(|error| error.to_string())?;

    loop {
        match session.next_event().await {
            Ok(RealtimeEvent::Ready) => break,
            Ok(_) => continue,
            Err(error) => return Err(error.to_string()),
        }
    }

    let mut confirmed: Vec<String> = Vec::new();
    for chunk in pcm.chunks(crate::capture::CHUNK_BYTES) {
        session
            .send_audio(chunk)
            .await
            .map_err(|error| error.to_string())?;
        tokio::time::sleep(chunk_interval).await;
        while let Ok(Some(event)) = next_available(&session).await {
            if let RealtimeEvent::Completed { transcript } = event {
                if !transcript.is_empty() {
                    confirmed.push(transcript);
                }
            }
        }
    }

    session.finish().await.map_err(|error| error.to_string())?;
    loop {
        match session.next_event().await {
            Ok(RealtimeEvent::Finished { transcript }) => {
                if !transcript.is_empty() {
                    return Ok(transcript);
                }
                return Ok(confirmed.join(""));
            }
            Ok(RealtimeEvent::Completed { transcript }) => {
                if !transcript.is_empty() {
                    confirmed.push(transcript);
                }
            }
            Ok(_) => continue,
            Err(error) => {
                if !confirmed.is_empty() {
                    return Ok(confirmed.join(""));
                }
                return Err(error.to_string());
            }
        }
    }
}

/// Drain already-buffered events without blocking on the network.
async fn next_available(session: &RealtimeSession) -> Result<Option<RealtimeEvent>, String> {
    match tokio::time::timeout(Duration::from_millis(1), session.next_event()).await {
        Ok(Ok(event)) => Ok(Some(event)),
        Ok(Err(error)) => Err(error.to_string()),
        Err(_) => Ok(None),
    }
}
