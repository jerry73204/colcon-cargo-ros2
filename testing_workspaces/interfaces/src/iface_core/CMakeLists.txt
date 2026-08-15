cmake_minimum_required(VERSION 3.10)
project(iface_core)

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(std_msgs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(sensor_msgs REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/AllPrimitives.msg"
  "msg/Collections.msg"
  "msg/Constants.msg"
  "msg/Handwritten.idl"
  "msg/Layout.msg"
  "msg/Marker.msg"
  "msg/Nested.msg"
  "msg/RobotStatus.msg"
  "msg/SensorReading.msg"
  "srv/Compute.srv"
  "srv/SetMode.srv"
  "action/Execute.action"
  "action/Navigate.action"
  DEPENDENCIES std_msgs geometry_msgs sensor_msgs
)

ament_package()
