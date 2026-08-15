# Constants of every constant-capable type.
#
# String constants are the case that must generate `&'static str` rather than
# `String`, which is not const-constructible.

int8 MODE_IDLE=0
int8 MODE_ACTIVE=1
int8 MODE_FAULT=2

uint32 MAX_RETRIES=5
float64 EPSILON=0.001
bool FEATURE_ENABLED=true
string PROTOCOL="rosidl"

# A field default has to be a literal, not a reference to a constant above.
int8 mode 0
uint32 retries 0
