# Troubleshooting Guide

Common issues and solutions.

> **Reviewed 2026-08-16.** This guide previously used `cargo ros2 build`,
> `cache` and `info` commands that were planned and never built. Every command
> below was checked against the real CLI; see
> [cli-reference.md](cli-reference.md).

## Table of Contents

- [Environment Issues](#environment-issues)
- [Build Errors](#build-errors)
- [Cache Problems](#cache-problems)
- [Installation Issues](#installation-issues)
- [Performance Issues](#performance-issues)
- [Advanced Debugging](#advanced-debugging)

---

## Environment Issues

### "Failed to load ament index" or "AMENT_PREFIX_PATH not set"

**Symptoms**:
```
Error: Failed to load ament index (is ROS 2 sourced?)
```

**Cause**: ROS 2 environment not sourced.

**Solution**:
```bash
# Source your ROS 2 installation
source /opt/ros/humble/setup.bash

# Verify environment
echo $AMENT_PREFIX_PATH
# Should output: /opt/ros/humble

echo $ROS_DISTRO
# Should output: humble (or your distro)

# Try again
colcon build
```

**Permanent fix**: Add to your `.bashrc` or `.zshrc`:
```bash
# Add to ~/.bashrc
source /opt/ros/humble/setup.bash
```

---

### "Package 'foo' not found in ament index"

**Symptoms**:
```
Error: Package 'vision_msgs' not found in ament index
```

**Cause**: Package not installed or not in AMENT_PREFIX_PATH.

**Solution 1 - Install missing package**:
```bash
# Find package name (usually ros-<distro>-<package>)
apt search ros-humble-vision

# Install package
sudo apt install ros-humble-vision-msgs

# Verify installation
colcon-cargo-ros2-doctor  # checks the whole chain, including this package
```

**Solution 2 - Source workspace overlay**:
```bash
# If package is in a local workspace
source /path/to/my_workspace/install/setup.bash

# Verify AMENT_PREFIX_PATH includes both paths
echo $AMENT_PREFIX_PATH
# Should output: /path/to/my_workspace/install:/opt/ros/humble

# Try again
colcon build
```

---

### Wrong ROS distro detected

**Symptoms**:
```
Using packages from humble but I have jazzy installed
```

**Cause**: Multiple ROS installations or workspace overlays sourced incorrectly.

**Solution**:
```bash
# Start fresh shell (no ROS sourced)
exit  # or open new terminal

# Source only desired ROS distro
source /opt/ros/jazzy/setup.bash

# Verify
echo $ROS_DISTRO
# Should output: jazzy

# Clean cache and rebuild
colcon-cargo-ros2 clean
colcon build
```

---

### `No module named 'lark'`, or `module 'em' has no attribute 'BUFFERED_OPT'`

**Symptoms**: a CMake interface package in the workspace — not a Rust one —
fails during `rosidl_adapt_interfaces` or `rosidl_generator_c`:

```
AttributeError: module 'em' has no attribute 'BUFFERED_OPT'
ModuleNotFoundError: No module named 'lark'
```

**Cause**: this extension was installed into an isolated virtualenv. CMake runs
whichever `python3` is first on `PATH`, so ROS's own generators execute under an
interpreter that cannot see ROS's Python dependencies. `empy` compounds it:
ROS needs 3.3.4 and `colcon-core` declares `empy` unpinned, so a fresh
virtualenv resolves 4.x.

**Solution**: install with `pip install --user`, or create the virtualenv with
system site packages so the existing ROS ones are visible:

```bash
python3 -m venv --system-site-packages ~/.venvs/ros
~/.venvs/ros/bin/pip install colcon-cargo-ros2
```

Neither error mentions Rust or this extension, so it is easy to chase in the
wrong direction.

---

## Build Errors

### "cargo-ros2-bindgen not found"

**Symptoms**:
```
Error: cargo-ros2-bindgen not found. Please build it first with 'cargo build'
```

**Cause**: cargo-ros2-bindgen binary not in PATH or target/debug.

**Solution 1 - Build the tools**:
```bash
# Navigate to cargo-ros2 source directory
cd /path/to/cargo-ros2

# Build all binaries
just build

# Or build specific binary
cargo build --package cargo-ros2-bindgen
```

**Solution 2 - Install to PATH**:
```bash
# Install binaries to ~/.cargo/bin
cargo install --path cargo-ros2-bindgen
cargo install --path cargo-ros2

# Verify
which cargo-ros2-bindgen
colcon-cargo-ros2 --help  # confirms the wheel is installed and importable
```

---

### "linker error: undefined reference to rosidl_..."

**Symptoms**:
```
error: linking with `cc` failed
  = note: undefined reference to `rosidl_typesupport_c__get_message_type_support_handle__std_msgs__msg__String'
```

**Cause**: ROS C libraries not found by linker.

**Solution**:
```bash
# Ensure ROS is sourced (sets library paths)
source /opt/ros/humble/setup.bash

# Verify library paths
echo $LD_LIBRARY_PATH
# Should include /opt/ros/humble/lib

# Clean and rebuild
cargo clean
colcon build
```

**If problem persists**:
```bash
# Manually set library path
export LD_LIBRARY_PATH=/opt/ros/humble/lib:$LD_LIBRARY_PATH

# Or add to .bashrc
echo 'export LD_LIBRARY_PATH=/opt/ros/humble/lib:$LD_LIBRARY_PATH' >> ~/.bashrc
```

---

### IDE can't resolve ROS message dependencies

**Symptoms**: rust-analyzer or RustRover shows unresolved imports for `std_msgs`, `geometry_msgs`, etc.

**Cause**: `.cargo/config.toml` not yet generated. This file is created automatically by `colcon build`.

**Solution**:
```bash
# Build the workspace — this generates .cargo/config.toml for IDE support
colcon build

# After build, cargo check works without --config
cargo check
```

The generated `.cargo/config.toml` contains `[patch.crates-io]` entries and `[build] rustflags` pointing to the generated bindings in `build/`. Consider adding it to `.gitignore` (paths are machine-specific).

---

### Compilation fails with "trait bounds not satisfied"

**Symptoms**:
```
error[E0277]: the trait bound `Foo: Message` is not satisfied
```

**Cause**: Stale bindings or incomplete trait implementations.

**Solution**:
```bash
# Rebuild specific package
colcon build --packages-select foo

# Or clean everything
colcon-cargo-ros2 clean

# Rebuild
colcon build
```

### Start here: `cargo ros2 doctor`

Most of the failures below are diagnosed in one command, run from the package directory:

```bash
colcon-cargo-ros2-doctor        # installed with the wheel
cargo ros2 doctor               # equivalent, from a source checkout
```

It walks the same chain cargo does — ROS environment, generated `.cargo/config.toml`, patch markers, patched crate directories, binding freshness, `package.xml` declarations — and stops at the first broken link with the fix for it. Exit status is non-zero when anything failed, so CI can gate on it.

```
✓ ROS environment: 1 prefixes on AMENT_PREFIX_PATH
✓ Generated .cargo/config.toml: found at /ws/src/pkg_b/.cargo/config.toml
✓ Patch section: generated markers present
✓ Patched crates: 4 generated crates readable
✓ Binding freshness: 4 crates match their interface definitions
✗ package.xml declarations: used in Cargo.toml but not declared: sensor_msgs
    Add to package.xml, then re-run `colcon build`:
      <depend>sensor_msgs</depend>
```

### `cargo` reports a "yanked" version of a message crate

**Symptoms**:

```
error: failed to select a version for the requirement `sensor_msgs = "*"`
  version 4.2.3 is yanked
location searched: crates.io index
```

**Cause**: no `[patch.crates-io]` entry exists for that package, so cargo looked it up on the real crates.io. The error names the registry, never the missing patch. Three things produce it:

1. The package is used in `Cargo.toml` but has no `<depend>` tag in `package.xml`. Bindings are generated from `package.xml` only. `colcon build` warns about this by name before cargo fails — look one screen up.
2. No `colcon build` has run yet in this workspace, so no `.cargo/config.toml` exists.
3. The config was deleted or the crate was moved out of the workspace.

**Solution**: declare the package in `package.xml` and re-run `colcon build`.

```xml
<depend>sensor_msgs</depend>
```

Note that a Cargo workspace resolves as a unit: one member's undeclared dependency fails every member.

### `cargo` cannot read a generated crate under `build/`

**Symptoms**:

```
unable to update /path/to/ws/build/std_msgs/rosidl_cargo/std_msgs
failed to read /path/to/ws/build/std_msgs/rosidl_cargo/std_msgs/Cargo.toml
```

**Cause**: `build/` was cleaned while `.cargo/config.toml` still patched to it.

**Solution**: re-run `colcon build`, which regenerates both.

### Build script panics on `AMENT_PREFIX_PATH`

**Symptoms**:

```
thread 'main' panicked at rosidl_runtime_rs-0.6.0/build.rs:
AMENT_PREFIX_PATH environment variable not set - please source ROS 2 installation first.
```

**Cause**: the crate is being built without a generated `.cargo/config.toml` — its `[env]` section supplies `AMENT_PREFIX_PATH` for exactly this. Either no `colcon build` has run, or cargo is being invoked from outside the crate's directory tree, so the config is not discovered.

**Solution**: run `colcon build` once, then invoke cargo from the package directory. Sourcing the ROS environment also works and takes precedence.

### A binary cannot find ROS shared libraries at run time

**Symptoms**:

```
error while loading shared libraries: libstd_msgs__rosidl_typesupport_c.so:
cannot open shared object file: No such file or directory
```

**Cause**: the binary carries no rpath. This is expected if the workspace was built with `--no-rpath`, or if the binary predates that support.

**Solution**: source the workspace (`source install/setup.bash`), or rebuild without `--no-rpath` so library directories are baked in.

**Moving a built workspace** is supported without a rebuild: alongside the absolute entry, workspace-internal libraries get `$ORIGIN`-relative rpaths covering the installed layout, cargo's target directory, and the cross-compiled variant. Moving or renaming the workspace, or copying `install/` on its own to another machine, all keep working. What does not travel is `/opt/ros/<distro>/lib`, which is absolute by design — the target machine needs its own ROS installation.

### `the trait bound ...: MessageIDL is not satisfied`

**Symptoms**:

```
error[E0277]: the trait bound `std_msgs::msg::String: MessageIDL` is not satisfied
note: there are multiple different versions of crate `rosidl_runtime_rs`
      in the dependency graph
```

**Cause**: two versions of `rosidl_runtime_rs` in one graph. `rclrs` decides which one it needs, and the generated bindings must ask for the same:

| rclrs | rosidl_runtime_rs |
|---|---|
| 0.6 | 0.5 |
| 0.7 | 0.6 |

Cargo treats 0.5 and 0.6 as incompatible, so a mismatch keeps both — and the `Message` trait a generated crate implements is then not the `Message` trait `rclrs` requires, even though both are spelled the same.

`colcon build` derives the version from what your packages declare (`rosidl_runtime_rs` directly, else the `rclrs` version). This error therefore means the workspace disagrees with itself, and the build says so before cargo does:

```
WARNING Packages in this workspace need different rosidl_runtime_rs versions:
  0.5 (old_node); 0.6 (new_node).
  Bindings are generated once and shared, so only one can be satisfied; using 0.6.
```

**Solution**: align the packages on one `rclrs` version, or pick the runtime explicitly:

```bash
colcon build --rosidl-runtime-rs-version 0.5
```

**Related**: `rclrs = "*"` cannot be matched at all, because cargo resolves it to whatever is newest. Pin it.

### A dependency is declared in package.xml but never used in Cargo.toml

This is **not** reported as a problem, because it is often correct: a launch
file in the package may start a node publishing that type, or the dependency may
be there for the ament environment. Neither shows up in `Cargo.toml`.

It does cost something — bindings are generated for it — so the build says so at
info level:

```console
$ colcon --log-level info build
... installer_node: bindings generated for geometry_msgs, which package.xml
    declares but Cargo.toml does not use. Correct if the dependency is only
    needed at runtime; otherwise dropping the <depend> tag saves generating them.
```

Note the flag position: `--log-level` belongs to `colcon`, before the verb.

The opposite direction — used in `Cargo.toml`, missing from `package.xml` — *is*
a warning, because the build then fails with a misleading crates.io error.

### `redeclaration of enumerator` when building an action

**Symptoms**: building your own interface package fails inside rosidl's C generator, before anything Rust-related runs:

```
build/my_msgs/rosidl_generator_c/my_msgs/action/detail/dock__struct.h:54:3:
  error: redeclaration of enumerator 'my_msgs__action__Dock_Result__NONE'
```

**Cause**: two sections of the action declare a constant with the same name *and* the same value, for example `NONE=0` in both the result and the feedback section.

rosidl's adapter handles this correctly — the intermediate `.idl` puts them in separate `Dock_Result_Constants` and `Dock_Feedback_Constants` modules. The parser then binds each constant back to a section by searching the parse tree for a structurally equal node, and two identical declarations compare equal, so both are filed under the section that appears first. The C generator emits that section's enum with the constant twice.

**Workaround**: give the constants different values.

```
---
uint16 NONE=0        # result
uint16 FAILED=1
bool success
---
uint16 NONE=9        # feedback, distinct value
uint16 DOCKING=1
float32 progress
```

Differing comments do not help; the values have to differ. Verified on Humble.

**Not a limitation of this project**: the generated Rust keeps constants in a module per section, so duplicate names are fine once rosidl can produce the interface at all. `testing_workspaces/interfaces` covers that.

### Bindings are out of date

**Symptoms**:

```
Rust bindings for `my_msgs` are out of date: the interface definitions in
/ws/src/my_msgs have changed since they were generated.
Re-run `colcon build` to regenerate them, or set
COLCON_CARGO_ROS2_SKIP_STAMP_CHECK=1 to build anyway.
```

**Cause**: a `.msg`/`.srv`/`.action` file changed after the bindings were generated. `colcon build` regenerates on change, but a plain `cargo build` would otherwise compile against the previous generation — and the mismatch would surface much later as an error inside *your* code (`no field 'sequence_id' on type 'Reading'`) that names nothing responsible.

**Solution**: `colcon build`. To build against the old bindings anyway (e.g. bisecting), set `COLCON_CARGO_ROS2_SKIP_STAMP_CHECK=1`.

**Not triggered by**: a fresh `git clone` or `checkout`, a `cp -r`, a container mount, or `touch`. Freshness is keyed on the *contents* of the definitions, so rewriting their timestamps changes nothing.

### `unable to find library -l<package>__rosidl_typesupport_c`

**Symptoms**:

```
rust-lld: error: unable to find library -lsplat_msgs__rosidl_typesupport_c
```

**Cause**: the linker search path has no entry for the package's `lib/` directory. For a workspace-local interface package that means it has not been built and installed yet, so `install/<pkg>/lib` did not exist when the config was written.

**Solution**: build the interface package first (`colcon build --packages-select <pkg>`), then build the workspace. Check the result with `cargo ros2 doctor`; the `-L` entries live in the `[build]` block of the generated `.cargo/config.toml`.

### `InstallConfig.__new__() got an unexpected keyword argument`

**Symptoms**:

```
TypeError: InstallConfig.__new__() got an unexpected keyword argument 'arch'
```
or:
```
cargo_ros2_py 0.4.0 does not match colcon-cargo-ros2 0.4.1.
```

**Cause**: the bundled native module is older than the Python code calling it. Common with an editable install, where the `.pth` runs the source tree while `cargo_ros2_py*.so` stays whatever was last built.

**Solution**:

```bash
just build-python && just install
```

---

## Cache Problems

### Stale bindings after updating ROS packages

**Symptoms**:
- Old message fields still present after apt upgrade
- Missing new fields from updated package

**Cause**: Cache checksum doesn't detect system package updates (apt updates).

**Solution**:
```bash
# Rebuild specific package
colcon build  # regenerates whatever changed

# Or clean all cache
colcon-cargo-ros2 clean
colcon build
```

**Explanation**: Checksums are calculated from interface files. If apt updates a package but files have same content, checksum doesn't change. Force rebuild to regenerate.

---

### Cache fills up disk space

**Symptoms**:
```
df -h shows target/ros2_bindings/ is very large
```

**Cause**: Many packages cached over time.

**Solution**:
```bash
# List cached packages
ls build/*/rosidl_cargo  # the generated crates

# Clean all cache
colcon-cargo-ros2 clean

# Or selectively remove unused packages
colcon-cargo-ros2 clean  # drops generated bindings for this crate
```

**Prevention**: Add to `.gitignore`:
```
/target/
/.ros2_bindgen_cache
/.cargo/config.toml
```

---

### "Cache checksum mismatch" warnings

**Symptoms**:
```
Warning: Cache checksum mismatch for 'foo', regenerating...
```

**Cause**: Normal behavior when interface files changed.

**Solution**: No action needed - cargo-ros2 automatically regenerates. This is working as designed.

---

## Installation Issues

### ament-build fails: "Binary not found"

**Symptoms**:
```
Warning: Binary not found: my_binary (did you run with --release?)
```

**Cause**: Binary not built yet, or built with wrong profile.

**Solution**:
```bash
# If you used --release flag
colcon-cargo-ros2 install --install-base install/my_pkg --profile release

# Verify binary exists
ls target/release/my_binary

# If binary doesn't exist, cargo build failed
# Check build output for errors
cargo build --release
```

---

### ament-build creates empty lib/ directory

**Symptoms**:
```
install/my_pkg/lib/ is empty but I have binaries
```

**Cause**: Package detected as library-only (no [[bin]] or src/main.rs).

**Solution**:
```bash
# Check package type detection
ls -la src/
# Should have src/main.rs for binary package

# Or add [[bin]] section to Cargo.toml
cat >> Cargo.toml << EOF
[[bin]]
name = "my_binary"
path = "src/main.rs"
EOF

# Rebuild
colcon-cargo-ros2 install --install-base install/my_pkg --profile release
```

---

### Install fails: "Permission denied"

**Symptoms**:
```
Error: Failed to create directory: Permission denied
```

**Cause**: Installing to system directory without sudo.

**Solution**:
```bash
# Don't use sudo with cargo-ros2!
# Instead, install to local directory

# Install to local workspace
colcon-cargo-ros2 install --install-base install/my_pkg --profile release

# Or install to writable location
colcon-cargo-ros2 install --install-base ~/ros_workspace/install/my_pkg --profile release
```

---

## Performance Issues

### Slow binding generation

**Symptoms**:
- `colcon build` takes 30+ seconds
- Many packages regenerating unnecessarily

**Cause**: Cache misses or disabled parallelization.

**Solutions**:

**1. Check cache status**:
```bash
# See what's cached
ls build/*/rosidl_cargo  # the generated crates

# If empty, first build will be slow (normal)
```

**2. Verify parallel generation**:
```bash
# Watch output for parallel indicator
colcon build --event-handlers console_direct+

# Should see:
# Generating bindings for 3 packages...
# ⠁ [00:00:05] [##########>-----] 2/3 Generating geometry_msgs
```

**3. Reduce dependencies**:
```bash
# Audit Cargo.toml
# Remove unused ROS dependencies
# Each dependency requires binding generation
```

---

### Slow cargo build after bindings

**Symptoms**:
- Binding generation fast
- `cargo build` takes long time

**Cause**: Normal Rust compilation, not related to cargo-ros2.

**Solution**:
```bash
# Use release profile (faster runtime, slower compile)
colcon build

# Or use check instead of build (type-check only)
cargo check  # much faster, and works after one colcon build

# Or use cargo's own caching
# Subsequent builds are incremental
```

---

### Hot build still regenerates bindings

**Symptoms**:
- Second `colcon build` regenerates packages
- Cache exists but not used

**Cause**: Output directory deleted (`cargo clean`).

**Solution**:
```bash
# Don't use `cargo clean` - use cargo-ros2 clean instead
cargo ros2 clean  # preserves cache metadata

# Or rebuild cache if you already ran cargo clean
colcon build  # will regenerate (one-time cost)
```

---

## Advanced Debugging

### Enable verbose output

```bash
# All commands support --verbose
colcon build --event-handlers console_direct+
colcon-cargo-ros2 install --install-base install/my_pkg --profile release --verbose
colcon-cargo-ros2-doctor

# Shows:
# - Ament package discovery
# - Cache hit/miss decisions
# - Binding generation progress
# - File operations
```

---

### Inspect cache contents

```bash
# View cache JSON
cat .ros2_bindgen_cache | jq .

# Sample output:
# {
#   "entries": {
#     "std_msgs": {
#       "package_name": "std_msgs",
#       "checksum": "a1b2c3d4...",
#       "ros_distro": "humble",
#       "timestamp": 1730764800,
#       "output_dir": "/home/user/project/target/ros2_bindings/std_msgs"
#     }
#   }
# }
```

---

### Inspect generated bindings

```bash
# List generated packages
ls target/ros2_bindings/

# View generated code
cat target/ros2_bindings/std_msgs/src/msg/rmw.rs
cat target/ros2_bindings/std_msgs/src/msg/idiomatic.rs

# Check Cargo.toml dependencies
cat target/ros2_bindings/geometry_msgs/Cargo.toml
```

---

### Manually test binding generation

```bash
# Use cargo-ros2-bindgen directly
cargo-ros2-bindgen \
  --package std_msgs \
  --output target/test/std_msgs \
  --verbose

# Compile generated package
cargo build --manifest-path target/test/std_msgs/Cargo.toml
```

---

### Check ROS environment

```bash
# Verify all environment variables
env | grep -E 'ROS|AMENT'

# Should show:
# AMENT_PREFIX_PATH=/opt/ros/humble
# ROS_DISTRO=humble
# ROS_VERSION=2
# ROS_PYTHON_VERSION=3
# (and more)

# Verify package discovery
ros2 pkg list | grep std_msgs
# Should output: std_msgs

# Verify package location
ros2 pkg prefix std_msgs
# Should output: /opt/ros/humble
```

---

### Debug with strace (Linux)

```bash
# Trace file operations
strace -e open,openat,stat colcon build 2>&1 | grep ament

# Trace library loading
strace -e open,openat cargo build 2>&1 | grep rosidl
```

---

### Debug with RUST_LOG

```bash
# Enable debug logging (if implemented)
RUST_LOG=debug colcon build

# Or specific modules
RUST_LOG=cargo_ros2::workflow=debug colcon build
```

---

## Getting Help

### Check Documentation

- [README.md](../README.md) - Project overview
- [CLI_REFERENCE.md](CLI_REFERENCE.md) - Complete command reference
- [DESIGN.md](DESIGN.md) - Architecture details
- [examples/](../examples/) - Working examples

### Report Issues

If you encounter a bug:

1. **Check if it's already reported**: [GitHub Issues](https://github.com/yourusername/cargo-ros2/issues)

2. **Gather information**:
   ```bash
   # Cargo-ros2 version
   colcon-cargo-ros2 --help  # confirms the wheel is installed and importable

   # ROS environment
   echo "ROS_DISTRO=$ROS_DISTRO"
   echo "AMENT_PREFIX_PATH=$AMENT_PREFIX_PATH"

   # Rust version
   rustc --version
   cargo --version

   # OS information
   lsb_release -a  # Ubuntu/Debian
   uname -a        # All systems

   # Verbose output
   colcon build --event-handlers console_direct+ 2>&1 | tee build.log
   ```

3. **Create minimal reproduction**:
   ```bash
   # Minimal Cargo.toml
   cargo new minimal_repro
   cd minimal_repro
   echo 'std_msgs = "*"' >> Cargo.toml
   colcon build --event-handlers console_direct+
   ```

4. **Open issue** with:
   - Description of problem
   - Expected behavior
   - Actual behavior
   - Steps to reproduce
   - Environment information
   - Verbose output logs

---

## Common Pitfalls

### ❌ Using sudo with cargo-ros2

```bash
# WRONG - Don't do this
sudo colcon build
```

**Why**: Cargo tools should never run as root. Use local install directories.

---

### ❌ Mixing cargo clean and cargo ros2 clean

```bash
# WRONG - Don't do this
cargo clean
# Now cache is inconsistent!
```

**Why**: `cargo clean` removes output but not cache metadata. Use `cargo ros2 clean` instead.

---

### ❌ Editing generated code

```bash
# WRONG - Don't edit these files
vim target/ros2_bindings/std_msgs/src/msg/rmw.rs
```

**Why**: Generated code is overwritten on next generation. Modify templates in rosidl-codegen instead.

---

### ❌ Adding generated packages to version control

```bash
# WRONG - Don't commit generated code
git add target/ros2_bindings/
```

**Why**: Generated code should not be in version control. Add to `.gitignore` instead.

---

## Quick Reference

| Problem | Solution |
|---------|----------|
| "Failed to load ament index" | `source /opt/ros/humble/setup.bash` |
| "Package 'foo' not found" | `sudo apt install ros-humble-foo` |
| "cargo-ros2-bindgen not found" | `just build` in cargo-ros2 source |
| Stale bindings | `colcon build` (regenerates), or `COLCON_CARGO_ROS2_SKIP_STAMP_CHECK=1` to build anyway |
| Slow first build | Normal - bindings cached after first run |
| Permission denied on install | Use local install path, not system |
| Linker errors | `source /opt/ros/humble/setup.bash` |

---

**Last Updated**: 2025-11-04
