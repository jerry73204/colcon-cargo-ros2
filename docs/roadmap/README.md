# cargo-ros2: Project Roadmap

> **Note**: This is the main roadmap index. For detailed phase information, see the linked documents below.

## Quick Navigation

- [Progress Summary](#progress-summary)
- [Current Status](#current-status)
- [Phase Details](#phases)
- [Additional Resources](#additional-resources)

---

## Progress Summary

**Overall Progress**: 57 of 59 subphases complete (97%). Phase 4 is the only one open: multi-distro support and release preparation.

| Phase                                 | Status           | Progress             | Details |
|---------------------------------------|------------------|----------------------|---------|
| Phase 0: Project Preparation          | ✅ Complete      | 3/3 subphases        | [View](../phases/000-preparation.md) |
| Phase 1: Native Rust IDL Generator    | ✅ Complete      | 7/7 subphases        | [View](../phases/001-idl-generator.md) |
| Phase 2: cargo-ros2 Tools             | ✅ Complete      | 2/2 subphases        | [View](../phases/002-tools.md) |
| Phase 3: Production Features          | ✅ Complete      | 4/4 subphases        | [View](../phases/003-production.md) |
| Phase 4: colcon Integration & Release | 🔄 In Progress   | 6/8 subphases        | [View](../phases/004-integration.md) |
| Phase 5: OMG IDL 4.2 Support          | ✅ Complete      | 4/4 subphases        | [View](../phases/005-idl-support.md) |
| Phase 6: IDE Support                  | ✅ Complete      | 4/4 subphases        | [View](../phases/006-ide-support.md) |
| Phase 7: cargo-ament-build Parity     | ✅ Complete      | 7/7 subphases        | [View](../phases/007-cargo-ament-build-parity.md) |
| Phase 8: Diagnosable Builds           | ✅ Complete      | 7/7 subphases        | [View](../phases/008-build-diagnostics.md) |
| Phase 9: Manual `cargo` Workflow      | ✅ Complete      | 6/6 subphases        | [View](../phases/009-manual-cargo-workflow.md) |
| Phase 10: Testing Workspaces          | ✅ Complete      | 7/7 subphases        | [View](../phases/010-testing-workspaces.md) |

**Latest**: Phases 8, 9 and 10 landed, then every issue they surfaced was closed (#3–#10). A plain `cargo build` now works after one `colcon build` with nothing sourced; failures are diagnosed by name; and three dense testing workspaces plus a scenario harness assert on the result. 254 Rust and 229 Python tests, plus 101 workspace assertions.

---

## Current Status

**Audited 2026-08-16.** The per-phase checklists in `docs/phases/` were written
as plans and were not kept ticked as work landed. They have since been audited
item by item against the code and its tests: 341 open boxes became 66. Of the
275 resolved, 204 were verified done and 71 -- the whole of Subphase 4.1.1 --
were struck through as superseded, because they describe an architecture built
around `colcon-ros-cargo` that was never adopted.

What remains open is real: documentation and benchmarking for IDL support (12),
deferred performance and polish work in Phase 3 (15), and Phase 4's multi-distro
support and release preparation (39).

The audit also found two defects, both fixed: nothing exercised the `.idl` code
path end to end, and constant modules generated from `.idl` were wrapped in an
extra `pub mod`, so the only reachable path was
`msg::x_constants::x_constants::CONST`.

### Complete ✅

| Phase | What it delivered |
|---|---|
| 0 | Project structure, tooling, dependencies |
| 1 | Native Rust IDL parser and generator, including 1.7's code-generation fixes (verified by the `interfaces` workspace) |
| 2 | `cargo-ros2` tools |
| 3 | Services, actions, ament installation |
| 4.1–4.1.5 | colcon integration: single-writer `.cargo/config.toml`, workspace interface discovery, transitive dependency resolution, flat re-exports and associated constants |
| 5 | OMG IDL 4.2 support |
| 6 | IDE support via generated `.cargo/config.toml` |
| 7 | `cargo-ament-build` installer parity |
| 8 | Diagnosable builds: undeclared dependencies named, stale bindings refused, `doctor` |
| 9 | A bare `cargo build`/`run` working with nothing sourced |
| 10 | Testing workspaces that assert, plus CI |

### Open 🔄

- **Phase 4.2 — Multi-Distro Support.** Everything here has been developed and
  tested against Humble alone. Iron and Jazzy are unverified, which also leaves
  open the question from issue #6 of how nav2's `DockRobot` builds on a distro
  whose rosidl rejects it here.
- **Phase 4.3 — Release Preparation.** `cargo-deny`, a changelog, a security
  policy, and publishing. The wheel workflow exists and builds 31 artifacts; what
  is missing is the release hygiene around it.

### Known loose ends

- `packages/colcon-cargo-ros2/stdeb.cfg` is inert since `setup.py` was removed with issue #5; Debian packaging needs a decision.
- `testing_workspaces/upstream`'s `rust_pubsub` is excluded because it declares `rclrs = "*"`; pinning it upstream would let it build.

**Date**: 2026-08-16

---

## Phases

### Phase 0: Project Preparation ✅ [Complete]

**Goal**: Set up project structure, tooling, and development infrastructure.

**Duration**: 1 week | **Status**: ✅ Complete (3/3 subphases)

**Subphases**:
- ✅ 0.1: Workspace Setup (Cargo workspace, profiles, justfile)
- ✅ 0.2: Documentation Setup (README, CONTRIBUTING, templates)
- ✅ 0.3: Dependencies & Tooling (clap, eyre, cargo-nextest)

**[View Full Phase Details →](../phases/000-preparation.md)**

---

### Phase 1: Native Rust IDL Generator ✅ [Complete]

**Goal**: Implement pure Rust parser and code generator for ROS IDL files (.msg, .srv, .action).

**Duration**: 5 weeks | **Status**: ✅ Complete (7/7 subphases)

**Subphases**:
- ✅ 1.1: IDL Parser - Messages (lexer, parser, AST)
- ✅ 1.2: Code Generator - Messages (templates, type mapping, Cargo.toml generation)
- ✅ 1.3: Services & Actions Support (full .srv and .action support)
- ✅ 1.4: Parity Testing (comparison with rosidl_generator_rs)
- ✅ 1.5: Parser Enhancements (default values, negative constants)
- ✅ 1.6: FFI Bindings & Runtime Traits (complete C interop, all traits)
- ✅ 1.7: Code Generation Fixes (cross-package deps, imports, trait stubs)

**Key Achievement**: Pure Rust implementation with no Python dependency, 100% message parsing success rate, complete FFI bindings.

**[View Full Phase Details →](../phases/001-idl-generator.md)**

---

### Phase 2: cargo-ros2 Tools ✅ [Complete]

**Goal**: Build cargo-ros2-bindgen and cargo-ros2 tools using native generator.

**Duration**: 4 weeks | **Status**: ✅ Complete (2/2 subphases)

**Subphases**:
- ✅ 2.1: cargo-ros2-bindgen (ament index integration, CLI, 13 tests)
- ✅ 2.2: cargo-ros2 Core (cache system, config patcher, workflow, 26 tests)

**Key Achievement**: Complete CLI tools with intelligent caching, 181 tests passing, production-ready.

**[View Full Phase Details →](../phases/002-tools.md)**

---

### Phase 3: Production Features ✅ [Complete]

**Goal**: Add services, actions, ament installation, performance optimizations.

**Duration**: 5 weeks | **Status**: ✅ Complete (4/4 subphases)

**Subphases**:
- ✅ 3.1: Services & Actions Integration (already complete from Phase 1)
- ✅ 3.2: Ament Installation (ament_index markers, source/binary install)
- ✅ 3.3: Performance & CLI Polish (parallel generation, progress indicators, cache commands)
- ✅ 3.4: Testing & Documentation (190 tests, comprehensive docs)

**Key Achievement**: Production-ready with parallel builds, beautiful CLI, comprehensive testing.

**[View Full Phase Details →](../phases/003-production.md)**

---

### Phase 4: colcon Integration & Release 🔄 [In Progress]

**Goal**: Seamless colcon integration and public release.

**Duration**: 4 weeks | **Status**: 🔄 In Progress (6/8 subphases: 4.2 multi-distro and 4.3 release preparation remain)

**Subphases**:
- ✅ 4.1: colcon-ros-cargo Integration (rewrote to use cargo-ros2 exclusively)
- ✅ 4.1.1: config.toml Management Refactoring (centralized, no race conditions)
- ✅ 4.1.2: Code Generation Bug Fixes (Clone bounds, snake_case modules)
- ✅ 4.1.3: Workspace Interface Package Discovery (clarified - current design is correct!)
- ✅ 4.1.4: Transitive Dependency Discovery (generated crates list what their definitions reference)
- ✅ 4.1.5: Code Generation Ergonomics (flat re-exports, associated constants)
- 📋 4.2: Multi-Distro Support — everything so far is Humble-only; Iron and Jazzy unverified
- 📋 4.3: Release Preparation (cargo-deny, changelog, security policy, publishing)

**Key Achievement**: One writer for `.cargo/config.toml`, workspace and transitive dependency discovery, and an API shaped like the C++ and Python ones.

**[View Full Phase Details →](../phases/004-integration.md)**

---

### Phase 5: OMG IDL 4.2 Support ✅ [Complete]

**Goal**: Add native support for `.idl` files to enable advanced ROS 2 features and DDS interoperability.

**Duration**: 6 weeks | **Status**: ✅ Complete (4/4 subphases)

**Subphases**:
- ✅ 5.1: IDL Lexer and Parser (OMG IDL 4.2 subset, module hierarchy, annotations)
- ✅ 5.2: IDL Code Generation (constant modules, @default values, wide strings, enums)
- ✅ 5.3: Integration and Testing (cargo-ros2-bindgen integration, real-world validation)
- 🔧 5.4: Documentation and Polish (usage guide, examples, performance testing) - **IN PROGRESS**

**Motivation**:
- `.idl` is the primary ROS 2 format (`.msg/.srv/.action` are legacy, converted to IDL internally)
- Supports features unavailable in `.msg`: `@key` annotations, `@default` values, wide strings
- Required for full ROS 2 compatibility and DDS interoperability
- Real-world usage: packages like `rclrs_example_msgs` use `.idl` files directly

**Key Achievement**: Complete IDL lexer and parser, with the module path format settled as `pkg::ffi::msg::module::Type` for a correct FFI hierarchy.

**[View Full Phase Details →](../phases/005-idl-support.md)**

---

## Additional Resources

### Planning & Strategy
- **[Phase Documents](../phases/)** - Detailed per-phase objectives, design, and exit criteria
- **[Testing Strategy](testing-strategy.md)** - Unit, integration, end-to-end, regression testing approach
- **[Timeline & Schedule](timeline.md)** - Phase durations, cumulative timeline, 19-week total estimate
- **[colcon Test Support](colcon-test-support.md)** - `colcon test` integration notes

### Design Documentation
- **[design.md](../design.md)** - Technical design and architecture
- **[architecture.md](../architecture.md)** - System architecture overview
- **[CLAUDE.md](../../CLAUDE.md)** - Project instructions and overview

### Progress Tracking
- **[Current Status](#current-status)** - What's done, what's in progress, what's next
- **[Phase Details](#phases)** - Links to detailed phase documentation

---

## How to Use This Roadmap

1. **Quick Overview**: Check the [Progress Summary](#progress-summary) table for phase status
2. **Current Work**: See [Current Status](#current-status) for active tasks
3. **Detailed Planning**: Click phase links to see subphase details, work items, and acceptance criteria
4. **Context**: Read [Additional Resources](#additional-resources) for strategy and design docs

---

**Last Updated**: 2025-11-15

**Project Status**: Active development, 70% complete (21/30 subphases), Phase 1, Phase 4 & Phase 5 in progress, IDL support nearly complete!
