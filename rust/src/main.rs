use std::path::PathBuf;

use clap::{Args, Parser, Subcommand};

#[derive(Debug, Parser)]
#[command(
    name = "syllune",
    version,
    about = "Fast realtime voice input for Linux"
)]
struct Cli {
    #[arg(long, global = true)]
    config: Option<PathBuf>,
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    Stream(StreamArgs),
    Transcribe(TranscribeArgs),
    Record(RecordArgs),
    Model(ModelArgs),
    Doctor,
    Mode(ModeArgs),
    History(HistoryArgs),
    Daemon,
    Benchmark(BenchmarkArgs),
}

#[derive(Debug, Args)]
struct BenchmarkArgs {
    #[command(subcommand)]
    action: BenchmarkAction,
}

#[derive(Debug, Subcommand)]
enum BenchmarkAction {
    Asr {
        #[arg(long, default_value = "test")]
        split: String,
        #[arg(long, default_value = "cloud-realtime")]
        backend: String,
        #[arg(long)]
        enforce: bool,
    },
    Latency {
        #[arg(long, default_value_t = 100)]
        trials: usize,
        #[arg(long)]
        inject: bool,
        #[arg(long)]
        enforce: bool,
    },
}

#[derive(Debug, Args)]
struct StreamArgs {
    #[arg(long)]
    backend: Option<String>,
    #[arg(long)]
    json: bool,
    #[arg(long)]
    no_inject: bool,
    #[arg(long, default_value = "quick")]
    mode: String,
}

#[derive(Debug, Args)]
struct TranscribeArgs {
    wav: PathBuf,
    #[arg(long)]
    backend: Option<String>,
    #[arg(long)]
    inject: bool,
    #[arg(long)]
    json: bool,
}

#[derive(Debug, Args)]
struct RecordArgs {
    #[arg(long, default_value_t = 5.0)]
    seconds: f64,
    #[arg(long)]
    backend: Option<String>,
    #[arg(long)]
    no_inject: bool,
    #[arg(long)]
    json: bool,
}

#[derive(Debug, Args)]
struct ModelArgs {
    #[command(subcommand)]
    action: ModelAction,
    #[arg(long, global = true)]
    json: bool,
}

#[derive(Debug, Subcommand)]
enum ModelAction {
    List,
    Install { id: String },
    Check { id: String },
    Remove { id: String },
}

#[derive(Debug, Args)]
struct ModeArgs {
    #[command(subcommand)]
    action: ModeAction,
    #[arg(long, global = true)]
    id: Option<String>,
    #[arg(long, global = true)]
    name: Option<String>,
    #[arg(long, global = true)]
    prompt: Option<String>,
    #[arg(long, global = true)]
    processing_label: Option<String>,
}
#[derive(Debug, Subcommand)]
enum ModeAction {
    List,
    Reload,
    Add,
    Update,
    Remove,
}

#[derive(Debug, Args)]
struct HistoryArgs {
    #[command(subcommand)]
    action: HistoryAction,
    #[arg(long, global = true)]
    ids: Vec<String>,
    #[arg(long, global = true)]
    all: bool,
    #[arg(long, global = true, default_value_t = 50)]
    limit: i64,
    #[arg(long, global = true)]
    cursor: Option<String>,
    #[arg(long, global = true)]
    destination: Option<PathBuf>,
    #[arg(long, global = true, default_value_t = 7)]
    days: i64,
    #[arg(long, global = true, default_value = "127.0.0.1")]
    host: String,
    #[arg(long, global = true, default_value_t = 8790)]
    port: u16,
}

#[derive(Debug, Subcommand)]
enum HistoryAction {
    List,
    Delete,
    Export,
    Totals,
    Usage,
    /// Open the local web console for browsing records and recordings.
    Serve,
}

#[tokio::main]
async fn main() {
    let cli = Cli::parse();
    let code = match cli.command {
        Command::Stream(args) => match syllune::stream::run(syllune::stream::StreamOptions {
            config_path: cli.config,
            backend: args.backend,
            json: args.json,
            inject: !args.no_inject,
            mode: args.mode,
        })
        .await
        {
            Ok(code) => code,
            Err(error) => {
                eprintln!("Syllune: {error}");
                1
            }
        },
        Command::Transcribe(args) => {
            syllune::batch_cmd::transcribe(syllune::batch_cmd::TranscribeArgs {
                wav: args.wav,
                backend: args.backend,
                inject: args.inject,
                json: args.json,
            })
            .await
        }
        Command::Record(args) => {
            syllune::batch_cmd::record(syllune::batch_cmd::RecordArgs {
                seconds: args.seconds,
                backend: args.backend,
                no_inject: args.no_inject,
                json: args.json,
            })
            .await
        }
        Command::Model(args) => syllune::model_cmd::run(
            match args.action {
                ModelAction::List => syllune::model_cmd::ModelCommand::List,
                ModelAction::Install { id } => syllune::model_cmd::ModelCommand::Install { id },
                ModelAction::Check { id } => syllune::model_cmd::ModelCommand::Check { id },
                ModelAction::Remove { id } => syllune::model_cmd::ModelCommand::Remove { id },
            },
            args.json,
        ),
        Command::Mode(args) => {
            let action = match args.action {
                ModeAction::List => "list",
                ModeAction::Reload => "reload",
                ModeAction::Add => "add",
                ModeAction::Update => "update",
                ModeAction::Remove => "remove",
            };
            syllune::mode_cmd::run(
                action,
                syllune::mode_cmd::ModeArgs {
                    id: args.id,
                    name: args.name,
                    prompt: args.prompt,
                    processing_label: args.processing_label,
                },
            )
        }
        Command::History(args) if matches!(args.action, HistoryAction::Serve) => {
            match syllune::history_web::serve(
                syllune::models::default_data_dir().join("history.sqlite3"),
                syllune::history_web::ServeOptions {
                    host: args.host,
                    port: args.port,
                },
            )
            .await
            {
                Ok(code) => code,
                Err(error) => {
                    eprintln!("Syllune: {error}");
                    1
                }
            }
        }
        Command::History(args) => {
            let action = match args.action {
                HistoryAction::List => "list",
                HistoryAction::Delete => "delete",
                HistoryAction::Export => "export",
                HistoryAction::Totals => "totals",
                HistoryAction::Usage => "usage",
                HistoryAction::Serve => unreachable!("handled above"),
            };
            syllune::history_cmd::run(
                action,
                syllune::history_cmd::HistoryArgs {
                    ids: args.ids,
                    all: args.all,
                    limit: args.limit,
                    cursor: args.cursor,
                    destination: args.destination,
                    days: args.days,
                },
            )
        }
        Command::Doctor => {
            println!("Syllune doctor");
            let checks = syllune::doctor::run_checks();
            let mut all_ok = true;
            for check in &checks {
                if !check.pass() {
                    all_ok = false;
                }
                println!(
                    "{} {}: {}",
                    if check.pass() { "ok" } else { "FAIL" },
                    check.name,
                    check.detail
                );
            }
            i32::from(!all_ok)
        }
        Command::Daemon => {
            let options = syllune::stream::StreamOptions {
                config_path: cli.config,
                backend: None,
                json: false,
                inject: true,
                mode: "quick".to_owned(),
            };
            match syllune::daemon::serve(options).await {
                Ok(()) => 0,
                Err(error) => {
                    eprintln!("Syllune: {error}");
                    1
                }
            }
        }
        Command::Benchmark(args) => match args.action {
            BenchmarkAction::Asr {
                split,
                backend,
                enforce,
            } => {
                let mut bench_args = syllune::benchmark_cmd::AsrBenchmarkArgs::new(split, backend);
                bench_args.enforce = enforce;
                syllune::benchmark_cmd::run_asr(bench_args).await
            }
            BenchmarkAction::Latency {
                trials,
                inject,
                enforce,
            } => {
                let mut latency_args = syllune::latency_cmd::LatencyBenchmarkArgs::new();
                latency_args.trials = trials;
                latency_args.inject = inject;
                latency_args.enforce = enforce;
                syllune::latency_cmd::run_latency(latency_args).await
            }
        },
    };
    std::process::exit(code);
}
