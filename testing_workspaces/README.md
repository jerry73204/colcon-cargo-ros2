# Testing Workspaces

Real colcon workspaces that exercise this toolchain end to end. The unit suites
cover the pieces; these cover the assembled product — and assert on it, so a
regression fails a command instead of needing someone to read build output.

```bash
just test-workspaces        # from the repository root: base tier
just test-workspaces-heavy  # adds the third-party interface packages
just clean-workspaces
```

Every workspace exposes the same recipes: `build`, `verify`, `clean`,
`install-deps`.

## The workspaces

| Directory | What it proves | Tier |
|---|---|---|
| [`interfaces/`](interfaces/) | Every IDL shape the generator has to handle, asserted at runtime by its consumers | base (+ heavy) |
| [`layouts/`](layouts/) | Every workspace shape a user can present: standalone crate, Cargo workspace, deeply nested crate, hand-written config, installer metadata, workspace-local messages | base |
| [`scenarios/`](scenarios/) | The failure modes, and the messages each must produce | base |
| [`upstream/`](upstream/) | Third-party ROS 2 Rust packages at a pinned revision | manual — see its README |

### interfaces

`iface_core` defines the shapes itself — primitives with defaults, fixed arrays,
bounded and unbounded sequences over every element type, wide strings, constants,
nesting, services and actions with bounded sequences on every side — rather than
borrowing them from third-party packages, so the base tier builds on a stock ROS
install. `iface_deps` references `iface_core`'s types, and `consumer` round-trips
every value through the RMW representation and compares. A codegen bug that
produces compiling-but-wrong conversions fails there, not in a review.

The `heavy/` subdirectory adds `test_msgs` and `nav2_msgs` shapes and needs
`rosdep`. It lives outside `src/`, so the base tier never sees it.

### layouts

Eight packages, each a shape the config generator has to get right. `verify.sh`
asserts on the generated `.cargo/config.toml` itself: that `standalone_node` is
patched with exactly `std_msgs` and `builtin_interfaces` and not with its
neighbours' dependencies, that `preset_config`'s hand-written entries and its own
`target-dir` survive, that a crate five directories down resolves upward, that
binaries carry an RPATH covering workspace-local libraries, and that nothing
generated lands in the source tree.

### scenarios

Each scenario copies `layouts/`, breaks one thing, runs one command, and greps
for what should come out — for instance that a missing `<depend>` tag is named,
with the tag to add, *before* cargo's own "version 4.2.3 is yanked". Nothing
mutates the committed tree; scratch copies live in `.work/`.

```bash
cd scenarios
just list          # the scenarios
just run           # all of them, in parallel
just run stale_bindings no_rpath
just run -j 4      # fewer workers
```

Scenarios are isolated by construction, so they run concurrently, and workers
share a cargo target directory so the crates.io graph is compiled once rather
than once per scenario. Measured on 32 cores from cold: 366s sequential, 86s
now.

## Adding coverage

Prefer extending a workspace over adding one. A new workspace has to justify
itself with a class of failure the existing ones cannot express; a new package,
message or scenario usually does not.

Two rules keep these useful:

- **Assert, do not merely build.** A build that succeeds proves very little: it
  cannot catch too-broad patches, a trampled user config, or a conversion that
  compiles and corrupts.
- **Keep the base tier installable-free.** Anything that needs `rosdep` belongs
  in a heavy tier, or it will be skipped and rot — as the previous
  `complex_workspace` did, blocked for months on an uninstalled `moveit_msgs`.
