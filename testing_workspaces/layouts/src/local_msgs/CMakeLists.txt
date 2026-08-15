cmake_minimum_required(VERSION 3.10)
project(local_msgs)

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(std_msgs REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/Reading.msg"
  "srv/Toggle.srv"
  DEPENDENCIES std_msgs
)

ament_package()
