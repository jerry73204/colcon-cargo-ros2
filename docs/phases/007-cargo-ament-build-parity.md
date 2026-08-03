## Phase 7: `cargo-ament-build` Installer Parity

**Goal**: Close the installation-behavior gaps between our `cargo-ros2` ament installer and the reference implementation, [`cargo-ament-build`](https://github.com/ros2-rust/cargo-ament-build) (v0.2.x), so that any package that installs correctly under the official `colcon-ros-cargo` stack also installs correctly under `colcon-cargo-ros2`.

**Motivation**: A comparison of the official ROS 2 Rust build stack (`colcon-cargo` + `colcon-ros-cargo` + `cargo-ament-build` + `rosidl_generator_rs`) against this project found that our binding-generation model is strictly ahead (no source rebuild of interface packages required), but our *installer* is behind. `packages/cargo-ros2/src/ament_installer.rs` hand-rolls binary discovery from a line scan of `Cargo.toml` and installs nothing but executables. The reference uses `cargo_manifest::Manifest::complete_from_path()` and installs libraries, honors `required-features`, and handles cross-compilation target directories.

**Scope**: `packages/cargo-ros2` (installer), `packages/colcon-cargo-ros2` (PyO3 config plumbing + build task).

**Status**: Not started

---

### Background

#### Reference behavior (`cargo-ament-build` 0.2.x)

`src/main.rs` loads the manifest with `cargo_manifest::Manifest::from_path()` followed by `complete_from_path()`, which performs Cargo's own target auto-discovery (`src/main.rs`, `src/bin/*.rs`, `src/lib.rs`). It then calls `install_binaries()` (`src/lib.rs:247`) with the completed `manifest.bin` list, the active profile, an optional target triple, and the set of features that were active during compilation.

`install_binaries()` does four things we do not:

1. Skips any binary whose `required-features` were not all enabled — otherwise the binary is simply absent from the build directory and installation would spuriously fail or warn.
2. Resolves the artifact directory as `build_base/<arch>/<profile>` when a target triple is in play, `build_base/<profile>` otherwise. The triple comes from `--target` or `$CARGO_BUILD_TARGET`.
3. Copies library artifacts alongside binaries, trying every prefix/suffix combination: `("lib","so")`, `("lib","dylib")`, `("lib","a")`, `("","dll")`, `("","lib")`.
4. Appends `.exe` to both source and destination on Windows.

It also registers the package under `share/ament_index/resource_index/rust_packages/<pkg>`, which is the marker `colcon-ros-cargo` uses in `find_installed_cargo_packages()` to locate installed Rust crates.

#### Current behavior (`cargo-ros2`)

- `install_binaries()` (`ament_installer.rs:263`) computes `target_dir.join(&self.profile)` unconditionally — no target-triple subdirectory.
- Binary names come from `extract_binary_names()` (`ament_installer.rs:430`), a line-oriented scanner that recognizes only `[[bin]]` blocks with a `name = "..."` line, then falls back to the package name when none are found. It misses `src/bin/*.rs` auto-discovery entirely, misses inline-table syntax, and has no notion of `required-features`.
- No library artifacts are installed; `install()` skips `install_binaries()` altogether for library-only packages (`ament_installer.rs:69`).
- No `.exe` suffix handling.
- `create_markers()` (`ament_installer.rs:108`) writes `resource_index/packages/<pkg>` and `resource_index/package_type/<pkg>` but not `resource_index/rust_packages/<pkg>`.
- There is no preflight check that the toolchain the plugin needs is actually present; the official `AmentCargoPackageIdentification` runs `cargo ament-build --help` and emits a targeted error.

#### Key enabling fact

`packages/cargo-ros2` already depends on `cargo_metadata = "0.18"`, and `install_to_ament()` (`lib.rs:169`) already runs `MetadataCommand::exec()` and holds a `root_package`. `cargo_metadata::Package::targets` is a `Vec<Target>` carrying `name`, `kind` (`bin`, `lib`, `cdylib`, `staticlib`, `rlib`, …), and `required_features`. That single already-available structure replaces the hand-rolled scanner and supplies everything needed for gaps 1, 3, and 5 at once. No new dependency (`cargo_manifest`) is required.

---

### Subphase 7.1: Replace hand-rolled target discovery with `cargo_metadata`

**Objective**: Derive the list of installable artifacts from `cargo metadata` rather than from a line scan of `Cargo.toml`.

**Design**:

- Add an `InstallTarget` struct to `ament_installer.rs`:
  ```rust
  pub struct InstallTarget {
      pub name: String,
      pub kind: InstallTargetKind,      // Bin | CDylib | StaticLib | Dylib
      pub required_features: Vec<String>,
  }
  ```
- In `install_to_ament()` (`lib.rs:169`), map `root_package.targets` into `Vec<InstallTarget>`, dropping kinds that produce no installable file (`rlib`, `proc-macro`, `test`, `bench`, `example`, `custom-build`).
- Pass the vector into `AmentInstaller::new()` as a new field, replacing the `is_library` boolean threaded through `install()`.
- Delete `extract_binary_names()` and `extract_toml_string_value()` (`ament_installer.rs:430`, `:468`). Keep `is_library_package()` only if other call sites need it; otherwise delete it too and drop the `is_library` parameter from `install()`.

**Correctness notes**:

- Cargo target auto-discovery is done by `cargo metadata` itself, so `src/main.rs` and `src/bin/*.rs` are covered without our own filesystem walk.
- Cargo replaces `-` with `_` in *library* file names but not in *binary* file names. Bin artifacts use `target.name` verbatim; lib artifacts use `target.name.replace('-', "_")`. The current code's comment at `ament_installer.rs:459` is correct for binaries only.

**Tests**:
- Fixture crate with `[[bin]]` entries → both installed.
- Fixture crate with `src/main.rs` only and no `[[bin]]` → binary named after the package installed.
- Fixture crate with `src/bin/a.rs` and `src/bin/b.rs` → both installed (currently fails).
- Fixture crate with a hyphenated package name → binary keeps the hyphen, library gets an underscore.

---

### Subphase 7.2: Honor `required-features`

**Objective**: Skip artifacts whose required features were not enabled for the build, instead of warning that the binary is missing.

**Design**:

- Add `features: Vec<String>` to `cargo_ros2::InstallConfig` (`lib.rs:57`) and to the PyO3 `InstallConfig` (`packages/colcon-cargo-ros2/src/lib.rs:63`), defaulting to empty.
- In `AmentCargoBuildTask._install_package()` (`task/ament_cargo/build.py:236`), parse the effective feature set from `cargo_args`, mirroring the existing `_detect_cargo_profile()` pattern:
  - `--features a,b` / `--features=a,b` / repeated `--features` → union of comma- and space-separated names
  - `--all-features` → sentinel handled by skipping the filter entirely
  - `--no-default-features` → do not implicitly add `default`
  - otherwise → add `default` (and, for correctness, the transitive closure of `default` from `root_package.features`, resolved on the Rust side where the feature table is available)
- In `install_binaries()`, skip a target when any name in `required_features` is absent from the resolved set.

**Tests**:
- Target with `required-features = ["extra"]`, built without `--features extra` → not installed, no warning.
- Same target built with `--features extra` → installed.
- `--all-features` → every target installed.

---

### Subphase 7.3: Cross-compilation target directory

**Objective**: Locate build artifacts under `build_base/<triple>/<profile>` when building for a non-host target.

**Design**:

- Add `arch: Option<String>` to both `InstallConfig` structs.
- Resolve in `build.py` with the same precedence the reference uses: `--target <triple>` or `--target=<triple>` in `cargo_args`, else `$CARGO_BUILD_TARGET`, else `None`.
- In `install_binaries()`:
  ```rust
  let artifact_dir = match &self.arch {
      Some(arch) => self.target_dir.join(arch).join(&self.profile),
      None => self.target_dir.join(&self.profile),
  };
  ```

**Note**: `--target` may also come from `.cargo/config.toml` `[build] target`. We generate that file ourselves (`workspace_bindgen.py`) and do not set `target` there, so reading it is out of scope; document the limitation in `docs/troubleshooting.md`.

**Tests**:
- Installer unit test with `arch = Some("x86_64-unknown-linux-gnu")` and artifacts present in both `<triple>/debug/` and `debug/` → the triple-qualified one is installed (mirrors `cargo-ament-build`'s `test_install_binaries_with_arch`).

---

### Subphase 7.4: Install library artifacts

**Objective**: Install `cdylib`/`staticlib`/`dylib` outputs to `install/<pkg>/lib/<pkg>/`, matching the reference.

**Design**:

- For each library target from 7.1, probe the prefix/suffix combinations in the reference's order and copy every file that exists:
  `("lib","so")`, `("lib","dylib")`, `("lib","a")`, `("","dll")`, `("","lib")`.
- Probe by file existence rather than by mapping kind → extension. That is what the reference does, it is platform-agnostic, and it tolerates a crate declaring multiple `crate-type` values.
- The file stem is `target.name.replace('-', "_")`.
- Consequence for `install()` (`ament_installer.rs:69`): library-only packages must no longer skip artifact installation. Replace the `if !is_library` guard with an unconditional call — the target list is now the thing that decides what gets copied.

**Tests**:
- Fixture producing a `cdylib` → `lib<name>.so` installed on Linux.
- Fixture with all five artifact shapes present in the build dir → all five installed (mirrors `test_install_binaries_lib_variants`).
- Pure `rlib` library → nothing installed under `lib/<pkg>/`, `install()` still succeeds.

---

### Subphase 7.5: Windows executable suffix

**Objective**: Append `.exe` to binary source and destination paths on Windows.

**Design**: `#[cfg(target_os = "windows")]` on both the `src` and `dest` path construction in `install_binaries()`, exactly as the reference does. Library artifacts need no suffix handling — `dll`/`lib` are already in the probe table from 7.4.

**Tests**: Guard the assertion with `cfg!(windows)` in the existing installer tests; CI already builds wheels for Windows, so a wheel smoke test covers the real path.

---

### Subphase 7.6: `rust_packages` ament resource marker

**Objective**: Register installed Rust packages under `share/ament_index/resource_index/rust_packages/<pkg>` in addition to the existing `packages` and `package_type` markers.

**Rationale**: Interoperability. `colcon-ros-cargo`'s `find_installed_cargo_packages()` scans `resource_index/rust_packages` to discover installed crates and build its `[patch.crates-io]` table. Without the marker, a workspace built by `colcon-cargo-ros2` is invisible to a downstream workspace built with the official plugin. We do not consume this marker ourselves (our discovery goes through `package.xml` and colcon's package augmentation), so this is purely additive and cannot regress our own path.

**Design**: One more `fs::write` in `create_markers()` (`ament_installer.rs:108`), following the existing pattern.

**Tests**: Extend the existing marker test to assert the third path exists.

---

### Subphase 7.7: Toolchain preflight check

**Objective**: Fail with an actionable message when the PyO3 extension module is unusable, rather than surfacing an opaque import error mid-build.

**Design**: `_prepare_workspace_bindings()` (`task/ament_cargo/build.py:89`) already checks `cargo_ros2_py.__version__` and prints an install hint. Extend it to also verify that a `cargo` executable is on `PATH` before the build is attempted, reusing `colcon_cargo.task.cargo.CARGO_EXECUTABLE` semantics (locate via `shutil.which`) and erroring with the toolchain-install instruction. This is the analogue of the reference's `cargo ament-build --help` probe in `AmentCargoPackageIdentification`, adapted to our in-process design where there is no external subcommand to probe.

**Non-goal**: We do not add a `colcon_core.package_identification` extension. Identification is already handled by `colcon-ros` dispatching on `<build_type>ament_cargo</build_type>`, which is why our entry points are keyed `ros.ament_cargo`. Registering a competing identifier would conflict with `colcon-ros-cargo` if both are installed.

---

### Compatibility and risk

| Change | Risk | Mitigation |
|---|---|---|
| 7.1 target discovery | Behavior change for crates whose `Cargo.toml` the old scanner misparsed — some will now install *more* binaries | This is the intended fix; call it out in the changelog |
| 7.2 feature filtering | A binary previously installed by luck (feature enabled by default) could now be skipped if the default-feature closure is computed wrongly | Resolve the closure from `root_package.features` in Rust, not from string parsing in Python; test the `default` chain explicitly |
| 7.4 library install | Library-only packages now write into `lib/<pkg>/` where they previously wrote nothing | Additive; no existing file is overwritten |
| 7.6 `rust_packages` marker | None — additive marker | — |

Removing `is_library_package()` changes a public function in `packages/cargo-ros2/src/ament_installer.rs`. The crate is not published to crates.io (only the wheel ships its binaries), so this is not a semver break for external users, but bump the workspace version anyway.

---

### Exit criteria

- [ ] `src/bin/*.rs` binaries install without an explicit `[[bin]]` section
- [ ] `required-features`-gated binaries install iff their features were enabled
- [ ] `--target <triple>` builds install from `build_base/<triple>/<profile>`
- [ ] `cdylib`/`staticlib` artifacts install to `lib/<pkg>/`
- [ ] Windows binaries install with `.exe`
- [ ] `resource_index/rust_packages/<pkg>` marker written
- [ ] Missing `cargo` produces an actionable error before the build starts
- [ ] `just quality` clean: nightly rustfmt, zero clippy warnings, all tests pass
- [ ] `testing_workspaces/complex_workspace` and `testing_workspaces/my_robot_node` build and run after `just clean && just build`

---

### References

- [`cargo-ament-build`](https://github.com/ros2-rust/cargo-ament-build) — `src/lib.rs` `install_binaries()`, `install_package()`, `create_package_marker()`
- [`colcon-ros-cargo`](https://github.com/colcon/colcon-ros-cargo) — `task/ament_cargo/build.py` `find_installed_cargo_packages()`, `package_identification/ament_cargo.py`
- [`rosidl_rust`](https://github.com/ros2-rust/rosidl_rust) — `rosidl_generator_rs`, the CMake-time generator our design deliberately avoids
- [ament resource index design](https://github.com/ament/ament_cmake/blob/master/ament_cmake_core/doc/resource_index.md)
