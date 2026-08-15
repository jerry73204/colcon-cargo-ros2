# Security Policy

## Reporting a vulnerability

Report privately through [GitHub's security advisories][advisories] rather than
a public issue, so a fix can land before the details are public.

[advisories]: https://github.com/jerry73204/colcon-cargo-ros2/security/advisories/new

Please include what you need to reproduce it: the ROS distro, how the workspace
was built, and the smallest interface definitions or `Cargo.toml` that show the
problem. Reports are acknowledged within a week.

## Supported versions

The latest release on PyPI is supported. This project has not reached 1.0, so
fixes land on `main` and in the next release rather than being backported.

## What this project does with your machine

Worth knowing when assessing risk:

- **It runs `cargo` and reads your ROS installation.** Binding generation reads
  `.msg`, `.srv`, `.action` and `.idl` files from packages on `AMENT_PREFIX_PATH`
  and from the workspace source tree, and writes generated crates under `build/`.
- **It writes `.cargo/config.toml` into your source tree**, at each Cargo
  workspace root, between comment markers. Content outside those markers is
  preserved. The file carries `[patch.crates-io]` entries pointing into `build/`,
  linker flags, an `[env]` entry for `AMENT_PREFIX_PATH`, and a `target-dir`.
- **It bakes rpaths into binaries** so they run without a sourced environment:
  absolute paths for system prefixes and `$ORIGIN`-relative ones for workspace
  libraries. `--no-rpath` turns that off.
- **It appends to `.gitignore`** in git worktrees, between markers, so the
  generated config is not committed. `--no-gitignore` turns that off.

It does not fetch anything itself; `cargo` fetches the crates your manifests
declare, from the registries cargo is configured with.

## Supply chain

`cargo deny check` runs on every pull request and covers advisories, licences,
wildcard dependencies and registry sources. The policy is in
[`packages/deny.toml`](packages/deny.toml) and currently carries no advisory
exceptions. Run it locally with `just audit`.

## Unsafe code

The crates in this repository contain no `unsafe` blocks outside a single test
that removes an environment variable, which Rust 2024 requires to be `unsafe`.
The PyO3 macros generate unsafe code at the Python boundary, as they must.

Generated binding crates *do* contain `unsafe`: they are FFI wrappers around the
C type support libraries that ROS itself generates, and they call those C
functions to initialise, copy and finalise messages. That is the same trust
boundary any ROS client library sits on.
