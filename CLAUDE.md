# colcon-cargo-ros2: Development Guide

**Build Rust ROS 2 packages with automatic message binding generation.**

This file contains development instructions for Claude Code. User documentation is in [README.md](README.md).

## Repository Structure

```
colcon-cargo-ros2/  (THIS REPOSITORY)
├── .envrc                        # Automatic ROS 2 environment setup (direnv)
├── CLAUDE.md                     # This file (development instructions)
├── README.md                     # User-facing documentation
├── justfile                      # Build automation (dual workspace + Python)
│
├── # USER-FACING LIBRARIES (requires ROS 2)
├── user-libs/
│   ├── Cargo.toml                # Workspace manifest
│   ├── rclrs/                    # ROS 2 client library for Rust
│   └── rosidl-runtime-rs/        # Runtime library for ROS messages
│
├── # BUILD INFRASTRUCTURE (no ROS required)
├── packages/                     # Renamed from build-tools/
│   ├── Cargo.toml                # Workspace manifest
│   ├── rosidl-parser/            # ROS IDL parser (.msg, .srv, .action)
│   ├── rosidl-codegen/           # Code generator with Askama templates
│   ├── rosidl-bindgen/           # Binding generator
│   ├── cargo-ros2/               # Build orchestrator (pre-build + post-build)
│   └── colcon-cargo-ros2/        # Python/PyO3 colcon extension
│       ├── colcon_cargo_ros2/    # Python module
│       ├── cargo-ros2-py/        # Rust library exposed to Python
│       └── test/                 # Python tests
│
└── .github/workflows/            # CI for both workspaces
```

## Development Workflows

### Environment Setup

**Automatic (Recommended)**:
```bash
# Install direnv: https://direnv.net/
direnv allow  # Once - permits .envrc to run
# ROS 2 environment now automatically loads when entering directory
```

**Manual**:
```bash
source /opt/ros/jazzy/setup.bash  # Or humble, iron, etc.
```

### Quick Build Commands

```bash
# Build everything (both workspaces + Python wheel)
just build

# Test everything
just test

# Format and lint all code
just format
just check

# Full quality check (format + lint + test)
just quality

# Clean everything
just clean
```

### Workspace-Specific Commands

```bash
# Build-tools/packages workspace (no ROS required)
just build-build-tools
just test-build-tools
just clean-build-tools

# User-libs workspace (requires ROS)
just build-user-libs
just test-user-libs
just clean-user-libs

# Python package
just build-python
just install-python
just install  # Install from wheel
```

### Version Management

```bash
# Bump version in both pyproject.toml and Cargo.toml
just bump-version 0.4.0

# Output:
# ✓ Updated packages/colcon-cargo-ros2/pyproject.toml
# ✓ Updated packages/colcon-cargo-ros2/Cargo.toml
# Next steps:
#   1. Review changes: git diff
#   2. Commit: git add -u && git commit -m 'Bump version to 0.4.0'
#   3. Tag: git tag v0.4.0
#   4. Build: just build-python
```

### Development Cycle

**CRITICAL**: After modifying code, rebuild and reinstall to see changes:

#### Template Changes (Most Important!)

Askama embeds templates at compile time. Simply rebuilding isn't enough - you must **clean and reinstall**:

```bash
# REQUIRED after modifying .jinja templates
just clean-build-tools   # Clear build cache (forces template re-embedding)
just build-python        # Rebuild wheel (includes cargo-ros2 + rosidl-bindgen)
just install             # Install wheel with all tools
```

**Why this matters**:
- Templates in `build-tools/rosidl-codegen/templates/*.jinja` are embedded into `rosidl-bindgen` at compile time
- Without cleaning, Askama may use cached template artifacts
- The Python wheel bundles the Rust code as one PyO3 extension module (`cargo_ros2_py`), so `just build-python` rebuilds everything. It ships no binaries: the `cargo ros2` subcommands are source-checkout only, and installed users reach the same operations through the `colcon-cargo-ros2` console script
- Without reinstalling, the old wheel continues to be used

#### Code Changes (Less Critical)

For regular code changes (not templates):

```bash
just build-python    # Rebuild wheel
just install         # Install updated wheel
```

#### Testing Workspaces

**IMPORTANT**: Always use `just clean && just build` in testing workspaces:

```bash
# Testing workspaces, each with a justfile (build / verify / clean / install-deps):
# - testing_workspaces/interfaces/   every IDL shape
# - testing_workspaces/layouts/      every workspace shape
# - testing_workspaces/scenarios/    the failure modes
# - testing_workspaces/upstream/     third-party packages (manual)

# From the repository root: build and verify the base tier
just test-workspaces

# Install ROS dependencies first (only the heavy tier needs any)
just install-deps

# Build
just clean && just build

# ❌ NEVER use: rm -rf build install && colcon build
# ✅ ALWAYS use: just clean && just build
```

**Rationale**:
- Justfiles provide consistent build commands across all testing workspaces
- `install-deps` runs `rosdep install` to pull required ROS packages
- They handle proper cleanup and environment setup
- They abstract away colcon-specific details
- They make build commands shorter and easier to remember

#### Quick Development Cycle

```bash
# 1. Make changes to code/templates

# 2. Clean and rebuild (if templates changed)
just clean-build-tools
just build-python
just install

# 3. Test changes in the workspaces
just test-workspaces

# 4. Verify results
```

**Common Mistake**: Forgetting to rebuild the wheel after making changes. The Rust code reaches Python as a compiled extension module, so changes won't take effect until you run `just build-python && just install`.

### `[package.metadata.ros]` Installation Support (2025-11-17)

**Problem**: Rust ROS 2 packages often need to install additional files beyond binaries (launch files, config files, URDF models, RViz configs, etc.), but there was no way to specify these in Cargo.toml.

**Solution**: Implemented `[package.metadata.ros]` support for installing additional files and directories, providing 100% backward compatibility with cargo-ament-build:

```toml
[package.metadata.ros]
install_to_share = ["launch", "config", "README.md"]  # Directories and files
install_to_include = ["include", "mylib.h"]
install_to_lib = ["scripts", "setup.sh"]
```

**Semantics**:
- **Directories**: Copied recursively with name preserved
  - `"launch"` → `install/<pkg>/share/<pkg>/launch/`
- **Individual files**: Filename preserved (parent path dropped)
  - `"README.md"` → `install/<pkg>/share/<pkg>/README.md`
  - `"config/params.yaml"` → `install/<pkg>/share/<pkg>/params.yaml`
- **Missing paths**: Build fails with clear error message showing expected location

**Destination Mapping**:
- `install_to_share` → `install/<pkg>/share/<pkg>/`
- `install_to_include` → `install/<pkg>/include/<pkg>/`
- `install_to_lib` → `install/<pkg>/lib/<pkg>/`

**Files Modified**:
- `packages/cargo-ros2/src/ament_installer.rs`:
  - Enhanced `install_metadata_ros_files()` documentation with examples
  - Changed from "warn on missing" to "error on missing" for stricter validation
  - Already supported both directories and files (via `.is_dir()` check)
- `packages/cargo-ros2/tests/test_metadata_ros.rs`: Added 4 new integration tests
  - `test_metadata_ros_install_individual_file_to_share`
  - `test_metadata_ros_install_individual_file_to_include`
  - `test_metadata_ros_install_individual_file_to_lib`
  - `test_metadata_ros_mixed_files_and_dirs`
- `tmp/metadata_ros_recommendation.md`: Updated design documentation

**Benefits**:
- ✅ 100% backward compatible with cargo-ament-build
- ✅ Simple and declarative (no complex installation scripts)
- ✅ Supports common ROS 2 use cases (launch, config, urdf, rviz, meshes)
- ✅ Works with both standalone packages and colcon workspaces
- ✅ Clear error messages for missing paths

**Test Coverage**:
- ✅ All 206 tests passing (203 Rust + 3 Python)
- ✅ Test coverage for directories, individual files, and mixed usage
- ✅ Zero clippy warnings

**Example Usage**:
```toml
[package]
name = "my_robot_controller"
version = "0.1.0"

[[bin]]
name = "controller_node"

[package.metadata.ros]
install_to_share = ["launch", "config", "urdf", "README.md", "LICENSE"]
```

Results in:
```
install/my_robot_controller/
├── lib/my_robot_controller/
│   └── controller_node
└── share/my_robot_controller/
    ├── launch/            # Directory preserved
    ├── config/            # Directory preserved
    ├── urdf/              # Directory preserved
    ├── README.md          # Individual file
    ├── LICENSE            # Individual file
    ├── rust/              # Source code (automatic)
    └── package.xml        # Metadata (automatic)
```

---

## Recent Architectural Improvements (2025-11-14)

### Constant Type Fix and Colcon Package Discovery (2025-11-14)

**Problem 1**: String constants in generated code used non-const-compatible types (`String`, `rosidl_runtime_rs::String`), causing compilation errors:
```rust
pub const PASSWORD: String = "hunter2";  // ❌ Cannot initialize const String with &str
```

**Solution**: Created `rust_type_for_constant()` function that returns `&'static str` for all string types in constants:
```rust
pub const PASSWORD: &'static str = "hunter2";  // ✅ Const-compatible
```

**Files Modified**:
- `build-tools/rosidl-codegen/src/types.rs`: Added `rust_type_for_constant()` function
- `build-tools/rosidl-codegen/src/generator.rs`: Updated all constant generation (messages, services, actions) to use new function

---

**Problem 2**: Workspace binding generator hardcoded directory names `["src", "ros"]`, failing to discover packages in non-standard locations (e.g., `rclrs/`):
```python
for src_dir_name in ["src", "ros"]:  # ❌ Doesn't respect colcon's discovery
    src_dir = self.workspace_root / src_dir_name
```

**Solution**: Implemented `PackageAugmentationExtensionPoint` to leverage colcon's native package discovery:
- Receives ALL discovered packages from colcon (respects `--base-paths`, `--packages-select`, etc.)
- Runs after package discovery, before build tasks start
- Eliminates fragile filesystem scanning
- Properly integrates with colcon's architecture

**Files Created**:
- `build-tools/colcon-cargo-ros2/colcon_cargo_ros2/package_augmentation/__init__.py`: New extension point

**Files Modified**:
- `build-tools/colcon-cargo-ros2/colcon_cargo_ros2/workspace_bindgen.py`: Uses packages from `RustBindingAugmentation._interface_packages`
- `build-tools/colcon-cargo-ros2/pyproject.toml`: Registered new `colcon_core.package_augmentation` entry point

**Benefits**:
- **Proper colcon integration**: Uses colcon's package discovery instead of re-implementing it
- **Respects user configuration**: Works with `--base-paths`, `--packages-select`, and other colcon flags
- **No hardcoded paths**: Eliminates fragile directory name assumptions
- **Architecturally correct**: `PackageAugmentationExtensionPoint` is designed for workspace-level operations

---

## Recent Architectural Improvements (2025-11-11)

### Dual Workspace Architecture (2025-11-11)

Split the project into two independent Cargo workspaces to separate concerns:

**Problem**: Building `rclrs` requires ROS 2 environment (`ROS_DISTRO`, sourced setup.bash), which made it impossible to run `cargo check` on build tools without ROS installed.

**Solution**: Separate user-facing libraries from build infrastructure:

```
user-libs/          # Requires ROS 2 environment to build
├── rclrs/          # ROS 2 client library (Node, Publisher, Subscription)
└── rosidl-runtime-rs/  # Runtime support (Message trait, Sequence, String)

build-tools/        # No ROS required - can develop without ROS installed!
├── rosidl-parser/      # IDL parsing logic
├── rosidl-codegen/     # Code generation with templates
├── rosidl-bindgen/     # Binding generator
├── cargo-ros2/         # Build orchestrator
└── colcon-cargo-ros2/  # Python/PyO3 colcon extension
```

**Benefits**:
- **Independent development**: Can work on build tools without ROS environment
- **Faster CI**: Build tools workspace checks don't require ROS installation
- **Clear separation**: User-facing APIs separate from build infrastructure
- **Uses crates.io**: `rclrs` and `rosidl-runtime-rs` are published on crates.io for easy distribution

**New justfile commands**:
```bash
# 1. Make changes to code/templates

# 2. Clean and rebuild (REQUIRED for template changes)
just clean-build-tools   # Only needed if templates changed
just build-python        # Rebuild wheel (includes all Rust tools)
just install             # Install updated wheel

# 3. Test in a workspace
cd ~/test_workspace
rm -rf build/.colcon build/ros2_bindings
colcon build --packages-select <package>
```

**Why**: Templates are embedded at compile time, inside the extension module the wheel ships. Must reinstall to use updated tools.

## Key Development Guidelines

### File Manipulation Rules

**CRITICAL: ALWAYS use Write/Edit tools for file operations**

- ❌ **NEVER**: `cat > file`, `echo > file`, `cat <<EOF`, or any shell redirection
- ✅ **ALWAYS**: Use `Write` tool to create files
- ✅ **ALWAYS**: Use `Edit` tool to modify files
- ✅ **ALWAYS**: Use `Read` tool to view files

**Exception**: Bash is only for system commands (git, cargo, colcon, etc.), not file I/O.

### Temporary Files

**IMPORTANT**: Only create temporary files when explicitly requested by the user.

**When temporary files ARE needed** (user explicitly requests them or they're necessary for task execution):
- Create them in `$PROJECT_ROOT/tmp/` using the Write tool
- Examples: test scripts, temporary test data, exploration/debugging files

**Do NOT create**:
- Summary documents or notes in `tmp/` unless user explicitly asks
- Documentation files to record work completed
- Session reports or progress tracking files

**Rationale**: The `tmp/` directory should remain clean and only contain files that are truly temporary or explicitly requested by the user. Work summaries and documentation should be communicated directly to the user, not written to files.

```bash
# ✅ CORRECT - User requested a test script
Write: tmp/test_build.sh
Content: |
  #!/bin/bash
  cargo build --release

Bash: chmod +x tmp/test_build.sh && tmp/test_build.sh

# ✅ CORRECT - Needed for debugging
Write: tmp/test_data.json
Content: {"key": "value"}

# ❌ WRONG - Unsolicited summary document
Write: tmp/session_summary.md
Content: |
  # Session Summary
  Today we completed...

# ❌ WRONG - Using bash redirects instead of Write tool
Bash: echo '{"key": "value"}' > /tmp/test_data.json
Bash: cat > tmp/test.sh <<'EOF'
  #!/bin/bash
  ...
EOF
```

### Code Quality

**REQUIRED before committing**:
```bash
just quality  # Format + lint + test
```

Ensures:
- Code formatted with nightly rustfmt
- All clippy warnings fixed (`-D warnings`)
- All tests pass (Rust + Python)
- Zero warnings

## Recent Architectural Improvements

### One Source for the Version (2026-08-15)

**Problem** (issue #5): `colcon_cargo_ros2.__version__` was a literal that `just bump-version` did not rewrite, so it reported 0.2.0 while pyproject.toml and Cargo.toml were on 0.4.1 — wrong across several releases, and read by the skew guard the build task uses.

**Solution**: `colcon_cargo_ros2/_version.py` resolves it once (source `pyproject.toml`, falling back to installed distribution metadata); `__init__.py` exposes it through a PEP 562 `__getattr__`, so there is no literal left to drift. The build task's `python_package_version()` now delegates there instead of carrying its own copy.

**Guard**: `just bump-version` verifies pyproject.toml and Cargo.toml agree after rewriting them, and fails if a `sed` missed one — the failure mode that produced the drift. A test asserts `__version__` equals the pyproject version, which would have caught it years earlier.

**Removed**: `setup.py` and `setup.cfg`. Nothing built through them — the backend is maturin — and `setup.cfg` carried a fourth version declaration plus metadata from the upstream project this was forked from (wrong author, wrong URLs, dependencies on `cargo-ament-build`). `stdeb.cfg` is left alone; it is inert without `setup.py`, so decide what to do with it when Debian packaging comes up.

---

### Console Scripts for the CLI Operations (2026-08-15)

**Problem** (issue #4): `packages/cargo-ros2` builds a `cargo-ros2` binary with `bindgen`, `install`, `clean` and `doctor` subcommands, but the wheel contains only the PyO3 extension module. Anyone who installed from PyPI had none of them, while CLAUDE.md claimed the wheel "bundles the Rust tools".

**Solution**: `colcon_cargo_ros2/cli.py`, a thin argparse shell over the same PyO3 functions the build task calls, exposed as two console scripts:

| Command | Equivalent |
|---|---|
| `colcon-cargo-ros2 bindgen\|install\|clean\|doctor` | `cargo ros2 <subcommand>` |
| `colcon-cargo-ros2-doctor` | `cargo ros2 doctor` — its own name because it is what gets typed while a build is failing |

Shipping the Rust binary itself was the alternative. It would mean building the binary separately and placing it in the wheel's `data/scripts/`, for all 30 platform/version wheels, to duplicate functionality the extension module already exposes.

**Verified** by installing the built wheel into a clean venv and running both commands.

---

### rosidl_runtime_rs Version Derived From the Workspace (2026-08-15)

**Problem** (issue #3): generated crates pinned `rosidl_runtime_rs = "0.6"` regardless of what the workspace needed. `rclrs` decides that version — 0.6 requires runtime 0.5, 0.7 requires 0.6 — and cargo treats 0.5 and 0.6 as incompatible, so a mismatch left *both* in the graph. Every message type then failed a trait bound it appeared to satisfy:

```
error[E0277]: the trait bound `std_msgs::msg::String: MessageIDL` is not satisfied
note: there are multiple different versions of crate `rosidl_runtime_rs` in the dependency graph
```

**Solution**: `_detect_runtime_version()` derives it from the workspace's own packages — an explicit `rosidl_runtime_rs` requirement if present, else the version implied by `rclrs` via `RCLRS_RUNTIME_VERSIONS`. Conflicting requirements are reported by package name; `--rosidl-runtime-rs-version` still overrides.

**A version range does not work.** `>=0.5, <0.7` was tried first: cargo resolves it to the greatest match (0.6) rather than unifying with the 0.5 the rest of the graph uses, because 0.x minors are separate compatibility buckets. It has to be the exact version.

**Also**: the freshness stamp now covers the runtime version, or changing it would reuse crates pinned to the old one; `rclrs = "*"` is reported as unmatchable, since cargo resolves it to whatever is newest.

**Result**: `testing_workspaces/upstream` — third-party examples written against rclrs, which this project does not generate — builds 7 of 8 packages unaided. The eighth declares `rclrs = "*"`.

**Files**: `colcon_cargo_ros2/workspace_bindgen.py`, `packages/rosidl-bindgen/src/generator.rs`, `test/test_runtime_version.py`, `testing_workspaces/upstream/`.

---

### Diagnosable Builds — Phase 8 (2026-08-14)

**Problem**: A missing `[patch.crates-io]` entry is not an error to cargo — it resolves the name on the real crates.io, where ROS message crates exist as yanked uploads. Every such failure was reported as `version 4.2.3 is yanked`, naming a registry the user never asked for.

**Solution**:

| Subphase | Change |
|---|---|
| 8.1 | Warn when a Cargo.toml interface dep has no `<depend>` tag (the old check could never fire — see below) |
| 8.2 | `_cargo_dependency_names()` follows renames (`package = "..."`), `[target.*]` tables and `workspace = true` |
| 8.3 | Bindings with no `Cargo.toml` are regenerated, never patched to |
| 8.4 | `.bindgen_manifest` in each generated crate; its `build.rs` refuses to compile stale bindings |
| 8.5 | Python/native version-skew guard with the rebuild command |
| 8.6 | `cargo ros2 doctor` — six ordered checks, fix printed for the first failure, exit 1 |
| 8.7 | Troubleshooting catalogue keyed by the misleading message |

**Non-obvious findings**:
- The pre-existing check compared Cargo.toml deps against packages discovered *from package.xml*, so an undeclared package — the only case that breaks builds — was by construction invisible to it. Detection needs independent resolution (source tree, then ament index). `doctor` has the same trap and resolves it the same way.
- Cargo resolves relative `.cargo/config.toml` paths against the directory *containing* `.cargo`. Resolving against `.cargo` reports every patched crate missing.
- Generated crates have no dependencies, so `build.rs` compares record *sets* (`path:size:mtime_ns`) rather than a hash. `_interface_records()` feeds both that manifest and colcon's sha256 stamp.
- Version skew compares `pyproject.toml` (source tree), not `dist-info`: under an editable install the recorded metadata is routinely stale.

**Escape hatches**: `COLCON_CARGO_ROS2_SKIP_STAMP_CHECK=1` skips the freshness check; a missing manifest or unreadable source directory skips it too.

**Files**: `colcon_cargo_ros2/workspace_bindgen.py`, `task/ament_cargo/build.py`, `packages/cargo-ros2/src/doctor.rs` (new), `packages/rosidl-bindgen/src/generator.rs`, `docs/troubleshooting.md`.

**Tests**: `test/test_phase8_diagnostics.py`, `test/test_dependency_validation.py`, 10 tests in `doctor.rs`, and a generator test that compiles the emitted `build.rs` with `rustc`.

---

### First-Class Manual `cargo` Workflow — Phase 9 (2026-08-14)

**Problem**: After `colcon build`, a bare `cargo build` still needed a sourced ROS environment, `cargo run` failed to find ROS shared libraries, and both `.cargo/config.toml` and cargo's `target/` sat in the user's source tree.

**Solution**: The generated `.cargo/config.toml` now carries four regions instead of two:

| Section | Added by | Purpose |
|---|---|---|
| `[patch.crates-io]` | Phase 6 | Dependency resolution |
| `[build] rustflags` | Phase 6 | `-L` search paths, now narrowed per Cargo target, plus rpath link args |
| `[build] target-dir` | 9.4 | Artifacts under `build/.cargo_target/<slug>`, out of `src/` |
| `[env]` | 9.1 | `AMENT_PREFIX_PATH` for build scripts, `force = false` |

**Non-obvious findings** (each verified on a fixture workspace):
- Cargo **overwrites** `LD_LIBRARY_PATH` for `cargo run`, so `[env]` cannot fix runtime library lookup — rpath must.
- `-Wl,-rpath` alone emits `RUNPATH`, which the loader does not apply to *transitive* libraries; ROS typesupport is exactly that, so `--disable-new-dtags` (real `RPATH`) is required on Linux.
- `install_to_ament()` takes its artifact directory from `cargo metadata`'s `target_directory`, which already honors `[build] target-dir` — redirecting the config redirects installation, no Rust change needed.

**New CLI flags**: `--no-rpath`, `--no-gitignore`.

**Files Modified**: `colcon_cargo_ros2/workspace_bindgen.py` (`_merge_section()` now backs all three marker regions), `task/ament_cargo/build.py`, `README.md`, `docs/troubleshooting.md`, `docs/phases/009-manual-cargo-workflow.md`.

**Tests**: `test/test_manual_cargo.py` (29 tests) plus updated `TestComputeRustflags`.

---

### Consolidated `.cargo/config.toml` (2026-03-02)

**Problem**: Two config files were generated — `build/ros2_cargo_config.toml` (patches + rustflags, passed via `--config`) and per-crate `.cargo/config.toml` (patches only, for IDEs). This was redundant.

**Solution**: Consolidated everything into a single `.cargo/config.toml` per Cargo workspace/crate containing both `[patch.crates-io]` and `[build] rustflags`. Removed `build/ros2_cargo_config.toml` and the `--config` flag.

**Design**:
- Per-Cargo-workspace placement (walks up from crate to find `[workspace]` in `Cargo.toml`)
- Two marker systems: patches (`# BEGIN/END colcon-cargo-ros2`) and build flags (`# BEGIN/END colcon-cargo-ros2 build flags`)
- Patch paths are relative; rustflags use absolute paths (required by Cargo)
- Comment-based markers preserve user content in existing configs
- Automatic during `colcon build` (no separate command)

**Files Modified**:
- `packages/colcon-cargo-ros2/colcon_cargo_ros2/workspace_bindgen.py`: Removed `_write_cargo_config_file()`, added `_compute_rustflags()`, `_generate_build_marker_block()`, `_merge_build_into_config()`, renamed `_write_ide_cargo_configs()` → `_write_cargo_configs()`
- `packages/colcon-cargo-ros2/colcon_cargo_ros2/task/ament_cargo/build.py`: Removed `--config` flag
- `packages/colcon-cargo-ros2/colcon_cargo_ros2/task/ament_cargo/test.py`: Removed `--config` flag
- `packages/colcon-cargo-ros2/test/test_ide_config.py`: Added `TestComputeRustflags`, `TestGenerateBuildMarkerBlock`, `TestMergeBuildIntoConfig`
- `packages/colcon-cargo-ros2/test/test_test_task.py`: Replaced `--config` tests with `test_no_config_flag`

**Testing workspaces updated**: All testing workspaces now use rclrs 0.7 (matching rosidl_runtime_rs 0.6). Added justfiles with `install-deps` recipes for rosdep.

---

### Configurable rosidl_runtime_rs Version (2026-03-02)

**Problem**: `ROSIDL_RUNTIME_RS_VERSION` was hardcoded to `"0.5"`, forcing downstream projects using rclrs 0.7 + rosidl_runtime_rs 0.6 to patch generated files after every colcon build.

**Solution**:
1. Bumped default from `"0.5"` to `"0.6"` in `ROSIDL_RUNTIME_RS_VERSION`
2. Added `--rosidl-runtime-rs-version` CLI flag for override
3. Removed unused `RCLRS_VERSION` constant

**CLI usage**:
```bash
colcon build --rosidl-runtime-rs-version 0.5  # Override for older projects
```

**Files Modified**:
- `packages/rosidl-bindgen/src/generator.rs`: Bump default, add version param to `generate_package()` and `generate_cargo_toml()`, remove `RCLRS_VERSION`
- `packages/cargo-ros2/src/lib.rs`: Add `rosidl_runtime_rs_version` to `BindgenConfig`
- `packages/colcon-cargo-ros2/src/lib.rs`: Add field to PyO3 `BindgenConfig`
- `packages/colcon-cargo-ros2/colcon_cargo_ros2/task/ament_cargo/build.py`: Add CLI argument
- `packages/colcon-cargo-ros2/colcon_cargo_ros2/workspace_bindgen.py`: Pass version to `BindgenConfig`
- `packages/rosidl-bindgen/README.md`: Updated version references and documented override
- Testing workspace Cargo.toml files: Updated to `rosidl_runtime_rs = "0.6"`

---

### Capitalized Boolean Literals Support (2025-11-18)

**Problem**: Parser failed on ROS 2 action files using capitalized boolean literals (`True`/`False`):
```
Failed to parse action: DockRobot: Unexpected token: expected Identifier, got string
```

**Root Cause**: Lexer only recognized `true`/`TRUE` and `false`/`FALSE`, but not `True`/`False`.

**Solution**: Extended lexer token definitions in `packages/rosidl-parser/src/lexer.rs`:
```rust
#[token("true")]
#[token("TRUE")]
#[token("True")]   // Added
True,

#[token("false")]
#[token("FALSE")]
#[token("False")]  // Added
False,
```

**Files Modified**:
- `packages/rosidl-parser/src/lexer.rs`

**Impact**: Successfully parses nav2_msgs actions (DockRobot, etc.) and other packages using capitalized booleans.

**Tests**: Added `lex_capitalized_boolean_literals` test.

---

### Bounded Sequence Support for Services and Actions (2025-11-23)

**Problem**: Service and action idiomatic templates generated incorrect code for bounded sequences, causing type mismatch errors:
```rust
error[E0308]: mismatched types
  --> autoware_adapi_v1_msgs/src/srv/initialize_localization_idiomatic.rs:59:23
   |
59 |                   pose: crate::rosidl_runtime_rs::Sequence::from(
   |                         ^ expected `BoundedSequence<..., 1>`, found `Sequence<_>`
   |
   = note: expected struct `BoundedSequence<geometry_msgs::msg::rmw::PoseWithCovarianceStamped, 1>`
              found struct `Sequence<_>`
```

**Root Cause**: Service and action idiomatic templates were generating `Sequence::from(...)` for all sequences, not distinguishing between bounded and unbounded sequences. The RMW layer correctly used `BoundedSequence<T, N>` for bounded sequences (e.g., `geometry_msgs/PoseWithCovarianceStamped[<=1]`), but the idiomatic layer tried to convert Vec to Sequence instead of BoundedSequence.

**Solution**: Updated service and action idiomatic templates to check for bounded sequences before regular sequences and use `BoundedSequence::try_from(...)` instead of `Sequence::from(...)`:

```jinja
{% else if field.is_bounded_sequence && field.is_primitive_sequence %}
// Vec<primitive> → BoundedSequence<primitive> (use try_from)
{{ field.name }}: crate::rosidl_runtime_rs::BoundedSequence::try_from(idiomatic.{{ field.name }}.clone()).unwrap(),
{% else if field.is_bounded_sequence && field.is_string_sequence %}
// Vec<String> → BoundedSequence<rosidl_runtime_rs::String> (use try_from)
{{ field.name }}: crate::rosidl_runtime_rs::BoundedSequence::try_from(
    idiomatic.{{ field.name }}.iter().map(|item| crate::rosidl_runtime_rs::String::from(item.as_str())).collect::<Vec<_>>()
).unwrap(),
{% else if field.is_bounded_sequence %}
// Vec → BoundedSequence conversion with element conversion for nested messages (use try_from)
{{ field.name }}: crate::rosidl_runtime_rs::BoundedSequence::try_from(
    idiomatic.{{ field.name }}.iter().map(|item| item.into()).collect::<Vec<_>>()
).unwrap(),
```

**Files Modified**:
- `packages/rosidl-codegen/templates/service_idiomatic.rs.jinja` - Added bounded sequence checks for request and response
- `packages/rosidl-codegen/templates/action_idiomatic.rs.jinja` - Added bounded sequence checks for goal, result, and feedback

**Testing**:
- Reproduced with `autoware_adapi_v1_msgs::srv::InitializeLocalization`; the shape (bounded sequences in services and actions) is now covered by `testing_workspaces/interfaces`
- Verified fix builds successfully with Autoware ADAPI messages

**Impact**: Enables proper handling of bounded sequences in services and actions, fixing compilation errors when using Autoware and other packages with bounded sequence fields.

**Note**: Message idiomatic template already had correct bounded sequence handling - this fix brings service and action templates in line with message template.

---

### Action Constant Namespace Collision Fix (2025-11-18)

**Problem**: Action constants with duplicate names across Goal/Result/Feedback sections caused compilation errors:
```rust
error[E0428]: the name `NONE` is defined multiple times
  --> nav2_msgs/src/action/dock_robot_rmw.rs:32:1
   |
13 | pub const NONE: u16 = 0;  // Result section
32 | pub const NONE: u16 = 0;  // Feedback section
   | ^^^^^^^^^^^^^^^^^^^^^^^^ `NONE` redefined here
```

**Root Cause**: All constants were generated at module top level without namespace separation.

**Solution**: Wrapped constants in separate modules in `packages/rosidl-codegen/templates/action_rmw.rs.jinja`:
```rust
// Before (conflicting)
pub const NONE: u16 = 0;  // Result
pub const NONE: u16 = 0;  // Feedback (error!)

// After (namespaced)
pub mod result_constants {
    pub const NONE: u16 = 0;
}
pub mod feedback_constants {
    pub const NONE: u16 = 0;
}
```

**Usage**:
```rust
nav2_msgs::action::rmw::dock_robot::result_constants::NONE
nav2_msgs::action::rmw::dock_robot::feedback_constants::NONE
```

**Files Modified**:
- `packages/rosidl-codegen/templates/action_rmw.rs.jinja`

**Impact**: Enables actions with duplicate constant names (common in ROS 2 ecosystem).

---

### Package Version Extraction from package.xml (2025-11-18)

**Problem**: All generated message crates used hardcoded `ROSIDL_RUNTIME_RS_VERSION` ("0.5") instead of actual ROS package version.

**Solution**:
1. Added `version: String` field to `Package` struct
2. Implemented `parse_package_version()` to extract `<version>` from package.xml
3. Updated `generate_cargo_toml()` to use package version

**Example**:
```toml
# Before (incorrect)
[package]
name = "std_msgs"
version = "0.5.0"  # Wrong - used ROSIDL_RUNTIME_RS_VERSION

# After (correct)
[package]
name = "std_msgs"
version = "5.3.0"  # Correct - from package.xml
```

**Files Modified**:
- `packages/rosidl-bindgen/src/ament.rs`
- `packages/rosidl-bindgen/src/generator.rs`

**Impact**: Generated crates now have correct semantic versions matching ROS packages.

**Tests**: Added 6 new tests for version parsing and generation.

---

### Workspace-Local Library Linking Fix (2025-11-18)

**Problem**: Rust binaries failed to link against workspace-local ROS interface libraries:
```
rust-lld: error: unable to find library -lsplat_msgs__rosidl_typesupport_c
```

**Root Cause**: Cargo `build.rs` linker search paths (`cargo:rustc-link-search`) don't propagate to downstream binaries.

**Solution**: Added `[build]` rustflags to `.cargo/config.toml`:
```toml
[build]
rustflags = [
    "-L", "native=/path/to/install/package/lib",
    "-L", "native=/opt/ros/jazzy/lib"
]
```

**Files Modified**: `packages/colcon-cargo-ros2/colcon_cargo_ros2/workspace_bindgen.py`

**Impact**: Enables workspaces with custom interface packages used by Rust packages.

---

### `[package.metadata.ros]` Installation Support (2025-11-17)

Implemented support for installing additional files (launch, config, URDF, etc.):

```toml
[package.metadata.ros]
install_to_share = ["launch", "config", "README.md"]  # Directories and files
install_to_include = ["include"]
install_to_lib = ["scripts"]
```

**Semantics**:
- **Directories**: Copied recursively with name preserved
- **Individual files**: Filename preserved (parent path dropped)
- **Missing paths**: Build fails with clear error

100% backward compatible with cargo-ament-build.

---

### WString Array/Sequence Support (2025-11-17)

Added complete WString support across all idiomatic templates to fix type mismatches in generated bindings.

---

### `--cargo-args` Support (2025-11-17)

Added ability to pass arguments to Cargo:
```bash
colcon build --cargo-args --release
colcon build --cargo-args --profile dev-release
```

---

### Ruff Linter Migration (2025-11-17)

Migrated from flake8 to ruff (10-100x faster, written in Rust, no plugin dependencies).

---

### Dual Workspace Architecture (2025-11-11)

Split into two independent workspaces:
- **user-libs/**: Requires ROS 2 environment (`rclrs`, `rosidl-runtime-rs`)
- **packages/**: No ROS required (build infrastructure)

Benefits:
- Can develop build tools without ROS installed
- Faster CI for build tools
- Clear separation of concerns

---

### Workspace-Level Shared Bindings (2025-11-07)

Bindings generated once at `build/ros2_bindings/`, shared by all packages:
```
build/ros2_bindings/std_msgs/          # Generated once, shared by all packages
build/ros2_bindings/geometry_msgs/     # Shared by all packages
build/ros2_bindings/sensor_msgs/       # Shared by all packages
```

**How it works**:
- `colcon-cargo-ros2` discovers ROS dependencies from `package.xml` **ONLY**
- Users must declare ROS interface dependencies in `<depend>` tags in package.xml
- Generates bindings to `build/ros2_bindings/<package>/` (workspace-level)
- Generates `.cargo/config.toml` with `[patch.crates-io]` + `[build] rustflags` in each Cargo workspace/crate
- Build uses plain `cargo build` (config picked up automatically from `.cargo/config.toml`)

**Important**: ROS dependencies MUST be declared in package.xml, not just Cargo.toml:
```xml
<!-- In package.xml -->
<depend>std_msgs</depend>
<depend>geometry_msgs</depend>
<depend>autoware_adapi_v1_msgs</depend>
```
```toml
# In Cargo.toml (users maintain this separately)
[dependencies]
std_msgs = "*"
geometry_msgs = "*"
autoware_adapi_v1_msgs = "*"
```

If bindings fail to generate, check that the interface package is declared in package.xml. There is a validation step that will report missing interface packages.

Benefits:
- No duplication (std_msgs generated once, not per-package)
- Faster builds - no Cargo patch overhead
- Smaller workspace - no per-package .cargo directories
- Cleaner build process - colcon-cargo-ros2 manages all binding discovery

## Testing

### Unit Tests

```bash
# All tests
just test

# Workspace-specific
just test-build-tools  # No ROS required
just test-user-libs    # Requires ROS

# Specific package
cd packages/rosidl-codegen && cargo test
```

### Integration Testing Workspaces

Real colcon workspaces that assert on what the build produced. See
[testing_workspaces/README.md](testing_workspaces/README.md).

```bash
just test-workspaces        # interfaces + layouts + scenarios (base tier)
just test-workspaces-heavy  # adds test_msgs / nav2_msgs coverage (needs rosdep)
just clean-workspaces
```

| Workspace | Proves |
|---|---|
| `interfaces/` | Every IDL shape: primitives and defaults, fixed arrays, bounded/unbounded sequences over every element type, wide strings, constants, nesting, services and actions with bounded sequences, cross-package interface references. `consumer` round-trips values and compares, so a wrong-but-compiling conversion fails |
| `layouts/` | Every workspace shape: standalone crate, Cargo workspace with disjoint member dependencies, renamed / platform-table / workspace-inherited dependency forms, crate five directories deep, hand-written `.cargo/config.toml`, `[package.metadata.ros]` installs, workspace-local interface package |
| `scenarios/` | The failure modes: undeclared dependency, stale bindings, wiped `build/`, never built, gutted crate, `--no-rpath`, `--no-gitignore`, env-free build and run, `--cargo-args`, package at the workspace root |
| `upstream/` | Third-party packages at a pinned revision — currently incompatible, see its README |

**Rules**: assert, do not merely build; anything needing `rosdep` belongs in a
heavy tier. The previous `complex_workspace` was blocked for months on an
uninstalled `moveit_msgs` precisely because it ignored the second rule.

## CI/CD

GitHub Actions workflows:
- **ci.yaml**: Rust lint/test + Python lint/test on push to main and PRs
- **wheels.yml**: Production release (triggered by `v*` tags). Runs full lint/test gate, builds 31 artifacts (30 wheels + sdist) for Linux/macOS/Windows × Python 3.8-3.13, smoke-tests one wheel per platform, then publishes to PyPI via trusted publishing
- **test-build.yml**: Quick wheel build validation on push to main and PRs

## Status

**Version**: v0.4.0 (2026-03-03)
**Progress**: 16/20 subphases (80%) | 280 tests passing (215 Rust + 65 Python) | Zero warnings
**Latest**: Rust 2024 edition, consolidated `.cargo/config.toml`, release workflow audit ✅
**Testing**: Validated with:
- autoware_carla_bridge (118 packages) ✅
- cuda_ndt_matcher (16 Autoware + standard packages) ✅
- testing_workspaces/interfaces + layouts + scenarios ✅

**Versions**:
- Rust workspace: v0.2.0 (rosidl-parser, rosidl-codegen, rosidl-bindgen, cargo-ros2), edition 2024
- Python package: v0.4.0 (colcon-cargo-ros2)
- Target: rclrs 0.7 + rosidl_runtime_rs 0.6
- Author: Lin Hsiang-Jui <jerry73204@gmail.com>

**PyPI**: 31 artifacts for Linux/macOS/Windows × Python 3.8-3.13

**Architecture**:
- Two independent workspaces (user-libs + packages)
- Workspace-level binding generation via colcon's package discovery
- Single `.cargo/config.toml` per Cargo workspace/crate (patches + rustflags, no `--config` needed)
- Rustflags-based linker search paths for workspace libraries
- Complete ament layout installation
- Package versions extracted from package.xml

**Testing Coverage**:
- **Standard ROS 2 packages**: std_msgs, geometry_msgs, sensor_msgs, nav_msgs, trajectory_msgs, builtin_interfaces
- **Navigation**: nav2_msgs with complex actions (NavigateToPose, DockRobot with capitalized booleans)
- **Motion planning**: moveit_msgs with nested dependencies
- **Control**: control_msgs, diagnostic_msgs, action_msgs
- **Custom interfaces**: Messages, services, actions with full type support
- **Autoware**: autoware_internal_debug_msgs, autoware_map_msgs, tier4_localization_msgs

**Key Features**:
- ✅ Capitalized boolean literals (`True`/`False`)
- ✅ Action constants in separate namespaces
- ✅ Correct package versions in generated crates
- ✅ Workspace-local library linking
- ✅ `[package.metadata.ros]` installation
- ✅ `--cargo-args` pass-through
- ✅ Version management with `just bump-version`
- ✅ Configurable `rosidl_runtime_rs` version (default 0.6, `--rosidl-runtime-rs-version` CLI override)
- ✅ IDE support via `.cargo/config.toml` generation (Phase 6)
- ✅ Rust 2024 edition (let chains, unsafe env var ops)

**Next**: Phase 3.4 - Enhanced Testing & Documentation
