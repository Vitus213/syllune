use std::fs;

use syllune::config::{AppConfig, ConfigError};
use tempfile::tempdir;

#[test]
fn defaults_to_cloud_realtime_without_a_legacy_final_backend() {
    let config = AppConfig::default();

    assert_eq!(config.asr.streaming_backend, "cloud-realtime");
    assert!(config.asr.final_backend.is_none());
}

#[test]
fn rejects_unknown_keys_and_legacy_streaming_backends() {
    let unknown = "[asr]\nunknown = true\n";
    let err = AppConfig::from_toml(unknown).expect_err("unknown keys must fail");
    assert!(matches!(err, ConfigError::UnknownField { .. }));

    for backend in ["cloud-vad", "sensevoice-vad"] {
        let input = format!("[asr]\nstreaming_backend = \"{backend}\"\n");
        let err = AppConfig::from_toml(&input).expect_err("legacy backend must fail");
        assert!(err.to_string().contains("cloud-realtime"));
    }
}

#[cfg(unix)]
#[test]
fn rejects_cloud_keys_in_group_readable_config_files() {
    use std::os::unix::fs::PermissionsExt;

    let root = tempdir().expect("temporary directory");
    let path = root.path().join("config.toml");
    fs::write(
        &path,
        "[cloud]\napi_key = \"sk-test\"\n[asr]\nstreaming_backend = \"cloud-realtime\"\n",
    )
    .expect("config write");
    fs::set_permissions(&path, fs::Permissions::from_mode(0o640)).expect("permissions");

    let err = AppConfig::load(&path).expect_err("wide permissions must fail");
    assert!(err.to_string().contains("0600"));
}
