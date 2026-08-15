//! Library artifact, present so that a cdylib gets installed alongside the binaries.

/// Returns the answer, exported so the cdylib has something to export.
#[unsafe(no_mangle)]
pub extern "C" fn installer_node_answer() -> i32 {
    42
}
