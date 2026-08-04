//! Ament installation support for cargo-ros2
//!
//! This module handles installing Rust packages to the ament index structure,
//! similar to cargo-ament-build. It creates the necessary markers, installs
//! source files, binaries, and metadata.

use eyre::{Result, WrapErr};
use std::collections::{BTreeMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};

/// File name prefix/suffix pairs Cargo may use for a library artifact.
///
/// See <https://doc.rust-lang.org/reference/linkage.html> for the mapping from
/// crate type to file name.
const LIBRARY_NAME_PATTERNS: [(&str, &str); 5] = [
    ("lib", "so"),
    ("lib", "dylib"),
    ("lib", "a"),
    ("", "dll"),
    ("", "lib"),
];

/// The kind of artifact a Cargo target produces, restricted to the kinds that
/// are worth copying into the ament install space.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InstallTargetKind {
    /// An executable, installed to `lib/<pkg>/<name>`
    Bin,
    /// A linkable library (`cdylib`, `staticlib`, `dylib`), installed to `lib/<pkg>/`
    Lib,
}

/// A Cargo target that produces an installable artifact.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InstallTarget {
    /// Target name as Cargo reports it. Binary names keep hyphens; library
    /// names already have hyphens replaced with underscores.
    pub name: String,
    /// What kind of artifact this target produces
    pub kind: InstallTargetKind,
    /// Features that must all be enabled for this target to be built
    pub required_features: Vec<String>,
}

impl InstallTarget {
    /// Build an [`InstallTarget`] from a Cargo target's `kind` list.
    ///
    /// Returns `None` for kinds that produce no installable file: `lib`/`rlib`
    /// (consumed only by Cargo itself), build scripts, tests, benches,
    /// examples and proc macros.
    ///
    /// A single target may report several kinds — `crate-type = ["cdylib",
    /// "rlib"]` reports both — so any linkable kind makes the whole target
    /// installable.
    pub fn from_kinds(name: &str, kinds: &[String], required_features: &[String]) -> Option<Self> {
        let kind = if kinds.iter().any(|k| k == "bin") {
            InstallTargetKind::Bin
        } else if kinds
            .iter()
            .any(|k| matches!(k.as_str(), "cdylib" | "staticlib" | "dylib"))
        {
            InstallTargetKind::Lib
        } else {
            return None;
        };

        Some(Self {
            name: name.to_string(),
            kind,
            required_features: required_features.to_vec(),
        })
    }
}

/// The on-disk file name Cargo gives an executable target.
pub fn bin_file_name(target_name: &str) -> String {
    if cfg!(windows) {
        format!("{target_name}.exe")
    } else {
        target_name.to_string()
    }
}

/// Collect the installable targets of a Cargo package.
pub fn install_targets_from_package(package: &cargo_metadata::Package) -> Vec<InstallTarget> {
    package
        .targets
        .iter()
        .filter_map(|target| {
            InstallTarget::from_kinds(&target.name, &target.kind, &target.required_features)
        })
        .collect()
}

/// Resolve the set of features that were active during compilation.
///
/// `package_features` is the package's own feature table (`[features]` in
/// `Cargo.toml`). `requested` holds the names passed via `--features`;
/// `no_default_features` and `all_features` correspond to the Cargo flags of
/// the same name.
///
/// Entries that name something other than a feature of this package —
/// `dep:serde` (an optional dependency) and `other_crate/feat` (a feature of a
/// dependency) — are not part of this package's feature namespace and are
/// excluded, since `required-features` can only refer to local features.
pub fn resolve_enabled_features(
    package_features: &BTreeMap<String, Vec<String>>,
    requested: &[String],
    no_default_features: bool,
    all_features: bool,
) -> HashSet<String> {
    if all_features {
        return package_features.keys().cloned().collect();
    }

    let mut pending: Vec<String> = requested.to_vec();
    if !no_default_features && package_features.contains_key("default") {
        pending.push("default".to_string());
    }

    let mut enabled = HashSet::new();
    while let Some(feature) = pending.pop() {
        if feature.starts_with("dep:") || feature.contains('/') {
            continue;
        }
        if !enabled.insert(feature.clone()) {
            continue;
        }
        if let Some(implied) = package_features.get(&feature) {
            pending.extend(implied.iter().cloned());
        }
    }

    enabled
}

/// Collect the installable targets of the package rooted at `project_root`.
///
/// This runs `cargo metadata`, which performs Cargo's own target
/// auto-discovery, so implicit `src/main.rs` and `src/bin/*.rs` binaries are
/// found without an explicit `[[bin]]` section.
pub fn install_targets_for_project(project_root: &Path) -> Result<Vec<InstallTarget>> {
    let metadata = cargo_metadata::MetadataCommand::new()
        .manifest_path(project_root.join("Cargo.toml"))
        .no_deps()
        .exec()
        .wrap_err("Failed to read Cargo metadata")?;

    let root_package = metadata
        .root_package()
        .ok_or_else(|| eyre::eyre!("No root package found in Cargo.toml"))?;

    Ok(install_targets_from_package(root_package))
}

/// Ament installer for creating ament-compatible installations
pub struct AmentInstaller {
    /// Install base directory (e.g., install/package_name)
    install_base: PathBuf,
    /// Package name
    package_name: String,
    /// Project root directory
    project_root: PathBuf,
    /// Target directory (from cargo metadata - handles workspace builds)
    target_dir: PathBuf,
    /// Verbose output
    verbose: bool,
    /// Build profile (debug or release)
    profile: String,
    /// Target triple for cross-compiled builds, `None` for native builds
    arch: Option<String>,
    /// Targets whose artifacts should be installed
    targets: Vec<InstallTarget>,
    /// Features that were enabled during compilation
    enabled_features: HashSet<String>,
}

impl AmentInstaller {
    /// Create a new ament installer
    ///
    /// The installer starts with no targets, no target triple and no enabled
    /// features; use [`Self::with_targets`], [`Self::with_arch`] and
    /// [`Self::with_enabled_features`] to fill those in.
    pub fn new(
        install_base: PathBuf,
        package_name: String,
        project_root: PathBuf,
        target_dir: PathBuf,
        verbose: bool,
        profile: String,
    ) -> Self {
        Self {
            install_base,
            package_name,
            project_root,
            target_dir,
            verbose,
            profile,
            arch: None,
            targets: Vec::new(),
            enabled_features: HashSet::new(),
        }
    }

    /// Set the targets whose artifacts should be installed
    pub fn with_targets(mut self, targets: Vec<InstallTarget>) -> Self {
        self.targets = targets;
        self
    }

    /// Set the target triple, which moves artifacts into `<target_dir>/<triple>/<profile>`
    pub fn with_arch(mut self, arch: Option<String>) -> Self {
        self.arch = arch;
        self
    }

    /// Set the features that were enabled during compilation
    pub fn with_enabled_features(mut self, enabled_features: HashSet<String>) -> Self {
        self.enabled_features = enabled_features;
        self
    }

    /// Run the complete installation process
    pub fn install(&self) -> Result<()> {
        if self.verbose {
            eprintln!(
                "Installing {} to {}",
                self.package_name,
                self.install_base.display()
            );
        }

        // Create directory structure
        self.create_directories()?;

        // Create ament index markers
        self.create_markers()?;

        // Create colcon marker
        self.create_colcon_marker()?;

        // Install source files
        self.install_source_files()?;

        // Install binaries and libraries
        self.install_artifacts()?;

        // Install metadata
        self.install_metadata()?;

        // Install additional files from [package.metadata.ros]
        self.install_metadata_ros_files()?;

        // Create colcon DSV files (package.dsv and local_setup.dsv)
        self.create_dsv_files()?;

        if self.verbose {
            eprintln!("✓ Installation complete!");
        }

        Ok(())
    }

    /// Create necessary directory structure
    fn create_directories(&self) -> Result<()> {
        let dirs = [
            self.lib_dir(),
            self.share_dir(),
            self.ament_index_dir(),
            self.rust_source_dir(),
        ];

        for dir in &dirs {
            fs::create_dir_all(dir)
                .wrap_err_with(|| format!("Failed to create directory: {}", dir.display()))?;
        }

        Ok(())
    }

    /// Create ament index markers
    fn create_markers(&self) -> Result<()> {
        // Create package marker
        let marker_file = self
            .ament_index_dir()
            .join("resource_index")
            .join("packages")
            .join(&self.package_name);

        fs::create_dir_all(marker_file.parent().unwrap())?;
        fs::write(&marker_file, "")?;

        if self.verbose {
            eprintln!("  Created marker: {}", marker_file.display());
        }

        // Create package type marker (Rust)
        let package_type_file = self
            .ament_index_dir()
            .join("resource_index")
            .join("package_type")
            .join(&self.package_name);

        fs::create_dir_all(package_type_file.parent().unwrap())?;
        fs::write(&package_type_file, "rust")?;

        if self.verbose {
            eprintln!(
                "  Created package type marker: {}",
                package_type_file.display()
            );
        }

        // Create the rust_packages marker. We do not consume it ourselves, but
        // colcon-ros-cargo scans this index to discover installed Rust crates,
        // so writing it keeps our install space usable from the official stack.
        let rust_package_file = self
            .ament_index_dir()
            .join("resource_index")
            .join("rust_packages")
            .join(&self.package_name);

        fs::create_dir_all(rust_package_file.parent().unwrap())?;
        fs::write(&rust_package_file, "")?;

        if self.verbose {
            eprintln!(
                "  Created rust package marker: {}",
                rust_package_file.display()
            );
        }

        Ok(())
    }

    /// Create colcon marker file
    /// This marker file is required for colcon to discover the package
    fn create_colcon_marker(&self) -> Result<()> {
        let colcon_marker_dir = self
            .install_base
            .join("share")
            .join("colcon-core")
            .join("packages");

        fs::create_dir_all(&colcon_marker_dir)?;

        let colcon_marker_file = colcon_marker_dir.join(&self.package_name);

        // Parse package.xml to get dependencies
        let dependencies = self.extract_dependencies();
        let deps_string = dependencies.join(":");

        fs::write(&colcon_marker_file, deps_string)?;

        if self.verbose {
            eprintln!("  Created colcon marker: {}", colcon_marker_file.display());
        }

        Ok(())
    }

    /// Extract runtime dependencies from package.xml
    fn extract_dependencies(&self) -> Vec<String> {
        let package_xml_path = self.project_root.join("package.xml");

        if !package_xml_path.exists() {
            return Vec::new();
        }

        let xml_content = match fs::read_to_string(&package_xml_path) {
            Ok(content) => content,
            Err(_) => return Vec::new(),
        };

        let mut dependencies = Vec::new();

        // Simple XML parsing for <depend> tags
        for line in xml_content.lines() {
            let trimmed = line.trim();
            if trimmed.starts_with("<depend>") && trimmed.ends_with("</depend>") {
                let dep = trimmed
                    .trim_start_matches("<depend>")
                    .trim_end_matches("</depend>")
                    .trim();
                dependencies.push(dep.to_string());
            }
        }

        dependencies
    }

    /// Create colcon DSV files
    /// These files tell colcon what environment scripts to source
    fn create_dsv_files(&self) -> Result<()> {
        let share_pkg_dir = self.share_dir();

        // Create package.dsv
        let package_dsv = share_pkg_dir.join("package.dsv");
        let package_dsv_content = format!(
            "source;share/{}/hook/ament_prefix_path.ps1\n\
             source;share/{}/hook/ament_prefix_path.dsv\n\
             source;share/{}/hook/ament_prefix_path.sh\n",
            self.package_name, self.package_name, self.package_name
        );
        fs::write(&package_dsv, package_dsv_content)?;

        if self.verbose {
            eprintln!("  Created package.dsv");
        }

        // Create local_setup.dsv (points to package.dsv for simplicity)
        let local_setup_dsv = share_pkg_dir.join("local_setup.dsv");
        fs::write(&local_setup_dsv, "")?; // Empty for now, colcon will handle it

        if self.verbose {
            eprintln!("  Created local_setup.dsv");
        }

        Ok(())
    }

    /// Install source files to share directory
    fn install_source_files(&self) -> Result<()> {
        let source_files = [("Cargo.toml", false), ("Cargo.lock", false), ("src", true)];

        let dest_dir = self.rust_source_dir();

        for (name, is_dir) in &source_files {
            let source = self.project_root.join(name);
            let dest = dest_dir.join(name);

            if !source.exists() {
                continue;
            }

            if *is_dir {
                self.copy_dir_recursive(&source, &dest)?;
            } else {
                if let Some(parent) = dest.parent() {
                    fs::create_dir_all(parent)?;
                }
                fs::copy(&source, &dest).wrap_err_with(|| {
                    format!("Failed to copy {} to {}", source.display(), dest.display())
                })?;
            }

            if self.verbose {
                eprintln!("  Installed: {}", name);
            }
        }

        Ok(())
    }

    /// Install binaries and libraries to `lib/<pkg>/`
    ///
    /// Artifacts that are absent from the build directory are skipped rather
    /// than treated as an error: a target whose `required-features` were not
    /// enabled is never built in the first place.
    fn install_artifacts(&self) -> Result<()> {
        if self.targets.is_empty() {
            if self.verbose {
                eprintln!("  No artifacts to install");
            }
            return Ok(());
        }

        let artifact_dir = self.artifact_dir();
        let dest_dir = self.lib_dir().join(&self.package_name);
        fs::create_dir_all(&dest_dir)?;

        for target in &self.targets {
            if !self.features_satisfied(target) {
                if self.verbose {
                    eprintln!(
                        "  Skipping {} (requires features: {})",
                        target.name,
                        target.required_features.join(", ")
                    );
                }
                continue;
            }

            match target.kind {
                InstallTargetKind::Bin => {
                    self.install_executable(&artifact_dir, &dest_dir, &target.name)?
                }
                InstallTargetKind::Lib => {
                    self.install_library(&artifact_dir, &dest_dir, &target.name)?
                }
            }
        }

        Ok(())
    }

    /// The directory Cargo wrote this build's artifacts into
    ///
    /// Cross-compiled builds get an extra target-triple component.
    fn artifact_dir(&self) -> PathBuf {
        match &self.arch {
            Some(arch) => self.target_dir.join(arch).join(&self.profile),
            None => self.target_dir.join(&self.profile),
        }
    }

    /// Whether every feature a target requires was enabled during compilation
    ///
    /// A target with unmet `required-features` is never built, so installing
    /// it would fail; it is skipped instead.
    fn features_satisfied(&self, target: &InstallTarget) -> bool {
        target
            .required_features
            .iter()
            .all(|feature| self.enabled_features.contains(feature))
    }

    /// Copy one executable target, if it was built
    fn install_executable(
        &self,
        artifact_dir: &Path,
        dest_dir: &Path,
        target_name: &str,
    ) -> Result<()> {
        let file_name = bin_file_name(target_name);
        let source = artifact_dir.join(&file_name);

        if !source.exists() {
            if self.verbose {
                eprintln!("  Skipping binary (not built): {}", target_name);
            }
            return Ok(());
        }

        let dest = dest_dir.join(&file_name);
        fs::copy(&source, &dest)
            .wrap_err_with(|| format!("Failed to copy binary: {}", target_name))?;

        // Make executable on Unix
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = fs::metadata(&dest)?.permissions();
            perms.set_mode(0o755);
            fs::set_permissions(&dest, perms)?;
        }

        if self.verbose {
            eprintln!("  Installed binary: {}", file_name);
        }

        Ok(())
    }

    /// Copy every library artifact a target produced
    ///
    /// The artifacts are found by probing file names rather than by mapping
    /// crate types, so a crate declaring several `crate-type` values installs
    /// all of them.
    fn install_library(
        &self,
        artifact_dir: &Path,
        dest_dir: &Path,
        target_name: &str,
    ) -> Result<()> {
        for (prefix, suffix) in LIBRARY_NAME_PATTERNS {
            let file_name = format!("{prefix}{target_name}.{suffix}");
            let source = artifact_dir.join(&file_name);

            if !source.exists() {
                continue;
            }

            fs::copy(&source, dest_dir.join(&file_name))
                .wrap_err_with(|| format!("Failed to copy library: {}", file_name))?;

            if self.verbose {
                eprintln!("  Installed library: {}", file_name);
            }
        }

        Ok(())
    }

    /// Install metadata files
    fn install_metadata(&self) -> Result<()> {
        let package_xml_source = self.project_root.join("package.xml");
        let package_xml_dest = self.share_dir().join("package.xml");

        if package_xml_source.exists() {
            fs::copy(&package_xml_source, &package_xml_dest)
                .wrap_err("Failed to copy package.xml")?;

            if self.verbose {
                eprintln!("  Installed: package.xml");
            }
        } else if self.verbose {
            eprintln!("  Note: No package.xml found (optional)");
        }

        Ok(())
    }

    /// Install additional files from [package.metadata.ros] in Cargo.toml
    ///
    /// Supports both directories and individual files:
    /// - install_to_share: Array of paths to copy to install/<pkg>/share/<pkg>/
    /// - install_to_include: Array of paths to copy to install/<pkg>/include/<pkg>/
    /// - install_to_lib: Array of paths to copy to install/<pkg>/lib/<pkg>/
    ///
    /// Examples:
    /// ```toml
    /// [package.metadata.ros]
    /// install_to_share = ["launch", "config", "README.md"]  # Directories and files
    /// install_to_include = ["include"]
    /// install_to_lib = ["scripts"]
    /// ```
    ///
    /// Behavior:
    /// - Directories: Copied recursively, name preserved (e.g., "launch" → share/<pkg>/launch/)
    /// - Individual files: Filename only preserved (e.g., "config/params.yaml" → share/<pkg>/params.yaml)
    /// - Missing paths: Build fails with error
    fn install_metadata_ros_files(&self) -> Result<()> {
        use toml::Value;

        let cargo_toml_path = self.project_root.join("Cargo.toml");
        let cargo_toml_content = match fs::read_to_string(&cargo_toml_path) {
            Ok(content) => content,
            Err(_) => return Ok(()), // No Cargo.toml, nothing to do
        };

        let cargo_toml: Value = match cargo_toml_content.parse() {
            Ok(value) => value,
            Err(_) => return Ok(()), // Can't parse, skip
        };

        // Navigate to [package.metadata.ros]
        let metadata_ros = match cargo_toml
            .get("package")
            .and_then(|p| p.get("metadata"))
            .and_then(|m| m.get("ros"))
        {
            Some(Value::Table(table)) => table,
            _ => return Ok(()), // No metadata.ros section
        };

        // Process each installation target
        for (subdir, dest_base) in [
            ("share", self.share_dir()),
            (
                "include",
                self.install_base.join("include").join(&self.package_name),
            ),
            ("lib", self.lib_dir().join(&self.package_name)),
        ] {
            let key = format!("install_to_{}", subdir);

            if let Some(Value::Array(paths)) = metadata_ros.get(&key) {
                // Create destination directory
                fs::create_dir_all(&dest_base)?;

                for path_value in paths {
                    if let Value::String(rel_path) = path_value {
                        let src = self.project_root.join(rel_path);

                        if !src.exists() {
                            return Err(eyre::eyre!(
                                "[package.metadata.ros.{}] path not found: {} (expected at {})",
                                key,
                                rel_path,
                                src.display()
                            ));
                        }

                        // Get the file/directory name to preserve in destination
                        let name = src.file_name().ok_or_else(|| {
                            eyre::eyre!("Invalid path in metadata.ros: {}", rel_path)
                        })?;
                        let dest = dest_base.join(name);

                        if src.is_dir() {
                            self.copy_dir_recursive(&src, &dest)?;
                        } else {
                            fs::copy(&src, &dest).wrap_err_with(|| {
                                format!("Failed to copy {} to {}", src.display(), dest.display())
                            })?;
                        }

                        if self.verbose {
                            eprintln!("  Installed [metadata.ros.{}]: {}", key, rel_path);
                        }
                    }
                }
            }
        }

        Ok(())
    }

    /// Copy directory recursively
    fn copy_dir_recursive(&self, src: &Path, dst: &Path) -> Result<()> {
        copy_dir_recursive_impl(src, dst)
    }

    /// Get lib directory path
    fn lib_dir(&self) -> PathBuf {
        self.install_base.join("lib")
    }

    /// Get share directory path
    fn share_dir(&self) -> PathBuf {
        self.install_base.join("share").join(&self.package_name)
    }

    /// Get ament index directory path
    fn ament_index_dir(&self) -> PathBuf {
        self.install_base.join("share").join("ament_index")
    }

    /// Get rust source directory path
    fn rust_source_dir(&self) -> PathBuf {
        self.share_dir().join("rust")
    }
}

/// Copy directory recursively (helper function)
fn copy_dir_recursive_impl(src: &Path, dst: &Path) -> Result<()> {
    fs::create_dir_all(dst)?;

    for entry in fs::read_dir(src)? {
        let entry = entry?;
        let file_type = entry.file_type()?;
        let src_path = entry.path();
        let dst_path = dst.join(entry.file_name());

        if file_type.is_dir() {
            copy_dir_recursive_impl(&src_path, &dst_path)?;
        } else {
            fs::copy(&src_path, &dst_path)?;
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn test_ament_installer_directories() {
        let temp_dir = TempDir::new().unwrap();
        let install_base = temp_dir.path().join("install").join("test_pkg");
        let project_root = temp_dir.path().join("project");
        let target_dir = temp_dir.path().join("target");

        let installer = AmentInstaller::new(
            install_base.clone(),
            "test_pkg".to_string(),
            project_root,
            target_dir,
            false,
            "debug".to_string(),
        );

        assert_eq!(installer.lib_dir(), install_base.join("lib"));
        assert_eq!(
            installer.share_dir(),
            install_base.join("share").join("test_pkg")
        );
        assert_eq!(
            installer.ament_index_dir(),
            install_base.join("share").join("ament_index")
        );
        assert_eq!(
            installer.rust_source_dir(),
            install_base.join("share").join("test_pkg").join("rust")
        );
    }

    /// Build an installer over a temp dir, plus the artifact dir cargo would
    /// have written into (`<target_dir>/<profile>`).
    fn installer_fixture(targets: Vec<InstallTarget>) -> (TempDir, AmentInstaller, PathBuf) {
        let temp_dir = TempDir::new().unwrap();
        let install_base = temp_dir.path().join("install").join("test_pkg");
        let project_root = temp_dir.path().join("project");
        let target_dir = temp_dir.path().join("target");
        let artifact_dir = target_dir.join("debug");
        fs::create_dir_all(&artifact_dir).unwrap();
        fs::create_dir_all(&project_root).unwrap();

        let installer = AmentInstaller::new(
            install_base,
            "test_pkg".to_string(),
            project_root,
            target_dir,
            false,
            "debug".to_string(),
        )
        .with_targets(targets);

        (temp_dir, installer, artifact_dir)
    }

    /// Like [`installer_fixture`], but for a cross-compiled build: the
    /// artifact dir is `<target_dir>/<triple>/<profile>`.
    fn cross_installer_fixture(
        triple: &str,
        targets: Vec<InstallTarget>,
    ) -> (TempDir, AmentInstaller, PathBuf) {
        let temp_dir = TempDir::new().unwrap();
        let install_base = temp_dir.path().join("install").join("test_pkg");
        let project_root = temp_dir.path().join("project");
        let target_dir = temp_dir.path().join("target");
        let artifact_dir = target_dir.join(triple).join("debug");
        fs::create_dir_all(&artifact_dir).unwrap();
        fs::create_dir_all(&project_root).unwrap();

        let installer = AmentInstaller::new(
            install_base,
            "test_pkg".to_string(),
            project_root,
            target_dir,
            false,
            "debug".to_string(),
        )
        .with_targets(targets)
        .with_arch(Some(triple.to_string()));

        (temp_dir, installer, artifact_dir)
    }

    fn feature_table(entries: &[(&str, &[&str])]) -> BTreeMap<String, Vec<String>> {
        entries
            .iter()
            .map(|(name, deps)| {
                (
                    name.to_string(),
                    deps.iter().map(|d| d.to_string()).collect(),
                )
            })
            .collect()
    }

    fn gated_bin(name: &str, required_features: &[&str]) -> InstallTarget {
        InstallTarget {
            name: name.to_string(),
            kind: InstallTargetKind::Bin,
            required_features: required_features.iter().map(|f| f.to_string()).collect(),
        }
    }

    fn bin_target(name: &str) -> InstallTarget {
        InstallTarget {
            name: name.to_string(),
            kind: InstallTargetKind::Bin,
            required_features: Vec::new(),
        }
    }

    fn lib_target(name: &str) -> InstallTarget {
        InstallTarget {
            name: name.to_string(),
            kind: InstallTargetKind::Lib,
            required_features: Vec::new(),
        }
    }

    #[test]
    fn install_target_from_kinds_maps_bin() {
        let target = InstallTarget::from_kinds("my-binary", &["bin".to_string()], &[]);
        assert_eq!(
            target,
            Some(InstallTarget {
                name: "my-binary".to_string(),
                kind: InstallTargetKind::Bin,
                required_features: Vec::new(),
            })
        );
    }

    #[test]
    fn install_target_from_kinds_preserves_required_features() {
        let target = InstallTarget::from_kinds(
            "gated",
            &["bin".to_string()],
            &["extra".to_string(), "more".to_string()],
        )
        .unwrap();
        assert_eq!(target.required_features, vec!["extra", "more"]);
    }

    #[test]
    fn install_target_from_kinds_skips_rlib_only_library() {
        // A plain `[lib]` reports kind "lib", which produces an rlib: nothing to install.
        assert_eq!(
            InstallTarget::from_kinds("test_lib", &["lib".to_string()], &[]),
            None
        );
        assert_eq!(
            InstallTarget::from_kinds("test_lib", &["rlib".to_string()], &[]),
            None
        );
    }

    #[test]
    fn install_target_from_kinds_maps_linkable_library_kinds() {
        for kind in ["cdylib", "staticlib", "dylib"] {
            let target = InstallTarget::from_kinds("test_lib", &[kind.to_string()], &[]);
            assert_eq!(
                target.map(|t| t.kind),
                Some(InstallTargetKind::Lib),
                "kind {kind} should be installable"
            );
        }
    }

    #[test]
    fn install_target_from_kinds_maps_library_with_mixed_crate_types() {
        // `crate-type = ["cdylib", "rlib"]` reports both kinds on one target.
        let target =
            InstallTarget::from_kinds("test_lib", &["cdylib".to_string(), "rlib".to_string()], &[]);
        assert_eq!(target.map(|t| t.kind), Some(InstallTargetKind::Lib));
    }

    #[test]
    fn install_target_from_kinds_skips_non_artifact_kinds() {
        for kind in ["custom-build", "test", "bench", "example", "proc-macro"] {
            assert_eq!(
                InstallTarget::from_kinds("thing", &[kind.to_string()], &[]),
                None,
                "kind {kind} should not be installed"
            );
        }
    }

    #[test]
    fn bin_file_name_adds_exe_suffix_on_windows() {
        let name = bin_file_name("robot_node");
        if cfg!(windows) {
            assert_eq!(name, "robot_node.exe");
        } else {
            assert_eq!(name, "robot_node");
        }
    }

    #[test]
    fn installs_bin_targets_listed_in_targets() {
        let (_tmp, installer, artifact_dir) =
            installer_fixture(vec![bin_target("robot_node"), bin_target("other-node")]);
        fs::write(artifact_dir.join(bin_file_name("robot_node")), "elf").unwrap();
        fs::write(artifact_dir.join(bin_file_name("other-node")), "elf").unwrap();

        installer.install_artifacts().unwrap();

        let dest = installer.lib_dir().join("test_pkg");
        assert!(dest.join(bin_file_name("robot_node")).exists());
        // Binary names keep hyphens; only library file stems are underscored.
        assert!(dest.join(bin_file_name("other-node")).exists());
    }

    #[test]
    fn installs_library_artifacts_for_every_variant() {
        let (_tmp, installer, artifact_dir) = installer_fixture(vec![lib_target("my_rust_lib")]);
        for filename in [
            "libmy_rust_lib.so",
            "libmy_rust_lib.dylib",
            "libmy_rust_lib.a",
            "my_rust_lib.dll",
            "my_rust_lib.lib",
        ] {
            fs::write(artifact_dir.join(filename), "artifact").unwrap();
        }

        installer.install_artifacts().unwrap();

        let dest = installer.lib_dir().join("test_pkg");
        for filename in [
            "libmy_rust_lib.so",
            "libmy_rust_lib.dylib",
            "libmy_rust_lib.a",
            "my_rust_lib.dll",
            "my_rust_lib.lib",
        ] {
            assert!(dest.join(filename).exists(), "{filename} not installed");
        }
    }

    #[test]
    fn install_skips_artifacts_missing_from_build_dir() {
        // A binary whose required features were not enabled is simply absent.
        let (_tmp, installer, _artifact_dir) = installer_fixture(vec![bin_target("never_built")]);

        installer.install_artifacts().unwrap();

        assert!(
            !installer
                .lib_dir()
                .join("test_pkg")
                .join(bin_file_name("never_built"))
                .exists()
        );
    }

    #[test]
    fn library_only_package_still_installs_its_cdylib() {
        let (_tmp, installer, artifact_dir) = installer_fixture(vec![lib_target("test_pkg")]);
        fs::write(artifact_dir.join("libtest_pkg.so"), "artifact").unwrap();
        fs::write(installer.project_root.join("Cargo.toml"), "[package]\n").unwrap();

        installer.install().unwrap();

        assert!(
            installer
                .lib_dir()
                .join("test_pkg")
                .join("libtest_pkg.so")
                .exists()
        );
    }

    #[test]
    fn resolve_features_enables_default_and_its_closure() {
        let features = feature_table(&[
            ("default", &["std"]),
            ("std", &["alloc"]),
            ("alloc", &[]),
            ("extra", &[]),
        ]);

        let enabled = resolve_enabled_features(&features, &[], false, false);

        assert!(enabled.contains("default"));
        assert!(enabled.contains("std"));
        assert!(enabled.contains("alloc"), "closure must be transitive");
        assert!(!enabled.contains("extra"));
    }

    #[test]
    fn resolve_features_honors_no_default_features() {
        let features = feature_table(&[("default", &["std"]), ("std", &[]), ("extra", &[])]);

        let enabled = resolve_enabled_features(&features, &["extra".to_string()], true, false);

        assert!(enabled.contains("extra"));
        assert!(!enabled.contains("default"));
        assert!(!enabled.contains("std"));
    }

    #[test]
    fn resolve_features_expands_explicitly_requested_features() {
        let features = feature_table(&[("default", &[]), ("extra", &["helper"]), ("helper", &[])]);

        let enabled = resolve_enabled_features(&features, &["extra".to_string()], false, false);

        assert!(enabled.contains("default"));
        assert!(enabled.contains("extra"));
        assert!(enabled.contains("helper"));
    }

    #[test]
    fn resolve_features_with_all_features_enables_every_feature() {
        let features = feature_table(&[("default", &[]), ("a", &[]), ("b", &[])]);

        let enabled = resolve_enabled_features(&features, &[], true, true);

        assert!(enabled.contains("default"));
        assert!(enabled.contains("a"));
        assert!(enabled.contains("b"));
    }

    #[test]
    fn resolve_features_ignores_dependency_scoped_entries() {
        // "dep:serde" and "other/feat" refer to dependencies, not to features
        // of this package, so they must not leak into the enabled set.
        let features = feature_table(&[
            ("default", &["serde"]),
            ("serde", &["dep:serde", "rosidl_runtime_rs/serde"]),
        ]);

        let enabled = resolve_enabled_features(&features, &[], false, false);

        assert!(enabled.contains("serde"));
        assert!(!enabled.contains("dep:serde"));
        assert!(!enabled.contains("rosidl_runtime_rs/serde"));
    }

    #[test]
    fn skips_binary_whose_required_features_are_not_enabled() {
        let (_tmp, installer, artifact_dir) =
            installer_fixture(vec![gated_bin("gated", &["extra"])]);
        fs::write(artifact_dir.join(bin_file_name("gated")), "elf").unwrap();

        installer.install_artifacts().unwrap();

        assert!(
            !installer
                .lib_dir()
                .join("test_pkg")
                .join(bin_file_name("gated"))
                .exists(),
            "a binary gated behind a disabled feature must not be installed"
        );
    }

    #[test]
    fn installs_binary_whose_required_features_are_enabled() {
        let (_tmp, installer, artifact_dir) =
            installer_fixture(vec![gated_bin("gated", &["extra"])]);
        let installer = installer.with_enabled_features(HashSet::from(["extra".to_string()]));
        fs::write(artifact_dir.join(bin_file_name("gated")), "elf").unwrap();

        installer.install_artifacts().unwrap();

        assert!(
            installer
                .lib_dir()
                .join("test_pkg")
                .join(bin_file_name("gated"))
                .exists()
        );
    }

    #[test]
    fn requires_every_listed_feature_not_just_one() {
        let (_tmp, installer, artifact_dir) =
            installer_fixture(vec![gated_bin("gated", &["extra", "more"])]);
        let installer = installer.with_enabled_features(HashSet::from(["extra".to_string()]));
        fs::write(artifact_dir.join(bin_file_name("gated")), "elf").unwrap();

        installer.install_artifacts().unwrap();

        assert!(
            !installer
                .lib_dir()
                .join("test_pkg")
                .join(bin_file_name("gated"))
                .exists()
        );
    }

    #[test]
    fn installs_artifacts_from_the_target_triple_directory() {
        let triple = "x86_64-unknown-linux-gnu";
        let (_tmp, installer, artifact_dir) =
            cross_installer_fixture(triple, vec![bin_target("robot_node")]);
        fs::write(artifact_dir.join(bin_file_name("robot_node")), "cross").unwrap();
        // A host-profile artifact of the same name must be ignored.
        let host_dir = artifact_dir
            .parent()
            .unwrap()
            .parent()
            .unwrap()
            .join("debug");
        fs::create_dir_all(&host_dir).unwrap();
        fs::write(host_dir.join(bin_file_name("robot_node")), "host").unwrap();

        installer.install_artifacts().unwrap();

        let installed = installer
            .lib_dir()
            .join("test_pkg")
            .join(bin_file_name("robot_node"));
        assert_eq!(fs::read_to_string(installed).unwrap(), "cross");
    }

    #[test]
    fn create_markers_registers_package_in_rust_packages_index() {
        let (_tmp, installer, _artifact_dir) = installer_fixture(Vec::new());

        installer.create_markers().unwrap();

        let resource_index = installer.ament_index_dir().join("resource_index");
        assert!(resource_index.join("packages").join("test_pkg").exists());
        assert!(
            resource_index
                .join("package_type")
                .join("test_pkg")
                .exists()
        );
        assert!(
            resource_index
                .join("rust_packages")
                .join("test_pkg")
                .exists(),
            "rust_packages marker is required for colcon-ros-cargo interop"
        );
    }
}
