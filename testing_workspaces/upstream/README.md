# upstream

Third-party ROS 2 Rust packages, fetched at a pinned revision and built with
this toolchain.

```bash
just fetch          # clone the pinned revision into src/ (git-ignored)
just install-deps   # rosdep
just build
```

The sources are fetched rather than vendored: a submodule would make every clone
of this repository pay for code it may never build, and would turn upgrades into
a commit bump rather than a deliberate act.

## Status: not currently a passing tier

The pinned revision (`e7c18ef`, tip of `main`) does **not** build against the
bindings this toolchain generates:

```
error[E0599]: no associated function or constant named `into_rmw_message` found
              for struct `rclrs_example_msgs::msg::VariousTypes`
error[E0277]: the trait bound `rclrs_example_msgs::msg::rmw::NestedType: SequenceAlloc`
              is not satisfied
```

The examples target the ros2-rust generator's API surface, which differs from
ours. A local commit that updates them (`35e062c`, "update rclrs dependency to
0.7") exists in one developer's checkout but has never been pushed, so it cannot
be pinned here.

Because of that this workspace is **excluded from `just test-workspaces` and
`just test-workspaces-heavy`**: a tier that always fails teaches nothing. It is
kept because the fetch tooling is what makes the comparison reproducible the day
a compatible revision is published — at which point, bump `ref` in the justfile
and add it back to the heavy tier.
