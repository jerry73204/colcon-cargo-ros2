# A message with constants and no fields, like std_msgs/Empty.
#
# rosidl_generator_c gives such a message a `structure_needs_at_least_one_member`
# byte. A Rust struct without it is zero-sized, and anything embedding it -- see
# Layout.msg -- reads every later field from the wrong offset.

uint8 KIND_NONE=0
uint8 KIND_START=1
