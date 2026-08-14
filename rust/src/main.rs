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
    Transcribe,
    Record,
    Model(ModelArgs),
    Doctor,
    Mode(ModeArgs),
    History(HistoryArgs),
    Daemon,
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
struct ModelArgs {
    #[command(subcommand)]
    action: ModelAction,
    #[arg(long)]
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
}

#[derive(Debug, Subcommand)]
enum HistoryAction {
    List,
    Delete,
    Export,
    Totals,
    Usage,
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
        Command::Model(args) => syllune::model_cmd::run(
            match args.action {
                ModelAction::List => syllune::model_cmd::ModelCommand::List,
                ModelAction::Install { id } => {
                    syllune::model_cmd::ModelCommand::Install { id }
                }
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
        Command::History(args) => {
            let action = match args.action {
                HistoryAction::List => "list",
                HistoryAction::Delete => "delete",
                HistoryAction::Export => "export",
                HistoryAction::Totals => "totals",
                HistoryAction::Usage => "usage",
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
            0
        }
        other => {
            eprintln!("Syllune: {other:?} is not available in this build");
            2
        }
    };
    std::process::exit(code);
}
