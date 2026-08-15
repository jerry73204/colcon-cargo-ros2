cmake_minimum_required(VERSION 3.10)
project(iface_heavy)

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(test_msgs REQUIRED)
find_package(nav2_msgs REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/TestEdgeCases.msg"
  "msg/TestNav.msg"
  DEPENDENCIES test_msgs nav2_msgs
)

ament_package()
