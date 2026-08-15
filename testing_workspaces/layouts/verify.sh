#!/usr/bin/env bash
# Assert on what `colcon build` produced in this workspace.
#
# A successful build proves very little on its own: patches could be too broad,
# a user's config could have been trampled, artifacts could be missing, and
# nothing would fail. Every check below asserts on a specific string or path.

set -uo pipefail
cd "$(dirname "$0")"

pass=0
fail=0

ok() {
    printf '  \033[32m✓\033[0m %s\n' "$1"
    pass=$((pass + 1))
}

bad() {
    printf '  \033[31m✗\033[0m %s\n' "$1"
    fail=$((fail + 1))
}

# assert_contains <file> <needle> <description>
assert_contains() {
    if grep -qF -- "$2" "$1" 2>/dev/null; then ok "$3"; else bad "$3"; fi
}

# assert_absent <file> <needle> <description>
assert_absent() {
    if grep -qF -- "$2" "$1" 2>/dev/null; then bad "$3"; else ok "$3"; fi
}

# assert_file <path> <description>
assert_file() {
    if [ -e "$1" ]; then ok "$2"; else bad "$2"; fi
}

# assert_no_file <path> <description>
assert_no_file() {
    if [ -e "$1" ]; then bad "$2"; else ok "$2"; fi
}

# patched_crates <config> -> names patched in the generated block, one per line
patched_crates() {
    sed -n '/BEGIN colcon-cargo-ros2 generated patches/,/END colcon-cargo-ros2/p' "$1" |
        grep -oE '^[a-z_0-9]+ = \{ path' | cut -d' ' -f1 | sort
}

section() { printf '\n\033[1m%s\033[0m\n' "$1"; }

standalone_cfg=src/standalone_node/.cargo/config.toml
cargo_ws_cfg=src/cargo_ws/.cargo/config.toml
nested_cfg=src/nested/deep/deeper/nested_node/.cargo/config.toml
preset_cfg=src/preset_config/.cargo/config.toml

if [ ! -f "$standalone_cfg" ]; then
    echo "No generated config found. Run 'just build' first." >&2
    exit 1
fi

section "Patches are narrowed to what each Cargo target declares"
# standalone_node depends on std_msgs alone; builtin_interfaces arrives through it.
got=$(patched_crates "$standalone_cfg" | tr '\n' ' ')
if [ "$got" = "builtin_interfaces std_msgs " ]; then
    ok "standalone_node patches exactly: $got"
else
    bad "standalone_node patches should be 'builtin_interfaces std_msgs', got: $got"
fi

# The Cargo workspace pools its three members, including the renamed, inherited
# and platform-table dependencies gamma declares.
for crate in std_msgs geometry_msgs sensor_msgs nav_msgs diagnostic_msgs; do
    assert_contains "$cargo_ws_cfg" "$crate = { path" "cargo_ws pools $crate"
done
assert_absent "$cargo_ws_cfg" "local_msgs = { path" "cargo_ws is not patched with local_msgs"
assert_absent "$standalone_cfg" "geometry_msgs = { path" "standalone_node is not patched with beta's geometry_msgs"

section "A crate deep under src/ resolves upward"
assert_contains "$nested_cfg" '../../../../../build/local_msgs' "nested_node patch path climbs five levels"
assert_contains "$nested_cfg" "install/local_msgs/lib" "nested_node links against the workspace-local package"
assert_absent "$standalone_cfg" "install/local_msgs/lib" "standalone_node does not"

section "The user's own config survives"
assert_contains "$preset_cfg" 'target-dir = "cargo-target-of-my-own"' "user target-dir kept"
assert_absent "$preset_cfg" "build/.cargo_target" "generated target-dir stepped aside"
assert_contains "$preset_cfg" 'preset-check = "check --all-targets"' "user [alias] kept"
assert_contains "$preset_cfg" 'PRESET_CONFIG_MARKER = "kept"' "user [env] entry kept"
assert_contains "$preset_cfg" "BEGIN colcon-cargo-ros2 generated patches" "generated patches merged in"
assert_contains "$preset_cfg" "BEGIN colcon-cargo-ros2 generated environment" "generated environment merged in"
assert_file src/preset_config/cargo-target-of-my-own "cargo honoured the user's target-dir"

section "Generated configs carry flags, rpath and environment"
assert_contains "$standalone_cfg" "link-arg=-Wl,-rpath" "rpath link argument present"
assert_contains "$standalone_cfg" "--disable-new-dtags" "RPATH rather than RUNPATH"
assert_contains "$standalone_cfg" "AMENT_PREFIX_PATH = { value" "AMENT_PREFIX_PATH baked in"
assert_contains "$standalone_cfg" "force = false" "a sourced environment still wins"
assert_contains "$standalone_cfg" 'target-dir = "' "target-dir redirected"

section "Nothing generated lands in the source tree"
strays=$(find src -type d -name target 2>/dev/null)
if [ -z "$strays" ]; then
    ok "no cargo target/ directory under src/"
else
    bad "cargo target/ directories under src/: $strays"
fi
strays=$(find src -name .gitignore 2>/dev/null)
if [ -z "$strays" ]; then
    ok "no per-crate .gitignore written (the workspace one already covers them)"
else
    bad "unexpected .gitignore files: $strays"
fi
assert_file build/.cargo_target "cargo artifacts live under build/.cargo_target"

section "Installed layout"
for pkg in standalone_node alpha beta gamma nested_node preset_config installer_node; do
    assert_file "install/$pkg/lib/$pkg/$pkg" "$pkg binary installed"
done
assert_file install/installer_node/lib/installer_node/libinstaller_node.so "cdylib installed"
assert_no_file install/installer_node/lib/installer_node/gated_node "feature-gated binary skipped"
assert_file install/installer_node/share/installer_node/launch/demo.launch.py "install_to_share directory kept its name"
assert_file install/installer_node/share/installer_node/config/params.yaml "second share directory installed"
assert_file install/installer_node/share/installer_node/README.md "individual file installed by basename"
assert_file install/installer_node/include/installer_node/include/installer_node.h "install_to_include entry"
assert_file install/installer_node/lib/installer_node/scripts/helper.sh "install_to_lib entry"
assert_file install/installer_node/share/ament_index/resource_index/rust_packages/installer_node "rust_packages marker"

section "Binaries find ROS libraries without a sourced environment"
bin=install/nested_node/lib/nested_node/nested_node
if command -v objdump >/dev/null 2>&1; then
    # Captured rather than piped: `grep -q` exits at the first match, and under
    # `pipefail` objdump's resulting SIGPIPE would fail the whole pipeline.
    headers=$(objdump -x "$bin" 2>/dev/null)
    if grep -q "RPATH" <<<"$headers"; then
        ok "nested_node carries an RPATH"
    else
        bad "nested_node carries no RPATH"
    fi
    if grep -q "install/local_msgs/lib" <<<"$headers"; then
        ok "the RPATH covers the workspace-local package"
    else
        bad "the RPATH does not cover install/local_msgs/lib"
    fi
else
    ok "objdump unavailable; skipping RPATH inspection"
fi
if env -u AMENT_PREFIX_PATH -u LD_LIBRARY_PATH "$bin" >/dev/null 2>&1; then
    ok "nested_node runs with no ROS environment"
else
    bad "nested_node does not run without a sourced environment"
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
