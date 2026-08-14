## Phase 8: Diagnosable Builds

**Goal**: Every failure mode of the binding/patch mechanism should be reported by *this project*, naming the cause and the fix, before cargo gets a chance to report something misleading about crates.io.

**Motivation**: The workspace binding model has one structural weakness: when a required binding is absent from `[patch.crates-io]`, cargo does not fail — it happily resolves the name against the real crates.io registry, where ROS message crates exist as stale or yanked uploads. The resulting error names a registry the user never asked for, and never names the missing `<depend>` tag, the un-run `colcon build`, or the deleted `build/` directory that actually caused it. Every diagnosis in this phase turns one such misdirection into a statement of the real cause.

**Scope**: `packages/colcon-cargo-ros2` (validation, preflight), `packages/cargo-ros2` (doctor command, generated `build.rs`), `docs/troubleshooting.md`.

**Status**: Complete (8.1–8.7).

---

### Background

#### Observed failure modes

All reproduced against ROS 2 Humble with a fixture workspace of three Rust packages (one standalone crate, two members of one Cargo workspace) plus an `ament_cmake` interface package.

| Trigger | What the user sees | What it means |
|---|---|---|
| Interface package in `Cargo.toml`, no `<depend>` in `package.xml` | ``failed to select a version for the requirement `sensor_msgs = "*"` / version 4.2.3 is yanked`` | No bindings generated; name resolved on crates.io |
| `cargo build` before any `colcon build` (no `.cargo/config.toml`) | same yanked error | No patches exist at all |
| `build/` deleted, `.cargo/config.toml` kept | ``unable to update …/build/std_msgs/rosidl_cargo/std_msgs`` → ``failed to read …/Cargo.toml`` | Patch target vanished |
| `.msg` edited, plain `cargo build` | compiles happily against the previous generation | Stamp is only consulted by `colcon build` |
| Python code newer than the bundled native module | `TypeError: InstallConfig.__new__() got an unexpected keyword argument 'arch'` | Wheel not rebuilt after a source change |
| No ROS environment sourced | ``AMENT_PREFIX_PATH environment variable not set - please source ROS 2 installation first.`` (panic in `rosidl_runtime_rs` `build.rs`) | Correct message, but it comes from a dependency, not from us |

Note the blast radius of the first row: `pkg_b`'s undeclared dependency failed `pkg_c` as well, because both are members of one Cargo workspace and cargo resolves the workspace as a unit.

#### Why the old validation never fired

`_validate_cargo_dependencies()` compared Cargo.toml dependencies against `interface_packages`, the dict built *from* package.xml. An undeclared package is by construction absent from that dict, so the branch meant to catch the most common mistake could not reach it. Detection requires resolving the unknown dependency name independently — against the colcon source tree first (workspace-local message packages are not in the ament index until installed), then against installed packages.

---

### Subphase 8.1: Detect interface packages missing from package.xml — **done**

**Objective**: Warn, before cargo runs, when a Cargo.toml dependency is a ROS interface package with no `<depend>` tag.

**Design**:

- `_looks_like_interface_package(name)` resolves a name that binding generation never saw: colcon's `_all_descriptors` first (source tree, `msg/`|`srv/`|`action/` present), then `get_package_share_directory()` for installed packages.
- Undeclared set is `cargo_deps - xml_dep_names`, filtered by that predicate, so ordinary crates.io dependencies (`serde`, `rosidl_runtime_rs`) never warn.
- `_warn_once()` with a module-level `_REPORTED_MISMATCHES` set. Binding generation reruns for every package build task, so without suppression each warning repeated once per Cargo package in the workspace.
- The message states the package, the consequence (crates.io fallback, "yanked" error), and the literal `<depend>` lines to paste.

**Deliberately not fatal**: a hard error would break a workspace that legitimately consumes a message crate from crates.io. The warning is emitted before cargo's failure, which is enough to make the failure legible.

**Tests**: `test/test_dependency_validation.py` — undeclared interface package warns with the fix text, ordinary crates do not warn, declared-in-both is silent, repeated validation warns once, and `_looks_like_interface_package` over installed / non-interface / unknown / workspace-source packages.

---

### Subphase 8.2: Complete the Cargo.toml dependency reader — **done**

**Objective**: Stop missing dependency declarations that the current line-of-sight parser cannot see.

**Design**:

The validator reads only the `[dependencies]` and `[build-dependencies]` tables and treats each key as a package name. Three forms escape it:

- **Renamed dependencies** — `msgs = { package = "sensor_msgs", version = "*" }`. The key is the rename; the ROS package name is in `package`. Patches are keyed by real name, so the build works, but validation compares the wrong string.
- **Platform tables** — `[target.'cfg(unix)'.dependencies]`.
- **Workspace inheritance** — `sensor_msgs = { workspace = true }`, with the real requirement in the Cargo workspace root's `[workspace.dependencies]`.

Resolve each dependency entry to `entry.get("package", key)`, walk every `[target.*.dependencies]` table, and follow `workspace = true` to the Cargo workspace root manifest located by the existing `_detect_cargo_workspace_root()`.

**Tasks**:

- [x] Extract a `_cargo_dependency_names(cargo_toml_path)` helper returning resolved ROS package names
- [x] Handle `package = "..."` renames, `[target.*.dependencies]`, `dev-dependencies`, and `workspace = true` inheritance
- [x] Reuse it in both directions of `_validate_cargo_dependencies()`
- [x] Unit tests per form, including a rename whose key is not a ROS package name

---

### Subphase 8.3: Self-healing `.cargo/config.toml` — **done**

**Objective**: A patch entry must never point at a directory that is not there.

**Design**:

`_write_cargo_configs()` writes whatever `_collect_binding_dirs()` found and leaves earlier entries in place when a package is no longer required. Two additions:

- Before writing, drop any patch whose target directory lacks a `Cargo.toml`, and log which ones were dropped and why.
- When a package required by this build has no binding directory, regenerate it rather than emitting a dangling patch. `_assert_no_missing_bindings()` already raises in that case; the gap is the *stale config from a previous build* that a later bare `cargo build` will trip over after `build/` is cleaned.

Since the whole marker block is rewritten on every build, correctness here is about what the block contains, not about merge mechanics.

**Tasks**:

- [x] Filter `binding_dirs` on `Cargo.toml` existence at write time
- [x] Log dropped entries at warning level via `_warn_once()`
- [x] Regression test: config written for a package whose binding dir was removed contains no entry for it

---

### Subphase 8.4: Stale bindings visible to plain `cargo build` — **done**

**Objective**: A user who edits a `.msg` and runs `cargo build` directly should be told the bindings are stale, instead of compiling against the previous generation.

**Design**:

`STAMP_FILENAME` is written beside the generated crate and read only by `colcon build`. Cargo never consults it. Give the generated crate's own `build.rs` the check:

- `.bindgen_manifest` inside each generated crate: first line the interface directory, then one `path:size:mtime_ns` record per definition.
- `build.rs` re-derives those records and `panic!`s naming the package, the directory and the fix when the sets differ.
- `cargo:rerun-if-changed` for the manifest and each interface directory, so cargo reruns the script when definitions change.

**Records, not a digest**: the generated crates have no dependencies, and adding a hashing crate to every one of them to compare a checksum buys nothing — build.rs compares the record *sets* directly. That also removes any ordering question between the Python and Rust walks. The stamp colcon uses is now defined as the sha256 of the same record text (`_interface_records()`), so one function feeds both checks.

**Never fail a check it cannot judge**: no manifest, an unreadable source directory, or `COLCON_CARGO_ROS2_SKIP_STAMP_CHECK=1` skips the check rather than failing it. Bindings generated before this existed carry no manifest and keep building.

**Risk**: mtime-based records are sensitive to checkout order and copies, so a false "stale" is possible where the previous false-"fresh" was merely inconvenient — hence the escape hatch.

**Tasks**:

- [x] Write `.bindgen_manifest` (source path + records) into the generated crate
- [x] Freshness check with `rerun-if-changed` in the generated `build.rs` template
- [x] Escape-hatch env var
- [x] Unit test that the generated `build.rs` compiles (`rustc --emit=metadata`), since the wheel ships it as text
- [x] Integration test: edit a fixture `.msg`, assert plain `cargo build` fails with the re-run message

---

### Subphase 8.5: Version-skew guard between Python and the native module — **done**

**Objective**: Fail with an instruction, not a `TypeError`, when the bundled native module is older than the Python code calling it.

**Design**:

The editable install layout makes skew easy to hit: the `.pth` points at the source tree, so Python changes take effect immediately while `cargo_ros2_py*.so` stays whatever `just install` last built. Compare `cargo_ros2_py.__version__` against the Python package version in `_prepare_workspace_bindings()`, which already probes `__version__` for presence, and abort with:

```
cargo_ros2_py 0.4.0 does not match colcon-cargo-ros2 0.4.1.
Rebuild the native module:  just build-python && just install
```

**Which "Python version"**: `pyproject.toml` from the source tree, falling back to installed distribution metadata. Under an editable install those two disagree routinely — the first check written here fired immediately on a development machine whose `dist-info` said 0.4.0 while both the source tree and the native module were 0.4.1. It is the source tree that is paired with the module built alongside it.

**Never fail over a version it cannot read**: either side unknown means no mismatch.

**Tasks**:

- [x] Read the Python-side version from `pyproject.toml`, falling back to distribution metadata
- [x] Compare in `_prepare_workspace_bindings()`; abort with the rebuild instruction on mismatch
- [x] Unit tests for mismatch, match, unknown versions, and source-tree precedence

---

### Subphase 8.6: `cargo ros2 doctor` — **done**

**Objective**: One command that answers "why does my `cargo build` fail" without running a colcon build.

**Design**:

`cargo ros2 doctor` walks the chain in order and stops at the first broken link, because "3 patch targets missing" is noise when the real answer is "no `colcon build` has ever run here":

1. ROS environment sourced (`AMENT_PREFIX_PATH` set)
2. `.cargo/config.toml` findable by walking up from the crate, as cargo does
3. It carries our generated markers
4. Every patch target directory exists and contains a `Cargo.toml`
5. Every generated crate's `.bindgen_manifest` matches its interface sources
6. Every ROS interface dependency in `Cargo.toml` is declared in `package.xml`

Output is a checklist with ✓/✗ per item and the fix under each failure; exit code 1 when anything failed.

**Check 6 cannot rely on the patch table.** An undeclared package is by definition unpatched — the same blind spot 8.1 documents — so unknown dependency names are resolved against the ament index (`share/<name>/msg|srv|action` under any `AMENT_PREFIX_PATH` prefix).

**Cargo's path resolution.** Relative paths in `.cargo/config.toml` resolve against the directory *containing* `.cargo`, not `.cargo` itself. Resolving one level too deep reports every patched crate missing; the first version of check 4 did exactly that, and the unit test missed it by using absolute paths.

**Version skew (8.5) is not a doctor check**: it needs the Python side, and it already aborts the build with the same message.

**Tasks**:

- [x] `doctor` subcommand with per-check reporting and exit code
- [x] Checks 1–5 in Rust against the crate's resolved config
- [x] Check 6 reimplemented Rust-side, including renames and target tables
- [x] Prefix injection (`diagnose_with_prefixes`) so tests need no process-wide environment
- [x] Unit tests for each broken state, plus fixture-workspace verification

---

### Subphase 8.7: Troubleshooting catalogue keyed by the misleading message — **done**

**Objective**: A user who pastes cargo's error into a search box should land on the explanation.

**Design**: Extend `docs/troubleshooting.md` with one entry per row of the Background table, each quoting the error verbatim, naming the cause, and giving the fix. Cross-link from the README's manual-cargo section (Phase 9.6).

**Tasks**:

- [x] Entries for: yanked-version resolution, `unable to update <build path>`, `AMENT_PREFIX_PATH` panic, stale-bindings panic, `unable to find library -l…`, `InstallConfig` keyword `TypeError`
- [x] Note the Cargo-workspace blast radius: one member's unresolvable dependency fails every member
- [x] Lead the section with `cargo ros2 doctor`, which answers most of the entries in one command

---

### Acceptance

- Each failure mode in the Background table produces a message from this project that names the cause, ahead of any cargo output.
- `cargo ros2 doctor` reports the same diagnosis without a colcon build.
- No new warnings on a healthy workspace: the 5-package fixture (standalone crate, Cargo workspace with two members, `ament_cmake` interface package, Rust consumer of workspace-local messages) builds silently.
