#!/usr/bin/env bash
# Assert on the bindings this workspace generated, and run the consumers.
#
# The consumers carry the value assertions; this script checks the generated
# code itself for the properties no runtime assertion can observe.

# -u stays off: ROS setup scripts read unset variables.
set -o pipefail
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

assert_contains() {
    if grep -qF -- "$2" "$1" 2>/dev/null; then ok "$3"; else bad "$3"; fi
}

section() { printf '\n\033[1m%s\033[0m\n' "$1"; }

core=build/iface_core/rosidl_cargo/iface_core/src
deps=build/iface_deps/rosidl_cargo/iface_deps/src

if [ ! -d "$core" ]; then
    echo "No generated bindings found. Run 'just build' first." >&2
    exit 1
fi

section "Generated crate layout"
for kind in msg srv action; do
    if [ -d "$core/$kind" ]; then ok "iface_core generated $kind/"; else bad "iface_core has no $kind/"; fi
done
assert_contains build/iface_core/rosidl_cargo/iface_core/Cargo.toml 'version = "0.1.0"' \
    "crate version comes from package.xml"

section "Field-less messages match the C layout"
assert_contains "$core/msg/marker_rmw.rs" "pub structure_needs_at_least_one_member: u8" \
    "a constants-only message keeps the C placeholder member"
assert_contains "$core/msg/marker_idiomatic.rs" "structure_needs_at_least_one_member: 0" \
    "the conversion initialises it"

section "Bounded sequences use BoundedSequence, not Sequence"
assert_contains "$core/msg/collections_rmw.rs" "BoundedSequence" "message: bounded fields"
assert_contains "$core/srv/compute_rmw.rs" "BoundedSequence" "service: bounded fields"
assert_contains "$core/action/execute_rmw.rs" "BoundedSequence" "action: bounded fields"
assert_contains "$core/srv/compute_idiomatic.rs" "BoundedSequence::try_from" \
    "service conversion uses try_from"
assert_contains "$core/action/execute_idiomatic.rs" "BoundedSequence::try_from" \
    "action conversion uses try_from"

section "Constants"
assert_contains "$core/msg/constants_idiomatic.rs" "pub const PROTOCOL: &'static str" \
    "string constants are &'static str, which is const-constructible"
assert_contains "$core/action/execute_idiomatic.rs" "RESULT_NONE" "result constants generated"
assert_contains "$core/action/execute_idiomatic.rs" "FEEDBACK_NONE" "feedback constants generated"

section "Cross-package references"
assert_contains "$deps/msg/aggregate_rmw.rs" "iface_core" \
    "iface_deps references iface_core's types"
assert_contains build/iface_deps/rosidl_cargo/iface_deps/Cargo.toml "iface_core" \
    "and depends on its crate"

section "Consumers"
if [ ! -f install/setup.bash ]; then
    bad "workspace is not installed; run 'just build'"
else
    # shellcheck disable=SC1091
    source install/setup.bash
    if output=$(ros2 run consumer consumer 2>&1); then
        ok "consumer assertions pass"
    else
        bad "consumer failed: $output"
    fi
    if [ -d install/consumer_heavy ]; then
        if output=$(ros2 run consumer_heavy consumer_heavy 2>&1); then
            ok "consumer_heavy assertions pass"
        else
            bad "consumer_heavy failed: $output"
        fi
    else
        ok "heavy tier not built (run 'just build-heavy' to include it)"
    fi
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
