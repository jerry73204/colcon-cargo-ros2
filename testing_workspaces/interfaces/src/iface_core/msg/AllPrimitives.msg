# Every primitive type, with and without a default.
#
# Defined here rather than borrowed from test_msgs so the base tier needs no
# packages beyond a stock ROS install.

bool flag_plain
bool flag_default true
byte byte_plain
char char_plain
float32 f32_plain
float32 f32_default 1.5
float64 f64_plain
float64 f64_default -2.25
int8 i8_default -8
uint8 u8_default 8
int16 i16_default -16
uint16 u16_default 16
int32 i32_default -32
uint32 u32_default 32
int64 i64_default -64
uint64 u64_default 64
string str_plain
string str_default "hello"
string<=8 str_bounded
wstring wstr_plain
