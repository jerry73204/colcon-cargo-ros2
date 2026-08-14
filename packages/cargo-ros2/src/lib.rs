//! cargo-ros2 library
//!
//! This library provides functionality for building ROS 2 Rust projects,
//! including binding generation, build orchestration, and ament installation.
//!
//! # Public API
//!
//! This library exposes a high-level API for:
//! - Generating Rust bindings for ROS 2 interface packages
//! - Installing packages to ament layout
//! - Cleaning generated bindings and cache
//!
//! # Example
//!
//! ```no_run
//! use cargo_ros2::{BindgenConfig, generate_bindings};
//! use std::path::PathBuf;
//!
//! let config = BindgenConfig {
//!     package_name: "std_msgs".to_string(),
//!     package_path: None,
//!     output_dir: PathBuf::from("target/ros2_bindings"),
//!     verbose: false,
//!     rosidl_runtime_rs_version: None,
//! };
//!
//! generate_bindings(config).expect("Failed to generate bindings");
//! ```

pub mod ament_installer;
pub mod cache;
pub mod config_patcher;
pub mod dependency_parser;
pub mod doctor;
pub mod package_discovery;
pub mod workflow;

use eyre::{Result, WrapErr, eyre};
use std::path::{Path, PathBuf};

/// Configuration for binding generation
#[derive(Debug, Clone)]
pub struct BindgenConfig {
    /// ROS package name (e.g., "std_msgs")
    pub package_name: String,
    /// Optional direct path to package share directory (bypasses ament index)
    pub package_path: Option<PathBuf>,
    /// Output directory for generated bindings
    pub output_dir: PathBuf,
    /// Enable verbose output
    pub verbose: bool,
    /// Override rosidl_runtime_rs version in generated Cargo.toml (default: "0.6")
    pub rosidl_runtime_rs_version: Option<String>,
}

/// Configuration for ament installation
#[derive(Debug, Clone)]
pub struct InstallConfig {
    /// Project root directory (where Cargo.toml is located)
    pub project_root: PathBuf,
    /// Install base directory (install/<package>/)
    pub install_base: PathBuf,
    /// Workspace build directory
    pub build_base: PathBuf,
    /// Build profile: "debug" or "release"
    pub profile: String,
    /// Enable verbose output
    pub verbose: bool,
    /// Target triple the build was compiled for, `None` for native builds
    pub arch: Option<String>,
    /// Features requested via `--features`
    pub features: Vec<String>,
    /// Whether `--no-default-features` was passed
    pub no_default_features: bool,
    /// Whether `--all-features` was passed
    pub all_features: bool,
}

/// Generate Rust bindings for a ROS 2 interface package
///
/// This function generates Rust bindings for messages, services, and actions
/// defined in a ROS 2 interface package.
///
/// # Arguments
///
/// * `config` - Configuration for binding generation
///
/// # Returns
///
/// * `Ok(())` on success
/// * `Err` if binding generation fails
///
/// # Example
///
/// ```no_run
/// use cargo_ros2::{BindgenConfig, generate_bindings};
/// use std::path::PathBuf;
///
/// let config = BindgenConfig {
///     package_name: "geometry_msgs".to_string(),
///     package_path: None,
///     output_dir: PathBuf::from("target/ros2_bindings"),
///     verbose: true,
///     rosidl_runtime_rs_version: None,
/// };
///
/// generate_bindings(config)?;
/// # Ok::<(), eyre::Report>(())
/// ```
pub fn generate_bindings(config: BindgenConfig) -> Result<()> {
    use rosidl_bindgen::ament::{AmentIndex, Package};
    use rosidl_bindgen::generator;

    if config.verbose {
        eprintln!("Generating bindings for {}...", config.package_name);
    }

    // Get package either from path or ament index
    let package = if let Some(share_path) = config.package_path {
        Package::from_share_dir(share_path)?
    } else {
        let index =
            AmentIndex::from_env().wrap_err("Failed to load ament index (is ROS 2 sourced?)")?;
        index
            .find_package(&config.package_name)
            .ok_or_else(|| eyre!("Package '{}' not found in ament index", config.package_name))?
            .clone()
    };

    // Generate bindings using rosidl-bindgen library
    let result = generator::generate_package(
        &package,
        &config.output_dir,
        config.rosidl_runtime_rs_version.as_deref(),
    )?;

    if config.verbose {
        eprintln!(
            "✓ Generated {} messages, {} services, {} actions for {}",
            result.message_count, result.service_count, result.action_count, config.package_name
        );
    }

    Ok(())
}

/// Install package binaries and libraries to ament layout
///
/// This function installs compiled binaries, libraries, and creates necessary
/// ament package markers for ROS 2 integration.
///
/// # Arguments
///
/// * `config` - Configuration for installation
///
/// # Returns
///
/// * `Ok(())` on success
/// * `Err` if installation fails
///
/// # Example
///
/// ```no_run
/// use cargo_ros2::{InstallConfig, install_to_ament};
/// use std::path::PathBuf;
///
/// let config = InstallConfig {
///     project_root: PathBuf::from("/path/to/project"),
///     install_base: PathBuf::from("install/my_package"),
///     build_base: PathBuf::from("build/my_package"),
///     profile: "release".to_string(),
///     verbose: true,
///     arch: None,
///     features: Vec::new(),
///     no_default_features: false,
///     all_features: false,
/// };
///
/// install_to_ament(config)?;
/// # Ok::<(), eyre::Report>(())
/// ```
pub fn install_to_ament(config: InstallConfig) -> Result<()> {
    use crate::ament_installer::{
        AmentInstaller, install_targets_from_package, resolve_enabled_features,
    };
    use cargo_metadata::MetadataCommand;
    use std::env;

    if config.verbose {
        eprintln!("Installing package to ament layout...");
    }

    // Save current directory and change to project root
    let original_dir = env::current_dir()?;
    env::set_current_dir(&config.project_root)?;

    // Read package metadata
    // Patches and rustflags are picked up from .cargo/config.toml automatically
    let metadata = MetadataCommand::new()
        .exec()
        .wrap_err("Failed to read Cargo metadata")?;

    let root_package = metadata
        .root_package()
        .ok_or_else(|| eyre!("No root package found in Cargo.toml"))?;

    let package_name = root_package.name.clone();

    // Get target directory from metadata (handles workspace builds correctly)
    let target_dir = metadata.target_directory.clone().into_std_path_buf();

    // Collect the targets that produce installable artifacts. Cargo has
    // already done target auto-discovery for us, so implicit src/main.rs and
    // src/bin/*.rs binaries are included.
    let targets = install_targets_from_package(root_package);

    // Resolve which features were active, so that targets gated behind
    // features that were not enabled are skipped rather than reported missing.
    let enabled_features = resolve_enabled_features(
        &root_package.features,
        &config.features,
        config.no_default_features,
        config.all_features,
    );

    // Create installer
    let installer = AmentInstaller::new(
        config.install_base.clone(),
        package_name,
        config.project_root.clone(),
        target_dir,
        config.verbose,
        config.profile.clone(),
    )
    .with_targets(targets)
    .with_arch(config.arch.clone())
    .with_enabled_features(enabled_features);

    // Run installation
    let result = installer.install();

    // Restore original directory
    env::set_current_dir(original_dir)?;

    result
}

/// Clean generated bindings and cache
///
/// This function removes:
/// - Generated bindings directory
/// - Cache file
/// - (Note: .cargo/config.toml patches are NOT removed to avoid breaking other tools)
///
/// # Arguments
///
/// * `project_root` - Project root directory
/// * `verbose` - Enable verbose output
///
/// # Returns
///
/// * `Ok(())` on success
/// * `Err` if cleanup fails
///
/// # Example
///
/// ```no_run
/// use cargo_ros2::clean_bindings;
/// use std::path::PathBuf;
///
/// clean_bindings(&PathBuf::from("/path/to/project"), true)?;
/// # Ok::<(), eyre::Report>(())
/// ```
pub fn clean_bindings(project_root: &Path, verbose: bool) -> Result<()> {
    let ctx = workflow::WorkflowContext::new(project_root.to_path_buf(), verbose);

    // Remove bindings directory
    if ctx.output_dir.exists() {
        std::fs::remove_dir_all(&ctx.output_dir)
            .wrap_err_with(|| format!("Failed to remove {}", ctx.output_dir.display()))?;
        if verbose {
            eprintln!("Removed {}", ctx.output_dir.display());
        }
    }

    // Remove cache file
    if ctx.cache_file.exists() {
        std::fs::remove_file(&ctx.cache_file)
            .wrap_err_with(|| format!("Failed to remove {}", ctx.cache_file.display()))?;
        if verbose {
            eprintln!("Removed {}", ctx.cache_file.display());
        }
    }

    // Note: .cargo/config.toml patches not removed (would need selective removal)
    let cargo_config = project_root.join(".cargo").join("config.toml");
    if cargo_config.exists() && verbose {
        eprintln!("Note: .cargo/config.toml patches not removed (would need selective removal)");
    }

    Ok(())
}
