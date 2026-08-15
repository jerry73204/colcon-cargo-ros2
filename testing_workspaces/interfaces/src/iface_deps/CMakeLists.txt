cmake_minimum_required(VERSION 3.10)
project(iface_deps)

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(iface_core REQUIRED)
find_package(builtin_interfaces REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/Aggregate.msg"
  "srv/Summarize.srv"
  DEPENDENCIES iface_core builtin_interfaces
)

ament_package()
