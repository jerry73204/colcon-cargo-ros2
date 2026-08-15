# Embeds a field-less message, then reads fields after it.
#
# This is the shape that segfaulted: test_msgs/Arrays embeds Constants[3], and
# with a zero-sized Rust struct every field after it landed on the wrong bytes.
# The trailing fields below are the canaries.

Marker single_marker
Marker[3] marker_array
int32 alignment_check
string trailing_label
float64[2] trailing_values
