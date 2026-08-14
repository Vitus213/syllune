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
    Model,
    Doctor,
    Mode,
    History,
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
