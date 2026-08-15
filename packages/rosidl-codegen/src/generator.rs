use crate::templates::{
    ActionIdiomaticTemplate, ActionRmwTemplate, BuildRsTemplate, CargoTomlTemplate, FieldKind,
    IdiomaticField, LibRsTemplate, MessageConstant, MessageIdiomaticTemplate, MessageRmwTemplate,
    RmwField, ServiceIdiomaticTemplate, ServiceRmwTemplate,
};
use crate::types::{
    constant_value_to_rust, escape_keyword, rust_type_for_constant, rust_type_for_field,
};
use crate::utils::{extract_dependencies, needs_big_array, to_snake_case};
use askama::Template;
use rosidl_parser::{Action, FieldType, Message, Service};
use std::collections::HashSet;
use thiserror::Error;

#[derive(Error, Debug)]
pub enum GeneratorError {
    #[error("Template rendering failed: {0}")]
    TemplateError(#[from] askama::Error),

    #[error("Invalid message structure: {0}")]
    InvalidMessage(String),
}

pub struct GeneratedPackage {
    pub cargo_toml: String,
    pub build_rs: String,
    pub lib_rs: String,
    pub message_rmw: String,
    pub message_idiomatic: String,
}

/// Determine the exhaustive FieldKind enum variant for a given ROS 2 field type
/// This function provides compile-time guarantees that all field type combinations are handled
fn determine_field_kind(field_type: &FieldType) -> FieldKind {
    match field_type {
        // Scalar types
        FieldType::Primitive(_) => FieldKind::Primitive,

        FieldType::String => FieldKind::UnboundedString,
        FieldType::BoundedString(_) => FieldKind::BoundedString,

        FieldType::WString => FieldKind::UnboundedWString,
        FieldType::BoundedWString(_) => FieldKind::BoundedWString,

        FieldType::NamespacedType { .. } => FieldKind::NestedMessage,

        // Array types
        FieldType::Array { element_type, size } => {
            // Arrays > 32 elements don't impl Copy/Clone in Rust
            if *size > 32 {
                return FieldKind::LargeArray;
            }

            match element_type.as_ref() {
                FieldType::Primitive(_) => FieldKind::PrimitiveArray,

                FieldType::String => FieldKind::UnboundedStringArray,
                FieldType::BoundedString(_) => FieldKind::BoundedStringArray,

                FieldType::WString => FieldKind::UnboundedWStringArray,
                FieldType::BoundedWString(_) => FieldKind::BoundedWStringArray,

                _ => FieldKind::NestedMessageArray,
            }
        }

        // Bounded sequences (T[<=N])
        FieldType::BoundedSequence { element_type, .. } => match element_type.as_ref() {
            FieldType::Primitive(_) => FieldKind::BoundedPrimitiveSequence,

            FieldType::String => FieldKind::BoundedUnboundedStringSequence,
            FieldType::BoundedString(_) => FieldKind::BoundedBoundedStringSequence,

            FieldType::WString => FieldKind::BoundedUnboundedWStringSequence,
            FieldType::BoundedWString(_) => FieldKind::BoundedBoundedWStringSequence,

            _ => FieldKind::BoundedNestedMessageSequence,
        },

        // Unbounded sequences (T[])
        FieldType::Sequence { element_type } => match element_type.as_ref() {
            FieldType::Primitive(_) => FieldKind::UnboundedPrimitiveSequence,

            FieldType::String => FieldKind::UnboundedUnboundedStringSequence,
            FieldType::BoundedString(_) => FieldKind::UnboundedBoundedStringSequence,

            FieldType::WString => FieldKind::UnboundedUnboundedWStringSequence,
            FieldType::BoundedWString(_) => FieldKind::UnboundedBoundedWStringSequence,

            _ => FieldKind::UnboundedNestedMessageSequence,
        },
    }
}

/// Generate a complete ROS 2 message package with both RMW and idiomatic layers
pub fn generate_message_package(
    package_name: &str,
    message_name: &str,
    message: &Message,
    all_dependencies: &HashSet<String>,
) -> Result<GeneratedPackage, GeneratorError> {
    // Extract dependencies from this specific message
    let msg_deps = extract_dependencies(message);

    // Combine with externally provided dependencies
    let mut all_deps: Vec<String> = all_dependencies.iter().cloned().collect();
    all_deps.extend(msg_deps);
    all_deps.sort();
    all_deps.dedup();

    // Check if we need serde's big-array feature
    let needs_big_array_feature = needs_big_array(message);

    // Generate Cargo.toml
    let cargo_toml_template = CargoTomlTemplate {
        package_name,
        dependencies: &all_deps,
        needs_big_array: needs_big_array_feature,
    };
    let cargo_toml = cargo_toml_template.render()?;

    // Generate build.rs
    let build_rs_template = BuildRsTemplate;
    let build_rs = build_rs_template.render()?;

    // Generate lib.rs
    let lib_rs_template = LibRsTemplate {
        has_messages: true,
        has_services: false,
        has_actions: false,
    };
    let lib_rs = lib_rs_template.render()?;

    // Generate RMW layer message
    let rmw_fields: Vec<RmwField> = message
        .fields
        .iter()
        .map(|f| RmwField {
            name: escape_keyword(&f.name),
            rust_type: rust_type_for_field(&f.field_type, true, Some(package_name)),
            default_value: f
                .default_value
                .as_ref()
                .map(constant_value_to_rust)
                .unwrap_or_default(),
        })
        .collect();

    let rmw_constants: Vec<MessageConstant> = message
        .constants
        .iter()
        .map(|c| MessageConstant {
            name: c.name.clone(),
            rust_type: rust_type_for_constant(&c.constant_type),
            value: constant_value_to_rust(&c.value),
        })
        .collect();

    let message_module = &to_snake_case(message_name);

    let message_rmw_template = MessageRmwTemplate {
        package_name,
        message_name,
        message_module,
        fields: rmw_fields,
        constants: rmw_constants,
    };
    let message_rmw = message_rmw_template.render()?;

    // Generate idiomatic layer message
    let idiomatic_fields: Vec<IdiomaticField> = message
        .fields
        .iter()
        .map(|f| IdiomaticField {
            name: escape_keyword(&f.name),
            rust_type: rust_type_for_field(&f.field_type, false, Some(package_name)),
            default_value: f
                .default_value
                .as_ref()
                .map(constant_value_to_rust)
                .unwrap_or_default(),
            kind: determine_field_kind(&f.field_type),
        })
        .collect();

    let idiomatic_constants: Vec<MessageConstant> = message
        .constants
        .iter()
        .map(|c| MessageConstant {
            name: c.name.clone(),
            rust_type: rust_type_for_constant(&c.constant_type),
            value: constant_value_to_rust(&c.value),
        })
        .collect();

    let message_idiomatic_template = MessageIdiomaticTemplate {
        package_name,
        message_name,
        message_module,
        fields: idiomatic_fields,
        constants: idiomatic_constants,
    };
    let message_idiomatic = message_idiomatic_template.render()?;

    Ok(GeneratedPackage {
        cargo_toml,
        build_rs,
        lib_rs,
        message_rmw,
        message_idiomatic,
    })
}

pub struct GeneratedServicePackage {
    pub cargo_toml: String,
    pub build_rs: String,
    pub lib_rs: String,
    pub service_rmw: String,
    pub service_idiomatic: String,
}

/// Generate a complete ROS 2 service package with both RMW and idiomatic layers
pub fn generate_service_package(
    package_name: &str,
    service_name: &str,
    service: &Service,
    all_dependencies: &HashSet<String>,
) -> Result<GeneratedServicePackage, GeneratorError> {
    // Extract dependencies from request and response
    let mut req_deps = extract_dependencies(&service.request);
    let resp_deps = extract_dependencies(&service.response);
    req_deps.extend(resp_deps);

    // Combine with externally provided dependencies
    let mut all_deps: Vec<String> = all_dependencies.iter().cloned().collect();
    all_deps.extend(req_deps);
    all_deps.sort();
    all_deps.dedup();

    // Check if we need serde's big-array feature
    let needs_big_array_feature =
        needs_big_array(&service.request) || needs_big_array(&service.response);

    // Generate Cargo.toml
    let cargo_toml_template = CargoTomlTemplate {
        package_name,
        dependencies: &all_deps,
        needs_big_array: needs_big_array_feature,
    };
    let cargo_toml = cargo_toml_template.render()?;

    // Generate build.rs
    let build_rs_template = BuildRsTemplate;
    let build_rs = build_rs_template.render()?;

    // Generate lib.rs
    let lib_rs_template = LibRsTemplate {
        has_messages: false,
        has_services: true,
        has_actions: false,
    };
    let lib_rs = lib_rs_template.render()?;

    // Helper functions to convert Message to field vectors
    let message_to_rmw_fields = |msg: &Message| {
        msg.fields
            .iter()
            .map(|f| RmwField {
                name: escape_keyword(&f.name),
                rust_type: rust_type_for_field(&f.field_type, true, Some(package_name)),
                default_value: f
                    .default_value
                    .as_ref()
                    .map(constant_value_to_rust)
                    .unwrap_or_default(),
            })
            .collect()
    };

    let message_to_idiomatic_fields = |msg: &Message| {
        msg.fields
            .iter()
            .map(|f| IdiomaticField {
                name: escape_keyword(&f.name),
                rust_type: rust_type_for_field(&f.field_type, false, Some(package_name)),
                default_value: f
                    .default_value
                    .as_ref()
                    .map(constant_value_to_rust)
                    .unwrap_or_default(),
                kind: determine_field_kind(&f.field_type),
            })
            .collect()
    };

    let message_to_constants = |msg: &Message, _rmw_layer: bool| {
        msg.constants
            .iter()
            .map(|c| MessageConstant {
                name: c.name.clone(),
                rust_type: rust_type_for_constant(&c.constant_type),
                value: constant_value_to_rust(&c.value),
            })
            .collect()
    };

    // Generate RMW layer service
    let service_rmw_template = ServiceRmwTemplate {
        package_name,
        service_name,
        request_fields: message_to_rmw_fields(&service.request),
        request_constants: message_to_constants(&service.request, true),
        response_fields: message_to_rmw_fields(&service.response),
        response_constants: message_to_constants(&service.response, true),
    };
    let service_rmw = service_rmw_template.render()?;

    // Generate idiomatic layer service
    let service_idiomatic_template = ServiceIdiomaticTemplate {
        package_name,
        service_name,
        request_fields: message_to_idiomatic_fields(&service.request),
        request_constants: message_to_constants(&service.request, false),
        response_fields: message_to_idiomatic_fields(&service.response),
        response_constants: message_to_constants(&service.response, false),
    };
    let service_idiomatic = service_idiomatic_template.render()?;

    Ok(GeneratedServicePackage {
        cargo_toml,
        build_rs,
        lib_rs,
        service_rmw,
        service_idiomatic,
    })
}

pub struct GeneratedActionPackage {
    pub cargo_toml: String,
    pub build_rs: String,
    pub lib_rs: String,
    pub action_rmw: String,
    pub action_idiomatic: String,
}

/// Generate a complete ROS 2 action package with both RMW and idiomatic layers
pub fn generate_action_package(
    package_name: &str,
    action_name: &str,
    action: &Action,
    all_dependencies: &HashSet<String>,
) -> Result<GeneratedActionPackage, GeneratorError> {
    // Extract dependencies from goal, result, and feedback
    let mut goal_deps = extract_dependencies(&action.spec.goal);
    let result_deps = extract_dependencies(&action.spec.result);
    let feedback_deps = extract_dependencies(&action.spec.feedback);
    goal_deps.extend(result_deps);
    goal_deps.extend(feedback_deps);

    // Combine with externally provided dependencies
    let mut all_deps: Vec<String> = all_dependencies.iter().cloned().collect();
    all_deps.extend(goal_deps);
    all_deps.sort();
    all_deps.dedup();

    // Check if we need serde's big-array feature
    let needs_big_array_feature = needs_big_array(&action.spec.goal)
        || needs_big_array(&action.spec.result)
        || needs_big_array(&action.spec.feedback);

    // Generate Cargo.toml
    let cargo_toml_template = CargoTomlTemplate {
        package_name,
        dependencies: &all_deps,
        needs_big_array: needs_big_array_feature,
    };
    let cargo_toml = cargo_toml_template.render()?;

    // Generate build.rs
    let build_rs_template = BuildRsTemplate;
    let build_rs = build_rs_template.render()?;

    // Generate lib.rs
    let lib_rs_template = LibRsTemplate {
        has_messages: false,
        has_services: false,
        has_actions: true,
    };
    let lib_rs = lib_rs_template.render()?;

    // Helper functions to convert Message to field vectors
    let message_to_rmw_fields = |msg: &Message| {
        msg.fields
            .iter()
            .map(|f| RmwField {
                name: escape_keyword(&f.name),
                rust_type: rust_type_for_field(&f.field_type, true, Some(package_name)),
                default_value: f
                    .default_value
                    .as_ref()
                    .map(constant_value_to_rust)
                    .unwrap_or_default(),
            })
            .collect()
    };

    let message_to_idiomatic_fields = |msg: &Message| {
        msg.fields
            .iter()
            .map(|f| IdiomaticField {
                name: escape_keyword(&f.name),
                rust_type: rust_type_for_field(&f.field_type, false, Some(package_name)),
                default_value: f
                    .default_value
                    .as_ref()
                    .map(constant_value_to_rust)
                    .unwrap_or_default(),
                kind: determine_field_kind(&f.field_type),
            })
            .collect()
    };

    let message_to_constants = |msg: &Message, _rmw_layer: bool| {
        msg.constants
            .iter()
            .map(|c| MessageConstant {
                name: c.name.clone(),
                rust_type: rust_type_for_constant(&c.constant_type),
                value: constant_value_to_rust(&c.value),
            })
            .collect()
    };

    // Generate RMW layer action
    let action_rmw_template = ActionRmwTemplate {
        package_name,
        action_name,
        goal_fields: message_to_rmw_fields(&action.spec.goal),
        goal_constants: message_to_constants(&action.spec.goal, true),
        result_fields: message_to_rmw_fields(&action.spec.result),
        result_constants: message_to_constants(&action.spec.result, true),
        feedback_fields: message_to_rmw_fields(&action.spec.feedback),
        feedback_constants: message_to_constants(&action.spec.feedback, true),
    };
    let action_rmw = action_rmw_template.render()?;

    // Generate idiomatic layer action
    let action_idiomatic_template = ActionIdiomaticTemplate {
        package_name,
        action_name,
        goal_fields: message_to_idiomatic_fields(&action.spec.goal),
        goal_constants: message_to_constants(&action.spec.goal, false),
        result_fields: message_to_idiomatic_fields(&action.spec.result),
        result_constants: message_to_constants(&action.spec.result, false),
        feedback_fields: message_to_idiomatic_fields(&action.spec.feedback),
        feedback_constants: message_to_constants(&action.spec.feedback, false),
    };
    let action_idiomatic = action_idiomatic_template.render()?;

    Ok(GeneratedActionPackage {
        cargo_toml,
        build_rs,
        lib_rs,
        action_rmw,
        action_idiomatic,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use rosidl_parser::{
        Field, FieldType, PrimitiveType, parse_action, parse_message, parse_service,
    };

    #[test]
    fn test_simple_message_generation() {
        let msg = parse_message("int32 x\nfloat64 y\n").unwrap();
        let deps = HashSet::new();

        let result = generate_message_package("test_msgs", "Point", &msg, &deps);
        assert!(result.is_ok());

        let pkg = result.unwrap();
        assert!(pkg.cargo_toml.contains("test_msgs"));
        assert!(pkg.message_rmw.contains("i32"));
        assert!(pkg.message_rmw.contains("f64"));
    }

    #[test]
    fn test_message_with_dependencies() {
        let msg = parse_message("geometry_msgs/Point position\n").unwrap();
        let deps = HashSet::new();

        let result = generate_message_package("nav_msgs", "Odometry", &msg, &deps);
        assert!(result.is_ok());

        let pkg = result.unwrap();
        assert!(pkg.cargo_toml.contains("geometry_msgs"));
    }

    #[test]
    fn test_message_with_large_array() {
        let mut msg = Message::new();
        msg.fields.push(Field {
            field_type: FieldType::Array {
                element_type: Box::new(FieldType::Primitive(PrimitiveType::Int32)),
                size: 64,
            },
            name: "data".to_string(),
            default_value: None,
        });

        let deps = HashSet::new();
        let result = generate_message_package("test_msgs", "LargeArray", &msg, &deps);
        assert!(result.is_ok());

        let pkg = result.unwrap();
        assert!(pkg.cargo_toml.contains("big-array"));
    }

    #[test]
    fn test_message_with_keyword_field() {
        let msg = parse_message("int32 type\nfloat64 match\n").unwrap();
        let deps = HashSet::new();

        let result = generate_message_package("test_msgs", "Keywords", &msg, &deps);
        assert!(result.is_ok());

        let pkg = result.unwrap();
        assert!(pkg.message_rmw.contains("type_"));
        assert!(pkg.message_rmw.contains("match_"));
    }

    /// A message with no fields still occupies a byte in C.
    ///
    /// `rosidl_generator_c` emits `uint8_t structure_needs_at_least_one_member`
    /// for such messages -- `std_msgs/Empty`, and any constants-only message.
    /// Generating an empty Rust struct instead makes the RMW type zero-sized,
    /// so every field *after* one inside another message reads from the wrong
    /// offset. That is a silent memory error: `test_msgs/Arrays`, which embeds
    /// `Constants[3]`, segfaults on conversion.
    #[test]
    fn test_fieldless_message_matches_c_layout() {
        let msg = parse_message("int32 MAX=5\n").unwrap();
        assert!(msg.fields.is_empty(), "fixture should have no fields");
        let deps = HashSet::new();

        let pkg = generate_message_package("test_msgs", "Constants", &msg, &deps).unwrap();

        assert!(
            pkg.message_rmw
                .contains("pub structure_needs_at_least_one_member: u8"),
            "RMW struct must carry the C placeholder member:\n{}",
            pkg.message_rmw
        );
        assert!(
            pkg.message_idiomatic
                .contains("structure_needs_at_least_one_member: 0"),
            "conversion into the RMW type must initialise the placeholder:\n{}",
            pkg.message_idiomatic
        );
    }

    #[test]
    fn test_message_with_fields_has_no_placeholder() {
        let msg = parse_message("int32 x\n").unwrap();
        let deps = HashSet::new();

        let pkg = generate_message_package("test_msgs", "Point", &msg, &deps).unwrap();

        assert!(
            !pkg.message_rmw
                .contains("structure_needs_at_least_one_member"),
            "a message with fields must not carry the placeholder"
        );
    }

    #[test]
    fn test_simple_service_generation() {
        let srv = parse_service("int32 a\nint32 b\n---\nint32 sum\n").unwrap();
        let deps = HashSet::new();

        let result = generate_service_package("example_interfaces", "AddTwoInts", &srv, &deps);
        assert!(result.is_ok());

        let pkg = result.unwrap();
        assert!(pkg.cargo_toml.contains("example_interfaces"));
        assert!(pkg.lib_rs.contains("pub mod srv"));
        assert!(pkg.service_rmw.contains("AddTwoIntsRequest"));
        assert!(pkg.service_rmw.contains("AddTwoIntsResponse"));
        assert!(pkg.service_idiomatic.contains("AddTwoIntsRequest"));
        assert!(pkg.service_idiomatic.contains("AddTwoIntsResponse"));
    }

    #[test]
    fn test_service_with_dependencies() {
        let srv = parse_service("geometry_msgs/Point position\n---\nbool success\n").unwrap();
        let deps = HashSet::new();

        let result = generate_service_package("test_srvs", "CheckPoint", &srv, &deps);
        assert!(result.is_ok());

        let pkg = result.unwrap();
        assert!(pkg.cargo_toml.contains("geometry_msgs"));
    }

    #[test]
    fn test_simple_action_generation() {
        let action =
            parse_action("int32 order\n---\nint32[] sequence\n---\nint32[] partial_sequence\n")
                .unwrap();
        let deps = HashSet::new();

        let result = generate_action_package("example_interfaces", "Fibonacci", &action, &deps);
        assert!(result.is_ok());

        let pkg = result.unwrap();
        assert!(pkg.cargo_toml.contains("example_interfaces"));
        assert!(pkg.lib_rs.contains("pub mod action"));
        assert!(pkg.action_rmw.contains("FibonacciGoal"));
        assert!(pkg.action_rmw.contains("FibonacciResult"));
        assert!(pkg.action_rmw.contains("FibonacciFeedback"));
        assert!(pkg.action_idiomatic.contains("FibonacciGoal"));
        assert!(pkg.action_idiomatic.contains("FibonacciResult"));
        assert!(pkg.action_idiomatic.contains("FibonacciFeedback"));
    }

    #[test]
    fn test_action_with_dependencies() {
        let action = parse_action(
            "geometry_msgs/Point target\n---\nfloat64 distance\n---\nfloat64 current_distance\n",
        )
        .unwrap();
        let deps = HashSet::new();

        let result = generate_action_package("test_actions", "Navigate", &action, &deps);
        assert!(result.is_ok());

        let pkg = result.unwrap();
        assert!(pkg.cargo_toml.contains("geometry_msgs"));
    }
}
