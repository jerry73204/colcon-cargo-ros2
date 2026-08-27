//! Generator integration for generating Rust bindings from ROS 2 interface packages.
//!
//! This module integrates with rosidl-codegen to:
//! - Parse interface files (.msg, .srv, .action)
//! - Generate Rust code for messages, services, and actions
//! - Write generated code to output directory with proper structure

use crate::ament::Package;
use askama::Template;
use eyre::{Result, WrapErr};
use rosidl_codegen::{
    GeneratedPackage, generate_action_package, generate_idl_file, generate_message_package,
    generate_service_package,
    idl_generator::idl_struct_to_message,
    utils::{extract_dependencies, needs_big_array, to_snake_case},
};
use std::collections::HashSet;
use std::path::{Path, PathBuf};

/// Interface info for template rendering
#[derive(Debug, Clone)]
struct InterfaceInfo {
    type_name: String,
    module_name: String,
}

/// Generated IDL artifacts (messages, services, actions, constants, enums)
/// Note: IDL struct messages are tracked here and added to regular messages list
#[derive(Debug, Default)]
struct GeneratedIdlArtifacts {
    messages: Vec<String>,
    services: Vec<String>,
    actions: Vec<String>,
    constant_modules: Vec<String>,
    enums: Vec<String>,
}

/// Template for generating lib.rs
#[derive(Template)]
#[template(path = "lib.rs.jinja")]
struct LibRsTemplate {
    package_name: String,
    has_messages: bool,
    has_services: bool,
    has_actions: bool,
}

/// Template for generating msg.rs
#[derive(Template)]
#[template(path = "msg.rs.jinja")]
struct MsgRsTemplate {
    package_name: String,
    messages: Vec<InterfaceInfo>,
    constant_modules: Vec<InterfaceInfo>,
    enums: Vec<InterfaceInfo>,
}

/// Template for generating srv.rs
#[derive(Template)]
#[template(path = "srv.rs.jinja")]
struct SrvRsTemplate {
    package_name: String,
    services: Vec<InterfaceInfo>,
}

/// Template for generating action.rs
#[derive(Template)]
#[template(path = "action.rs.jinja")]
struct ActionRsTemplate {
    package_name: String,
    actions: Vec<InterfaceInfo>,
}

/// Fallback rosidl_runtime_rs version for generated bindings.
///
/// Used only when nothing better is known. The version is not really ours to
/// choose: it has to match whatever the consumer's `rclrs` depends on, because
/// cargo treats 0.5 and 0.6 as incompatible and will happily instantiate both,
/// after which the `Message` trait from one is not the `Message` trait from the
/// other:
///
/// ```text
/// error[E0277]: the trait bound `std_msgs::msg::String: MessageIDL` is not satisfied
/// note: there are multiple different versions of crate `rosidl_runtime_rs`
///       in the dependency graph
/// ```
///
/// | rclrs | rosidl_runtime_rs |
/// |-------|-------------------|
/// | 0.6   | 0.5               |
/// | 0.7   | 0.6               |
///
/// A version *range* does not help: cargo picks the greatest match per
/// incompatible bucket rather than unifying across them. The colcon extension
/// therefore derives the version from what the workspace's own packages declare
/// (see `_detect_runtime_version` in `workspace_bindgen.py`) and passes it in;
/// `--rosidl-runtime-rs-version` overrides both.
pub const ROSIDL_RUNTIME_RS_VERSION: &str = "0.6";

/// Version stamped on every generated binding crate.
///
/// Fixed on purpose. The crates are reached through `[patch.crates-io]` path
/// entries, so cargo never resolves between versions of them — but it does copy
/// whatever version it finds into the consumer's `Cargo.lock`. Using the ROS
/// package version there turned that lock into a record of one machine's ROS
/// installation, so a workspace that commits its lock came back dirty after
/// every build elsewhere. The ROS version is kept in `[package.metadata.ros]`.
pub const GENERATED_CRATE_VERSION: &str = "0.0.0";

/// Generated Rust package structure.
///
/// The dual-layer architecture is:
/// - `pkg::rmw::msg::Type` - C-compatible FFI structs for interop with ROS C libraries
/// - `pkg::msg::Type` - Idiomatic Rust wrappers with safe types (String, Vec, etc.)
#[derive(Debug)]
pub struct GeneratedRustPackage {
    /// Package name
    pub name: String,
    /// Output directory where code was written
    pub output_dir: PathBuf,
    /// Number of messages generated
    pub message_count: usize,
    /// Number of services generated
    pub service_count: usize,
    /// Number of actions generated
    pub action_count: usize,
}

/// Generate Rust bindings for a ROS 2 package
///
/// If `rosidl_runtime_rs_version` is `Some`, it overrides the default version
/// used in the generated Cargo.toml dependency on `rosidl_runtime_rs`.
pub fn generate_package(
    package: &Package,
    output_dir: &Path,
    rosidl_runtime_rs_version: Option<&str>,
) -> Result<GeneratedRustPackage> {
    let package_output = output_dir.join(&package.name);
    std::fs::create_dir_all(&package_output).wrap_err_with(|| {
        format!(
            "Failed to create output directory: {}",
            package_output.display()
        )
    })?;

    let mut message_count = 0;
    let mut service_count = 0;
    let mut action_count = 0;
    let mut all_dependencies = HashSet::new();
    let mut package_needs_big_array = false;

    // Track successfully generated IDL artifacts
    // Note: IDL struct messages are tracked separately to add to regular messages
    let mut generated_idl_messages = Vec::new();
    let mut generated_idl_services = Vec::new();
    let mut generated_idl_actions = Vec::new();
    let mut generated_idl_constant_modules = Vec::new();
    let mut generated_idl_enums = Vec::new();

    // For dependency tracking (cross-package references)
    let known_packages = HashSet::new(); // TODO: populate from ament index

    // Generate messages
    for msg_name in &package.interfaces.messages {
        let msg_path = package.get_message_path(msg_name);
        let content = std::fs::read_to_string(&msg_path)
            .wrap_err_with(|| format!("Failed to read message file: {}", msg_path.display()))?;

        let parsed_msg = rosidl_parser::parse_message(&content)
            .wrap_err_with(|| format!("Failed to parse message: {}", msg_name))?;

        // Extract dependencies from this message
        let msg_deps = extract_dependencies(&parsed_msg);
        all_dependencies.extend(msg_deps);

        // Check if this message needs big_array support
        if needs_big_array(&parsed_msg) {
            package_needs_big_array = true;
        }

        let generated =
            generate_message_package(&package.name, msg_name, &parsed_msg, &known_packages)
                .wrap_err_with(|| format!("Failed to generate message: {}", msg_name))?;

        write_generated_package(&generated, &package_output, msg_name)?;
        message_count += 1;
    }

    // Generate services
    for srv_name in &package.interfaces.services {
        let srv_path = package.get_service_path(srv_name);
        let content = std::fs::read_to_string(&srv_path)
            .wrap_err_with(|| format!("Failed to read service file: {}", srv_path.display()))?;

        let parsed_srv = rosidl_parser::parse_service(&content)
            .wrap_err_with(|| format!("Failed to parse service: {}", srv_name))?;

        // Extract dependencies from request and response messages
        let req_deps = extract_dependencies(&parsed_srv.request);
        let resp_deps = extract_dependencies(&parsed_srv.response);
        all_dependencies.extend(req_deps);
        all_dependencies.extend(resp_deps);

        // Check if request or response needs big_array support
        if needs_big_array(&parsed_srv.request) || needs_big_array(&parsed_srv.response) {
            package_needs_big_array = true;
        }

        let generated =
            generate_service_package(&package.name, srv_name, &parsed_srv, &known_packages)
                .wrap_err_with(|| format!("Failed to generate service: {}", srv_name))?;

        write_generated_service(&generated, &package_output, srv_name)?;
        service_count += 1;
    }

    // Generate actions
    for action_name in &package.interfaces.actions {
        let action_path = package.get_action_path(action_name);
        let content = std::fs::read_to_string(&action_path)
            .wrap_err_with(|| format!("Failed to read action file: {}", action_path.display()))?;

        let parsed_action = rosidl_parser::parse_action(&content)
            .wrap_err_with(|| format!("Failed to parse action: {}", action_name))?;

        // Extract dependencies from goal, result, and feedback messages
        let goal_deps = extract_dependencies(&parsed_action.spec.goal);
        let result_deps = extract_dependencies(&parsed_action.spec.result);
        let feedback_deps = extract_dependencies(&parsed_action.spec.feedback);
        all_dependencies.extend(goal_deps);
        all_dependencies.extend(result_deps);
        all_dependencies.extend(feedback_deps);

        // Actions always require these dependencies
        all_dependencies.insert("unique_identifier_msgs".to_string());
        all_dependencies.insert("action_msgs".to_string());
        all_dependencies.insert("builtin_interfaces".to_string());

        // Check if goal, result, or feedback needs big_array support
        if needs_big_array(&parsed_action.spec.goal)
            || needs_big_array(&parsed_action.spec.result)
            || needs_big_array(&parsed_action.spec.feedback)
        {
            package_needs_big_array = true;
        }

        let generated =
            generate_action_package(&package.name, action_name, &parsed_action, &known_packages)
                .wrap_err_with(|| format!("Failed to generate action: {}", action_name))?;

        write_generated_action(&generated, &package_output, action_name)?;
        action_count += 1;
    }

    // Generate IDL files (OMG IDL 4.2 format with advanced features)
    // Only process manually-written IDL files, not auto-generated ones
    // IDL files in msg/ directory (messages)
    for idl_name in &package.interfaces.idl_messages {
        let idl_path = package.get_idl_message_path(idl_name);
        let content = std::fs::read_to_string(&idl_path)
            .wrap_err_with(|| format!("Failed to read IDL file: {}", idl_path.display()))?;

        // Skip auto-generated IDL files (they're generated from .msg files)
        // Auto-generated files start with "// generated from rosidl_adapter"
        if content
            .trim_start()
            .starts_with("// generated from rosidl_adapter")
        {
            continue;
        }

        // Try to parse the IDL file - skip if parsing fails (might use unsupported IDL features)
        let parsed_idl = match rosidl_parser::idl::parse_idl_file(&content) {
            Ok(parsed) => parsed,
            Err(e) => {
                eprintln!(
                    "Warning: Skipping IDL file {} (parsing failed): {:?}",
                    idl_name, e
                );
                continue;
            }
        };

        // Generate code from IDL file (constants and enums use IDL-specific generation)
        let idl_deps = HashSet::new();
        let generated = generate_idl_file(&package.name, &parsed_idl, &idl_deps)
            .map_err(|e| eyre::eyre!("Failed to generate IDL code for {}: {}", idl_name, e))?;

        // Write IDL-specific artifacts (constants and enums)
        write_generated_idl(&generated, &package_output, idl_name)?;

        // Track constants and enums
        for (const_mod_name, _) in &generated.constant_modules {
            generated_idl_constant_modules.push(const_mod_name.clone());
        }
        for (enum_name, _) in &generated.enums {
            generated_idl_enums.push(enum_name.clone());
        }

        // Extract all structs from the IDL module to convert to Messages
        fn extract_structs(
            module: &rosidl_parser::idl::ast::IdlModule,
            structs: &mut Vec<rosidl_parser::idl::ast::IdlStruct>,
        ) {
            for struct_def in &module.structs {
                structs.push(struct_def.clone());
            }
            for nested_module in &module.modules {
                extract_structs(nested_module, structs);
            }
        }

        let mut idl_structs = Vec::new();
        extract_structs(&parsed_idl.module, &mut idl_structs);

        // Generate RMW + idiomatic code for each IDL struct (same as .msg files)
        for idl_struct in idl_structs {
            // Convert IDL struct to Message for RMW generation
            let message = idl_struct_to_message(&idl_struct, &package.name);

            // Check if this message needs big_array support
            if needs_big_array(&message) {
                package_needs_big_array = true;
            }

            // Generate both RMW and idiomatic layers (same as .msg files)
            let generated_msg = generate_message_package(
                &package.name,
                &idl_struct.name,
                &message,
                &known_packages,
            )
            .wrap_err_with(|| {
                format!(
                    "Failed to generate message from IDL struct: {}",
                    idl_struct.name
                )
            })?;

            // Write both RMW and idiomatic files
            write_generated_package(&generated_msg, &package_output, &idl_struct.name)?;
            message_count += 1;

            // Track this IDL struct as a regular message
            generated_idl_messages.push(idl_struct.name.clone());
        }
    }

    // IDL files in srv/ directory (services)
    for idl_name in &package.interfaces.idl_services {
        let idl_path = package.get_idl_service_path(idl_name);
        let content = std::fs::read_to_string(&idl_path)
            .wrap_err_with(|| format!("Failed to read IDL file: {}", idl_path.display()))?;

        // Skip auto-generated IDL files
        if content
            .trim_start()
            .starts_with("// generated from rosidl_adapter")
        {
            continue;
        }

        // Try to parse the IDL file - skip if parsing fails
        let parsed_idl = match rosidl_parser::idl::parse_idl_file(&content) {
            Ok(parsed) => parsed,
            Err(e) => {
                eprintln!(
                    "Warning: Skipping IDL file {} (parsing failed): {:?}",
                    idl_name, e
                );
                continue;
            }
        };

        let idl_deps = HashSet::new();
        let generated = generate_idl_file(&package.name, &parsed_idl, &idl_deps)
            .map_err(|e| eyre::eyre!("Failed to generate IDL code for {}: {}", idl_name, e))?;

        all_dependencies.extend(idl_deps);

        write_generated_idl(&generated, &package_output, idl_name)?;
        service_count += generated.structs.len();

        // Track successfully generated IDL service
        generated_idl_services.push(idl_name.clone());
    }

    // IDL files in action/ directory (actions)
    for idl_name in &package.interfaces.idl_actions {
        let idl_path = package.get_idl_action_path(idl_name);
        let content = std::fs::read_to_string(&idl_path)
            .wrap_err_with(|| format!("Failed to read IDL file: {}", idl_path.display()))?;

        // Skip auto-generated IDL files
        if content
            .trim_start()
            .starts_with("// generated from rosidl_adapter")
        {
            continue;
        }

        // Try to parse the IDL file - skip if parsing fails
        let parsed_idl = match rosidl_parser::idl::parse_idl_file(&content) {
            Ok(parsed) => parsed,
            Err(e) => {
                eprintln!(
                    "Warning: Skipping IDL file {} (parsing failed): {:?}",
                    idl_name, e
                );
                continue;
            }
        };

        let idl_deps = HashSet::new();
        let generated = generate_idl_file(&package.name, &parsed_idl, &idl_deps)
            .map_err(|e| eyre::eyre!("Failed to generate IDL code for {}: {}", idl_name, e))?;

        all_dependencies.extend(idl_deps);

        write_generated_idl(&generated, &package_output, idl_name)?;
        action_count += generated.structs.len();

        // Track successfully generated IDL action
        generated_idl_actions.push(idl_name.clone());
    }

    // Remove self-dependency (package shouldn't depend on itself)
    all_dependencies.remove(&package.name);

    // Generate lib.rs that re-exports all generated code
    let idl_artifacts = GeneratedIdlArtifacts {
        messages: generated_idl_messages,
        services: generated_idl_services,
        actions: generated_idl_actions,
        constant_modules: generated_idl_constant_modules,
        enums: generated_idl_enums,
    };
    generate_lib_rs(&package_output, package, &all_dependencies, &idl_artifacts)?;

    // Generate Cargo.toml for the package
    generate_cargo_toml(
        &package_output,
        &package.name,
        &package.version,
        &all_dependencies,
        package_needs_big_array,
        rosidl_runtime_rs_version,
    )?;

    // Generate build.rs for FFI linking
    generate_build_rs(&package_output, &package.name)?;

    Ok(GeneratedRustPackage {
        name: package.name.clone(),
        output_dir: package_output,
        message_count,
        service_count,
        action_count,
    })
}

/// Write generated message package to files
fn write_generated_package(
    generated: &GeneratedPackage,
    output_dir: &Path,
    name: &str,
) -> Result<()> {
    // Create src/msg/ subdirectory
    let msg_dir = output_dir.join("src").join("msg");
    std::fs::create_dir_all(&msg_dir)?;

    // Write RMW (FFI) message to src/msg/
    let rmw_file = msg_dir.join(format!("{}_rmw.rs", to_snake_case(name)));
    std::fs::write(&rmw_file, &generated.message_rmw)?;

    // Write idiomatic message to src/msg/
    let idiomatic_file = msg_dir.join(format!("{}_idiomatic.rs", to_snake_case(name)));
    std::fs::write(&idiomatic_file, &generated.message_idiomatic)?;

    Ok(())
}

/// Write generated service package to files
fn write_generated_service(
    generated: &rosidl_codegen::GeneratedServicePackage,
    output_dir: &Path,
    name: &str,
) -> Result<()> {
    // Create src/srv/ subdirectory
    let srv_dir = output_dir.join("src").join("srv");
    std::fs::create_dir_all(&srv_dir)?;

    // Write RMW (FFI) service to src/srv/
    let rmw_file = srv_dir.join(format!("{}_rmw.rs", to_snake_case(name)));
    std::fs::write(&rmw_file, &generated.service_rmw)?;

    // Write idiomatic service to src/srv/
    let idiomatic_file = srv_dir.join(format!("{}_idiomatic.rs", to_snake_case(name)));
    std::fs::write(&idiomatic_file, &generated.service_idiomatic)?;

    Ok(())
}

/// Write generated action package to files
fn write_generated_action(
    generated: &rosidl_codegen::GeneratedActionPackage,
    output_dir: &Path,
    name: &str,
) -> Result<()> {
    // Create src/action/ subdirectory
    let action_dir = output_dir.join("src").join("action");
    std::fs::create_dir_all(&action_dir)?;

    // Write RMW (FFI) action to src/action/
    let rmw_file = action_dir.join(format!("{}_rmw.rs", to_snake_case(name)));
    std::fs::write(&rmw_file, &generated.action_rmw)?;

    // Write idiomatic action to src/action/
    let idiomatic_file = action_dir.join(format!("{}_idiomatic.rs", to_snake_case(name)));
    std::fs::write(&idiomatic_file, &generated.action_idiomatic)?;

    Ok(())
}

/// Write generated IDL code to files
fn write_generated_idl(
    generated: &rosidl_codegen::GeneratedIdlCode,
    output_dir: &Path,
    _idl_file_name: &str,
) -> Result<()> {
    // Create src directory
    let src_dir = output_dir.join("src");
    std::fs::create_dir_all(&src_dir)?;

    // Write each struct (message) as an idiomatic-only file
    // IDL structs are idiomatic Rust code (no RMW layer needed)
    for (struct_name, struct_code) in &generated.structs {
        let idiomatic_file = src_dir.join(format!("{}_idiomatic.rs", to_snake_case(struct_name)));
        std::fs::write(&idiomatic_file, struct_code)?;
    }

    // Write each constant module
    for (module_name, module_code) in &generated.constant_modules {
        let module_file = src_dir.join(format!("{}_constants.rs", to_snake_case(module_name)));
        std::fs::write(&module_file, module_code)?;
    }

    // Write each enum
    for (enum_name, enum_code) in &generated.enums {
        let enum_file = src_dir.join(format!("{}_enum.rs", to_snake_case(enum_name)));
        std::fs::write(&enum_file, enum_code)?;
    }

    Ok(())
}

/// Generate lib.rs that re-exports all generated modules
fn generate_lib_rs(
    output_dir: &Path,
    package: &Package,
    _dependencies: &HashSet<String>,
    idl_artifacts: &GeneratedIdlArtifacts,
) -> Result<()> {
    let src_dir = output_dir.join("src");
    std::fs::create_dir_all(&src_dir)?;

    // Collect message info from .msg files and IDL structs
    let mut messages: Vec<InterfaceInfo> = package
        .interfaces
        .messages
        .iter()
        .map(|name| InterfaceInfo {
            type_name: name.clone(),
            module_name: to_snake_case(name),
        })
        .collect();

    // Add IDL struct messages (they have RMW layer now)
    messages.extend(idl_artifacts.messages.iter().map(|name| InterfaceInfo {
        type_name: name.clone(),
        module_name: to_snake_case(name),
    }));

    // Collect service info (both .srv and .idl services)
    let mut services: Vec<InterfaceInfo> = package
        .interfaces
        .services
        .iter()
        .map(|name| InterfaceInfo {
            type_name: name.clone(),
            module_name: to_snake_case(name),
        })
        .collect();

    // Add only successfully generated IDL services
    services.extend(idl_artifacts.services.iter().map(|name| InterfaceInfo {
        type_name: name.clone(),
        module_name: to_snake_case(name),
    }));

    // Collect action info (both .action and .idl actions)
    let mut actions: Vec<InterfaceInfo> = package
        .interfaces
        .actions
        .iter()
        .map(|name| InterfaceInfo {
            type_name: name.clone(),
            module_name: to_snake_case(name),
        })
        .collect();

    // Add only successfully generated IDL actions
    actions.extend(idl_artifacts.actions.iter().map(|name| InterfaceInfo {
        type_name: name.clone(),
        module_name: to_snake_case(name),
    }));

    // Collect constant modules from IDL files
    let constant_modules: Vec<InterfaceInfo> = idl_artifacts
        .constant_modules
        .iter()
        .map(|name| InterfaceInfo {
            type_name: name.clone(),
            module_name: to_snake_case(name),
        })
        .collect();

    // Collect enums from IDL files
    let enums: Vec<InterfaceInfo> = idl_artifacts
        .enums
        .iter()
        .map(|name| InterfaceInfo {
            type_name: name.clone(),
            module_name: to_snake_case(name),
        })
        .collect();

    // Render template
    let template = LibRsTemplate {
        package_name: package.name.clone(),
        has_messages: !messages.is_empty(),
        has_services: !services.is_empty(),
        has_actions: !actions.is_empty(),
    };

    let lib_rs = template.render()?;
    std::fs::write(src_dir.join("lib.rs"), lib_rs)?;

    // Generate msg/mod.rs if there are messages or IDL artifacts
    if !messages.is_empty() || !constant_modules.is_empty() || !enums.is_empty() {
        let msg_dir = src_dir.join("msg");
        std::fs::create_dir_all(&msg_dir)?;
        let msg_template = MsgRsTemplate {
            package_name: package.name.clone(),
            messages: messages.clone(),
            constant_modules,
            enums,
        };
        let msg_rs = msg_template.render()?;
        std::fs::write(msg_dir.join("mod.rs"), msg_rs)?;
    }

    // Generate srv/mod.rs if there are services
    if !services.is_empty() {
        let srv_dir = src_dir.join("srv");
        std::fs::create_dir_all(&srv_dir)?;
        let srv_template = SrvRsTemplate {
            package_name: package.name.clone(),
            services: services.clone(),
        };
        let srv_rs = srv_template.render()?;
        std::fs::write(srv_dir.join("mod.rs"), srv_rs)?;
    }

    // Generate action/mod.rs if there are actions
    if !actions.is_empty() {
        let action_dir = src_dir.join("action");
        std::fs::create_dir_all(&action_dir)?;
        let action_template = ActionRsTemplate {
            package_name: package.name.clone(),
            actions: actions.clone(),
        };
        let action_rs = action_template.render()?;
        std::fs::write(action_dir.join("mod.rs"), action_rs)?;
    }

    Ok(())
}

/// Generate Cargo.toml for the generated package
fn generate_cargo_toml(
    output_dir: &Path,
    package_name: &str,
    package_version: &str,
    dependencies: &HashSet<String>,
    needs_big_array: bool,
    rosidl_runtime_rs_version: Option<&str>,
) -> Result<()> {
    let version = rosidl_runtime_rs_version.unwrap_or(ROSIDL_RUNTIME_RS_VERSION);
    let mut cargo_toml = format!(
        r#"[package]
name = "{}"
version = "{}"
edition = "2021"

# The version above is deliberately fixed and is NOT the ROS package version,
# which is recorded under [package.metadata.ros] instead.
#
# These crates are reached through [patch.crates-io] path entries, so cargo
# never selects between versions here — but it does write whatever it finds
# into the consumer's Cargo.lock. Stamping the ROS package version made that
# lock a record of one machine's ROS installation: a workspace with a committed
# lock came back dirty after every build on any machine whose message packages
# differed, and two developers could not both keep it clean. Autoware is the
# case that shows it, where autoware_common_msgs is 1.3.0 in one distribution
# and 1.11.0 in another.
#
# A fixed version makes the lock reproducible across installations. The ROS
# version is not lost, only moved somewhere cargo does not propagate.
[package.metadata.ros]
package_version = "{}"

# Standalone package (not part of parent workspace)
[workspace]

[dependencies]
# Shared runtime library for ROS 2 types and traits (from crates.io)
rosidl_runtime_rs = "{}"
serde = {{ version = "1.0", features = ["derive"], optional = true }}
"#,
        package_name, GENERATED_CRATE_VERSION, package_version, version
    );

    // Add serde-big-array if needed for arrays > 32 elements
    if needs_big_array {
        cargo_toml.push_str("serde-big-array = { version = \"0.5\", optional = true }\n");
    }

    // Add cross-package dependencies
    for dep in dependencies {
        // Convert package name to valid crate name (replace - with _)
        let crate_name = dep.replace('-', "_");
        cargo_toml.push_str(&format!("{} = {{ path = \"../{}\" }}\n", crate_name, dep));
    }

    // Add features section.
    //
    // The serde feature has to propagate to the message crates this one depends
    // on. A std_msgs/Header holds a builtin_interfaces/Time, so deriving
    // Serialize for Header is only possible if builtin_interfaces was built with
    // its own serde feature on. Without the propagation a consumer asking for
    // `std_msgs = { version = "*", features = ["serde"] }` gets:
    //
    //   error[E0277]: the trait bound `builtin_interfaces::msg::rmw::Time:
    //                 serde::Deserialize<'de>` is not satisfied
    cargo_toml.push_str("\n[features]\ndefault = []\n");
    let mut serde_feature = vec!["\"dep:serde\"".to_string()];
    if needs_big_array {
        serde_feature.push("\"dep:serde-big-array\"".to_string());
    }
    serde_feature.push("\"rosidl_runtime_rs/serde\"".to_string());
    let mut sorted_deps: Vec<&String> = dependencies.iter().collect();
    sorted_deps.sort();
    for dep in sorted_deps {
        let crate_name = dep.replace('-', "_");
        serde_feature.push(format!("\"{crate_name}/serde\""));
    }
    cargo_toml.push_str(&format!("serde = [{}]\n", serde_feature.join(", ")));

    cargo_toml.push_str(
        r#"
[build-dependencies]
# For linking against ROS 2 C libraries
"#,
    );

    std::fs::write(output_dir.join("Cargo.toml"), cargo_toml)?;
    Ok(())
}

/// Generate build.rs for linking against ROS 2 C libraries
fn generate_build_rs(output_dir: &Path, package_name: &str) -> Result<()> {
    let build_rs = format!(
        r#"fn main() {{
    // Refuse to build against bindings whose definitions have moved on
    check_bindings_fresh();

    // Add ROS library search paths from AMENT_PREFIX_PATH (for system packages)
    if let Ok(ament_prefix_path) = std::env::var("AMENT_PREFIX_PATH") {{
        for prefix in ament_prefix_path.split(':') {{
            let lib_path = std::path::Path::new(prefix).join("lib");
            if lib_path.exists() {{
                println!("cargo:rustc-link-search=native={{}}", lib_path.display());
            }}
        }}
    }}

    // Also search for workspace-local install directory (for custom packages)
    // This is critical for colcon workspaces where packages are built incrementally
    if let Ok(manifest_dir) = std::env::var("CARGO_MANIFEST_DIR") {{
        let mut search_dir = std::path::Path::new(&manifest_dir);

        // Walk up the directory tree to find workspace root
        for _ in 0..10 {{
            // Check if this looks like a colcon workspace root
            let install_dir = search_dir.join("install");
            if install_dir.exists() && install_dir.is_dir() {{
                // Add all package lib directories from install/
                if let Ok(entries) = std::fs::read_dir(&install_dir) {{
                    for entry in entries.flatten() {{
                        let lib_path = entry.path().join("lib");
                        if lib_path.exists() {{
                            println!("cargo:rustc-link-search=native={{}}", lib_path.display());
                        }}
                    }}
                }}
                break;
            }}

            // Move up one directory
            if let Some(parent) = search_dir.parent() {{
                search_dir = parent;
            }} else {{
                break;
            }}
        }}
    }}

    // Link against ROS 2 C libraries
    println!("cargo:rustc-link-lib={package}__rosidl_typesupport_c");
    println!("cargo:rustc-link-lib={package}__rosidl_generator_c");
}}

/// Fail the build when these bindings no longer match the .msg/.srv/.action
/// files they were generated from.
///
/// `colcon build` regenerates on change, but a plain `cargo build` never
/// consults its records, so a stale crate compiles happily and the mismatch
/// surfaces much later as an error inside *consumer* code ("no field `pid` on
/// type `ComponentEvent`") that names nothing responsible.
///
/// `.bindgen_manifest` is written by colcon-cargo-ros2 next to this build
/// script: first line the interface directory, then one `path:size:mtime_ns`
/// record per definition. No manifest, no readable source directory, or
/// COLCON_CARGO_ROS2_SKIP_STAMP_CHECK set means the check is skipped rather
/// than failed -- it must never block a build it cannot judge.
fn check_bindings_fresh() {{
    if std::env::var_os("COLCON_CARGO_ROS2_SKIP_STAMP_CHECK").is_some() {{
        return;
    }}

    let Ok(manifest_dir) = std::env::var("CARGO_MANIFEST_DIR") else {{
        return;
    }};
    let manifest_path = std::path::Path::new(&manifest_dir).join(".bindgen_manifest");
    println!("cargo:rerun-if-changed={{}}", manifest_path.display());

    let Ok(content) = std::fs::read_to_string(&manifest_path) else {{
        return;
    }};
    let mut lines = content.lines();
    let Some(source_dir) = lines.next().filter(|line| !line.is_empty()) else {{
        return;
    }};
    let source_dir = std::path::Path::new(source_dir);
    if !source_dir.is_dir() {{
        return;
    }}

    let recorded: std::collections::BTreeSet<String> =
        lines.filter(|line| !line.is_empty()).map(str::to_string).collect();

    let mut current = std::collections::BTreeSet::new();
    for subdir in ["msg", "srv", "action"] {{
        let root = source_dir.join(subdir);
        if root.is_dir() {{
            println!("cargo:rerun-if-changed={{}}", root.display());
            collect_interface_records(&root, source_dir, &mut current);
        }}
    }}

    if current != recorded {{
        panic!(
            "Rust bindings for `{package}` are out of date: the interface \
             definitions in {{}} have changed since they were generated.\n\
             Re-run `colcon build` to regenerate them, or set \
             COLCON_CARGO_ROS2_SKIP_STAMP_CHECK=1 to build anyway.",
            source_dir.display()
        );
    }}
}}

/// Record every definition file under `dir` as `relative/path:size:mtime_ns`.
fn collect_interface_records(
    dir: &std::path::Path,
    base: &std::path::Path,
    out: &mut std::collections::BTreeSet<String>,
) {{
    let Ok(entries) = std::fs::read_dir(dir) else {{
        return;
    }};
    for entry in entries.flatten() {{
        let path = entry.path();
        if path.is_dir() {{
            collect_interface_records(&path, base, out);
            continue;
        }}
        let is_definition = matches!(
            path.extension().and_then(|ext| ext.to_str()),
            Some("msg") | Some("srv") | Some("action") | Some("idl")
        );
        if !is_definition {{
            continue;
        }}
        let (Ok(metadata), Ok(relative)) = (entry.metadata(), path.strip_prefix(base)) else {{
            continue;
        }};
        let Ok(contents) = std::fs::read(&path) else {{
            continue;
        }};
        out.insert(format!(
            "{{}}:{{}}:{{}}",
            relative.display(),
            metadata.len(),
            fnv1a64(&contents)
        ));
    }}
}}

/// FNV-1a, 64-bit, as 16 hex digits.
///
/// Freshness is keyed on what the definitions *say*, not when they were touched:
/// a checkout or a copy rewrites mtimes without changing a byte. This crate has
/// no dependencies and must not gain any, so the hash is spelled out here and in
/// the generator that writes the manifest -- the two have to agree exactly.
fn fnv1a64(data: &[u8]) -> String {{
    let mut digest: u64 = 0xcbf2_9ce4_8422_2325;
    for byte in data {{
        digest ^= *byte as u64;
        digest = digest.wrapping_mul(0x0000_0100_0000_01b3);
    }}
    format!("{{digest:016x}}")
}}
"#,
        package = package_name
    );

    std::fs::write(output_dir.join("build.rs"), build_rs)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ament::Package;
    use std::fs;

    /// Helper to create a test package with interface files
    fn create_test_package(temp_dir: &Path) -> Package {
        let share_dir = temp_dir.join("test_pkg");

        // Create msg files
        let msg_dir = share_dir.join("msg");
        fs::create_dir_all(&msg_dir).unwrap();
        fs::write(msg_dir.join("Point.msg"), "float64 x\nfloat64 y\n").unwrap();

        // Create srv files
        let srv_dir = share_dir.join("srv");
        fs::create_dir_all(&srv_dir).unwrap();
        fs::write(
            srv_dir.join("AddTwoInts.srv"),
            "int64 a\nint64 b\n---\nint64 sum\n",
        )
        .unwrap();

        // Create action files
        let action_dir = share_dir.join("action");
        fs::create_dir_all(&action_dir).unwrap();
        fs::write(
            action_dir.join("Fibonacci.action"),
            "int32 order\n---\nint32[] sequence\n---\nint32[] partial_sequence\n",
        )
        .unwrap();

        Package::from_share_dir(share_dir).unwrap()
    }

    #[test]
    fn test_generate_message() {
        let temp_dir = tempfile::tempdir().unwrap();
        let package = create_test_package(temp_dir.path());
        let output_dir = temp_dir.path().join("output");

        let result = generate_package(&package, &output_dir, None);
        assert!(result.is_ok());

        let generated = result.unwrap();
        assert_eq!(generated.message_count, 1);
        assert_eq!(generated.service_count, 1);
        assert_eq!(generated.action_count, 1);

        // Check that files were created
        let pkg_dir = output_dir.join("test_pkg");
        assert!(pkg_dir.join("Cargo.toml").exists());
        assert!(pkg_dir.join("build.rs").exists());
        assert!(pkg_dir.join("src").join("lib.rs").exists());
    }

    #[test]
    fn test_generate_lib_rs_structure() {
        let temp_dir = tempfile::tempdir().unwrap();
        let package = create_test_package(temp_dir.path());
        let output_dir = temp_dir.path().join("output");
        std::fs::create_dir_all(&output_dir).unwrap();

        let deps = HashSet::new();
        let idl_artifacts = GeneratedIdlArtifacts::default();
        generate_lib_rs(&output_dir, &package, &deps, &idl_artifacts).unwrap();

        let lib_rs_content =
            std::fs::read_to_string(output_dir.join("src").join("lib.rs")).unwrap();
        assert!(lib_rs_content.contains("pub mod msg"));
        assert!(lib_rs_content.contains("pub mod srv"));
        assert!(lib_rs_content.contains("pub mod action"));
    }

    #[test]
    fn test_cargo_toml_generation() {
        let temp_dir = tempfile::tempdir().unwrap();
        let deps = HashSet::new();
        generate_cargo_toml(temp_dir.path(), "test_pkg", "0.1.0", &deps, false, None).unwrap();

        let cargo_toml = std::fs::read_to_string(temp_dir.path().join("Cargo.toml")).unwrap();
        assert!(cargo_toml.contains("name = \"test_pkg\""));
        assert!(cargo_toml.contains(&format!("version = \"{GENERATED_CRATE_VERSION}\"")));
        assert!(cargo_toml.contains("package_version = \"0.1.0\""));
        assert!(cargo_toml.contains(&format!(
            "rosidl_runtime_rs = \"{ROSIDL_RUNTIME_RS_VERSION}\""
        )));
        assert!(cargo_toml.contains("serde"));
        assert!(!cargo_toml.contains("serde-big-array"));
    }

    /// The fallback is a single version, not a range: cargo resolves a range
    /// spanning 0.5 and 0.6 to the greatest match rather than unifying with the
    /// rest of the graph, which is what produced two copies of the runtime and
    /// a `Message` trait that did not match itself.
    #[test]
    fn test_runtime_fallback_is_a_single_version() {
        let req = semver::VersionReq::parse(ROSIDL_RUNTIME_RS_VERSION)
            .expect("fallback should parse as a semver requirement");
        assert!(req.matches(&semver::Version::parse("0.6.0").unwrap()));
        assert!(
            !req.matches(&semver::Version::parse("0.5.0").unwrap()),
            "a fallback matching both majors would let cargo pick either"
        );
    }

    /// An explicit override still pins, for a workspace that needs one exact
    /// version.
    #[test]
    fn test_runtime_version_override_is_exact() {
        let temp_dir = tempfile::tempdir().unwrap();
        let deps = HashSet::new();
        generate_cargo_toml(
            temp_dir.path(),
            "test_pkg",
            "0.1.0",
            &deps,
            false,
            Some("0.5"),
        )
        .unwrap();

        let cargo_toml = std::fs::read_to_string(temp_dir.path().join("Cargo.toml")).unwrap();
        assert!(cargo_toml.contains("rosidl_runtime_rs = \"0.5\""));
    }

    /// A message crate's serde feature has to turn on serde in the message
    /// crates it depends on, or a nested type from another package fails to
    /// derive. Found by building eclipse-iceoryx/iceoryx2's ROS demo, which asks
    /// for `std_msgs = { version = "*", features = ["serde"] }`.
    #[test]
    fn test_serde_feature_propagates_to_dependencies() {
        let temp_dir = tempfile::tempdir().unwrap();
        let mut deps = HashSet::new();
        deps.insert("builtin_interfaces".to_string());
        deps.insert("geometry_msgs".to_string());

        generate_cargo_toml(temp_dir.path(), "std_msgs", "1.0.0", &deps, false, None).unwrap();

        let cargo_toml = std::fs::read_to_string(temp_dir.path().join("Cargo.toml")).unwrap();
        let serde_line = cargo_toml
            .lines()
            .find(|line| line.starts_with("serde = ["))
            .expect("no serde feature");
        assert!(
            serde_line.contains("\"builtin_interfaces/serde\""),
            "{serde_line}"
        );
        assert!(
            serde_line.contains("\"geometry_msgs/serde\""),
            "{serde_line}"
        );
        assert!(
            serde_line.contains("\"rosidl_runtime_rs/serde\""),
            "{serde_line}"
        );
    }

    #[test]
    fn test_cargo_toml_with_dependencies() {
        let temp_dir = tempfile::tempdir().unwrap();
        let mut deps = HashSet::new();
        deps.insert("std_msgs".to_string());
        deps.insert("geometry_msgs".to_string());

        generate_cargo_toml(temp_dir.path(), "test_pkg", "1.2.3", &deps, false, None).unwrap();

        let cargo_toml = std::fs::read_to_string(temp_dir.path().join("Cargo.toml")).unwrap();
        assert!(cargo_toml.contains("name = \"test_pkg\""));
        assert!(cargo_toml.contains("version = \"1.2.3\""));
        assert!(cargo_toml.contains("serde"));
        assert!(cargo_toml.contains("std_msgs = { path = \"../std_msgs\" }"));
        assert!(cargo_toml.contains("geometry_msgs = { path = \"../geometry_msgs\" }"));
    }

    #[test]
    fn test_cargo_toml_with_big_array() {
        let temp_dir = tempfile::tempdir().unwrap();
        let deps = HashSet::new();
        generate_cargo_toml(temp_dir.path(), "test_pkg", "2.0.0", &deps, true, None).unwrap();

        let cargo_toml = std::fs::read_to_string(temp_dir.path().join("Cargo.toml")).unwrap();
        assert!(cargo_toml.contains("name = \"test_pkg\""));
        assert!(cargo_toml.contains("version = \"2.0.0\""));
        assert!(cargo_toml.contains("serde"));
        assert!(cargo_toml.contains("serde-big-array"));
    }

    #[test]
    fn test_build_rs_generation() {
        let temp_dir = tempfile::tempdir().unwrap();
        generate_build_rs(temp_dir.path(), "test_pkg").unwrap();

        let build_rs = std::fs::read_to_string(temp_dir.path().join("build.rs")).unwrap();
        assert!(build_rs.contains("test_pkg__rosidl_typesupport_c"));
        assert!(build_rs.contains("test_pkg__rosidl_generator_c"));
    }

    #[test]
    fn test_build_rs_checks_binding_freshness() {
        let temp_dir = tempfile::tempdir().unwrap();
        generate_build_rs(temp_dir.path(), "test_pkg").unwrap();

        let build_rs = std::fs::read_to_string(temp_dir.path().join("build.rs")).unwrap();
        assert!(build_rs.contains("check_bindings_fresh()"));
        assert!(build_rs.contains(".bindgen_manifest"));
        assert!(build_rs.contains("COLCON_CARGO_ROS2_SKIP_STAMP_CHECK"));
        // The panic must name the package and the way out.
        assert!(build_rs.contains("bindings for `test_pkg` are out of date"));
        assert!(build_rs.contains("Re-run `colcon build`"));
    }

    /// The generated build script has to compile, and no test above would notice
    /// a syntax error in it -- the wheel ships it as text.
    #[test]
    fn test_build_rs_compiles() {
        let temp_dir = tempfile::tempdir().unwrap();
        generate_build_rs(temp_dir.path(), "test_pkg").unwrap();

        let output = std::process::Command::new("rustc")
            .arg("--edition")
            .arg("2021")
            .arg("--emit=metadata")
            .arg("--crate-type")
            .arg("bin")
            .arg("-o")
            .arg(temp_dir.path().join("build_rs_check"))
            .arg(temp_dir.path().join("build.rs"))
            .output();

        let Ok(output) = output else {
            // No rustc on PATH (unlikely here, but not worth failing over).
            return;
        };
        assert!(
            output.status.success(),
            "generated build.rs does not compile:\n{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }

    #[test]
    fn test_invalid_message_file() {
        let temp_dir = tempfile::tempdir().unwrap();
        let share_dir = temp_dir.path().join("bad_pkg");
        let msg_dir = share_dir.join("msg");
        fs::create_dir_all(&msg_dir).unwrap();
        fs::write(msg_dir.join("Bad.msg"), "invalid syntax here!!! @#$%\n").unwrap();

        let package = Package::from_share_dir(share_dir).unwrap();
        let output_dir = temp_dir.path().join("output");

        let result = generate_package(&package, &output_dir, None);
        assert!(result.is_err());
    }

    /// The ROS package version must not become the crate version.
    ///
    /// These crates are patched in by path, so the version never affects
    /// resolution — but cargo copies it into the consumer's `Cargo.lock`, and a
    /// version that tracks the installed ROS distribution makes that lock
    /// machine-specific. A workspace committing its lock then came back dirty
    /// after every build on a machine whose message packages differed.
    #[test]
    fn test_generated_cargo_toml_keeps_ros_version_out_of_the_crate_version() {
        let temp_dir = tempfile::tempdir().unwrap();
        let share_dir = temp_dir.path().join("versioned_msgs");

        // Create message file
        let msg_dir = share_dir.join("msg");
        fs::create_dir_all(&msg_dir).unwrap();
        fs::write(msg_dir.join("Point.msg"), "float64 x\nfloat64 y\n").unwrap();

        // Create package.xml with specific version
        let package_xml = r#"<?xml version="1.0"?>
<package format="3">
  <name>versioned_msgs</name>
  <version>4.5.6</version>
  <description>Test messages with version</description>
</package>
"#;
        fs::write(share_dir.join("package.xml"), package_xml).unwrap();

        let package = Package::from_share_dir(share_dir).unwrap();
        let output_dir = temp_dir.path().join("output");

        let result = generate_package(&package, &output_dir, None);
        assert!(result.is_ok());

        // Check that generated Cargo.toml has correct version
        let cargo_toml_path = output_dir.join("versioned_msgs").join("Cargo.toml");
        let cargo_toml = fs::read_to_string(cargo_toml_path).unwrap();
        assert!(cargo_toml.contains("name = \"versioned_msgs\""));
        // Fixed crate version, so the consumer's lock does not record which ROS
        // installation generated the bindings.
        assert!(cargo_toml.contains(&format!("version = \"{GENERATED_CRATE_VERSION}\"")));
        // The ROS version is kept, somewhere cargo does not propagate.
        assert!(cargo_toml.contains("[package.metadata.ros]"));
        assert!(cargo_toml.contains("package_version = \"4.5.6\""));
        // Should NOT contain the hardcoded ROSIDL_RUNTIME_RS_VERSION as the package version
        assert!(!cargo_toml.contains("version = \"0.6.0\""));
        // The rosidl_runtime_rs requirement is the default range, not the
        // package version.
        assert!(cargo_toml.contains(&format!(
            "rosidl_runtime_rs = \"{ROSIDL_RUNTIME_RS_VERSION}\""
        )));
    }

    #[test]
    fn test_generated_cargo_toml_defaults_to_0_0_0_without_package_xml() {
        let temp_dir = tempfile::tempdir().unwrap();
        let share_dir = temp_dir.path().join("no_version_msgs");

        // Create message file without package.xml
        let msg_dir = share_dir.join("msg");
        fs::create_dir_all(&msg_dir).unwrap();
        fs::write(msg_dir.join("Point.msg"), "float64 x\nfloat64 y\n").unwrap();

        let package = Package::from_share_dir(share_dir).unwrap();
        let output_dir = temp_dir.path().join("output");

        let result = generate_package(&package, &output_dir, None);
        assert!(result.is_ok());

        // Check that generated Cargo.toml defaults to 0.0.0
        let cargo_toml_path = output_dir.join("no_version_msgs").join("Cargo.toml");
        let cargo_toml = fs::read_to_string(cargo_toml_path).unwrap();
        assert!(cargo_toml.contains("name = \"no_version_msgs\""));
        assert!(cargo_toml.contains(&format!("version = \"{GENERATED_CRATE_VERSION}\"")));
        // The crate version is fixed either way, so the fallback has to be
        // asserted where it is actually visible: the recorded ROS version.
        assert!(cargo_toml.contains("package_version = \"0.0.0\""));
    }
}
