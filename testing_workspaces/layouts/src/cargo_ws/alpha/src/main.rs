use rosidl_runtime_rs::RmwMessage;

fn main() {
    let msg = std_msgs::msg::Int32 { data: 1 };
    assert_eq!(msg.data, 1);
    assert!(!<std_msgs::msg::rmw::Int32 as RmwMessage>::get_type_support().is_null());
    println!("alpha ok");
}
