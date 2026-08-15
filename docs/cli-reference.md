# CLI Reference

Normal use needs none of these commands: `colcon build` runs everything. They
exist for generating bindings outside a colcon workspace, inspecting what a build
would produce, and diagnosing one that misbehaved.

> **Rewritten 2026-08-16.** The previous version of this document described
> `cargo ros2 build`, `check`, `info`, `cache` and `ament-build` — a command set
> that was planned and never built. Everything below was checked against
> `--help`.

## Two ways in

| | Comes from | Subcommands |
|---|---|---|
| `colcon-cargo-ros2` | the wheel, on `PATH` after `pip install` | `bindgen`, `install`, `clean`, `doctor` |
| `cargo ros2` | a source checkout (`cargo install --path packages/cargo-ros2`) | the same four |

`colcon-cargo-ros2-doctor` is a direct alias for `colcon-cargo-ros2 doctor`,
under its own name because it is what gets typed while a build is failing.

The wheel ships the PyO3 extension module and no binaries, which is why the
console scripts exist; see [issue #4](https://github.com/jerry73204/colcon-cargo-ros2/issues/4).

---

## `bindgen`

Generate Rust bindings for one ROS interface package. `colcon build` does this
for every dependency it discovers; this is for doing one on its own.

```bash
colcon-cargo-ros2 bindgen --package std_msgs --output build/bindings
```

| Option | Meaning |
|---|---|
| `--package <name>` | ROS package name (required) |
| `--output <dir>` | directory to generate into (required) |
| `--package-path <dir>` | the package's share directory; default is to ask the ament index |
| `--rosidl-runtime-rs-version <ver>` | version the generated crate depends on. It has to match what your `rclrs` pulls in — `colcon build` derives this from the workspace |
| `--verbose` | |

## `install`

Copy binaries, libraries and `[package.metadata.ros]` entries into
`install/<pkg>/` and write the ament markers, as the build task does after
`cargo build`.

```bash
colcon-cargo-ros2 install --install-base install/my_node --profile release
```

| Option | Meaning |
|---|---|
| `--install-base <dir>` | `install/<package>` directory (required) |
| `--project-root <dir>` | crate directory; default is the current directory |
| `--build-base <dir>` | colcon build directory; default is the project root |
| `--profile <name>` | cargo profile the build used (default `debug`) |
| `--target <triple>` | for a cross build |
| `--features <list>` | comma-separated features that were enabled |
| `--no-default-features`, `--all-features` | |
| `--verbose` | |

## `clean`

Remove the generated bindings and cache for a crate.

```bash
colcon-cargo-ros2 clean --path .
```

## `doctor`

Explain why a plain `cargo` invocation fails in this workspace. Walks the chain
in order and prints the fix for the first thing that is wrong; exits non-zero if
anything failed, so CI can gate on it.

```bash
colcon-cargo-ros2-doctor          # or: colcon-cargo-ros2 doctor [path]
```

```
✓ ROS environment: 1 prefixes on AMENT_PREFIX_PATH
✓ Generated .cargo/config.toml: found at /ws/src/pkg_b/.cargo/config.toml
✓ Patch section: generated markers present
✓ Patched crates: 4 generated crates readable
✓ Dependency sources: interface crates come from the patches
✓ Binding freshness: 4 crates match their interface definitions
✗ package.xml declarations: used in Cargo.toml but not declared: sensor_msgs
    Add to package.xml, then re-run `colcon build`:
      <depend>sensor_msgs</depend>
```

---

## Options on `colcon build`

Added by this extension to the `build` and `test` verbs.

| Option | Meaning |
|---|---|
| `--cargo-args <args...>` | passed through to cargo. Arguments that look like colcon options need a leading space: `--cargo-args " --help"` |
| `--rosidl-runtime-rs-version <ver>` | override the version generated crates depend on. Default is derived from what the workspace's packages declare |
| `--no-rpath` | do not bake library directories into binaries. They then need a sourced environment to run |
| `--no-gitignore` | do not add the generated `.cargo/config.toml` to `.gitignore` |

```bash
colcon build --cargo-args --release
colcon build --cargo-args --features extra
colcon build --rosidl-runtime-rs-version 0.5
```

## Environment variables

| Variable | Effect |
|---|---|
| `COLCON_CARGO_ROS2_SKIP_STAMP_CHECK=1` | build against bindings whose interface definitions have changed, instead of failing. For bisecting; the normal fix is `colcon build` |
| `AMENT_PREFIX_PATH` | where interface packages are discovered. A generated `.cargo/config.toml` supplies it to build scripts, so a sourced environment is not required |
| `CARGO_TARGET_DIR` | overrides the `target-dir` the generated config sets |

## See also

- [Working with `cargo` Directly](../packages/colcon-cargo-ros2/README.md) — what one `colcon build` gives you
- [docs/troubleshooting.md](troubleshooting.md) — keyed by the error message
