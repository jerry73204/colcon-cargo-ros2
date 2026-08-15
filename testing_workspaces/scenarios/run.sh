#!/usr/bin/env bash
# Deliberately broken states, and the messages they must produce.
#
# Every diagnosis this project added exists because cargo reports the symptom
# somewhere unrelated -- a missing <depend> tag surfaces as "version 4.2.3 is
# yanked" against crates.io. A unit test can prove the string is composed; only
# a real workspace proves it reaches the user before cargo's own error does.
#
# Each scenario copies ../layouts into .work/, breaks one thing, runs one
# command, and greps for what should come out. Nothing here mutates the
# committed tree.
#
# Scenarios are isolated by construction -- each gets its own copy -- so they
# also run concurrently. Two things keep the cost down:
#
#   * a shared cargo target directory per worker, so rosidl_runtime_rs and the
#     other crates.io dependencies are compiled once rather than once per
#     scenario (measured: 40s -> 20s for a cold scenario build)
#   * -j workers in parallel, since nothing is shared but that pool
#
# Usage: ./run.sh [-j N] [scenario ...]   (no arguments runs every scenario)

# -u stays off: ROS setup scripts read unset variables.
set -o pipefail

cd "$(dirname "$0")"
readonly ROOT="$PWD"
readonly LAYOUTS="$ROOT/../layouts"
readonly WORK="$ROOT/.work"
readonly LOGS="$ROOT/.work/logs"
readonly ROS_DISTRO_DEFAULT="${ROS_DISTRO:-humble}"

readonly RESULTS="$WORK/results"
current=setup

# Logs are written beside the work directories, never inside the workspace
# being examined.
log_for() { echo "$LOGS/$current-$1.log"; }

# Results go to a file per scenario rather than straight to the terminal:
# scenarios run concurrently, and interleaved output would be unreadable. The
# driver prints them in order as each finishes.
ok() { printf 'PASS\t%s\n' "$1" >>"$RESULTS/$current"; }
bad() { printf 'FAIL\t%s\n' "$1" >>"$RESULTS/$current"; }

# expect_contains <haystack-file> <needle> <description>
expect_contains() {
    if grep -qF -- "$2" "$1"; then
        ok "$3"
    else
        bad "$3 (expected to find: $2)"
    fi
}

# expect_absent <haystack-file> <needle> <description>
expect_absent() {
    if grep -qF -- "$2" "$1"; then
        bad "$3 (unexpectedly found: $2)"
    else
        ok "$3"
    fi
}

# expect_before <file> <first> <second> <description>
# Asserts <first> appears before <second>: a diagnosis after the failure it
# explains has already scrolled past is a diagnosis the user will not read.
expect_before() {
    local first second
    first=$(grep -nF -- "$2" "$1" | head -1 | cut -d: -f1)
    second=$(grep -nF -- "$3" "$1" | head -1 | cut -d: -f1)
    if [ -n "$first" ] && [ -n "$second" ] && [ "$first" -lt "$second" ]; then
        ok "$4"
    else
        bad "$4 (positions: $first vs $second)"
    fi
}

# fresh <name> -> path to a clean copy of the layouts sources
fresh() {
    local dir="$WORK/$1"
    rm -rf "$dir"
    mkdir -p "$dir"
    tar -C "$LAYOUTS" \
        --exclude=build --exclude=install --exclude=log \
        --exclude=target --exclude=.cargo --exclude=cargo-target-of-my-own \
        -cf - . | tar -C "$dir" -xf -
    echo "$dir"
}

# build <dir> [extra colcon args...] -> writes <dir>/build.log, returns colcon's status
build() {
    local dir="$1"
    shift
    (
        cd "$dir" || exit 1
        # shellcheck disable=SC1090
        source "/opt/ros/$ROS_DISTRO_DEFAULT/setup.bash"
        mkdir -p src/preset_config/.cargo
        [ -f src/preset_config/.cargo/config.toml ] ||
            cp src/preset_config/preset.config.toml src/preset_config/.cargo/config.toml
        # `--log-level` belongs to colcon itself, before the verb; everything
        # else is an argument to `build`.
        local main_args=() verb_args=()
        while [ $# -gt 0 ]; do
            case "$1" in
                --log-level)
                    main_args+=("$1" "$2")
                    shift 2
                    ;;
                *)
                    verb_args+=("$1")
                    shift
                    ;;
            esac
        done
        colcon "${main_args[@]}" build "${verb_args[@]}"
    ) >"$(log_for build)" 2>&1
}

# doctor <dir> <crate-relative-path> -> writes <dir>/doctor.log, returns its status
#
# Prefers the console script the wheel installs, which is what a user has; falls
# back to `cargo ros2 doctor` for a source checkout that has the binary.
doctor() {
    local dir="$1" crate="$2"
    (
        cd "$dir/$crate" || exit 1
        # shellcheck disable=SC1090
        source "/opt/ros/$ROS_DISTRO_DEFAULT/setup.bash"
        if command -v colcon-cargo-ros2-doctor >/dev/null 2>&1; then
            colcon-cargo-ros2-doctor
        elif python3 -c "import colcon_cargo_ros2.doctor" >/dev/null 2>&1; then
            python3 -m colcon_cargo_ros2.doctor
        else
            cargo ros2 doctor
        fi
    ) >"$(log_for doctor)" 2>&1
}

# cargo_in <dir> <crate> [args...] -> writes <dir>/cargo.log
cargo_in() {
    local dir="$1" crate="$2"
    shift 2
    (
        cd "$dir/$crate" || exit 1
        # shellcheck disable=SC1090
        source "/opt/ros/$ROS_DISTRO_DEFAULT/setup.bash"
        cargo "$@"
    ) >"$(log_for cargo)" 2>&1
}

# cargo_bare <dir> <crate> [args...] -- no ROS environment at all
cargo_bare() {
    local dir="$1" crate="$2"
    shift 2
    (
        cd "$dir/$crate" || exit 1
        env -u AMENT_PREFIX_PATH -u LD_LIBRARY_PATH -u ROS_DISTRO -u CMAKE_PREFIX_PATH \
            cargo "$@"
    ) >"$(log_for cargo)" 2>&1
}

# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

# An interface package used in Cargo.toml with no <depend> tag in package.xml.
# Bindings come from package.xml alone, so cargo resolves the name on crates.io
# and reports a yanked version -- naming a registry, never the missing tag.
scenario_undeclared_dep() {
    local dir
    dir=$(fresh undeclared_dep)
    sed -i '/<depend>geometry_msgs<\/depend>/d' "$dir/src/cargo_ws/beta/package.xml"

    build "$dir" --packages-select beta
    expect_contains "$(log_for build)" "not declared in package.xml: geometry_msgs" \
        "the missing declaration is named"
    expect_contains "$(log_for build)" "<depend>geometry_msgs</depend>" \
        "the fix is spelled out"
    expect_before "$(log_for build)" "not declared in package.xml" "yanked" \
        "the diagnosis precedes cargo's yanked error"
}

# The same mistake behind a renamed dependency, which a scan of dependency keys
# cannot see: the key is `msgs`, the ROS package is sensor_msgs.
scenario_undeclared_dep_renamed() {
    local dir
    dir=$(fresh undeclared_dep_renamed)
    sed -i '/<depend>sensor_msgs<\/depend>/d' "$dir/src/cargo_ws/gamma/package.xml"

    build "$dir" --packages-select gamma
    expect_contains "$(log_for build)" "not declared in package.xml: sensor_msgs" \
        "a renamed dependency is still resolved to its ROS package"
}

# No colcon build has run here, so there is no config and no patches.
scenario_never_built() {
    local dir
    dir=$(fresh never_built)

    doctor "$dir" src/standalone_node
    expect_contains "$(log_for doctor)" "Generated .cargo/config.toml" "doctor reports the config check"
    expect_contains "$(log_for doctor)" "Run \`colcon build\`" "and says to run colcon build"
}

# Everything healthy: doctor must be silent about problems and exit zero.
scenario_doctor_healthy() {
    local dir=$BUILT
    if doctor "$dir" src/nested/deep/deeper/nested_node; then
        ok "doctor exits zero on a healthy workspace"
    else
        bad "doctor failed on a healthy workspace: $(cat "$(log_for doctor)")"
    fi
    expect_absent "$(log_for doctor)" "✗" "no check reports a problem"
    expect_contains "$(log_for doctor)" "match their interface definitions" "bindings are reported fresh"
}

# A .msg changed after the bindings were generated. colcon would regenerate;
# a plain cargo build has to refuse instead of compiling against the old shape.
scenario_stale_bindings() {
    local dir
    dir=$(fresh stale_bindings)
    build "$dir" --packages-select local_msgs nested_node
    echo "int32 sequence_id" >>"$dir/src/local_msgs/msg/Reading.msg"

    cargo_in "$dir" src/nested/deep/deeper/nested_node build
    expect_contains "$(log_for cargo)" "out of date" "the build refuses to use stale bindings"
    expect_contains "$(log_for cargo)" "Re-run \`colcon build\`" "and says how to fix it"

    # The escape hatch still builds.
    (
        cd "$dir/src/nested/deep/deeper/nested_node" || exit 1
        # shellcheck disable=SC1090
        source "/opt/ros/$ROS_DISTRO_DEFAULT/setup.bash"
        COLCON_CARGO_ROS2_SKIP_STAMP_CHECK=1 cargo build
    ) >"$(log_for override)" 2>&1
    if [ $? -eq 0 ]; then
        ok "COLCON_CARGO_ROS2_SKIP_STAMP_CHECK=1 builds anyway"
    else
        bad "the escape hatch did not build: $(tail -3 "$(log_for override)")"
    fi

    # And colcon regenerates rather than reusing.
    build "$dir" --packages-select local_msgs nested_node
    expect_contains "$(log_for build)" "Finished" "colcon regenerates the changed package"
}

# Definitions touched but not changed -- a fresh checkout, a copy, a container
# mount. Freshness is keyed on what the files say, so none of that is a change.
scenario_touched_definitions() {
    local dir
    dir=$(fresh touched_definitions)
    build "$dir" --packages-select local_msgs nested_node

    # Same bytes, new mtime, exactly as `git checkout` would leave them.
    local msg="$dir/src/local_msgs/msg/Reading.msg"
    local contents
    contents=$(cat "$msg")
    printf '%s\n' "$contents" >"$msg"
    touch "$msg"

    cargo_in "$dir" src/nested/deep/deeper/nested_node build
    expect_absent "$(log_for cargo)" "out of date" \
        "a touched but unchanged definition is not stale"

    # colcon agrees, and does not regenerate.
    build "$dir" --packages-select local_msgs nested_node
    expect_absent "$(log_for build)" "Interface definitions changed" \
        "colcon does not regenerate for a touch"

    # An actual edit is still caught.
    echo "int32 sequence_id" >>"$msg"
    cargo_in "$dir" src/nested/deep/deeper/nested_node build
    expect_contains "$(log_for cargo)" "out of date" "an edit is still caught"
}

# build/ cleaned while the config still patches into it.
scenario_wiped_build_dir() {
    local dir
    dir=$(fresh wiped_build_dir)
    build "$dir" --packages-select standalone_node
    rm -rf "$dir/build"

    doctor "$dir" src/standalone_node
    expect_contains "$(log_for doctor)" "generated crates missing" "doctor names the vanished crates"
    expect_contains "$(log_for doctor)" "Re-run \`colcon build\`" "and says to rebuild"
}

# A generated crate loses its manifest: the stamp still matches, so only an
# explicit check catches it.
scenario_gutted_crate() {
    local dir
    dir=$(fresh gutted_crate)
    build "$dir" --packages-select standalone_node
    rm -f "$dir/build/std_msgs/rosidl_cargo/std_msgs/Cargo.toml"

    build "$dir" --packages-select standalone_node
    if [ -f "$dir/build/std_msgs/rosidl_cargo/std_msgs/Cargo.toml" ]; then
        ok "the gutted crate was regenerated"
    else
        bad "the gutted crate was not regenerated"
    fi
}

# With nothing sourced: the [env] block supplies what build scripts read, and
# the rpath supplies what the loader needs.
scenario_env_free_build() {
    local dir=$BUILT
    cargo_bare "$dir" src/nested/deep/deeper/nested_node clean
    cargo_bare "$dir" src/nested/deep/deeper/nested_node build
    if [ $? -eq 0 ]; then
        ok "cargo build succeeds with no ROS environment"
    else
        bad "cargo build failed with no ROS environment: $(tail -5 "$(log_for cargo)")"
    fi
}

scenario_env_free_run() {
    local dir=$BUILT
    cargo_bare "$dir" src/nested/deep/deeper/nested_node run
    expect_contains "$(log_for cargo)" "nested_node ok" \
        "the binary runs with no ROS environment, linking workspace-local typesupport"
}

# --no-rpath must actually change the artifact.
scenario_no_rpath() {
    local dir
    dir=$(fresh no_rpath)
    build "$dir" --no-rpath --packages-select standalone_node
    grep -c "link-arg" "$dir/src/standalone_node/.cargo/config.toml" >"$(log_for flags)" 2>&1
    expect_contains "$(log_for flags)" "0" "no rpath link arguments are generated"

    local bin="$dir/install/standalone_node/lib/standalone_node/standalone_node"
    objdump -x "$bin" 2>/dev/null >"$(log_for headers)"
    expect_absent "$(log_for headers)" "RPATH" "the binary carries no RPATH"
}

# --no-gitignore leaves version control alone.
scenario_no_gitignore() {
    local dir
    dir=$(fresh no_gitignore)
    rm -f "$dir/.gitignore"
    git -C "$dir" init -q

    build "$dir" --no-gitignore --packages-select standalone_node
    if [ -f "$dir/src/standalone_node/.gitignore" ]; then
        bad "a .gitignore was written despite --no-gitignore"
    else
        ok "no .gitignore written"
    fi

    # Without the flag it is written, and it covers the generated config.
    build "$dir" --packages-select standalone_node
    expect_contains "$dir/src/standalone_node/.gitignore" ".cargo/config.toml" \
        "without the flag, the generated config is ignored"
}

# A normal build leaves nothing for the user to commit or clean up.
scenario_source_tree_clean() {
    local dir
    dir=$(fresh source_tree_clean)
    git -C "$dir" init -q
    git -C "$dir" add -A >/dev/null 2>&1
    git -C "$dir" -c user.email=t@t -c user.name=t commit -qm baseline >/dev/null 2>&1

    build "$dir"
    git -C "$dir" status --porcelain >"$(log_for status)"
    if [ -s "$(log_for status)" ]; then
        bad "the build dirtied the source tree: $(cat "$(log_for status)")"
    else
        ok "git status is clean after a build"
    fi

    find "$dir/src" -type d -name target >"$(log_for targets)"
    if [ -s "$(log_for targets)" ]; then
        bad "cargo target directories under src/: $(cat "$(log_for targets)")"
    else
        ok "no cargo target/ directory under src/"
    fi
}

# --cargo-args reaches cargo, and the installer follows the profile it implies.
scenario_cargo_args_release() {
    local dir
    dir=$(fresh cargo_args_release)
    build "$dir" --packages-select installer_node --cargo-args --release

    if [ -f "$dir/install/installer_node/lib/installer_node/installer_node" ]; then
        ok "the release binary was installed"
    else
        bad "no binary installed for a release build"
    fi
    if [ -d "$dir/build/.cargo_target/src_installer_node/release" ]; then
        ok "artifacts came from the release profile directory"
    else
        bad "no release profile directory under the redirected target-dir"
    fi

    # And the feature-gated binary appears once its feature is on.
    build "$dir" --packages-select installer_node --cargo-args --features extra
    if [ -f "$dir/install/installer_node/lib/installer_node/gated_node" ]; then
        ok "a required-features binary installs once its feature is enabled"
    else
        bad "the feature-gated binary was not installed"
    fi
}

# A built workspace that has been moved, renamed, or had its install tree copied
# somewhere else. An absolute rpath stops resolving the moment any of that
# happens; the $ORIGIN-relative entries are what keep the binaries working.
scenario_relocated_workspace() {
    local dir moved install_only
    dir=$(fresh relocated_workspace)
    build "$dir" --packages-select local_msgs nested_node

    moved="$WORK/relocated_workspace.moved"
    install_only="$WORK/relocated_workspace.install-only"
    rm -rf "$moved" "$install_only"
    mv "$dir" "$moved"

    if env -u AMENT_PREFIX_PATH -u LD_LIBRARY_PATH \
        "$moved/install/nested_node/lib/nested_node/nested_node" >/dev/null 2>&1; then
        ok "an installed binary runs after the workspace is moved"
    else
        bad "the installed binary broke when the workspace moved"
    fi

    local built
    built="$moved/build/.cargo_target/src_nested_deep_deeper_nested_node/debug/nested_node"
    if env -u AMENT_PREFIX_PATH -u LD_LIBRARY_PATH "$built" >/dev/null 2>&1; then
        ok "a built binary runs after the workspace is moved"
    else
        bad "the built binary broke when the workspace moved"
    fi

    # The install tree on its own, as one would copy to another machine.
    mkdir -p "$install_only"
    cp -a "$moved/install" "$install_only/"
    if env -u AMENT_PREFIX_PATH -u LD_LIBRARY_PATH \
        "$install_only/install/nested_node/lib/nested_node/nested_node" >/dev/null 2>&1; then
        ok "an install tree copied on its own still runs"
    else
        bad "the copied install tree does not run"
    fi

    # Put it back where the rest of the harness expects to find it.
    mv "$moved" "$dir"
}

# A dependency declared in package.xml and never compiled against.
#
# installer_node's launch file starts a node publishing geometry_msgs, so the
# declaration is correct and the crate has no reason to name it in Cargo.toml.
# Warning about that would ask the user to delete a right answer; the cost --
# bindings generated for it -- is still worth being able to look up.
scenario_runtime_only_dependency() {
    local dir
    dir=$(fresh runtime_only_dependency)

    build "$dir" --packages-select installer_node
    expect_absent "$(log_for build)" "not used in Cargo.toml" \
        "a runtime-only dependency does not produce a warning"
    expect_absent "$(log_for build)" "WARNING" "the build is quiet"

    # Still discoverable when asked for.
    build "$dir" --log-level info --packages-select installer_node
    expect_contains "$(log_for build)" "bindings generated for geometry_msgs" \
        "the cost is reported at info level"
}

# A package at the colcon workspace root rather than under src/.
scenario_package_at_workspace_root() {
    local dir="$WORK/package_at_workspace_root"
    rm -rf "$dir"
    mkdir -p "$dir"
    cp -a "$LAYOUTS/src/standalone_node/." "$dir/"
    rm -rf "$dir/.cargo" "$dir/target"

    (
        cd "$dir" || exit 1
        # shellcheck disable=SC1090
        source "/opt/ros/$ROS_DISTRO_DEFAULT/setup.bash"
        colcon build
    ) >"$(log_for build)" 2>&1

    expect_contains "$(log_for build)" "standalone_node" "the root package is discovered and built"
    if [ -f "$dir/.cargo/config.toml" ]; then
        ok "its config lands at the workspace root"
    else
        bad "no config generated for a package at the workspace root"
    fi
}

# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

ALL_SCENARIOS=(
    undeclared_dep
    undeclared_dep_renamed
    never_built
    doctor_healthy
    stale_bindings
    wiped_build_dir
    gutted_crate
    env_free_build
    env_free_run
    no_rpath
    no_gitignore
    source_tree_clean
    cargo_args_release
    package_at_workspace_root
    runtime_only_dependency
    relocated_workspace
    touched_definitions
)

# Scenarios that only read a healthy workspace share one build.
NEEDS_BUILT=(doctor_healthy env_free_build env_free_run)

# Scenarios that assert on where artifacts land cannot have their target
# directory redirected out from under them: relocated_workspace needs the built
# binary to sit inside the workspace it moves, which the shared pool prevents.
NO_SHARED_TARGET=(cargo_args_release source_tree_clean relocated_workspace)

# A quarter of the cores: each scenario's colcon build parallelises internally,
# so more workers than this mostly contend. Measured on 32 cores, cold: 366s
# sequential, 198s at 4, 86s at 8, 84s at 14 -- the floor is the longest single
# scenario, which runs two builds back to back.
jobs=$(( $(nproc 2>/dev/null || echo 8) / 4 ))
[ "$jobs" -lt 2 ] && jobs=2
[ "$jobs" -gt 8 ] && jobs=8

requested=()
while [ $# -gt 0 ]; do
    case "$1" in
        -j)
            jobs="$2"
            shift 2
            ;;
        -j*)
            jobs="${1#-j}"
            shift
            ;;
        *)
            requested+=("$1")
            shift
            ;;
    esac
done
[ ${#requested[@]} -eq 0 ] && requested=("${ALL_SCENARIOS[@]}")

rm -rf "$RESULTS"
mkdir -p "$WORK" "$LOGS" "$RESULTS"

for name in "${requested[@]}"; do
    if ! declare -F "scenario_$name" >/dev/null; then
        echo "unknown scenario: $name" >&2
        exit 2
    fi
done

# One healthy workspace, built once, for the scenarios that only read one.
BUILT=""
for name in "${requested[@]}"; do
    for shared in "${NEEDS_BUILT[@]}"; do
        if [ "$name" = "$shared" ] && [ -z "$BUILT" ]; then
            printf '\033[1mpreparing a healthy workspace\033[0m\n'
            current=baseline
            BUILT=$(fresh healthy)
            if build "$BUILT"; then
                ok "baseline workspace builds"
            else
                bad "baseline workspace failed to build: $(tail -5 "$(log_for build)")"
            fi
        fi
    done
done

# run_one <scenario> <worker slot>
#
# The slot picks a cargo target directory. Sharing one per worker lets
# rosidl_runtime_rs and the rest of the crates.io graph be compiled once instead
# of once per scenario; the generated message crates still differ per scenario,
# since they live at that scenario's path.
run_one() {
    local name="$1" slot="$2"
    current="$name"
    : >"$RESULTS/$name"

    local shared=1
    for excluded in "${NO_SHARED_TARGET[@]}"; do
        [ "$name" = "$excluded" ] && shared=0
    done
    if [ "$shared" = 1 ]; then
        export CARGO_TARGET_DIR="$WORK/target-pool/$slot"
        mkdir -p "$CARGO_TARGET_DIR"
    else
        unset CARGO_TARGET_DIR
    fi

    local started=$SECONDS
    "scenario_$name"
    printf 'TIME\t%s\n' "$((SECONDS - started))" >>"$RESULTS/$name"
}

printf '\033[2mrunning %d scenarios, %d at a time\033[0m\n' "${#requested[@]}" "$jobs"

slot=0
running=0
for name in "${requested[@]}"; do
    run_one "$name" "$slot" &
    slot=$(( (slot + 1) % jobs ))
    running=$((running + 1))
    if [ "$running" -ge "$jobs" ]; then
        wait -n 2>/dev/null || wait
        running=$((running - 1))
    fi
done
wait

pass=0
fail=0
failed_names=()
for name in baseline "${requested[@]}"; do
    [ -f "$RESULTS/$name" ] || continue
    duration=""
    printf '\n\033[1m%s\033[0m\n' "$name"
    while IFS=$'\t' read -r verdict text; do
        case "$verdict" in
            PASS)
                printf '  \033[32m✓\033[0m %s\n' "$text"
                pass=$((pass + 1))
                ;;
            FAIL)
                printf '  \033[31m✗\033[0m %s\n' "$text"
                fail=$((fail + 1))
                failed_names+=("$name: $text")
                ;;
            TIME) duration="$text" ;;
        esac
    done <"$RESULTS/$name"
    [ -n "$duration" ] && printf '  \033[2m%ss\033[0m\n' "$duration"
done

printf '\n%d passed, %d failed in %ss\n' "$pass" "$fail" "$SECONDS"
if [ "$fail" -gt 0 ]; then
    printf '\nfailures:\n'
    printf '  %s\n' "${failed_names[@]}"
    exit 1
fi
