## Phase 9: First-Class Manual `cargo` Workflow

**Goal**: `cargo build`, `cargo run`, `cargo test` and `cargo clippy` work from a crate directory after one `colcon build`, with no sourced environment and no flags, and the files that make that work stay out of the way.

**Motivation**: Phase 6 made dependency *resolution* work outside colcon — `cargo metadata` resolves `std_msgs` to the generated crate, so IDEs light up. What it did not make work is everything around resolution: compiling still needs `AMENT_PREFIX_PATH`, running still needs `LD_LIBRARY_PATH`, the generated config and cargo's `target/` land in the user's source tree, and the linker search paths are derived from whatever happened to exist in `install/` at the moment the config was written rather than from the dependency graph.

**Scope**: `packages/colcon-cargo-ros2/colcon_cargo_ros2/workspace_bindgen.py`, README, `docs/troubleshooting.md`.

**Status**: Complete (9.1–9.6).

---

### Background

Measured on the fixture workspace (ROS 2 Humble, cargo 1.97.1), after a successful `colcon build`:

| Action, no environment sourced | Result |
|---|---|
| `cargo build` with a warm `target/` | works |
| `cargo build` after `cargo clean` | panics in `rosidl_runtime_rs` `build.rs`: `AMENT_PREFIX_PATH environment variable not set` |
| `./target/debug/<bin>` | `error while loading shared libraries: libstd_msgs__rosidl_typesupport_c.so` |

Three experiments settle the design:

1. **`[env]` in `.cargo/config.toml` fixes the build.** With `AMENT_PREFIX_PATH = "/opt/ros/humble"` in an `[env]` table, a full `cargo clean && cargo run` compiles `rosidl_runtime_rs` and the generated crates with nothing sourced.

2. **`[env]` cannot fix the run.** Cargo overwrites `LD_LIBRARY_PATH` for `cargo run`, replacing it with its own dylib path list:
   ```
   LD_LIBRARY_PATH=Ok("…/target/debug:…/target/debug/deps:…/toolchains/stable-…/lib")
   AMENT_PREFIX_PATH=Ok("/opt/ros/humble")
   ```
   The `AMENT_PREFIX_PATH` entry survives; the `LD_LIBRARY_PATH` entry does not.

3. **`RUNPATH` is not enough; `RPATH` is.** With `-C link-arg=-Wl,-rpath,/opt/ros/humble/lib` the binary carries `RUNPATH /opt/ros/humble/lib` and still fails — on `librosidl_typesupport_c.so`, which is needed by `libstd_msgs__rosidl_typesupport_c.so`, not by the executable, and `RUNPATH` does not apply to transitive dependencies. Adding `--disable-new-dtags` emits a real `RPATH`, which does, and the binary runs with no ROS environment at all.

Two things that are *not* problems, checked so they do not get "fixed":

- Rewriting `.cargo/config.toml` on every build does not cause recompiles. Cargo fingerprints the rustflags value, not the file, and repeat builds finish in ~0.07 s per package.
- colcon and manual cargo already share one `target/` directory, because the build task runs cargo with the crate directory as CWD. Redirecting it (9.4) must preserve that sharing.

---

### Subphase 9.1: `[env]` block so a bare `cargo build` needs no sourced environment — **done**

**Objective**: Make the generated config carry the environment that generated-crate build scripts require.

**Design**:

Add a third marker region to `.cargo/config.toml`, alongside the patch and build-flag regions:

```toml
[env]
# BEGIN colcon-cargo-ros2 generated environment
AMENT_PREFIX_PATH = { value = "/home/u/ws/install/my_msgs:/opt/ros/humble", force = false }
# END colcon-cargo-ros2 environment
```

- `force = false` (cargo's default) means a sourced environment wins. That is the right precedence: an overlay workspace the user sourced must not be shadowed by a value baked at build time.
- The value is every workspace install prefix, followed by the `AMENT_PREFIX_PATH` in effect during generation (deduplicated). Composing it rather than copying the environment matters: a workspace built without `install/setup.bash` sourced would otherwise bake a value that cannot see its own message packages.
- Reuse the existing merge machinery: a third `_merge_*_into_config()` following `_merge_build_into_config()`, so user content outside the markers is preserved exactly as it is today.

**Tasks**:

- [x] `_compute_env()` returning the environment entries to bake
- [x] `_generate_env_marker_block()` and `_merge_env_into_config()`
- [x] Wire into `_write_cargo_configs()`
- [x] Unit tests mirroring the existing patch/build merge tests (fresh file, existing `[env]` section, existing markers, user entries preserved)
- [x] Fixture check: `cargo clean && cargo build` with no ROS environment sourced

---

### Subphase 9.2: `RPATH` so `cargo run` and `cargo test` work — **done**

**Objective**: Binaries built by cargo should find ROS shared libraries without `LD_LIBRARY_PATH`.

**Design**:

Extend the rustflags block with one link argument per library directory already passed as `-L native=`:

- Linux: `-C link-arg=-Wl,-rpath,<dir>,--disable-new-dtags` — `--disable-new-dtags` is required, per experiment 3: the default `RUNPATH` does not cover transitive dependencies, which is exactly how ROS typesupport libraries are linked.
- macOS: `-C link-arg=-Wl,-rpath,<dir>` (no dtags flag; `RUNPATH`/`RPATH` distinction does not exist there).
- Windows: no rpath concept; skip.

**Consequences to accept deliberately**:

- Paths are absolute and machine-specific. So are the existing rustflags, and so is everything under `build/`, so this does not change what is portable.
- The rpath is also baked into the artifacts colcon installs into `install/<pkg>/lib/<pkg>/`. That makes installed binaries runnable without the workspace's `setup.bash` too, which is a gain, but it does mean a moved or deleted workspace leaves stale paths in an installed binary. Provide `--no-rpath` to opt out.

**Tasks**:

- [x] Platform-conditional rpath link args in `_compute_rustflags()`
- [x] `--no-rpath` colcon argument threaded through to generation
- [x] Unit tests for the flag text per platform and for the opt-out
- [x] Fixture check: run `target/debug/<bin>` with `AMENT_PREFIX_PATH` and `LD_LIBRARY_PATH` unset, including a binary using workspace-local messages

---

### Subphase 9.3: Derive linker search paths from the dependency graph — **done**

**Objective**: Put the library directories a crate actually needs into its config, and only those.

**Design**:

`_compute_rustflags()` iterates `install/` and adds `-L native=<pkg>/lib` for every subdirectory that exists, plus every `AMENT_PREFIX_PATH` entry. Two problems follow: pure-binary Rust packages contribute empty `lib/` directories to the search path, and the set depends on what had been installed at the moment the writing package's build task ran, rather than on what the crate depends on.

Patches are already narrowed per Cargo target by `_select_bindings_for_target()`. Reuse that selection: for each config target, emit `-L native=` for the install `lib/` directories of its own interface dependencies (plus the system prefixes), skipping directories that contain no library files.

Keep the same failure direction as `_select_bindings_for_target()`: when attribution is unknown for any crate under a config target, fall back to today's include-everything behaviour rather than risk dropping a path.

**Tasks**:

- [x] Make `_compute_rustflags()` take the per-target interface package set
- [x] Skip `lib/` directories with no library artifacts
- [x] Preserve the unknown-attribution fallback
- [x] Unit tests: narrowed set, unknown attribution fallback, empty `lib/` skipped

---

### Subphase 9.4: Move cargo's `target/` out of the source tree — **done**

**Objective**: Stop `colcon build` from creating build artifacts inside `src/`, without losing the shared cache between colcon and manual cargo.

**Design**:

Add `target-dir` to the generated `[build]` block, pointing at a directory under the colcon build base:

```toml
[build]
target-dir = "/home/u/ws/build/.cargo_target/<cargo workspace name>"
```

Because both colcon and a manual `cargo build` read the same `.cargo/config.toml`, both keep using the same directory — the cache stays shared, it simply stops living in `src/`. One directory per config target (Cargo workspace or standalone crate) preserves cargo's own per-workspace isolation.

No installer change was needed, contrary to the original plan: `install_to_ament()` takes its artifact directory from `cargo metadata`'s `target_directory` (`lib.rs`), and cargo already resolves that against the same `.cargo/config.toml`. Redirecting the config redirects the installer with it.

**Risk**: a user who has set `CARGO_TARGET_DIR` or their own `target-dir` expects theirs to win. `CARGO_TARGET_DIR` (environment) already takes precedence over config, and a user-written `target-dir` outside our markers must not be overwritten — detect and leave it alone, logging that we did.

**Tasks**:

- [x] Emit `target-dir` in the build marker block
- [x] Confirm the installer follows it (no change required — `cargo metadata` reports the redirected directory)
- [x] Skip when the config already sets `target-dir` outside our markers
- [x] Fixture check: `colcon build` then manual `cargo build` produce no `src/**/target`, and the second command is a cache hit

---

### Subphase 9.5: Keep generated files out of version control — **done**

**Objective**: The generated `.cargo/config.toml` should not show up in `git status` in every workspace that uses this tool.

**Design**:

The config must live in the source tree — cargo discovers it by walking up from the crate — so the answer is ignoring it, not moving it. After writing a config, if the directory is inside a git work tree, ensure a marker-delimited block in the nearest `.gitignore`:

```
# BEGIN colcon-cargo-ros2
.cargo/config.toml
# END colcon-cargo-ros2
```

Idempotent, marker-based like the config merge, skipped when the entry is already ignored (`git check-ignore`), and disabled by `--no-gitignore`. Today this is a log line advising the user to do it by hand, which every workspace's author has to act on separately.

**Tasks**:

- [x] Write the ignore block when inside a git work tree and the path is not already ignored
- [x] `--no-gitignore` opt-out
- [x] Unit tests: fresh `.gitignore`, existing markers, already-ignored path, non-git directory

---

### Subphase 9.6: Document the manual workflow — **done**

**Objective**: State what works, what each generated file is for, and what to do when cargo reports something confusing.

**Design**: A README section "Working with cargo directly" covering: what one `colcon build` gives you (patches, build flags, environment, rpath), which cargo commands then work unaided, which files are generated and why they are in the source tree, that adding a message dependency means editing `package.xml` *and* `Cargo.toml` followed by `colcon build`, and that a Cargo workspace resolves as a unit so one member's unresolvable dependency fails its siblings. Cross-link the Phase 8.7 troubleshooting entries.

**Tasks**:

- [x] README section
- [x] Cross-links from `docs/troubleshooting.md`
- [x] Update Phase 6's IDE section to point at it rather than restating it

---

### Acceptance

On the fixture workspace, with no ROS environment sourced and after a single `colcon build`:

- `cargo clean && cargo build` succeeds
- `cargo run` succeeds, including for a crate using workspace-local messages
- `git status` is clean
- No `target/` directory exists anywhere under `src/`
- Each crate's config lists only the interface packages and library directories it depends on
