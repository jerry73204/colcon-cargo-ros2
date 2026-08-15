"""Installed as part of the launch/ directory, which keeps its name."""

from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription(
        [
            Node(
                package="installer_node",
                executable="installer_node",
                name="installer_node",
            )
        ]
    )
