//! Identity cutover: the repository ships Syllune as the sole product
//! identity; no `type4me-linux` command, package attribute, Home Manager
//! option or desktop entry survives as a compatibility path.

use std::fs;
use std::path::Path;

fn repo_root() -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("repo root above rust/")
        .to_path_buf()
}

fn read(path: &Path) -> String {
    fs::read_to_string(path).unwrap_or_else(|error| panic!("read {}: {error}", path.display()))
}

#[test]
fn flake_default_package_is_syllune_and_drops_python_identity() {
    let flake = read(&repo_root().join("flake.nix"));
    assert!(
        flake.contains("packages.default = syllune;"),
        "default package must be syllune"
    );
    assert!(
        !flake.contains("packages.type4me-linux"),
        "no type4me-linux package attribute may remain"
    );
    assert!(
        !flake.contains("buildPythonApplication"),
        "the Python application package must be removed"
    );
    assert!(
        !flake.contains("io.github.vitus.Type4Me"),
        "the old desktop identity must be gone"
    );
}

#[test]
fn home_manager_module_exposes_only_syllune_options() {
    let module = read(&repo_root().join("nix/home-manager.nix"));
    assert!(module.contains("options.programs.syllune"));
    assert!(
        !module.contains("programs.type4me-linux"),
        "old Home Manager options must not remain"
    );
    assert!(
        !module.contains("type4me-linux toggle"),
        "old CLI commands must not be referenced"
    );
}

#[test]
fn no_desktop_entry_or_python_distribution_survives() {
    let root = repo_root();
    assert!(!root.join("data/io.github.vitus.Type4Me.desktop").exists());
    assert!(!root.join("src/type4me_linux").exists());
    assert!(!root.join("pyproject.toml").exists());
}

#[test]
fn rust_binary_reports_syllune_identity() {
    let output = std::process::Command::new(env!("CARGO_BIN_EXE_syllune"))
        .arg("--help")
        .output()
        .expect("syllune binary should be runnable");
    let help = String::from_utf8(output.stdout).expect("UTF-8");
    assert!(help.contains("syllune"));
    assert!(
        !help.contains("type4me"),
        "help must not mention the old identity"
    );
}
