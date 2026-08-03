//! Target discovery tests for the ament installer.
//!
//! These exercise the real `cargo metadata` path, which is what makes Cargo's
//! own target auto-discovery (`src/main.rs`, `src/bin/*.rs`) work. They only
//! read metadata, so no compilation happens.

use cargo_ros2::ament_installer::{InstallTargetKind, install_targets_for_project};
use std::fs;
use std::path::Path;
use tempfile::TempDir;

fn write(path: &Path, contents: &str) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, contents).unwrap();
}

/// A crate with an implicit `src/main.rs` binary, two auto-discovered
/// `src/bin/*.rs` binaries, and a `cdylib` library. None of them are declared
/// in `[[bin]]`.
fn auto_discovery_project(root: &Path) {
    write(
        &root.join("Cargo.toml"),
        r#"
[package]
name = "my-pkg"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib", "rlib"]

[workspace]
"#,
    );
    write(&root.join("src/lib.rs"), "");
    write(&root.join("src/main.rs"), "fn main() {}");
    write(&root.join("src/bin/helper.rs"), "fn main() {}");
    write(&root.join("src/bin/other_helper.rs"), "fn main() {}");
}

#[test]
fn discovers_binaries_without_an_explicit_bin_section() {
    let temp_dir = TempDir::new().unwrap();
    let project_root = temp_dir.path().join("project");
    auto_discovery_project(&project_root);

    let targets = install_targets_for_project(&project_root).unwrap();

    let mut bins: Vec<_> = targets
        .iter()
        .filter(|t| t.kind == InstallTargetKind::Bin)
        .map(|t| t.name.as_str())
        .collect();
    bins.sort_unstable();

    // "my-pkg" comes from src/main.rs and keeps its hyphen; the other two are
    // auto-discovered from src/bin/ and are invisible to a Cargo.toml scan.
    assert_eq!(bins, vec!["helper", "my-pkg", "other_helper"]);
}

#[test]
fn discovers_library_target_with_underscored_name() {
    let temp_dir = TempDir::new().unwrap();
    let project_root = temp_dir.path().join("project");
    auto_discovery_project(&project_root);

    let targets = install_targets_for_project(&project_root).unwrap();

    let libs: Vec<_> = targets
        .iter()
        .filter(|t| t.kind == InstallTargetKind::Lib)
        .map(|t| t.name.as_str())
        .collect();
    // Cargo reports the lib target with dashes already replaced, matching the
    // `libmy_pkg.so` file it writes.
    assert_eq!(libs, vec!["my_pkg"]);
}

#[test]
fn reports_required_features_for_gated_binaries() {
    let temp_dir = TempDir::new().unwrap();
    let project_root = temp_dir.path().join("project");
    write(
        &project_root.join("Cargo.toml"),
        r#"
[package]
name = "gated-pkg"
version = "0.1.0"
edition = "2021"

[features]
extra = []

[[bin]]
name = "gated"
path = "src/bin/gated.rs"
required-features = ["extra"]

[workspace]
"#,
    );
    write(&project_root.join("src/bin/gated.rs"), "fn main() {}");

    let targets = install_targets_for_project(&project_root).unwrap();

    let gated = targets.iter().find(|t| t.name == "gated").unwrap();
    assert_eq!(gated.required_features, vec!["extra".to_string()]);
}

#[test]
fn skips_targets_that_produce_no_installable_artifact() {
    let temp_dir = TempDir::new().unwrap();
    let project_root = temp_dir.path().join("project");
    write(
        &project_root.join("Cargo.toml"),
        r#"
[package]
name = "rlib-only"
version = "0.1.0"
edition = "2021"
build = "build.rs"

[workspace]
"#,
    );
    write(&project_root.join("src/lib.rs"), "");
    write(&project_root.join("build.rs"), "fn main() {}");
    write(&project_root.join("tests/it.rs"), "");

    let targets = install_targets_for_project(&project_root).unwrap();

    // An rlib library, a build script and an integration test: nothing to install.
    assert!(targets.is_empty(), "unexpected targets: {targets:?}");
}
