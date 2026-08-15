//! Uses all three dependency forms declared in Cargo.toml.

use rosidl_runtime_rs::RmwMessage;

fn main() {
    // Renamed dependency: `msgs` is sensor_msgs.
    let mut imu = msgs::msg::Imu::default();
    imu.linear_acceleration.z = 9.81;
    assert!(imu.linear_acceleration.z > 9.0);

    // Inherited from [workspace.dependencies].
    let status = diagnostic_msgs::msg::DiagnosticStatus {
        level: 0,
        name: "gamma".to_string(),
        ..Default::default()
    };
    assert_eq!(status.name, "gamma");

    // Platform table.
    #[cfg(unix)]
    {
        let odom = nav_msgs::msg::Odometry::default();
        assert_eq!(odom.pose.pose.position.x, 0.0);
        assert!(!<nav_msgs::msg::rmw::Odometry as RmwMessage>::get_type_support().is_null());
    }

    assert!(!<msgs::msg::rmw::Imu as RmwMessage>::get_type_support().is_null());

    println!("gamma ok");
}
