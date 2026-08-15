fn main() {
    let msg = std_msgs::msg::Bool { data: true };
    assert!(msg.data);
    assert_eq!(installer_node::installer_node_answer(), 42);
    println!("installer_node ok");
}
