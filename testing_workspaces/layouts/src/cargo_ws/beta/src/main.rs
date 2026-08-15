use rosidl_runtime_rs::RmwMessage;

fn main() {
    let point = geometry_msgs::msg::Point {
        x: 1.0,
        y: 2.0,
        z: 3.0,
    };
    assert_eq!(point.x + point.y + point.z, 6.0);
    assert!(!<geometry_msgs::msg::rmw::Point as RmwMessage>::get_type_support().is_null());
    println!("beta ok");
}
