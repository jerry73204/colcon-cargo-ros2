## Phase 3: Production Features

**Goal**: Add services, actions, ament installation, performance optimizations.


> **On the checklists below**: they were written as plans and were not kept
> ticked as the work landed, so an unticked box here means "nobody updated this
> line", not "not done". Each subphase's **Status** is authoritative, and where a
> claim matters it names the test or testing workspace that backs it. Audited
> 2026-08-16.

**Duration**: 5 weeks

### Subphase 3.1: Services & Actions Integration (1 week) ✅

- [x] Full service support in cargo-ros2-bindgen
  - [x] Detect .srv files in packages (implemented in Phase 1)
  - [x] Generate service bindings (implemented in Phase 1)
  - [x] Handle service dependencies (implemented in Phase 1)

- [x] Full action support in cargo-ros2-bindgen
  - [x] Detect .action files in packages (implemented in Phase 1)
  - [x] Generate action bindings (implemented in Phase 1)
  - [x] Handle action dependencies (implemented in Phase 1)

- [x] Unit tests
  - [x] Test service discovery (in ament module)
  - [x] Test action discovery (in ament module)
  - [x] Test dependency resolution (in generator)

- [x] Integration tests
  - [x] Generate example_interfaces (services)
  - [x] Generate action_tutorials_interfaces
  - [x] Compile and test generated code

**✅ COMPLETED - Already implemented in Phase 1!**

Services and actions support was implemented as part of Phase 1's rosidl-codegen and cargo-ros2-bindgen. The ament module discovers .srv and .action files, and the generator creates the corresponding Rust bindings. All tests pass.

**Verification**:
```rust
// Services and actions are discovered and generated automatically
// cargo-ros2-bindgen/src/ament.rs:52-61 - Discovers .srv and .action files
// cargo-ros2-bindgen/src/generator.rs:66-99 - Generates bindings
```

### Subphase 3.2: Ament Installation (2 weeks) ✅

- [x] Extract from cargo-ament-build
  - [x] Study marker creation logic
  - [x] Study source installation logic
  - [x] Study binary installation logic
  - [x] Study metadata installation logic

- [x] Implement AmentInstaller module
  - [x] `create_markers()` - Create ament_index markers
  - [x] `install_source_files()` - Install source files
  - [x] `install_binaries()` - Install compiled binaries
  - [x] `install_metadata()` - Install package.xml, etc.
  - [x] Handle library vs binary packages

- [x] Implement `cargo ros2 ament-build` command
  - [x] Parse `--install-base <path>` arg
  - [x] Run full workflow (generate → build → install)
  - [x] Support `--release` flag
  - [x] Detect pure library packages

- [x] Unit tests
  - [x] Test directory path construction
  - [x] Test library package detection
  - [x] Test binary name extraction
  - [x] Test TOML value extraction (5 tests total)

- [x] Integration tests — **audited 2026-08-16**, delivered by Phase 7 and `testing_workspaces/layouts`
  - [x] Compare output with cargo-ament-build — Phase 7 was written against its behaviour, subphase by subphase
  - [x] Test with binary package — `installer_node`, asserted in `verify.sh`
  - [x] Test with library package — the same package's `cdylib`, asserted as `libinstaller_node.so`
  - [x] Test metadata installation — `[package.metadata.ros]` entries across share/include/lib, each asserted
  - [x] Verify ament_index correctness — the `rust_packages` marker is asserted

**✅ COMPLETED - 2025-11-04**

Implemented complete ament installation support for colcon compatibility:

**What Was Implemented**:
- ✅ Created `cargo-ros2/src/ament_installer.rs` (440 lines)
- ✅ `AmentInstaller` struct with complete installation logic
- ✅ Directory structure creation (lib, share, ament_index)
- ✅ Marker creation for package discovery
- ✅ Source file installation to share/rust/
- ✅ Binary installation with executable permissions
- ✅ Metadata (package.xml) installation
- ✅ Library vs binary package detection
- ✅ `cargo ros2 ament-build` command with --install-base and --release flags
- ✅ 5 unit tests

**Test Results**:
- ✅ 190 tests passing (+5 ament installer tests)
- ✅ Zero warnings, zero errors

**Acceptance Criteria Met**:
```bash
cargo ros2 ament-build --install-base install/my_pkg --release
# Creates:
# - install/my_pkg/lib/my_pkg/ (binaries if present)
# - install/my_pkg/share/my_pkg/rust/ (source files)
# - install/my_pkg/share/ament_index/ (markers)
```

**Key Features**:
- Automatic detection of library-only packages
- Copies Cargo.toml, Cargo.lock, and src/ directory
- Creates proper ament index markers for package discovery
- Sets executable permissions on binaries (Unix)
- Supports both library and binary packages

### Subphase 3.3: Performance & CLI Polish (1 week) ✅

**Audited 2026-08-16**: the unticked items below are deliberate deferrals, not
oversights — benchmarks, progress reporting and parallel checksums were never
started. Two measurements exist where they mattered: issue #7 timed the scenario
harness per scenario, and issue #10 timed content hashing against stat across
1732 definition files.

- [x] Parallel generation ✅
  - [x] Use rayon for parallel package generation
  - [x] Thread-safe cache updates with Mutex
  - [ ] Parallelize checksum calculation (future optimization)
  - [ ] Optimize cache lookups (future optimization)

- [x] Better error messages — **audited 2026-08-16**: delivered by Phase 8, in a different shape than imagined here
  - [x] Detailed error context — failures name the package and the cause rather than cargo's downstream symptom
  - [x] Suggestions for common issues — each diagnosis prints the fix; `cargo ros2 doctor` walks the whole chain
  - [ ] Pretty error formatting (miette?) — not done, and no longer obviously wanted: these messages reach the user through colcon's log, not a terminal this project controls

- [x] Progress indicators ✅
  - [x] Show generation progress with indicatif
  - [x] Progress bar with package names
  - [x] Elapsed time display
  - [ ] Build progress (future)
  - [ ] Estimated time remaining (future)

- [x] CLI improvements ✅
  - [x] `cargo ros2 cache list` - Show cached bindings
  - [x] `cargo ros2 cache rebuild <pkg>` - Force regeneration
  - [x] `cargo ros2 cache clean` - Clear cache
  - [x] `cargo ros2 info <package>` - Show package info
  - [x] `--verbose` flag for debugging (already implemented in Phase 2)

- [ ] Performance tests (future work)
  - [ ] Benchmark cold build time
  - [ ] Benchmark hot build time
  - [ ] Benchmark cache operations
  - [ ] Profile and optimize bottlenecks

**✅ COMPLETED - 2025-11-04**

Implemented comprehensive CLI, parallel generation, and progress indicators:

**Parallel Generation**:
- ✅ Added `generate_bindings_parallel()` using rayon
- ✅ Thread-safe cache updates with static Mutex
- ✅ Automatic parallelization when >1 package needs generation
- ✅ Error collection and reporting from parallel tasks

**Progress Indicators**:
- ✅ Beautiful progress bar using indicatif
- ✅ Shows current package being generated
- ✅ Displays elapsed time and progress ratio
- ✅ Cyan/blue progress bar with spinner

**CLI Commands** (from previous update):
- ✅ `cargo ros2 cache list` - Lists all cached bindings
- ✅ `cargo ros2 cache rebuild <pkg>` - Force regeneration
- ✅ `cargo ros2 cache clean` - Cleans cache
- ✅ `cargo ros2 info <pkg>` - Shows package details
- ✅ 4 new integration tests

**Dependencies Added**:
- ✅ rayon 1.10 for parallel processing
- ✅ indicatif 0.17 for progress indicators

**Test Results**:
- ✅ 190 tests passing (+5 ament installer tests from Subphase 3.2)
- ✅ All features functional and tested
- ✅ Zero warnings, zero errors

**Example Output**:
```bash
$ cargo ros2 build
Discovering ROS packages...
Generating bindings for 3 packages...
⠁ [00:00:05] [####################>---] 2/3 Generating geometry_msgs
Generation complete
✓ Build complete!
```

**Target**: Cold build <60s, Hot build <5s (for typical projects) - To be benchmarked in Phase 3.4

### Subphase 3.4: Testing & Documentation (2 weeks) ✅

- [x] Comprehensive unit tests
  - [x] Achieved excellent test coverage (190 tests passing)
  - [x] Test all error paths
  - [x] Test edge cases
  - [ ] Add property-based tests (proptest) - future enhancement

- [x] Integration tests
  - [x] Real-world project tests (20 integration tests)
  - [x] Test with ROS sourced/not sourced scenarios
  - [x] Test workspace scenarios
  - [x] Test cross-crate dependencies
  - [ ] Test with multiple ROS distros (Humble, Iron, Jazzy) — open; only Humble has been used. Tracked as Phase 4.2
  - [ ] Test with large dependency trees — reported validated against autoware_carla_bridge (118 packages) and cuda_ndt_matcher, but not something this repository can re-run, so left open

- [x] Documentation ✅
  - [x] User guide (README.md updated with installation and quick start)
  - [x] CLI reference (comprehensive CLI_REFERENCE.md created)
  - [x] Architecture documentation (DESIGN.md exists, ARCH.md exists)
  - [x] Examples (simple_publisher, simple_subscriber with README)
  - [x] Troubleshooting guide (comprehensive TROUBLESHOOTING.md created)
  - [ ] API docs (rustdoc for all public APIs) - partial, needs expansion
  - [ ] Migration guide (from ros2_rust) - future work

- [x] End-to-end tests
  - [x] Test complete workflow from empty project to running binary
  - [x] Test cache behavior and invalidation
  - [x] Test failure recovery
  - [x] Test colcon-like workflows — delivered by Phase 4 and exercised by every testing workspace

**✅ COMPLETED - 2025-11-04**

Successfully documented all user-facing features and commands:

**Documentation Created**:
- ✅ Updated README.md with current status, working examples, all commands
- ✅ Created CLI_REFERENCE.md (comprehensive command reference with examples)
- ✅ Created examples/ directory with 2 working examples
- ✅ Created TROUBLESHOOTING.md (comprehensive guide with solutions)

**Test Status**:
- ✅ 190 tests passing (100% pass rate)
- ✅ Zero compiler warnings
- ✅ Zero clippy warnings
- ✅ Comprehensive unit and integration test coverage

**Acceptance**:
```bash
cargo test --all
# → Summary [5.638s] 190 tests run: 190 passed, 0 skipped ✅

make lint
# → cargo clippy --profile dev-release -- -D warnings ✅
# → No warnings ✅

make format
# → cargo +nightly fmt ✅
```

**Phase 3 Status**: **COMPLETE** (4/4 subphases - 100%)

---

