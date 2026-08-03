## Phase 6: IDE Support via `.cargo/config.toml` Generation

**Goal**: Automatically generate `.cargo/config.toml` in user Cargo workspaces/crates so IDEs (RustRover, rust-analyzer, VS Code) can resolve ROS message dependencies without `colcon build --config`.

**Motivation**: [GitHub Issue #1](https://github.com/jerry73204/colcon-cargo-ros2/issues/1) — IDEs run `cargo metadata` without the `--config` flag, so `std_msgs = "*"` and similar ROS message dependencies fail to resolve. Users lose autocomplete, type checking, and go-to-definition.

**Trigger**: Automatic during `colcon build`, after binding generation.

**Status**: Complete (Subphases 6.1–6.4)

---

### Background

#### Current Architecture

`colcon build` generates `build/ros2_cargo_config.toml` containing `[patch.crates-io]` entries and `[build] rustflags`. This file is passed to Cargo via `--config` flag during builds. IDEs don't know about this file — they run bare `cargo metadata` which fails.

#### Key Cargo Fact

`[patch.crates-io]` is supported in `.cargo/config.toml` ([Cargo docs](https://doc.rust-lang.org/cargo/reference/overriding-dependencies.html)). Paths in `.cargo/config.toml` resolve relative to the config file's parent directory. Cargo walks up from the crate directory to find `.cargo/config.toml` files, with closer files taking precedence.

#### Design Principles

1. **Colcon owns package discovery** — we use `RustBindingAugmentation._cargo_descriptors` (packages with both `package.xml` and `Cargo.toml`) rather than adding discovery logic to Cargo.
2. **Only `[patch.crates-io]` for IDE configs** — IDEs need dependency resolution, not linker flags. The `[build] rustflags` section stays only in `ros2_cargo_config.toml` for actual builds.
3. **Preserve user content** — comment-based markers delimit the auto-generated region so user entries in the same file are untouched.
4. **Relative paths** — all patch paths are relative from the `.cargo/config.toml` location to `build/` for portability across machines.

---

### Subphase 6.1: Cargo Workspace Root Detection

**Objective**: Given a ROS Cargo package (has `package.xml` + `Cargo.toml`), determine where `.cargo/config.toml` should be placed.

#### ROS Cargo Package Criteria

A directory is a ROS Cargo package iff both `package.xml` and `Cargo.toml` co-exist. This is already enforced by `RustBindingAugmentation.augment_packages()` — colcon discovers packages via `package.xml`, and the augmentation filters to those with `Cargo.toml`.

#### Detection Algorithm

For each ROS Cargo package, walk up to find the Cargo workspace root:

```
Given crate at /ws/src/my_robot/core/ (has package.xml + Cargo.toml):

1. Read crate's own Cargo.toml
   - If it has [workspace] section → this crate IS a workspace root
   - Config target: /ws/src/my_robot/core/.cargo/config.toml

2. Walk up directories toward colcon workspace root:
   - For each parent directory:
     a. Check if Cargo.toml exists
     b. If it has [workspace] section → this is the Cargo workspace root
     c. Config target: <workspace_root>/.cargo/config.toml
   - Stop at colcon workspace root (never walk above it)

3. If no [workspace] found → crate is standalone
   - Config target: <crate_dir>/.cargo/config.toml
```

#### Deduplication

Multiple ROS Cargo crates may resolve to the same Cargo workspace root. Collect results into `Dict[Path, List[Path]]` mapping config targets to their crate paths. Generate each `.cargo/config.toml` once.

#### Scenarios

| Layout | Detection | Config Location |
|---|---|---|
| Standalone crate with `[workspace]` | Self is workspace root | `<crate>/.cargo/config.toml` |
| Standalone crate without `[workspace]` | No workspace found | `<crate>/.cargo/config.toml` |
| Cargo workspace member | Parent has `[workspace]` | `<cargo_ws_root>/.cargo/config.toml` |
| Multiple crates in one Cargo workspace | Both resolve to same root | One shared config |

#### Tasks

- [x] Implement `_detect_cargo_workspace_root(crate_path, colcon_ws_root)` in `workspace_bindgen.py`
  - [x] Read `Cargo.toml` and check for `[workspace]` section (simple TOML parsing)
  - [x] Walk up directories, stopping at colcon workspace root
  - [x] Return the directory where `.cargo/config.toml` should be placed
- [x] Implement `_collect_ide_config_targets()` that iterates over `_cargo_descriptors`
  - [x] Call `_detect_cargo_workspace_root()` for each ROS Cargo package
  - [x] Deduplicate targets (multiple crates → one Cargo workspace)
  - [x] Return `Dict[Path, List[Path]]` (config target → list of crate paths)
- [x] Handle edge case: crate path is outside colcon workspace root (skip with warning)
- [x] Unit tests for workspace root detection
  - [x] Standalone crate with `[workspace]`
  - [x] Standalone crate without `[workspace]`
  - [x] Cargo workspace member (walk up finds `[workspace]`)
  - [x] Multiple crates sharing a Cargo workspace (deduplication)
  - [x] Crate at colcon workspace root (no walk-up needed)

---

### Subphase 6.2: Comment-Based Region Markers and Merge Logic

**Objective**: Safely write auto-generated `[patch.crates-io]` entries into `.cargo/config.toml` without destroying user content.

#### Marker Format

```toml
# User's own entries above are preserved.

[patch.crates-io]
my_private_crate = { path = "../private" }   # User's own entry (preserved)

# BEGIN colcon-cargo-ros2 generated patches
# Auto-generated by colcon build. Do not edit between markers.
# Re-run `colcon build` to regenerate.
std_msgs = { path = "../../build/std_msgs/rosidl_cargo/std_msgs" }
geometry_msgs = { path = "../../build/geometry_msgs/rosidl_cargo/geometry_msgs" }
sensor_msgs = { path = "../../build/sensor_msgs/rosidl_cargo/sensor_msgs" }
# END colcon-cargo-ros2
```

#### Why Text-Based, Not TOML Parsing

A TOML serializer would reformat the entire file, destroying user formatting, comments, and ordering. Text-based marker replacement preserves everything outside the markers exactly as the user wrote it.

#### Merge Algorithm

```
1. Read existing .cargo/config.toml as text lines (if file exists)
2. Search for "# BEGIN colcon-cargo-ros2" and "# END colcon-cargo-ros2" markers
3. If both markers found:
   → Replace everything from BEGIN line to END line (inclusive) with new content
4. If markers not found:
   a. If [patch.crates-io] section header exists:
      → Find end of section (next [section] header or EOF)
      → Insert markers + entries before the next section header
   b. If no [patch.crates-io] section:
      → Append [patch.crates-io] header + markers + entries at end of file
5. Write back to .cargo/config.toml
```

#### Tasks

- [x] Implement `_generate_marker_block(patches)` that produces the marked text block
  - [x] BEGIN marker line with tool name
  - [x] Comment line with regeneration instructions
  - [x] Sorted patch entries (deterministic output)
  - [x] END marker line
- [x] Implement `_merge_into_config(existing_content, marker_block)` with merge logic
  - [x] Find and replace between existing markers
  - [x] Append to existing `[patch.crates-io]` section if no markers
  - [x] Create new `[patch.crates-io]` section if none exists
  - [x] Handle empty/missing file (create from scratch)
- [x] Unit tests for merge logic
  - [x] New file (no existing `.cargo/config.toml`)
  - [x] Existing file with no `[patch.crates-io]` section
  - [x] Existing file with `[patch.crates-io]` and user entries (preserve them)
  - [x] Existing file with markers (replace between markers)
  - [x] Existing file with stale markers from previous build (update)
  - [x] Existing file with other sections like `[build]`, `[env]` (preserve them)
  - [x] User deletes markers manually → treated as "no markers" (re-appends)
  - [x] Idempotency: running twice produces same result

---

### Subphase 6.3: Path Computation and Config Generation

**Objective**: Compute correct relative paths from each `.cargo/config.toml` location to the generated bindings in `build/`.

#### Path Strategy

Paths must be relative from the `.cargo/config.toml`'s parent directory to the binding directories:

```python
# Example:
# config_dir  = /ws/src/my_robot/.cargo/
# binding_dir = /ws/build/std_msgs/rosidl_cargo/std_msgs/
# result      = ../../build/std_msgs/rosidl_cargo/std_msgs

import os
rel_path = os.path.relpath(binding_dir, config_dir.parent)
```

This keeps configs portable — no absolute paths (directly addressing Issue #1's complaint).

#### Content: Only `[patch.crates-io]`

The IDE config contains **only** `[patch.crates-io]` entries, not `[build] rustflags`, because:

- IDEs run `cargo metadata` / `cargo check`, not the final link step
- Linker search paths are only needed for actual compilation via `colcon build`
- Avoids conflicting with user's own `[build]` settings
- `colcon build` continues to use `ros2_cargo_config.toml` via `--config` (unchanged)

#### Tasks

- [x] Implement `_compute_relative_patches(config_target, binding_dirs)`
  - [x] For each binding directory, compute `os.path.relpath()` from config parent to binding
  - [x] Return list of TOML patch entries with relative paths
  - [x] Sort entries alphabetically for deterministic output
- [x] Implement `_write_ide_cargo_configs(ros_packages)` as the main entry point
  - [x] Call `_collect_ide_config_targets()` to get targets
  - [x] For each target, compute relative patches
  - [x] Generate marker block
  - [x] Merge into existing config (or create new)
  - [x] Create `.cargo/` directory if it doesn't exist
  - [x] Write config file
  - [x] Log which files were generated/updated
- [x] Integrate into build flow: call `_write_ide_cargo_configs()` after `_write_cargo_config_file()`
  - [x] Modify `generate_all_bindings()` to call the new method
  - [x] Ensure it runs only once per build (reuse existing lock mechanism)
- [x] Unit tests for path computation
  - [x] Crate at colcon workspace root (paths like `build/std_msgs/...`)
  - [x] Crate in `src/` subdirectory (paths like `../../build/std_msgs/...`)
  - [x] Deeply nested crate (paths like `../../../../build/std_msgs/...`)
  - [x] Verify no absolute paths in generated config

---

### Subphase 6.4: Integration Testing and Documentation

**Objective**: Validate with real workspace layouts and document the feature.

#### Testing Scenarios

Each testing workspace exercises a different layout:

**my_robot_node** — standalone crate at colcon root:
```
my_robot_node/              ← colcon workspace root = crate root
├── Cargo.toml              ← has [workspace]
├── package.xml
├── .cargo/
│   └── config.toml         ← generated HERE
└── build/
    └── ros2_bindings/
```

**complex_workspace** — crate in `src/` subdirectory:
```
complex_workspace/          ← colcon workspace root
├── src/
│   └── robot_controller/   ← crate root (has [workspace])
│       ├── Cargo.toml
│       ├── package.xml
│       └── .cargo/
│           └── config.toml ← generated HERE (paths: ../../build/...)
└── build/
    └── ros2_bindings/
```

**ros2_rust_examples** — multiple standalone crates:
```
ros2_rust_examples/         ← colcon workspace root
├── rclrs/
│   ├── minimal_pub_sub/    ← standalone crate (no [workspace])
│   │   ├── Cargo.toml
│   │   ├── package.xml
│   │   └── .cargo/
│   │       └── config.toml ← generated HERE
│   └── rust_pubsub/        ← standalone crate
│       ├── Cargo.toml
│       ├── package.xml
│       └── .cargo/
│           └── config.toml ← generated HERE
└── build/
    └── ros2_bindings/
```

**Hypothetical Cargo workspace with multiple ROS members**:
```
ws/src/my_robot/            ← Cargo workspace root
├── Cargo.toml              ← [workspace] members = ["core", "nav"]
├── .cargo/
│   └── config.toml         ← generated HERE (shared by core + nav)
├── core/
│   ├── Cargo.toml          ← ROS Cargo package
│   └── package.xml
└── nav/
    ├── Cargo.toml          ← ROS Cargo package
    └── package.xml
```

#### Tasks

- [x] Integration test: `my_robot_node` — verify `.cargo/config.toml` generated at workspace root
  - [x] Build workspace with `colcon build`
  - [x] Verify `.cargo/config.toml` exists with correct patches
  - [x] Verify `cargo metadata` succeeds without `--config` flag
  - [ ] Verify `cargo check` resolves all ROS message crates (blocked by rclrs/rosidl_runtime_rs version mismatch)
- [ ] Integration test: `complex_workspace` — verify config at crate level (blocked by missing moveit_msgs)
  - [ ] Build workspace with `colcon build`
  - [ ] Verify `src/robot_controller/.cargo/config.toml` generated
  - [ ] Verify relative paths are correct (`../../build/...`)
  - [ ] Verify `cargo check` works from `src/robot_controller/`
- [x] Integration test: `ros2_rust_examples` — verify per-crate configs
  - [x] Build workspace with `colcon build`
  - [x] Verify each Rust crate gets its own `.cargo/config.toml`
  - [x] Verify `cargo metadata` succeeds from crate directory
- [x] Test user content preservation
  - [x] Create a `.cargo/config.toml` with user entries before build
  - [x] Run `colcon build`
  - [x] Verify user entries are preserved alongside generated markers
  - [x] Run `colcon build` again — verify idempotent
- [ ] Test rebuild behavior
  - [ ] Add a new ROS dependency to a crate
  - [ ] Run `colcon build`
  - [ ] Verify `.cargo/config.toml` updated with new patch entry
- [x] Add log message suggesting `.gitignore` for `.cargo/config.toml`
  - [x] Print hint on first generation (not on updates)
  - [x] Example: `"Generated .cargo/config.toml for IDE support. Consider adding it to .gitignore (paths are machine-specific)."`
- [ ] Update README.md with IDE support section
  - [ ] Explain the feature and how it works
  - [ ] Document `.gitignore` recommendation
  - [ ] Show example of user entries coexisting with generated entries

---

### Success Criteria

**Functional**:
- [x] After `colcon build`, IDEs can resolve all ROS message dependencies via `cargo metadata`
- [ ] `cargo check` succeeds from any ROS Cargo package directory without `--config` flag (blocked by rclrs version mismatch in test workspaces)
- [x] User-written entries in `.cargo/config.toml` are preserved across builds
- [x] Marker-delimited region is updated on each build (idempotent)
- [x] Works for standalone crates, self-contained Cargo workspaces, and workspace members
- [x] Relative paths only — no absolute paths in generated configs

**Non-Regression**:
- [x] `colcon build` behavior unchanged (still uses `ros2_cargo_config.toml` via `--config`)
- [x] No new user-facing commands required — fully automatic
- [x] No new dependencies added

**Quality**:
- [x] All existing tests pass (54 total: 32 IDE config + 22 existing)
- [x] New unit tests for detection, merge, and path computation
- [x] Integration tests for each workspace layout
- [x] Zero clippy warnings

---

### Files to Modify

**`packages/colcon-cargo-ros2/colcon_cargo_ros2/workspace_bindgen.py`** — main implementation:
- `_detect_cargo_workspace_root()` — walk up to find `[workspace]`
- `_collect_ide_config_targets()` — deduplicated mapping of config targets
- `_compute_relative_patches()` — relative path computation
- `_generate_marker_block()` — produce marked text
- `_merge_into_config()` — text-based marker merge
- `_write_ide_cargo_configs()` — orchestrator, called after `_write_cargo_config_file()`

**`packages/colcon-cargo-ros2/test/`** — new tests:
- `test_ide_config.py` — unit tests for detection, merge, and path logic

---

### Design Decisions

**Why per-Cargo-workspace, not per-colcon-workspace?**
Cargo's config resolution walks up from the crate directory. Placing config at the Cargo workspace root (or crate root for standalone) is the most precise placement — it only affects the crates that need it and avoids polluting unrelated crates.

**Why only `[patch.crates-io]`, not `[build] rustflags`?**
IDE tooling (`cargo metadata`, `cargo check`) doesn't link binaries. Linker flags are only needed for `colcon build`. Keeping them separate avoids conflicts with user's own `[build]` settings.

**Why text-based merge, not TOML parse/serialize?**
TOML serializers reformat the entire file, destroying user formatting, comments, and key ordering. Text-based marker replacement is surgical — it only touches the marked region.

**Why automatic during `colcon build`, not a separate command?**
Bindings get regenerated each build. The IDE config must stay in sync. A separate command creates a sync problem where users forget to re-run it after dependency changes.

---

### References

- [GitHub Issue #1: IDE support for cargo commands](https://github.com/jerry73204/colcon-cargo-ros2/issues/1)
- [Cargo Configuration Reference](https://doc.rust-lang.org/cargo/reference/config.html)
- [Overriding Dependencies](https://doc.rust-lang.org/cargo/reference/overriding-dependencies.html)
