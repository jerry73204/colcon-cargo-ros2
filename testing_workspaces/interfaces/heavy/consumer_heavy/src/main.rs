//! Asserts on bindings generated from third-party interface packages.
//!
//! test_msgs is the upstream torture suite for the IDL surface; nav2_msgs adds
//! deeply nested real-world messages. Neither ships in a stock ROS install,
//! which is why this lives outside src/.

use rosidl_runtime_rs::{Message, RmwMessage};
use std::borrow::Cow;

fn round_trip<T: Message>(value: T) -> T {
    T::from_rmw_message(T::into_rmw_message(Cow::Owned(value)).into_owned())
}

fn check_test_msgs() {
    let mut edge = iface_heavy::msg::TestEdgeCases::default();

    edge.all_primitives.int32_value = -5;
    edge.string_types.string_value = "strings".to_string();
    edge.unbounded_seqs.int32_values = vec![1, 2, 3];
    edge.bounded_sequences.int32_values = vec![4, 5];

    let back = round_trip(edge.clone());
    assert_eq!(back.all_primitives.int32_value, -5);
    assert_eq!(back.unbounded_seqs.int32_values, vec![1, 2, 3]);
    assert_eq!(
        back.bounded_sequences.int32_values,
        vec![4, 5],
        "test_msgs bounded sequence round-trip"
    );

    println!("  test_msgs shapes ok");
}

fn check_nav2() {
    let mut nav = iface_heavy::msg::TestNav::default();
    nav.speed_limit.speed_limit = 1.5;
    nav.costmap.metadata.size_x = 10;
    nav.particles = vec![nav2_msgs::msg::Particle::default(); 2];

    let back = round_trip(nav.clone());
    assert_eq!(back.speed_limit.speed_limit, 1.5);
    assert_eq!(back.costmap.metadata.size_x, 10);
    assert_eq!(back.particles.len(), 2);

    assert!(!<iface_heavy::msg::rmw::TestNav as RmwMessage>::get_type_support().is_null());

    println!("  nav2_msgs shapes ok");
}

/// Round-trips each borrowed type on its own, announcing each step on stderr
/// (unbuffered) so a crash names the shape that caused it.
fn probe() {
    macro_rules! step {
        ($label:expr, $ty:ty) => {{
            eprintln!("probe: {}", $label);
            let _ = round_trip(<$ty>::default());
        }};
    }
    step!("BasicTypes", test_msgs::msg::BasicTypes);
    step!("Strings", test_msgs::msg::Strings);
    step!("WStrings", test_msgs::msg::WStrings);
    step!("Arrays", test_msgs::msg::Arrays);
    step!("Constants", test_msgs::msg::Constants);
    step!("Defaults", test_msgs::msg::Defaults);
    step!("BoundedSequences", test_msgs::msg::BoundedSequences);
    step!("BoundedPlainSequences", test_msgs::msg::BoundedPlainSequences);
    step!("UnboundedSequences", test_msgs::msg::UnboundedSequences);
    step!("MultiNested", test_msgs::msg::MultiNested);
    step!("Empty", test_msgs::msg::Empty);
    eprintln!("probe: TestEdgeCases::default");
    let edge = iface_heavy::msg::TestEdgeCases::default();
    eprintln!("probe: TestEdgeCases round-trip");
    let _ = round_trip(edge);
    eprintln!("probe: done");
}

fn main() {
    // Selectable so a crash can be attributed to one shape without a rebuild.
    let which = std::env::args().nth(1).unwrap_or_else(|| "all".to_string());
    if which == "fields" {
        let rmw = test_msgs::msg::rmw::Arrays::default();
        eprintln!("fields: primitives");
        let _ = rmw.bool_values;
        let _ = rmw.float64_values;
        eprintln!("fields: string to_string");
        let _ = rmw.string_values[0].to_string();
        eprintln!("fields: string array map");
        let _: [std::string::String; 3] = std::array::from_fn(|i| rmw.string_values[i].to_string());
        eprintln!("fields: nested BasicTypes");
        let _ = test_msgs::msg::BasicTypes::from_rmw_message(rmw.basic_types_values[0].clone());
        eprintln!("fields: nested Constants");
        let _ = test_msgs::msg::Constants::from_rmw_message(rmw.constants_values[0].clone());
        eprintln!("fields: nested Defaults");
        let _ = test_msgs::msg::Defaults::from_rmw_message(rmw.defaults_values[0].clone());
        eprintln!("fields: string_values_default");
        let _: [std::string::String; 3] =
            std::array::from_fn(|i| rmw.string_values_default[i].to_string());
        eprintln!("fields: done");
        return;
    }
    if which == "arrays" {
        eprintln!("arrays: rmw default");
        let rmw = test_msgs::msg::rmw::Arrays::default();
        eprintln!("arrays: rmw string_values[0] len {}", rmw.string_values[0].len());
        eprintln!("arrays: from_rmw");
        let idiomatic = test_msgs::msg::Arrays::from_rmw_message(rmw);
        eprintln!("arrays: idiomatic default");
        let _ = idiomatic.clone();
        eprintln!("arrays: into_rmw");
        let _ = test_msgs::msg::Arrays::into_rmw_message(Cow::Owned(idiomatic)).into_owned();
        eprintln!("arrays: done");
        return;
    }
    if which == "probe" {
        probe();
        return;
    }
    if which == "all" || which == "test_msgs" {
        check_test_msgs();
    }
    if which == "all" || which == "nav2" {
        check_nav2();
    }
    println!("consumer_heavy ok");
}
