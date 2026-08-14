use std::process::Command;

fn syllune() -> Command {
    Command::new(env!("CARGO_BIN_EXE_syllune"))
}

#[test]
fn help_lists_the_core_cli_commands() {
    let output = syllune()
        .arg("--help")
        .output()
        .expect("syllune binary should be runnable");

    assert!(output.status.success());
    let help = String::from_utf8(output.stdout).expect("help should be UTF-8");
    for command in [
        "stream",
        "transcribe",
        "record",
        "model",
        "doctor",
        "mode",
        "history",
        "daemon",
    ] {
        assert!(
            help.contains(command),
            "missing command {command} in {help}"
        );
    }
}

#[test]
fn doctor_runs_without_python_or_model_files() {
    let output = syllune()
        .arg("doctor")
        .output()
        .expect("syllune binary should be runnable");

    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).expect("doctor output should be UTF-8");
    assert!(stdout.contains("Syllune"));
}
