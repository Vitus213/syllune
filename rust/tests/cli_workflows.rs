use std::process::Command;

fn syllune() -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_syllune"));
    command.env("HOME", std::env::temp_dir().join("syllune-cli-tests-home"));
    command
}

fn json_lines(stdout: &str) -> Vec<serde_json::Value> {
    stdout
        .lines()
        .map(|line| serde_json::from_str(line).expect("each output line should be JSON"))
        .collect()
}

#[test]
fn doctor_reports_dependency_checks_and_exits_zero_when_tools_exist() {
    let output = syllune().arg("doctor").output().expect("run doctor");
    let stdout = String::from_utf8(output.stdout).expect("UTF-8");
    assert!(stdout.contains("pw-record"), "{stdout}");
    assert!(stdout.contains("wtype"), "{stdout}");
}

#[test]
fn model_list_outputs_catalog_entries_with_supply_chain_fields() {
    let output = syllune()
        .args(["model", "list", "--json"])
        .output()
        .expect("run model list");
    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).expect("UTF-8");
    let entries = json_lines(&stdout);
    assert!(!entries.is_empty());
    let entry = &entries[0];
    assert!(entry["sha256_sri"].is_string());
    assert!(entry["size_bytes"].is_number());
    assert!(entry["license_status"].is_string());
}

#[test]
fn model_check_unknown_id_fails_with_nonzero_exit() {
    let output = syllune()
        .args(["model", "check", "no-such-model"])
        .output()
        .expect("run model check");
    assert_eq!(output.status.code(), Some(1));
}

#[test]
fn mode_list_returns_json_array_with_quick_first() {
    let output = syllune()
        .args(["mode", "list"])
        .output()
        .expect("run mode list");
    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).expect("UTF-8");
    let modes: Vec<serde_json::Value> = serde_json::from_str(&stdout).expect("JSON array");
    assert_eq!(modes[0]["id"], "quick");
    assert!(modes.iter().any(|mode| mode["id"] == "translate-en"));
}

#[test]
fn mode_add_update_remove_roundtrip_via_cli() {
    let dir = tempfile::tempdir().expect("temporary root");
    let mut add = syllune();
    add.args([
        "mode",
        "add",
        "--name",
        "CLI模式",
        "--prompt",
        "原文：{text}",
    ])
    .env("XDG_CONFIG_HOME", dir.path());
    let output = add.output().expect("mode add");
    assert!(output.status.success(), "{output:?}");
    let added: serde_json::Value =
        serde_json::from_str(&String::from_utf8(output.stdout).unwrap()).expect("JSON");
    let id = added["id"].as_str().expect("mode id").to_owned();

    let mut update = syllune();
    update
        .args(["mode", "update", "--id", &id, "--name", "CLI模式改名"])
        .env("XDG_CONFIG_HOME", dir.path());
    let output = update.output().expect("mode update");
    assert!(output.status.success(), "{output:?}");

    let mut remove = syllune();
    remove
        .args(["mode", "remove", "--id", &id])
        .env("XDG_CONFIG_HOME", dir.path());
    let output = remove.output().expect("mode remove");
    assert!(output.status.success(), "{output:?}");

    let mut list = syllune();
    list.args(["mode", "list"])
        .env("XDG_CONFIG_HOME", dir.path());
    let output = list.output().expect("mode list");
    let modes: Vec<serde_json::Value> =
        serde_json::from_str(&String::from_utf8(output.stdout).unwrap()).expect("JSON array");
    assert!(!modes.iter().any(|mode| mode["id"] == id));
}

#[test]
fn mode_remove_builtin_is_rejected() {
    let output = syllune()
        .args(["mode", "remove", "--id", "quick"])
        .output()
        .expect("run mode remove quick");
    assert_eq!(output.status.code(), Some(1));
}

#[test]
fn history_list_and_totals_have_stable_json_shape() {
    let dir = tempfile::tempdir().expect("temporary root");
    let mut list = syllune();
    list.args(["history", "list", "--limit", "5"])
        .env("XDG_DATA_HOME", dir.path());
    let output = list.output().expect("history list");
    assert!(output.status.success(), "{output:?}");
    let page: serde_json::Value =
        serde_json::from_str(&String::from_utf8(output.stdout).unwrap()).expect("JSON");
    assert!(page["records"].is_array());
    assert!(page.get("next_cursor").is_some());

    let mut totals = syllune();
    totals
        .args(["history", "totals"])
        .env("XDG_DATA_HOME", dir.path());
    let output = totals.output().expect("history totals");
    assert!(output.status.success());
    let totals: serde_json::Value =
        serde_json::from_str(&String::from_utf8(output.stdout).unwrap()).expect("JSON");
    assert_eq!(totals["records"], 0);
}

#[test]
fn transcribe_missing_file_fails_with_diagnostic() {
    let output = syllune()
        .args(["transcribe", "/nonexistent/file.wav"])
        .output()
        .expect("run transcribe");
    assert_eq!(output.status.code(), Some(1));
    let stderr = String::from_utf8(output.stderr).expect("UTF-8");
    assert!(stderr.contains("Syllune"), "{stderr}");
}

#[test]
fn stream_rejects_unknown_mode_before_capture() {
    let dir = tempfile::tempdir().expect("temporary root");
    let output = syllune()
        .args([
            "stream",
            "--backend",
            "local-streaming",
            "--mode",
            "不存在",
            "--json",
            "--no-inject",
        ])
        .env("XDG_CONFIG_HOME", dir.path())
        .env("XDG_DATA_HOME", dir.path())
        .output()
        .expect("run stream");
    assert_eq!(output.status.code(), Some(1));
    let stderr = String::from_utf8(output.stderr).expect("UTF-8");
    assert!(
        stderr.contains("mode not found") || stderr.contains("不存在"),
        "{stderr}"
    );
}
