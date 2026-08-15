//! Asserts on generated bindings rather than merely compiling against them.
//!
//! A codegen bug that produces compiling-but-wrong conversions -- the class the
//! bounded-sequence fix addressed -- passes a build and fails here.

use rosidl_runtime_rs::{Message, RmwMessage, Sequence};
use std::borrow::Cow;

/// Convert to the RMW representation and back, returning the result.
fn round_trip<T>(value: T) -> T
where
    T: Message,
{
    let rmw = T::into_rmw_message(Cow::Owned(value)).into_owned();
    T::from_rmw_message(rmw)
}

fn check_primitives() {
    let value = iface_core::msg::AllPrimitives::default();

    // Defaults declared in the .msg must survive into Default::default().
    assert!(value.flag_default, "bool default lost");
    assert_eq!(value.f32_default, 1.5, "float32 default lost");
    assert_eq!(value.f64_default, -2.25, "float64 default lost");
    assert_eq!(value.i8_default, -8);
    assert_eq!(value.u8_default, 8);
    assert_eq!(value.i16_default, -16);
    assert_eq!(value.u16_default, 16);
    assert_eq!(value.i32_default, -32);
    assert_eq!(value.u32_default, 32);
    assert_eq!(value.i64_default, -64);
    assert_eq!(value.u64_default, 64);
    assert_eq!(value.str_default, "hello", "string default lost");

    let mut sent = value.clone();
    sent.str_plain = "round trip".to_string();
    sent.wstr_plain = "wide".to_string();
    let back = round_trip(sent.clone());
    assert_eq!(back.str_plain, sent.str_plain, "string round-trip");
    assert_eq!(back.wstr_plain, sent.wstr_plain, "wstring round-trip");
    assert_eq!(back.str_default, sent.str_default);

    println!("  primitives and defaults ok");
}

fn check_collections() {
    let mut value = iface_core::msg::Collections::default();

    assert_eq!(value.fixed_defaults, [1.0, 2.0, 3.0], "array default lost");
    assert_eq!(value.fixed_primitives.len(), 3, "fixed array length");
    assert_eq!(value.fixed_strings.len(), 2);
    assert_eq!(value.fixed_points.len(), 2);

    value.unbounded_primitives = vec![1, 2, 3, 4, 5];
    value.bounded_primitives = vec![9, 8];
    value.unbounded_strings = vec!["a".to_string(), "b".to_string()];
    value.bounded_strings = vec!["x".to_string()];
    value.unbounded_wstrings = vec!["wide".to_string()];
    value.bounded_wstrings = vec!["w".to_string()];
    value.unbounded_points = vec![geometry_msgs::msg::Point {
        x: 1.0,
        y: 2.0,
        z: 3.0,
    }];
    value.bounded_points = vec![geometry_msgs::msg::Point::default()];

    let back = round_trip(value.clone());
    assert_eq!(back.unbounded_primitives, value.unbounded_primitives);
    assert_eq!(
        back.bounded_primitives, value.bounded_primitives,
        "bounded sequence of primitives round-trip"
    );
    assert_eq!(back.bounded_strings, value.bounded_strings, "bounded string sequence");
    assert_eq!(back.bounded_wstrings, value.bounded_wstrings, "bounded wstring sequence");
    assert_eq!(back.unbounded_points.len(), 1);
    assert_eq!(back.bounded_points.len(), 1, "bounded message sequence");
    assert_eq!(back.unbounded_points[0].x, 1.0);

    // The RMW side must use BoundedSequence for the bounded fields; building one
    // over its limit is what the generated try_from guards.
    let rmw = iface_core::msg::Collections::into_rmw_message(Cow::Owned(value)).into_owned();
    assert_eq!(rmw.bounded_primitives.len(), 2);
    assert_eq!(rmw.unbounded_primitives.len(), 5);

    println!("  collections ok");
}

fn check_constants() {
    assert_eq!(iface_core::msg::Constants::MODE_IDLE, 0);
    assert_eq!(iface_core::msg::Constants::MODE_ACTIVE, 1);
    assert_eq!(iface_core::msg::Constants::MAX_RETRIES, 5);
    assert_eq!(iface_core::msg::Constants::EPSILON, 0.001);
    assert!(iface_core::msg::Constants::FEATURE_ENABLED);
    // A string constant must be &'static str: `String` is not const-constructible.
    assert_eq!(iface_core::msg::Constants::PROTOCOL, "rosidl");

    println!("  constants ok");
}

fn check_fieldless_layout() {
    // A field-less message has a one-byte placeholder in C. If the generated
    // Rust struct is zero-sized instead, everything after it in an embedding
    // message reads from the wrong offset -- silently, until it segfaults.
    let mut layout = iface_core::msg::Layout::default();
    layout.alignment_check = 0x5A5A5A;
    layout.trailing_label = "canary".to_string();
    layout.trailing_values = [1.25, -2.5];

    let back = round_trip(layout.clone());
    assert_eq!(
        back.alignment_check, 0x5A5A5A,
        "field after an embedded field-less message was corrupted"
    );
    assert_eq!(back.trailing_label, "canary", "trailing string corrupted");
    assert_eq!(back.trailing_values, [1.25, -2.5], "trailing array corrupted");
    assert_eq!(back.marker_array.len(), 3);

    assert_eq!(iface_core::msg::Marker::KIND_START, 1);

    println!("  field-less message layout ok");
}

fn check_nested() {
    let mut nested = iface_core::msg::Nested::default();
    nested.primitives.str_plain = "inner".to_string();
    nested.pose.pose.position.x = 4.5;
    nested.roi.width = 640;
    nested.header.frame_id = "map".to_string();

    let back = round_trip(nested.clone());
    assert_eq!(back.primitives.str_plain, "inner", "nested own-package field");
    assert_eq!(back.pose.pose.position.x, 4.5, "nested cross-package field");
    assert_eq!(back.roi.width, 640);
    assert_eq!(back.header.frame_id, "map");

    println!("  nesting ok");
}

fn check_service() {
    let mut request = iface_core::srv::Compute_Request::default();
    request.label = "compute".to_string();
    request.waypoints = vec![geometry_msgs::msg::Point::default()];
    let back = round_trip(request.clone());
    assert_eq!(back.label, "compute");
    assert_eq!(
        back.waypoints.len(),
        1,
        "bounded sequence in a service request"
    );

    let mut response = iface_core::srv::Compute_Response::default();
    response.success = true;
    response.resolved = vec![geometry_msgs::msg::Point::default(); 2];
    response.notes = vec!["ok".to_string()];
    let back = round_trip(response.clone());
    assert!(back.success);
    assert_eq!(
        back.resolved.len(),
        2,
        "bounded sequence in a service response"
    );
    assert_eq!(back.notes.len(), 1);

    println!("  services ok");
}

fn check_action() {
    let mut goal = iface_core::action::ExecuteGoal::default();
    goal.label = "run".to_string();
    goal.waypoints = vec![geometry_msgs::msg::Point::default(); 3];
    let back = round_trip(goal.clone());
    assert_eq!(back.label, "run");
    assert_eq!(back.waypoints.len(), 3, "bounded sequence in an action goal");

    let mut result = iface_core::action::ExecuteResult::default();
    result.success = true;
    result.visited = vec![geometry_msgs::msg::Point::default()];
    let back = round_trip(result.clone());
    assert!(back.success);
    assert_eq!(back.visited.len(), 1, "bounded sequence in an action result");

    let mut feedback = iface_core::action::ExecuteFeedback::default();
    feedback.progress = 0.5;
    feedback.remaining = vec![geometry_msgs::msg::Point::default(); 2];
    let back = round_trip(feedback.clone());
    assert_eq!(back.progress, 0.5);
    assert_eq!(
        back.remaining.len(),
        2,
        "bounded sequence in action feedback"
    );

    // The same constant name in two sections must resolve per section, which is
    // what the generated per-section constant modules are for. Same name,
    // different values, so a collision would show up as a wrong value rather
    // than a compile error.
    assert_eq!(iface_core::action::ExecuteResult::NONE, 0, "result NONE");
    assert_eq!(iface_core::action::ExecuteFeedback::NONE, 9, "feedback NONE");
    assert_eq!(iface_core::action::ExecuteResult::SUCCEEDED, 1);
    assert_eq!(iface_core::action::ExecuteFeedback::RUNNING, 1);

    println!("  actions ok");
}

fn check_cross_package_interfaces() {
    let mut aggregate = iface_deps::msg::Aggregate::default();
    aggregate.primitives.str_plain = "from iface_core".to_string();
    aggregate.batches = vec![iface_core::msg::Collections::default()];
    aggregate.history = vec![iface_core::msg::Constants::default(); 2];
    aggregate.stamp.sec = 7;

    let back = round_trip(aggregate.clone());
    assert_eq!(back.primitives.str_plain, "from iface_core");
    assert_eq!(back.batches.len(), 1, "bounded sequence of another package's type");
    assert_eq!(back.history.len(), 2);
    assert_eq!(back.stamp.sec, 7);

    let mut request = iface_deps::srv::Summarize_Request::default();
    request.subject.primitives.str_default = "kept".to_string();
    let back = round_trip(request.clone());
    assert_eq!(back.subject.primitives.str_default, "kept");

    println!("  cross-package interfaces ok");
}

fn check_stock_packages() {
    let mut odom = nav_msgs::msg::Odometry::default();
    odom.pose.pose.position.z = 1.0;
    assert_eq!(round_trip(odom).pose.pose.position.z, 1.0);

    let mut scan = sensor_msgs::msg::LaserScan::default();
    scan.ranges = vec![1.0, 2.0];
    assert_eq!(round_trip(scan).ranges.len(), 2);

    let mut status = diagnostic_msgs::msg::DiagnosticStatus::default();
    status.name = "diag".to_string();
    status.values = vec![diagnostic_msgs::msg::KeyValue {
        key: "k".to_string(),
        value: "v".to_string(),
    }];
    let back = round_trip(status);
    assert_eq!(back.name, "diag");
    assert_eq!(back.values[0].key, "k");

    let mut traj = trajectory_msgs::msg::JointTrajectory::default();
    traj.joint_names = vec!["j1".to_string()];
    assert_eq!(round_trip(traj).joint_names[0], "j1");

    let goal_info = action_msgs::msg::GoalInfo::default();
    assert_eq!(round_trip(goal_info).stamp.sec, 0);

    let mut multi = example_interfaces::msg::Int32MultiArray::default();
    multi.data = vec![1, 2, 3];
    assert_eq!(round_trip(multi).data.len(), 3);

    println!("  stock interface packages ok");
}

fn check_type_supports() {
    // Forces the link against each package's typesupport library, which is what
    // the -L flags and rpath in the generated config exist for.
    assert!(!<iface_core::msg::rmw::Nested as RmwMessage>::get_type_support().is_null());
    assert!(!<iface_deps::msg::rmw::Aggregate as RmwMessage>::get_type_support().is_null());
    assert!(!<std_msgs::msg::rmw::String as RmwMessage>::get_type_support().is_null());
    assert!(!<nav_msgs::msg::rmw::Odometry as RmwMessage>::get_type_support().is_null());

    // Sequence is re-exported by every generated crate.
    let mut seq: Sequence<i32> = Sequence::new(2);
    seq[0] = 1;
    assert_eq!(seq.len(), 2);

    println!("  type supports ok");
}

fn main() {
    check_primitives();
    check_collections();
    check_constants();
    check_fieldless_layout();
    check_nested();
    check_service();
    check_action();
    check_cross_package_interfaces();
    check_stock_packages();
    check_type_supports();
    println!("consumer ok");
}
