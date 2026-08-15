# upstream

Third-party ROS 2 Rust packages, fetched at a pinned revision and built with
this toolchain, to catch changes in code we do not control.

```bash
just fetch          # clone the pinned revision into src/ (git-ignored)
just install-deps   # rosdep
just build
just verify
```

The sources are fetched rather than vendored: a submodule would make every clone
of this repository pay for code it may never build, and would turn upgrades into
a commit bump rather than a deliberate act. The revision is pinned in the
`justfile` (`ref`) — bump it deliberately.

Heavy tier: needs network access and `rosdep`, so it runs under
`just test-workspaces-heavy`, not on every pull request.

## What these examples exercise

They are written against `rclrs`, which this project does not generate — so they
test the seam: bindings we generate, consumed by a client library we do not
control, in code we did not write.

`rclrs` decides which `rosidl_runtime_rs` the graph uses:

| rclrs | rosidl_runtime_rs |
|---|---|
| 0.6 | 0.5 |
| 0.7 | 0.6 |

Generated crates must ask for the same one. Cargo treats 0.5 and 0.6 as
incompatible, so a mismatch leaves *both* in the graph and every message type
fails a trait bound it appears to satisfy:

```
error[E0277]: the trait bound `std_msgs::msg::String: MessageIDL` is not satisfied
note: there are multiple different versions of crate `rosidl_runtime_rs`
      in the dependency graph
```

`colcon build` now derives the version from what the workspace's own packages
declare, which is why these examples build unaided. `--rosidl-runtime-rs-version`
overrides it.

## One package is excluded

`rust_pubsub` declares `rclrs = "*"`, so cargo resolves it to the newest rclrs
while its siblings pin 0.6. Those two need different `rosidl_runtime_rs`
versions, and bindings are generated once per colcon workspace — no single build
satisfies both.

`just fetch` writes a `COLCON_IGNORE` into that package. The build reports the
cause by name before failing:

```
WARNING rust_pubsub declares rclrs = "*", so cargo resolves it to whatever
version is newest.
  Generated bindings cannot be matched to an unbounded requirement; pin a
  version (e.g. rclrs = "0.7") to have them agree.
```

Pinning `rclrs` in that package upstream would let it build here too.
