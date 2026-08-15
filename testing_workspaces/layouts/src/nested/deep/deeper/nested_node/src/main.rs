//! Links against a workspace-local package's typesupport.
//!
//! Nothing in `install/local_msgs/lib` exists until local_msgs has been built,
//! so this also pins the ordering: colcon must install the interface package
//! before this crate links.

use rosidl_runtime_rs::RmwMessage;

fn main() {
    let reading = local_msgs::msg::Reading {
        value: 42.0,
        label: "nested".to_string(),
        ..Default::default()
    };
    assert_eq!(reading.value, 42.0);
    assert_eq!(reading.label, "nested");

    let request = local_msgs::srv::Toggle_Request { enable: true };
    assert!(request.enable);

    assert!(!<local_msgs::msg::rmw::Reading as RmwMessage>::get_type_support().is_null());

    println!("nested_node ok");
}
