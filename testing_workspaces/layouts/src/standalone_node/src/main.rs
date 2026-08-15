//! Round-trips a std_msgs value and links its typesupport.
//!
//! Exits non-zero on mismatch, so `just verify` fails on a codegen regression
//! rather than on a build error alone.

use rosidl_runtime_rs::RmwMessage;

fn main() {
    let idiomatic = std_msgs::msg::String {
        data: "standalone".to_string(),
    };
    let rmw = std_msgs::msg::rmw::String::from(idiomatic.clone());
    let back = std_msgs::msg::String::from(rmw);
    assert_eq!(back.data, idiomatic.data, "String round-trip changed data");

    let counter = std_msgs::msg::Int32 { data: 7 };
    let rmw_counter = std_msgs::msg::rmw::Int32::from(counter.clone());
    assert_eq!(rmw_counter.data, counter.data);

    // Referencing the type support forces the link against
    // libstd_msgs__rosidl_typesupport_c, which is what the -L flags and rpath
    // in the generated config exist for.
    let support = <std_msgs::msg::rmw::String as RmwMessage>::get_type_support();
    assert!(!support.is_null(), "null type support");

    println!("standalone_node ok");
}
