//! Python bindings for cargo-ros2
//!
//! This module exposes cargo-ros2's functionality to Python using PyO3.

#![allow(clippy::useless_conversion)] // False positive with the PyO3 macros

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use std::path::PathBuf;

/// Python wrapper for BindgenConfig
#[pyclass(from_py_object)]
#[derive(Clone)]
struct BindgenConfig {
    #[pyo3(get, set)]
    package_name: String,
    #[pyo3(get, set)]
    package_path: Option<String>,
    #[pyo3(get, set)]
    output_dir: String,
    #[pyo3(get, set)]
    verbose: bool,
    #[pyo3(get, set)]
    rosidl_runtime_rs_version: Option<String>,
}

#[pymethods]
impl BindgenConfig {
    #[new]
    #[pyo3(signature = (package_name, output_dir, package_path=None, verbose=false, rosidl_runtime_rs_version=None))]
    fn new(
        package_name: String,
        output_dir: String,
        package_path: Option<String>,
        verbose: bool,
        rosidl_runtime_rs_version: Option<String>,
    ) -> Self {
        Self {
            package_name,
            package_path,
            output_dir,
            verbose,
            rosidl_runtime_rs_version,
        }
    }

    fn __repr__(&self) -> String {
        format!(
            "BindgenConfig(package_name='{}', output_dir='{}', package_path={:?}, verbose={}, rosidl_runtime_rs_version={:?})",
            self.package_name,
            self.output_dir,
            self.package_path,
            self.verbose,
            self.rosidl_runtime_rs_version
        )
    }
}

/// Python wrapper for InstallConfig
#[pyclass(from_py_object)]
#[derive(Clone)]
struct InstallConfig {
    #[pyo3(get, set)]
    project_root: String,
    #[pyo3(get, set)]
    install_base: String,
    #[pyo3(get, set)]
    build_base: String,
    #[pyo3(get, set)]
    profile: String,
    #[pyo3(get, set)]
    verbose: bool,
    #[pyo3(get, set)]
    arch: Option<String>,
    #[pyo3(get, set)]
    features: Vec<String>,
    #[pyo3(get, set)]
    no_default_features: bool,
    #[pyo3(get, set)]
    all_features: bool,
}

#[pymethods]
impl InstallConfig {
    #[new]
    #[pyo3(signature = (
        project_root,
        install_base,
        build_base,
        profile="debug".to_string(),
        verbose=false,
        arch=None,
        features=Vec::new(),
        no_default_features=false,
        all_features=false,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        project_root: String,
        install_base: String,
        build_base: String,
        profile: String,
        verbose: bool,
        arch: Option<String>,
        features: Vec<String>,
        no_default_features: bool,
        all_features: bool,
    ) -> Self {
        Self {
            project_root,
            install_base,
            build_base,
            profile,
            verbose,
            arch,
            features,
            no_default_features,
            all_features,
        }
    }

    fn __repr__(&self) -> String {
        format!(
            "InstallConfig(project_root='{}', install_base='{}', build_base='{}', profile='{}', verbose={})",
            self.project_root, self.install_base, self.build_base, self.profile, self.verbose
        )
    }
}

/// Generate Rust bindings for a ROS 2 interface package
///
/// Args:
///     config (BindgenConfig): Configuration for binding generation
///
/// Raises:
///     RuntimeError: If binding generation fails
///
/// Example:
///     >>> import cargo_ros2_py
///     >>> config = cargo_ros2_py.BindgenConfig(
///     ...     package_name="std_msgs",
///     ...     output_dir="target/ros2_bindings",
///     ...     package_path=None,
///     ...     verbose=True
///     ... )
///     >>> cargo_ros2_py.generate_bindings(config)
#[pyfunction]
fn generate_bindings(config: BindgenConfig) -> PyResult<()> {
    let rust_config = cargo_ros2::BindgenConfig {
        package_name: config.package_name,
        package_path: config.package_path.map(PathBuf::from),
        output_dir: PathBuf::from(config.output_dir),
        verbose: config.verbose,
        rosidl_runtime_rs_version: config.rosidl_runtime_rs_version,
    };

    cargo_ros2::generate_bindings(rust_config)
        .map_err(|e| PyRuntimeError::new_err(format!("Binding generation failed: {:#}", e)))?;
    Ok(())
}

/// Install package binaries and libraries to ament layout
///
/// Args:
///     config (InstallConfig): Configuration for installation
///
/// Raises:
///     RuntimeError: If installation fails
///
/// Example:
///     >>> import cargo_ros2_py
///     >>> config = cargo_ros2_py.InstallConfig(
///     ...     project_root="/path/to/project",
///     ...     install_base="install/my_package",
///     ...     profile="release",
///     ...     verbose=True
///     ... )
///     >>> cargo_ros2_py.install_to_ament(config)
#[pyfunction]
fn install_to_ament(config: InstallConfig) -> PyResult<()> {
    let rust_config = cargo_ros2::InstallConfig {
        project_root: PathBuf::from(config.project_root),
        install_base: PathBuf::from(config.install_base),
        build_base: PathBuf::from(config.build_base),
        profile: config.profile,
        verbose: config.verbose,
        arch: config.arch,
        features: config.features,
        no_default_features: config.no_default_features,
        all_features: config.all_features,
    };

    cargo_ros2::install_to_ament(rust_config)
        .map_err(|e| PyRuntimeError::new_err(format!("Installation failed: {:#}", e)))?;
    Ok(())
}

/// Clean generated bindings and cache
///
/// Args:
///     project_root (str): Project root directory
///     verbose (bool): Enable verbose output
///
/// Raises:
///     RuntimeError: If cleanup fails
///
/// Example:
///     >>> import cargo_ros2_py
///     >>> cargo_ros2_py.clean_bindings("/path/to/project", verbose=True)
#[pyfunction]
fn clean_bindings(project_root: String, verbose: bool) -> PyResult<()> {
    cargo_ros2::clean_bindings(&PathBuf::from(project_root), verbose)
        .map_err(|e| PyRuntimeError::new_err(format!("Clean failed: {:#}", e)))?;
    Ok(())
}

/// Diagnose why a plain `cargo` invocation fails in a workspace
///
/// Runs the same checks as `cargo ros2 doctor`, printing a checklist and the fix
/// for the first thing that is wrong. Exposed here because the wheel ships this
/// extension module and no binaries, so this is how an installed user reaches
/// the diagnosis.
///
/// Args:
///     crate_dir: Directory of the crate to diagnose (default: current directory)
///
/// Returns:
///     True when every check passed
///
/// Example:
///     >>> import cargo_ros2_py
///     >>> cargo_ros2_py.doctor()
#[pyfunction]
#[pyo3(signature = (crate_dir = None))]
fn doctor(crate_dir: Option<String>) -> PyResult<bool> {
    let dir = match crate_dir {
        Some(path) => PathBuf::from(path),
        None => std::env::current_dir()
            .map_err(|e| PyRuntimeError::new_err(format!("Cannot read current directory: {e}")))?,
    };
    cargo_ros2::doctor::run(&dir)
        .map_err(|e| PyRuntimeError::new_err(format!("Doctor failed: {:#}", e)))
}

/// Python module for cargo-ros2
///
/// This module provides Python bindings to the cargo-ros2 library,
/// enabling direct function calls from Python without subprocess overhead.
#[pymodule]
fn cargo_ros2_py(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Register classes
    m.add_class::<BindgenConfig>()?;
    m.add_class::<InstallConfig>()?;

    // Register functions
    m.add_function(wrap_pyfunction!(generate_bindings, m)?)?;
    m.add_function(wrap_pyfunction!(install_to_ament, m)?)?;
    m.add_function(wrap_pyfunction!(clean_bindings, m)?)?;
    m.add_function(wrap_pyfunction!(doctor, m)?)?;

    // Module metadata
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add(
        "__doc__",
        "Python bindings for cargo-ros2 - Unified build tool for ROS 2 Rust projects",
    )?;

    Ok(())
}
