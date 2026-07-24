cmake_minimum_required(VERSION 3.0.2)
project(piper_http_bridge)

find_package(catkin REQUIRED COMPONENTS
  rospy
  std_msgs
  std_srvs
  sensor_msgs
  geometry_msgs
  piper_msgs
)

catkin_package(
  CATKIN_DEPENDS rospy std_msgs std_srvs sensor_msgs geometry_msgs piper_msgs
)

include_directories(
  ${catkin_INCLUDE_DIRS}
)

catkin_install_python(PROGRAMS
  scripts/piper_http_bridge_node.py
  DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)

install(DIRECTORY launch/
  DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION}/launch
)
