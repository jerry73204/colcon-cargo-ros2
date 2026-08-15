# Every collection shape: fixed arrays, unbounded and bounded sequences, over
# primitives, strings, wide strings and nested messages.
#
# Bounded sequences are the shape that produced type mismatches in generated
# services and actions (BoundedSequence<T, N> vs Sequence<T>), so they appear
# here in every element type.

int32[3] fixed_primitives
int32[] unbounded_primitives
int32[<=4] bounded_primitives

string[2] fixed_strings
string[] unbounded_strings
string[<=3] bounded_strings

wstring[] unbounded_wstrings
wstring[<=2] bounded_wstrings

geometry_msgs/Point[2] fixed_points
geometry_msgs/Point[] unbounded_points
geometry_msgs/Point[<=5] bounded_points

float64[3] fixed_defaults [1.0, 2.0, 3.0]
