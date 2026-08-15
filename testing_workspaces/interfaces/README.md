# interfaces

Every IDL shape the generator has to handle, and a consumer that asserts on the
values rather than merely compiling against the types.

```bash
just build          # base tier: needs nothing beyond a stock ROS install
just verify         # assert on the generated code, then run the consumers
just build-heavy    # adds test_msgs / nav2_msgs coverage (needs rosdep)
just clean
```

## Packages

| Package | Type | Covers |
|---|---|---|
| `iface_core` | ament_cmake | Primitives with and without defaults, bounded strings, wide strings, fixed arrays, unbounded and bounded sequences over primitives / strings / wstrings / messages, constants of every constant-capable type, nesting within and across packages, a service with bounded sequences on both sides, an action with bounded sequences and per-section constants, and a field-less message embedded in another (`Marker` in `Layout`) |
| `iface_deps` | ament_cmake | An interface package referencing another one's types, including inside a bounded sequence |
| `consumer` | ament_cargo | Round-trips every shape through the RMW representation and compares; links each package's typesupport |
| `heavy/iface_heavy` | ament_cmake | `test_msgs` (the upstream IDL torture suite) and `nav2_msgs` shapes |
| `heavy/consumer_heavy` | ament_cargo | Asserts on those; `consumer_heavy probe` walks each borrowed type one at a time, which is how the field-less-message layout bug was isolated |

## Why the shapes are defined here

Earlier versions of this workspace borrowed their coverage from `moveit_msgs`,
`control_msgs` and `nav2_msgs`, which do not ship in a stock ROS install — so the
workspace could not be built without `rosdep`, and was recorded as blocked for
months. Defining the shapes here keeps the base tier runnable anywhere; the
genuinely third-party coverage lives in `heavy/`, outside `src/`, selected with
`colcon build --base-paths src heavy`.

## Duplicate constant names across action sections

`Execute.action` declares `NONE` in both its result and feedback sections, which
is the shape newer nav2 actions use (`DockRobot` defines `NONE` in both). The
generated crate keeps them apart with a constant module per section, and
`consumer` asserts each resolves to its own value.

The two values differ deliberately, and that is not cosmetic. rosidl's parser
binds a constant to a section by searching the parse tree for a *structurally
equal* node (`_find_path` compares with `==`), so two identical declarations —
same name **and** same value — are both filed under whichever section came
first. The C generator then emits the same enumerator twice:

```
error: redeclaration of enumerator 'iface_core__action__Execute_Result__NONE'
```

Reproduced on Humble with a two-line action; giving the constants different
values makes it build. Differing comments do not help. See
`docs/troubleshooting.md`.
