fn main() {
    let twist = geometry_msgs::msg::Twist::default();
    assert_eq!(twist.linear.x, 0.0);

    // Set by the user's own [env] block, which the merge must not drop.
    assert_eq!(
        option_env!("PRESET_CONFIG_MARKER"),
        Some("kept"),
        "the user's [env] entry did not survive config generation"
    );

    println!("preset_config ok");
}
