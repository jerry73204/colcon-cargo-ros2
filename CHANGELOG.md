# Changelog

Notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html) — with the caveat that
before 1.0, minor versions may break things.

Entries describe what changed for someone *using* the tool. The reasoning behind
each change is in the commit that made it and in `docs/phases/`.

## [Unreleased]

### Fixed

- **A package provided by two prefixes now resolves to the earlier one, as ROS
  does.** `AMENT_PREFIX_PATH` is ordered highest-precedence first — sourcing an
  overlay after an underlay prepends it — but the index inserted into a map as
  it walked, so the *last* prefix won.

  With Autoware 1.5.0 and ROS Humble both providing `autoware_common_msgs`
  (1.11.0 and 1.3.0), sourcing Humble then Autoware gave
  `ros2 pkg prefix autoware_common_msgs` = `/opt/autoware/1.5.0` while this tool
  generated bindings from `/opt/ros/humble`. The node then linked Autoware's
  typesupport against bindings built from Humble's `.msg` definitions: the same
  struct at two definitions, which is a silent ABI mismatch rather than a
  visible error, and only benign while the definitions happen to agree.

- **A committed `Cargo.lock` no longer records which ROS installation generated
  the bindings.** Generated binding crates were stamped with the ROS package's
  own version, and cargo copies that into every consumer's lock. The version has
  no effect on resolution — these crates are reached by path through
  `[patch.crates-io]` — but it made the lock machine-specific, so a workspace
  that commits one came back dirty after every build elsewhere and two
  developers could not both keep it clean.

  It is not only a cross-machine problem: on a single machine
  `/opt/autoware/1.5.0` and `/opt/ros/humble` can both provide
  `autoware_common_msgs`, at 1.11.0 and 1.3.0 respectively, so the recorded
  version flipped depending on whether the Autoware underlay had been sourced.

  Generated crates now carry a fixed `0.0.0`, and the ROS package version moves
  to `[package.metadata.ros] package_version`, where cargo does not propagate
  it. Existing locks will show the message crates dropping to `0.0.0` once on
  the next build, and stop changing after that.

## [0.5.1] — 2026-08-16

### Added

- **`colcon build` says so when `colcon-ros-cargo` is installed alongside it.**
  Both extensions build `ament_cargo` packages, and colcon-ros-cargo wins the
  package identification (priority 160 against colcon-ros's 150), so every Rust
  package goes to `cargo ament-build`, this extension's build task never runs,
  and no bindings are generated — while the build still reports success. The
  only other symptom is an `argparse.ArgumentError` about `--cargo-args`, which
  names neither cause nor cure.

## [0.5.0] — 2026-08-16

### Added

- **Plain `cargo` works after one `colcon build`**, with nothing sourced.
  The generated `.cargo/config.toml` now also carries an `[env]` block supplying
  `AMENT_PREFIX_PATH` to build scripts, rpath entries so binaries find ROS
  libraries at run time, and a `target-dir` under `build/.cargo_target/` that
  keeps cargo artifacts out of `src/` while colcon and manual cargo share one
  cache. `--no-rpath` opts out of the rpath.
- **`colcon-cargo-ros2` and `colcon-cargo-ros2-doctor` console scripts.** The
  wheel ships no binaries, so the `cargo ros2` subcommands — `bindgen`,
  `install`, `clean`, `doctor` — were previously unavailable to anyone who
  installed from PyPI.
- **`cargo ros2 doctor`**: walks the ROS environment, the generated config, the
  patched crate directories, dependency sources, binding freshness and
  `package.xml` declarations, and prints the fix for the first thing that is
  wrong.
- **An interface package resolved from a `path` or `git` source is now named.**
  `[patch.crates-io]` cannot redirect one, so the generated crate goes unread
  and cargo reports a missing manifest in a directory from whichever machine ran
  a different generator. The build warns before cargo runs and `doctor` fails its
  `Dependency sources` check.
- **Diagnostics for the failures cargo misattributes.** An interface package used
  in `Cargo.toml` but missing from `package.xml` is now named, with the
  `<depend>` tag to add, before cargo reports a "yanked" version against
  crates.io. Stale bindings are refused by the generated crate's `build.rs`
  rather than compiled against, with `COLCON_CARGO_ROS2_SKIP_STAMP_CHECK=1` to
  override.
- **The generated config is added to `.gitignore`** automatically in a git
  worktree; `--no-gitignore` opts out.
- **Supply-chain policy** in `packages/deny.toml`, checked by `cargo deny` on
  every pull request and by `just audit` locally, plus a `SECURITY.md`.
- **Testing workspaces that assert**: `interfaces` (every IDL shape, checked at
  runtime), `layouts` (every workspace shape, checked against the generated
  config) and a `scenarios` harness for the failure modes. They run in CI.

### Changed

- **The `rosidl_runtime_rs` version is derived from the workspace** rather than
  pinned. `rclrs` 0.6 needs runtime 0.5 and `rclrs` 0.7 needs 0.6; generating
  the wrong one left two incompatible copies in the graph, and every message type
  failed a trait bound it appeared to satisfy. Packages that disagree are named.
  `--rosidl-runtime-rs-version` still overrides.
- **Linker search paths are derived from the dependency graph**, not from
  whatever happened to be in `install/`, and directories holding no libraries are
  skipped. The generated config no longer depends on whether the workspace had
  been sourced.
- **Binding freshness is keyed on file contents**, not size and mtime, so a fresh
  `git clone`, a `cp -r` or a `touch` no longer makes good bindings look stale.
- **A dependency declared in `package.xml` but unused in `Cargo.toml` is no
  longer a warning.** Declaring one is often correct — a launch file may start a
  node publishing that type — so the cost is reported at info level instead.
- **`colcon_cargo_ros2.__version__` is derived** from `pyproject.toml` rather
  than duplicated, after sitting at 0.2.0 for several releases.
- pyo3 0.22 → 0.29, clearing two advisories; indicatif 0.17 → 0.18, dropping an
  unmaintained transitive dependency.

### Fixed

- **The `serde` feature did not reach dependency crates or long arrays.** Asking
  for `features = ["serde"]` on a generated crate left its dependencies without
  the feature, so a nested field failed `builtin_interfaces::msg::rmw::Time:
  serde::Deserialize`. Separately, the RMW layer never annotated arrays longer
  than 32 — serde has no impls for those — so any message with one, such as
  `geometry_msgs/PoseWithCovariance`, failed at `[f64; 36]: serde::Deserialize`
  once the feature reached it. Both found by building
  [iceoryx2](https://github.com/eclipse-iceoryx/iceoryx2)'s ROS 2 demo.
- **A field-less message generated a zero-sized Rust struct** while
  `rosidl_generator_c` gives it a one-byte placeholder. Anything embedding one —
  `std_msgs/Empty`, any constants-only message — read every later field from the
  wrong offset and could segfault.
- **Constant modules generated from `.idl` were wrapped in an extra `pub mod`**,
  so the only reachable path was `msg::x_constants::x_constants::CONST`.
- **Installed binaries stopped working when the workspace moved.** rpaths now
  include `$ORIGIN`-relative entries, so a moved or renamed workspace, or an
  `install/` tree copied to another machine, keeps running.
- Internal crates carried path dependencies without versions, which would have
  failed to publish to crates.io.

### Removed

- `setup.py` and `setup.cfg`. Nothing built through them — the backend is
  maturin — and `setup.cfg` carried a stale version declaration and metadata from
  the project this was forked from.

## [0.4.1] — 2026-04-08

### Fixed

- Lock `bindgen.lock` with `fcntl`, so a crashed build cannot leave a stale lock
  behind.

## [0.4.0] — 2026-03-03

### Added

- Rust 2024 edition across the workspace.
- `--rosidl-runtime-rs-version` to override the version generated crates depend
  on.

### Changed

- One `.cargo/config.toml` per Cargo workspace, carrying both
  `[patch.crates-io]` and `[build] rustflags`, replacing the separate
  `build/ros2_cargo_config.toml` passed with `--config`.

---

Releases before 0.4.0 predate this changelog; see the git history.

[Unreleased]: https://github.com/jerry73204/colcon-cargo-ros2/compare/v0.5.1...HEAD
[0.5.1]: https://github.com/jerry73204/colcon-cargo-ros2/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/jerry73204/colcon-cargo-ros2/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/jerry73204/colcon-cargo-ros2/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/jerry73204/colcon-cargo-ros2/compare/v0.3.4...v0.4.0
