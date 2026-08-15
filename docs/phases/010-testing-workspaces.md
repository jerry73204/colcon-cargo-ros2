## Phase 10: Testing Workspaces That Prove the Features

**Goal**: A small number of dense workspaces, plus a scenario harness, that exercise every behaviour Phases 6–9 added — and fail loudly when one regresses.

**Motivation**: The unit suites cover the *pieces* (marker merging, flag text, record digests). Nothing covers the assembled product: whether a real colcon workspace still builds, whether patches land narrowed, whether a bare `cargo run` works with no environment sourced, whether a stale `.msg` is caught. Each of those was verified once, by hand, on a scratch fixture that no longer exists. The workspaces that do live in the repo were built for older investigations, and no CI job runs any of them.

**Scope**: `testing_workspaces/`, top-level `justfile`, `.github/workflows/`, and the docs that name the old workspace paths.

**Status**: Complete (10.1–10.7).

---

### Background

#### What was in `testing_workspaces/` before this phase

| Workspace | Tracked in git | Packages | Assessment |
|---|---|---|---|
| `complex_workspace/` | yes | `robot_interfaces` (ament_cmake), `robot_controller` (ament_cargo) | The only real coverage. Depends on `nav2_msgs`, `moveit_msgs`, `control_msgs` — so it cannot build on a stock ROS image without `rosdep install`. Phase 7 records exactly this: "blocked on `moveit_msgs` not being installed locally" |
| `my_robot_node/` | yes | one package, at the workspace root, no `src/` | Micro. Its one distinctive property — package at the colcon workspace root — is worth keeping as a case inside a larger workspace |
| `minimal_path_test/` | yes | none: no `package.xml` anywhere | Dead. A 2025 codegen reproduction, its README headed "SOLUTION FOUND". Not a workspace |
| `ros2_rust_examples/` | **no** — an untracked clone carrying its own `.git` | upstream examples | Named by the top-level `justfile` and by CLAUDE.md, but absent from a fresh checkout, so those recipes fail for everyone but the machine that cloned it |

Total tracked content across all four: 35 files.

#### What runs them

`just install-test-deps` runs `rosdep` for three of them. Nothing builds them, nothing asserts anything about the result, and no GitHub Actions workflow mentions `testing_workspaces` at all. Verification today means a human reading build output.

#### Design principles

1. **Few workspaces, each dense.** One workspace per *axis* of behaviour, not per feature. A workspace earns its place by covering a class of failures no other one can.
2. **Negative cases are scenarios, not workspaces.** A workspace that must fail cannot also be a workspace that must build. Deliberately broken states are produced by mutating a copy, so the committed tree stays green.
3. **Assertions, not output.** Every check greps for the message it expects. A regression that quietly stops warning must fail the run.
4. **The base tier runs anywhere.** Stock `ros:humble`, no `rosdep`. Heavy third-party interface packages are real coverage but must not gate the common path.

---

### Subphase 10.1: Retire what does not carry weight — **done**

**Objective**: Remove the dead workspace and fold the micro one, so the directory only holds things that are exercised.

**Design**:

- Delete `minimal_path_test/`. It contains no ROS package; the finding it documents (RMW files in `src/msg/rmw/`, `#[path]` relative to the inline module) is already implemented in the generator and belongs in the generator's own tests, not in a directory named "workspace".
- Fold `my_robot_node/` into `layouts/at_root_node` (10.3), preserving the property that makes it interesting: a package sitting at the colcon workspace root rather than under `src/`.
- Update every reference: `justfile` (`install-test-deps`), `CLAUDE.md` (three blocks), `CONTRIBUTING.md`, `testing_workspaces/README.md`, and the phase docs that name the old paths (001, 007).

**Tasks**:

- [x] Delete `minimal_path_test/`, moving its finding into a comment or test in `rosidl-codegen` if it is not already covered
- [x] Remove `my_robot_node/` once `layouts/at_root_node` builds
- [x] Update all references (`rg 'my_robot_node|minimal_path_test|complex_workspace'`)
- [x] Rewrite `testing_workspaces/README.md` around the new layout

---

### Subphase 10.2: `interfaces/` — codegen breadth — **done**

**Objective**: Prove that generated Rust compiles and behaves for every IDL shape the parser accepts.

**Design**:

Evolves `complex_workspace/`, renamed for what it covers.

| Package | Build type | Covers |
|---|---|---|
| `iface_core` | ament_cmake | Bounded and unbounded sequences (including in **services and actions**, the Autoware bug class), fixed arrays, `string`/`wstring` in every position, nested messages, constants, `@default`, capitalized `True`/`False`, constants with the same name across a action's Goal/Result/Feedback |
| `iface_deps` | ament_cmake | An interface package that depends on `iface_core`, so transitive binding generation and cross-package type references are exercised |
| `consumer` | ament_cargo | Constructs and asserts on every type from both, plus `std_msgs`, `geometry_msgs`, `sensor_msgs`, `nav_msgs`, `action_msgs`, `builtin_interfaces` — all present in a stock ROS install |
| `iface_heavy`, `consumer_heavy` | in `heavy/`, outside `src/` | `test_msgs` (the upstream IDL torture suite) and `nav2_msgs` |

**Built differently from the plan, deliberately:**

- **Shapes are defined here, not borrowed.** The plan kept using third-party packages for coverage. `iface_core` now declares every shape itself, which is what lets the base tier run on a stock image.
- **`moveit_msgs` and `control_msgs` were dropped, not quarantined.** Neither is installed on the development machine, so fixtures using them could not be verified — which is exactly how the old workspace ended up permanently blocked. Their coverage (deep nesting, many-package dependencies) is now in `iface_core/Nested` and `consumer`.
- **Heavy packages live in `heavy/`, not behind `COLCON_IGNORE`.** `colcon build --base-paths src` and `--base-paths src heavy` express the two tiers without a marker file to add and remove.
- **The same constant name in two action sections cannot be expressed.** rosidl's own C and C++ generators emit one enum per action and fail with "redeclaration of enumerator" before our generator sees the definition, so `Execute.action` uses distinct names per section and the duplicate case stays in the action template's unit tests.

`consumer` **asserts**, rather than merely compiling: round-trip a value through the RMW representation and compare, so a codegen bug that produces compiling-but-wrong conversions is caught. That is the class of bug the bounded-sequence fix addressed.

`just build` builds the base tier; `just build-heavy` removes the `COLCON_IGNORE` and runs `rosdep install` first.

**Tasks**:

- [x] Rename `complex_workspace/` → `interfaces/`
- [x] Split `robot_interfaces` into `iface_core` + `iface_deps`; extend definitions to cover the shapes above
- [x] Rewrite `robot_controller` as `consumer` with runtime assertions (non-zero exit on mismatch)
- [x] Move heavy dependencies into `heavy/`, selected by `--base-paths`
- [x] `just build`, `just build-heavy`, `just verify`, `just clean`

---

### Subphase 10.3: `layouts/` — packaging, config, install — **done**

**Objective**: Prove that patches, linker flags, environment, target-dir and installation land correctly for every workspace shape a user can present.

**Design**:

New workspace. This is the one that must always be green: it is "does a normal build still work" for every layout at once.

| Package | Layout | Covers |
|---|---|---|
| `standalone_node` | `src/standalone_node`, no `[workspace]` | The common case: config at the crate root |
| `cargo_ws/alpha`, `cargo_ws/beta` | one Cargo workspace, deliberately **disjoint** message dependencies | Patch narrowing: `alpha`'s config must not carry `beta`'s patches. Also documents the blast radius — one member's unresolvable dependency fails its siblings |
| `cargo_ws/gamma` | dependency forms: rename (`msgs = { package = "..." }`), `[target.'cfg(unix)'.dependencies]`, `workspace = true` inheritance | The three forms 8.2 added to the reader; all must build *and* produce no false warnings |
| `nested/deep/deeper/nested_node` | crate several directories below `src/` | Walk-up config detection, relative patch paths that traverse upward |
| `preset_config` | ships a `.cargo/config.toml` with user entries **and** a user `target-dir` | Marker merge preserves user content; our `target-dir` steps aside and says so |
| `installer_node` | `[package.metadata.ros]` with directories and individual files across share/include/lib; a binary behind `required-features`; a `cdylib` | Phase 7 installer parity |
| `local_msgs` | ament_cmake | A workspace-local interface package: rpath to `install/local_msgs/lib`, `-L` narrowing, and the ordering that makes its lib directory exist before consumers link |

**A package at the colcon workspace root** — `my_robot_node`'s one distinctive property — became the `package_at_workspace_root` scenario rather than a package here. Putting it in this workspace would have meant a crate whose `src/` is also the directory holding every other package, which is a shape no user has.

`just verify` asserts on the *generated configuration*, not only on a successful build:

- `standalone_node`'s config patches its own dependencies and not `beta`'s
- `preset_config` still contains the user's entries, and its `target-dir` is the user's
- no `target/` directory exists anywhere under the source tree
- `installer_node` installed the expected files under `install/installer_node/{share,include,lib}`
- the feature-gated binary is absent by default and present after `--cargo-args --features`

**Tasks**:

- [x] Create the workspace and its eight packages
- [x] `just verify` with per-case assertions on config content and install layout
- [x] Confirm a clean build emits **zero** warnings from this project

---

### Subphase 10.4: `scenarios/` — the failure modes — **done**

**Objective**: Prove that each diagnosable failure still produces its message, and each escape hatch still works.

**Design**:

A script, not a workspace. Each scenario copies `layouts/` (or a slice of it) into a scratch directory, mutates one thing, runs one command, and asserts on the output. Committed sources stay healthy.

| Scenario | Mutation | Assertion |
|---|---|---|
| `undeclared_dep` | Remove a `<depend>` tag whose package is used in Cargo.toml | Our warning names the package and prints `<depend>…</depend>`, and appears **before** cargo's yanked error |
| `undeclared_dep_renamed` | Same, via a renamed dependency | Same warning (8.2 reader) |
| `stale_bindings` | Append a field to a `.msg`, then bare `cargo build` | `bindings for X are out of date` panic naming the source directory |
| `stale_bindings_override` | Same, with `COLCON_CARGO_ROS2_SKIP_STAMP_CHECK=1` | Build succeeds |
| `wiped_build_dir` | `rm -rf build/`, keep the config, run `cargo ros2 doctor` | `Patched crates: generated crates missing for: …` |
| `never_built` | Fresh copy, no colcon build, run `doctor` | `Run colcon build in the workspace once` |
| `gutted_crate` | Delete a generated crate's `Cargo.toml`, re-run `colcon build` | Regenerated, build succeeds |
| `doctor_healthy` | Nothing | All checks ✓, exit 0 |
| `env_free_build` | `cargo clean && cargo build` with `AMENT_PREFIX_PATH` and `LD_LIBRARY_PATH` unset | Succeeds (the `[env]` block) |
| `env_free_run` | Run a binary linking workspace-local typesupport, same scrubbed environment | Runs (RPATH, applied transitively) |
| `no_rpath` | Build with `--no-rpath`, run without environment | Fails to load — proves the flag has an effect |
| `no_gitignore` | `git init` the copy, build with `--no-gitignore` | No `.gitignore` written |
| `source_tree_clean` | `git init` the copy, build normally | `git status --porcelain` empty; no `target/` under `src/` |
| `cargo_args_release` | `--cargo-args --release` | Artifacts installed from `release/`, not `debug/` |
| `version_skew` | Point the guard at a stubbed native version | Build aborts naming `just build-python && just install` |

Scenarios run against a workspace that was built once, and each starts from a copy, so the suite is order-independent and re-runnable.

**Tasks**:

- [x] `scenarios/run.sh` with one function per scenario, a shared `expect` helper, and per-scenario pass/fail output
- [x] `just scenario <name>` to run one, `just scenarios` to run all
- [x] Every scenario asserts on a specific string, never on exit status alone

---

### Subphase 10.5: `upstream/` — third-party compatibility — **done**

**Objective**: Keep the upstream-examples signal, but make it reproducible from a fresh clone.

**Design**:

`ros2_rust_examples/` is currently an untracked clone with its own `.git`, so the recipes naming it work only on the machine that created it. Replace with `upstream/` holding a `justfile` whose `fetch` recipe clones a **pinned ref** into an ignored subdirectory.

A fetch recipe rather than a submodule: a submodule makes every clone of this repository pay for third-party sources it may never build, and pins upgrades to a commit bump rather than an explicit action.

Heavy tier, never in the base CI job.

**Outcome**: the fetch tooling works, but the pinned revision does **not** build against the bindings this toolchain generates (`no associated function ... into_rmw_message`, `NestedType: SequenceAlloc` not satisfied) — the examples target the ros2-rust generator's API surface. The local commit that updates them (`35e062c`, "update rclrs dependency to 0.7") exists only in one developer's checkout and was never pushed, so it cannot be pinned. `upstream/` is therefore excluded from both aggregate recipes and documented as manual: a tier that always fails teaches nothing.

**Tasks**:

- [x] `upstream/justfile` with `fetch` (pinned ref), `build`, `clean`
- [x] `.gitignore` the fetched tree
- [x] Remove the stale `ros2_rust_examples` entries from the top-level `justfile` and CLAUDE.md

---

### Subphase 10.6: One harness across all workspaces — **done**

**Objective**: A single command that says whether the assembled product works.

**Design**:

Every workspace exposes the same four recipes — `build`, `verify`, `clean`, `install-deps` — so the top level can iterate without special cases:

```
just test-workspaces        # interfaces (base) + layouts + scenarios
just test-workspaces-heavy  # adds interfaces heavy tier + upstream
```

`verify` is where assertions live; `build` only builds. A workspace whose `verify` passes trivially is a workspace that is not pulling its weight.

**Tasks**:

- [x] Normalise the four recipes across `interfaces/`, `layouts/`, `upstream/`
- [x] Add `test-workspaces` and `test-workspaces-heavy` to the top-level `justfile`
- [x] Replace `install-test-deps` with per-workspace `install-deps` invocations that match the new set

---

### Subphase 10.7: CI — **done**

**Objective**: The base tier runs on every pull request.

**Design**:

A new job in `ci.yaml` on the `ros:humble-ros-base` container: install the wheel, then `just test-workspaces`. No `rosdep`, because the base tier depends only on what a stock image ships.

**Measured**: a full base-tier run from clean takes about **7 minutes** on the development machine, most of it the scenario harness — eleven of its fourteen scenarios need their own colcon build, because a broken state cannot be shared. `interfaces` and `layouts` together are under a minute once bindings exist.

The heavy tier runs on a schedule or behind a label, where `rosdep install` and an upstream clone are acceptable.

**Tasks**:

- [x] `workspaces` job in `.github/workflows/ci.yaml` (base tier, every PR)
- [x] Scheduled or label-gated job for the heavy tier
- [x] Cache the ROS container layer and cargo registry between runs

---

### Decisions taken

Recorded because they were open when this phase was proposed:

1. **Upstream examples: pinned fetch recipe**, not a submodule and not dropped. Keeps the only third-party-code signal without imposing it on every clone.
2. **No Autoware package.** The bug it caught — bounded sequences in service and action definitions — is a *shape*, and shapes belong in `iface_core` where they cost nothing to build. A workspace pulling `autoware_adapi_v1_msgs` would add minutes and an apt dependency to catch a class already covered. (CLAUDE.md refers to a `testing_workspaces/autoware_msgs_test` that does not exist in the tree; that reference should go.)
3. **Rename `complex_workspace` → `interfaces/`, new `layouts/`.** The current names say how big a workspace is, not what it proves. Renaming touches roughly a dozen doc references, all mechanical.

---

### Acceptance

Measured on ROS 2 Humble, from clean:

| Check | Result |
|---|---|
| `just test-workspaces` | interfaces 18/18, layouts 44/44, scenarios 30/30 |
| `just test-workspaces-heavy` | adds `test_msgs` + `nav2_msgs` coverage, 18/18 |
| Base tier without `rosdep` | yes — every dependency ships in a stock install |
| Untracked or single-package workspaces | none |
| Warnings from this project on a clean `layouts` build | none |

### What this phase found

Building the workspaces surfaced three defects that every unit suite had missed:

1. **A field-less message generated a zero-sized Rust struct** while `rosidl_generator_c` gives it a `structure_needs_at_least_one_member` byte. Anything embedding one — `test_msgs/Arrays`, which holds `Constants[3]` — read every later field from the wrong offset and segfaulted. This affects `std_msgs/Empty` and any constants-only message. Fixed in the message, service and action templates; `iface_core/Layout.msg` is the regression fixture, and the codegen suite gained a test.
2. **`cargo ros2 doctor` never reached users.** The wheel ships the PyO3 extension module and no binaries, so the command added in Phase 8.6 existed only for people who build this repository. Exposed as `cargo_ros2_py.doctor()` with a `colcon-cargo-ros2-doctor` console script.
3. **Linker search paths widened on the second build.** Sourcing `install/setup.bash` put the workspace's own prefixes on `AMENT_PREFIX_PATH`, and those bypassed the per-target narrowing — so the generated config depended on whether the workspace had been sourced. Workspace-internal prefixes are now skipped there.
