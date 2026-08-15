//! Diagnose a workspace whose `cargo build` fails for reasons cargo misreports.
//!
//! The binding model has one structural weakness: a missing `[patch.crates-io]`
//! entry is not an error to cargo. It resolves the name against the real
//! crates.io instead, where ROS message crates exist as stale or yanked uploads,
//! and the failure names a registry the user never asked for. The checks here
//! walk the same chain in order and stop at the first link that is broken, so
//! the answer is "no `<depend>` tag for sensor_msgs" rather than "version 4.2.3
//! is yanked".

use eyre::Result;
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

/// Marker introducing the generated patch region of a `.cargo/config.toml`.
const PATCH_MARKER: &str = "# BEGIN colcon-cargo-ros2 generated patches";

/// Written inside each generated crate by colcon-cargo-ros2; see
/// `workspace_bindgen.py`.
const MANIFEST_FILENAME: &str = ".bindgen_manifest";

/// How far up the tree to look for `.cargo/config.toml`, matching cargo's own
/// unbounded walk closely enough for a workspace.
const MAX_WALK_UP: usize = 20;

/// One diagnostic and, when it failed, what to do about it.
#[derive(Debug, Clone)]
pub struct Check {
    pub name: String,
    pub ok: bool,
    pub detail: String,
    /// Present only on failure.
    pub fix: Option<String>,
}

impl Check {
    fn pass(name: &str, detail: impl Into<String>) -> Self {
        Self {
            name: name.to_string(),
            ok: true,
            detail: detail.into(),
            fix: None,
        }
    }

    fn fail(name: &str, detail: impl Into<String>, fix: impl Into<String>) -> Self {
        Self {
            name: name.to_string(),
            ok: false,
            detail: detail.into(),
            fix: Some(fix.into()),
        }
    }
}

/// Run every check for the crate rooted at `crate_dir`.
///
/// Checks are ordered so that a failure explains the failures that would follow
/// it; the walk stops at the first one, because reporting "3 patch targets
/// missing" when no `colcon build` has ever run is noise, not diagnosis.
pub fn diagnose(crate_dir: &Path) -> Vec<Check> {
    let prefixes: Vec<PathBuf> = std::env::var("AMENT_PREFIX_PATH")
        .unwrap_or_default()
        .split(':')
        .filter(|prefix| !prefix.is_empty())
        .map(PathBuf::from)
        .collect();
    diagnose_with_prefixes(crate_dir, &prefixes)
}

/// [`diagnose`] with the ament prefixes supplied rather than read from the
/// environment, so tests do not have to mutate process-wide state.
pub fn diagnose_with_prefixes(crate_dir: &Path, prefixes: &[PathBuf]) -> Vec<Check> {
    let mut checks = Vec::new();

    checks.push(check_ros_environment(prefixes));
    if !checks.last().unwrap().ok {
        return checks;
    }

    let config_path = match find_cargo_config(crate_dir) {
        Some(path) => {
            checks.push(Check::pass(
                "Generated .cargo/config.toml",
                format!("found at {}", path.display()),
            ));
            path
        }
        None => {
            checks.push(Check::fail(
                "Generated .cargo/config.toml",
                format!("none found above {}", crate_dir.display()),
                "Run `colcon build` in the workspace once; cargo needs the patches it writes.",
            ));
            return checks;
        }
    };

    let config_text = std::fs::read_to_string(&config_path).unwrap_or_default();
    if config_text.contains(PATCH_MARKER) {
        checks.push(Check::pass("Patch section", "generated markers present"));
    } else {
        checks.push(Check::fail(
            "Patch section",
            "config exists but carries no colcon-cargo-ros2 markers",
            "Re-run `colcon build`; the config was written by something else.",
        ));
        return checks;
    }

    // Cargo resolves relative config paths against the directory *containing*
    // `.cargo`, not against `.cargo` itself -- and every generated patch path is
    // relative. Resolving them one level too deep reports every crate missing.
    let config_base = config_path
        .parent()
        .and_then(Path::parent)
        .unwrap_or(crate_dir);
    let patches = parse_patches(&config_text, config_base);
    checks.push(check_patch_targets(&patches));
    if !checks.last().unwrap().ok {
        return checks;
    }

    // Before asking whether the patched crates are any good, ask whether cargo
    // will read them at all. A dependency with its own source makes every later
    // check true and irrelevant at the same time.
    checks.push(check_dependency_sources(crate_dir, &patches, prefixes));

    checks.push(check_bindings_fresh(&patches));
    checks.push(check_declared_dependencies(crate_dir, &patches, prefixes));

    checks
}

fn check_ros_environment(prefixes: &[PathBuf]) -> Check {
    if prefixes.is_empty() {
        return Check::fail(
            "ROS environment",
            "AMENT_PREFIX_PATH is not set",
            "Source your ROS installation (e.g. `source /opt/ros/humble/setup.bash`). \
             A generated .cargo/config.toml supplies this too, so this only matters \
             outside a built workspace.",
        );
    }
    Check::pass(
        "ROS environment",
        format!("{} prefixes on AMENT_PREFIX_PATH", prefixes.len()),
    )
}

/// Walk up from `crate_dir` looking for `.cargo/config.toml`, as cargo does.
fn find_cargo_config(crate_dir: &Path) -> Option<PathBuf> {
    let mut current = crate_dir;
    for _ in 0..MAX_WALK_UP {
        let candidate = current.join(".cargo").join("config.toml");
        if candidate.is_file() {
            return Some(candidate);
        }
        current = current.parent()?;
    }
    None
}

/// Map every `[patch.crates-io]` path entry to an absolute directory.
fn parse_patches(config_text: &str, config_dir: &Path) -> BTreeMap<String, PathBuf> {
    let mut patches = BTreeMap::new();

    let Ok(value) = config_text.parse::<toml::Value>() else {
        return patches;
    };
    let Some(table) = value
        .get("patch")
        .and_then(|patch| patch.get("crates-io"))
        .and_then(toml::Value::as_table)
    else {
        return patches;
    };

    for (name, spec) in table {
        if let Some(path) = spec.get("path").and_then(toml::Value::as_str) {
            patches.insert(name.clone(), config_dir.join(path));
        }
    }
    patches
}

fn check_patch_targets(patches: &BTreeMap<String, PathBuf>) -> Check {
    if patches.is_empty() {
        return Check::pass("Patched crates", "no ROS message dependencies patched");
    }

    let missing: Vec<&String> = patches
        .iter()
        .filter(|(_, dir)| !dir.join("Cargo.toml").is_file())
        .map(|(name, _)| name)
        .collect();

    if missing.is_empty() {
        return Check::pass(
            "Patched crates",
            format!("{} generated crates readable", patches.len()),
        );
    }

    Check::fail(
        "Patched crates",
        format!(
            "generated crates missing for: {}",
            missing
                .iter()
                .map(|name| name.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        ),
        "The build/ directory was cleaned while the config still points into it. \
         Re-run `colcon build`.",
    )
}

fn check_bindings_fresh(patches: &BTreeMap<String, PathBuf>) -> Check {
    let mut stale = Vec::new();
    let mut checked = 0usize;

    for (name, dir) in patches {
        match manifest_state(&dir.join(MANIFEST_FILENAME)) {
            ManifestState::Fresh => checked += 1,
            ManifestState::Stale => {
                checked += 1;
                stale.push(name.as_str());
            }
            ManifestState::Unknown => {}
        }
    }

    if stale.is_empty() {
        let detail = if checked == 0 {
            // Bindings generated before this check existed carry no records.
            "no freshness records to check; re-run `colcon build` to write them".to_string()
        } else {
            format!("{checked} crates match their interface definitions")
        };
        return Check::pass("Binding freshness", detail);
    }

    Check::fail(
        "Binding freshness",
        format!("stale bindings for: {}", stale.join(", ")),
        "The .msg/.srv/.action files changed since these were generated. \
         Re-run `colcon build`.",
    )
}

enum ManifestState {
    Fresh,
    Stale,
    /// No manifest, or a source directory we cannot read: not judgeable.
    Unknown,
}

/// Compare a generated crate's recorded interface files against what is on disk.
///
/// Mirrors the check compiled into each generated crate's build.rs, so `doctor`
/// answers the same question without a build.
fn manifest_state(manifest_path: &Path) -> ManifestState {
    let Ok(content) = std::fs::read_to_string(manifest_path) else {
        return ManifestState::Unknown;
    };
    let mut lines = content.lines();
    let Some(source_dir) = lines.next().filter(|line| !line.is_empty()) else {
        return ManifestState::Unknown;
    };
    let source_dir = Path::new(source_dir);
    if !source_dir.is_dir() {
        return ManifestState::Unknown;
    }

    let recorded: BTreeSet<String> = lines
        .filter(|line| !line.is_empty())
        .map(str::to_string)
        .collect();

    let mut current = BTreeSet::new();
    for subdir in ["msg", "srv", "action"] {
        let root = source_dir.join(subdir);
        if root.is_dir() {
            collect_interface_records(&root, source_dir, &mut current);
        }
    }

    if current == recorded {
        ManifestState::Fresh
    } else {
        ManifestState::Stale
    }
}

fn collect_interface_records(dir: &Path, base: &Path, out: &mut BTreeSet<String>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            collect_interface_records(&path, base, out);
            continue;
        }
        let is_definition = matches!(
            path.extension().and_then(|ext| ext.to_str()),
            Some("msg") | Some("srv") | Some("action") | Some("idl")
        );
        if !is_definition {
            continue;
        }
        let (Ok(metadata), Ok(relative)) = (entry.metadata(), path.strip_prefix(base)) else {
            continue;
        };
        let Ok(contents) = std::fs::read(&path) else {
            continue;
        };
        out.insert(format!(
            "{}:{}:{}",
            relative.display(),
            metadata.len(),
            fnv1a64(&contents)
        ));
    }
}

/// FNV-1a, 64-bit, as 16 hex digits.
///
/// Must match the digest colcon-cargo-ros2 writes into `.bindgen_manifest` and
/// the one compiled into each generated crate's build.rs; all three describe the
/// same records.
fn fnv1a64(data: &[u8]) -> String {
    let mut digest: u64 = 0xcbf2_9ce4_8422_2325;
    for byte in data {
        digest ^= *byte as u64;
        digest = digest.wrapping_mul(0x0000_0100_0000_01b3);
    }
    format!("{digest:016x}")
}

/// Interface packages used in Cargo.toml must be declared in package.xml.
///
/// Bindings are generated from package.xml alone, so an undeclared package gets
/// no patch and cargo falls through to crates.io. Checking only against the
/// patches would miss exactly the case that breaks builds -- an undeclared
/// package is by definition unpatched -- so unknown dependency names are also
/// resolved against the ament index, where an interface package has
/// `share/<name>/msg|srv|action`.
/// Interface packages the manifest resolves from its own `path` or `git` source.
///
/// `[patch.crates-io]` redirects registry dependencies only. A dependency
/// carrying its own source is taken from there, the generated crate is never
/// read, and cargo reports whatever is at that source -- for upstream
/// safe_drive_tutorial, a `/tmp` directory belonging to the machine that ran a
/// different generator.
fn check_dependency_sources(
    crate_dir: &Path,
    patches: &BTreeMap<String, PathBuf>,
    prefixes: &[PathBuf],
) -> Check {
    let cargo_toml = crate_dir.join("Cargo.toml");
    let bypassed: Vec<(String, String)> = dependency_sources(&cargo_toml)
        .into_iter()
        .filter(|(name, source)| {
            if !(patches.contains_key(name) || is_interface_package(name, prefixes)) {
                return false;
            }
            // Pointing straight at the crate the patch names is equivalent, and
            // saying so would ask for a rewrite that changes nothing.
            match patches.get(name) {
                Some(target) => !resolves_to(crate_dir, source, target),
                None => true,
            }
        })
        .collect();

    if bypassed.is_empty() {
        return Check::pass(
            "Dependency sources",
            "interface crates come from the patches",
        );
    }

    let detail = bypassed
        .iter()
        .map(|(name, source)| format!("{name} is taken from {source}"))
        .collect::<Vec<_>>()
        .join(", ");
    let fix = bypassed
        .iter()
        .map(|(name, _)| format!("  {name} = \"*\""))
        .collect::<Vec<_>>()
        .join("\n");

    Check::fail(
        "Dependency sources",
        format!("{detail}, so the generated bindings are unused"),
        format!("Drop the source key in Cargo.toml so the patch applies:\n{fix}"),
    )
}

/// True when *source*, read from a manifest in *base*, names *target*.
fn resolves_to(base: &Path, source: &str, target: &Path) -> bool {
    let candidate = Path::new(source);
    let candidate = if candidate.is_absolute() {
        candidate.to_path_buf()
    } else {
        base.join(candidate)
    };
    match (candidate.canonicalize(), target.canonicalize()) {
        (Ok(a), Ok(b)) => a == b,
        _ => candidate == target,
    }
}

/// Package name -> the `path` or `git` value it is resolved from.
fn dependency_sources(cargo_toml: &Path) -> Vec<(String, String)> {
    let mut sources = Vec::new();

    let Ok(text) = std::fs::read_to_string(cargo_toml) else {
        return sources;
    };
    let Ok(value) = text.parse::<toml::Value>() else {
        return sources;
    };

    for table in dependency_tables(&value) {
        for (key, spec) in table {
            let Some(spec) = spec.as_table() else {
                continue;
            };
            let name = spec
                .get("package")
                .and_then(toml::Value::as_str)
                .unwrap_or(key);
            for kind in ["path", "git"] {
                if let Some(source) = spec.get(kind).and_then(toml::Value::as_str) {
                    sources.push((name.to_string(), source.to_string()));
                    break;
                }
            }
        }
    }
    sources
}

fn check_declared_dependencies(
    crate_dir: &Path,
    patches: &BTreeMap<String, PathBuf>,
    prefixes: &[PathBuf],
) -> Check {
    let cargo_deps = cargo_dependency_names(&crate_dir.join("Cargo.toml"));
    let declared = package_xml_dependencies(&crate_dir.join("package.xml"));

    let undeclared: Vec<&str> = cargo_deps
        .iter()
        .filter(|name| {
            !declared.contains(*name)
                && (patches.contains_key(*name) || is_interface_package(name, prefixes))
        })
        .map(String::as_str)
        .collect();

    if undeclared.is_empty() {
        return Check::pass(
            "package.xml declarations",
            "every patched crate is declared",
        );
    }

    let tags = undeclared
        .iter()
        .map(|name| format!("  <depend>{name}</depend>"))
        .collect::<Vec<_>>()
        .join("\n");

    Check::fail(
        "package.xml declarations",
        format!(
            "used in Cargo.toml but not declared: {}",
            undeclared.join(", ")
        ),
        format!("Add to package.xml, then re-run `colcon build`:\n{tags}"),
    )
}

/// True when an installed package under any prefix carries interface definitions.
fn is_interface_package(name: &str, prefixes: &[PathBuf]) -> bool {
    prefixes.iter().any(|prefix| {
        let share = prefix.join("share").join(name);
        ["msg", "srv", "action"]
            .iter()
            .any(|subdir| share.join(subdir).is_dir())
    })
}

/// Every package name a manifest depends on, following renames and target tables.
fn cargo_dependency_names(cargo_toml: &Path) -> BTreeSet<String> {
    let mut names = BTreeSet::new();

    let Ok(text) = std::fs::read_to_string(cargo_toml) else {
        return names;
    };
    let Ok(value) = text.parse::<toml::Value>() else {
        return names;
    };

    for table in dependency_tables(&value) {
        for (key, spec) in table {
            let name = spec
                .get("package")
                .and_then(toml::Value::as_str)
                .unwrap_or(key);
            names.insert(name.to_string());
        }
    }
    names
}

/// Every dependency table in a manifest, including the `[target.*]` ones.
fn dependency_tables(value: &toml::Value) -> Vec<&toml::value::Table> {
    let kinds = ["dependencies", "build-dependencies", "dev-dependencies"];
    let mut tables: Vec<&toml::value::Table> = Vec::new();
    for kind in kinds {
        if let Some(table) = value.get(kind).and_then(toml::Value::as_table) {
            tables.push(table);
        }
    }
    if let Some(targets) = value.get("target").and_then(toml::Value::as_table) {
        for target in targets.values() {
            for kind in kinds {
                if let Some(table) = target.get(kind).and_then(toml::Value::as_table) {
                    tables.push(table);
                }
            }
        }
    }
    tables
}

/// Package names from `<depend>`-family tags, without pulling in an XML parser.
fn package_xml_dependencies(package_xml: &Path) -> BTreeSet<String> {
    let mut names = BTreeSet::new();
    let Ok(text) = std::fs::read_to_string(package_xml) else {
        return names;
    };

    for tag in [
        "depend",
        "build_depend",
        "build_export_depend",
        "exec_depend",
        "run_depend",
        "test_depend",
    ] {
        let open = format!("<{tag}>");
        let close = format!("</{tag}>");
        let mut rest = text.as_str();
        while let Some(start) = rest.find(&open) {
            let after = &rest[start + open.len()..];
            let Some(end) = after.find(&close) else {
                break;
            };
            names.insert(after[..end].trim().to_string());
            rest = &after[end + close.len()..];
        }
    }
    names
}

/// Print the checklist and report whether everything passed.
pub fn report(checks: &[Check]) -> bool {
    let mut all_ok = true;
    for check in checks {
        let mark = if check.ok { "✓" } else { "✗" };
        println!("{mark} {}: {}", check.name, check.detail);
        if let Some(fix) = &check.fix {
            all_ok = false;
            for line in fix.lines() {
                println!("    {line}");
            }
        }
    }
    all_ok
}

/// Diagnose `crate_dir` and print the result.
pub fn run(crate_dir: &Path) -> Result<bool> {
    let checks = diagnose(crate_dir);
    Ok(report(&checks))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::TempDir;

    fn write(path: &Path, content: &str) {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, content).unwrap();
    }

    /// A crate with a generated config patching one message package.
    fn workspace(with_binding: bool) -> (TempDir, PathBuf) {
        let tmp = TempDir::new().unwrap();
        let crate_dir = tmp.path().join("src/pkg_a");
        let binding = tmp.path().join("build/std_msgs/rosidl_cargo/std_msgs");

        if with_binding {
            write(
                &binding.join("Cargo.toml"),
                "[package]\nname = \"std_msgs\"\n",
            );
        }
        // Relative, exactly as colcon-cargo-ros2 writes them: resolved against
        // the crate directory, which is the parent of `.cargo`.
        write(
            &crate_dir.join(".cargo/config.toml"),
            &format!(
                "[patch.crates-io]\n{PATCH_MARKER}\nstd_msgs = {{ path = \"{}\" }}\n",
                "../../build/std_msgs/rosidl_cargo/std_msgs"
            ),
        );
        write(
            &crate_dir.join("Cargo.toml"),
            "[package]\nname = \"pkg_a\"\n\n[dependencies]\nstd_msgs = \"*\"\n",
        );
        write(
            &crate_dir.join("package.xml"),
            "<package><name>pkg_a</name><depend>std_msgs</depend></package>",
        );
        (tmp, crate_dir)
    }

    fn check<'a>(checks: &'a [Check], name: &str) -> &'a Check {
        checks.iter().find(|c| c.name == name).unwrap()
    }

    /// Any non-empty prefix list; only its emptiness is examined by most checks.
    fn ros_prefix() -> PathBuf {
        PathBuf::from("/opt/ros/humble")
    }

    #[test]
    fn healthy_workspace_passes_every_check() {
        let (_tmp, crate_dir) = workspace(true);

        let checks = diagnose_with_prefixes(&crate_dir, &[ros_prefix()]);

        assert!(checks.iter().all(|c| c.ok), "{checks:#?}");
    }

    #[test]
    fn missing_config_stops_the_walk_with_a_fix() {
        let tmp = TempDir::new().unwrap();
        let crate_dir = tmp.path().join("pkg");
        fs::create_dir_all(&crate_dir).unwrap();

        let checks = diagnose_with_prefixes(&crate_dir, &[ros_prefix()]);

        let config = check(&checks, "Generated .cargo/config.toml");
        assert!(!config.ok);
        assert!(config.fix.as_ref().unwrap().contains("colcon build"));
        // Later checks would only restate this one.
        assert!(checks.iter().all(|c| c.name != "Patched crates"));
    }

    #[test]
    fn vanished_binding_directory_is_named() {
        let (_tmp, crate_dir) = workspace(false);

        let checks = diagnose_with_prefixes(&crate_dir, &[ros_prefix()]);

        let patched = check(&checks, "Patched crates");
        assert!(!patched.ok);
        assert!(patched.detail.contains("std_msgs"));
    }

    #[test]
    fn undeclared_dependency_is_reported_with_the_tag_to_add() {
        let (_tmp, crate_dir) = workspace(true);
        write(
            &crate_dir.join("package.xml"),
            "<package><name>pkg_a</name></package>",
        );

        let checks = diagnose_with_prefixes(&crate_dir, &[ros_prefix()]);

        let declarations = check(&checks, "package.xml declarations");
        assert!(!declarations.ok);
        assert!(
            declarations
                .fix
                .as_ref()
                .unwrap()
                .contains("<depend>std_msgs</depend>")
        );
    }

    #[test]
    fn undeclared_unpatched_interface_package_is_still_caught() {
        // The case that actually breaks builds: no <depend> tag means no
        // bindings, so the package is absent from the patch table too. Only the
        // ament index can identify it.
        let (tmp, crate_dir) = workspace(true);
        write(
            &crate_dir.join("Cargo.toml"),
            "[package]\nname = \"pkg_a\"\n\n[dependencies]\nstd_msgs = \"*\"\nsensor_msgs = \"*\"\n",
        );
        let prefix = tmp.path().join("opt/ros");
        fs::create_dir_all(prefix.join("share/sensor_msgs/msg")).unwrap();

        let checks = diagnose_with_prefixes(&crate_dir, &[prefix]);

        let declarations = check(&checks, "package.xml declarations");
        assert!(!declarations.ok);
        assert!(declarations.detail.contains("sensor_msgs"));
    }

    #[test]
    fn ordinary_crates_io_dependencies_are_not_flagged() {
        let (tmp, crate_dir) = workspace(true);
        write(
            &crate_dir.join("Cargo.toml"),
            "[package]\nname = \"pkg_a\"\n\n[dependencies]\nstd_msgs = \"*\"\nserde = \"1\"\n",
        );
        let prefix = tmp.path().join("opt/ros");
        fs::create_dir_all(prefix.join("share/sensor_msgs/msg")).unwrap();

        let checks = diagnose_with_prefixes(&crate_dir, &[prefix]);

        assert!(check(&checks, "package.xml declarations").ok);
    }

    #[test]
    fn renamed_dependency_is_resolved_to_the_real_package() {
        let tmp = TempDir::new().unwrap();
        let manifest = tmp.path().join("Cargo.toml");
        write(
            &manifest,
            "[dependencies]\nmsgs = { package = \"sensor_msgs\", version = \"*\" }\n\
             [target.'cfg(unix)'.dependencies]\nstd_msgs = \"*\"\n",
        );

        let names = cargo_dependency_names(&manifest);

        assert!(names.contains("sensor_msgs"));
        assert!(names.contains("std_msgs"));
        assert!(!names.contains("msgs"));
    }

    #[test]
    fn package_xml_tags_are_read_across_dependency_kinds() {
        let tmp = TempDir::new().unwrap();
        let package_xml = tmp.path().join("package.xml");
        write(
            &package_xml,
            "<package>\n  <depend>std_msgs</depend>\n  \
             <build_depend>geometry_msgs</build_depend>\n  \
             <exec_depend>sensor_msgs</exec_depend>\n</package>",
        );

        let names = package_xml_dependencies(&package_xml);

        assert_eq!(
            names.iter().map(String::as_str).collect::<Vec<_>>(),
            vec!["geometry_msgs", "sensor_msgs", "std_msgs"]
        );
    }

    /// The digest has to be byte-identical to the one Python writes into the
    /// manifest, or every crate looks stale to `doctor` and fresh to colcon.
    #[test]
    fn fnv1a64_matches_the_reference_vectors() {
        // Computed with the Python implementation in workspace_bindgen.py.
        assert_eq!(fnv1a64(b""), "cbf29ce484222325");
        assert_eq!(fnv1a64(b"int32 x\n"), "696106d176cc877f");
        assert_eq!(fnv1a64(b"a"), "af63dc4c8601ec8c");
    }

    #[test]
    fn stale_manifest_is_detected() {
        let tmp = TempDir::new().unwrap();
        let source = tmp.path().join("share/my_msgs");
        write(&source.join("msg/Thing.msg"), "int32 value\n");
        let crate_dir = tmp.path().join("build/my_msgs");
        fs::create_dir_all(&crate_dir).unwrap();

        // Record the file as it is, then change its contents.
        let contents = fs::read(source.join("msg/Thing.msg")).unwrap();
        write(
            &crate_dir.join(MANIFEST_FILENAME),
            &format!(
                "{}\nmsg/Thing.msg:{}:{}\n",
                source.display(),
                contents.len(),
                fnv1a64(&contents)
            ),
        );
        assert!(matches!(
            manifest_state(&crate_dir.join(MANIFEST_FILENAME)),
            ManifestState::Fresh
        ));

        write(&source.join("msg/Thing.msg"), "int32 value\nstring name\n");

        assert!(matches!(
            manifest_state(&crate_dir.join(MANIFEST_FILENAME)),
            ManifestState::Stale
        ));
    }

    /// A checkout or a copy rewrites mtimes; the definitions are unchanged and
    /// the bindings are still good.
    #[test]
    fn a_touched_but_unchanged_definition_is_fresh() {
        let tmp = TempDir::new().unwrap();
        let source = tmp.path().join("share/my_msgs");
        write(&source.join("msg/Thing.msg"), "int32 value\n");
        let crate_dir = tmp.path().join("build/my_msgs");
        fs::create_dir_all(&crate_dir).unwrap();

        let contents = fs::read(source.join("msg/Thing.msg")).unwrap();
        write(
            &crate_dir.join(MANIFEST_FILENAME),
            &format!(
                "{}\nmsg/Thing.msg:{}:{}\n",
                source.display(),
                contents.len(),
                fnv1a64(&contents)
            ),
        );

        // Rewrite the same bytes, which moves the mtime.
        write(&source.join("msg/Thing.msg"), "int32 value\n");

        assert!(matches!(
            manifest_state(&crate_dir.join(MANIFEST_FILENAME)),
            ManifestState::Fresh
        ));
    }

    #[test]
    fn absent_manifest_is_not_a_failure() {
        let tmp = TempDir::new().unwrap();

        assert!(matches!(
            manifest_state(&tmp.path().join(MANIFEST_FILENAME)),
            ManifestState::Unknown
        ));
    }

    /// `[patch.crates-io]` only redirects registry dependencies, so a patched
    /// name with its own source is resolved from that source and the generated
    /// crate is never read. Every other check still passes, which is how
    /// safe_drive_tutorial reached "Workspace looks healthy" while failing to
    /// build. See issue #11.
    #[test]
    fn a_path_dependency_on_a_patched_package_is_not_healthy() {
        let (_tmp, crate_dir) = workspace(true);
        write(
            &crate_dir.join("Cargo.toml"),
            "[package]\nname = \"pkg_a\"\n\n[dependencies]\n\
             safe_drive = \"0.3\"\n\
             std_msgs = { path = \"/tmp/elsewhere/std_msgs\" }\n",
        );

        let checks = diagnose_with_prefixes(&crate_dir, &[ros_prefix()]);

        let sources = check(&checks, "Dependency sources");
        assert!(!sources.ok, "{checks:#?}");
        assert!(sources.detail.contains("std_msgs"), "{sources:#?}");
        assert!(
            sources.detail.contains("/tmp/elsewhere/std_msgs"),
            "the source has to be named: {sources:#?}"
        );
        assert!(
            sources
                .fix
                .as_deref()
                .unwrap_or_default()
                .contains("std_msgs = \"*\""),
            "{sources:#?}"
        );
        assert!(
            !sources.detail.contains("safe_drive"),
            "an ordinary path-less dependency is not the subject: {sources:#?}"
        );
    }

    #[test]
    fn a_git_dependency_on_a_patched_package_is_reported_too() {
        let (_tmp, crate_dir) = workspace(true);
        write(
            &crate_dir.join("Cargo.toml"),
            "[package]\nname = \"pkg_a\"\n\n[dependencies]\n\
             std_msgs = { git = \"https://example.invalid/msgs.git\" }\n",
        );

        let checks = diagnose_with_prefixes(&crate_dir, &[ros_prefix()]);

        let sources = check(&checks, "Dependency sources");
        assert!(!sources.ok, "{checks:#?}");
        assert!(
            sources.detail.contains("https://example.invalid/msgs.git"),
            "{sources:#?}"
        );
    }

    #[test]
    fn a_path_dependency_on_an_ordinary_crate_is_fine() {
        let (_tmp, crate_dir) = workspace(true);
        write(
            &crate_dir.join("Cargo.toml"),
            "[package]\nname = \"pkg_a\"\n\n[dependencies]\n\
             std_msgs = \"*\"\n\
             my_helpers = { path = \"../my_helpers\" }\n",
        );

        let checks = diagnose_with_prefixes(&crate_dir, &[ros_prefix()]);

        assert!(checks.iter().all(|c| c.ok), "{checks:#?}");
    }

    /// Pointing straight at the crate the patch names is unusual but equivalent,
    /// and telling someone to replace a working setup with the same thing is
    /// noise.
    #[test]
    fn a_path_dependency_on_the_generated_crate_itself_is_fine() {
        let (tmp, crate_dir) = workspace(true);
        let generated = tmp.path().join("build/std_msgs/rosidl_cargo/std_msgs");
        write(
            &crate_dir.join("Cargo.toml"),
            &format!(
                "[package]\nname = \"pkg_a\"\n\n[dependencies]\nstd_msgs = {{ path = \"{}\" }}\n",
                generated.display()
            ),
        );

        let checks = diagnose_with_prefixes(&crate_dir, &[ros_prefix()]);

        assert!(checks.iter().all(|c| c.ok), "{checks:#?}");
    }

    /// An interface package resolved by path that is not patched either -- both
    /// the missing declaration and the bypassed bindings are worth saying.
    #[test]
    fn an_unpatched_interface_package_with_a_path_is_still_caught() {
        let (tmp, crate_dir) = workspace(true);
        let share = tmp.path().join("opt/share/sensor_msgs/msg");
        fs::create_dir_all(&share).unwrap();
        write(&share.join("Thing.msg"), "int32 value\n");
        write(
            &crate_dir.join("Cargo.toml"),
            "[package]\nname = \"pkg_a\"\n\n[dependencies]\n\
             std_msgs = \"*\"\n\
             sensor_msgs = { path = \"../vendor/sensor_msgs\" }\n",
        );

        let checks = diagnose_with_prefixes(&crate_dir, &[tmp.path().join("opt")]);

        assert!(!check(&checks, "Dependency sources").ok, "{checks:#?}");
        assert!(
            !check(&checks, "package.xml declarations").ok,
            "{checks:#?}"
        );
    }
}
