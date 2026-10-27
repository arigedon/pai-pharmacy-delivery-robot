"""薬局ワールドの Gazebo と ROS 2 ブリッジを起動する.

司令ノード（commander）は日本語を入力するため、別の端末で起動する:
  ros2 run pharmacy_delivery commander
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription, SetEnvironmentVariable
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node


def generate_launch_description():
    share = get_package_share_directory('pharmacy_delivery')
    robot_models = os.path.join(get_package_share_directory('nav_dev'), 'models')

    # ロボットのメッシュ(model://meshes/...)は nav_dev（Qiita記事のモデル）から読む
    resource_path = SetEnvironmentVariable(
        name='IGN_GAZEBO_RESOURCE_PATH',
        value='/opt/ros/humble/share:' + robot_models)

    gazebo = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(
            get_package_share_directory('ros_ign_gazebo'), 'launch', 'ign_gazebo.launch.py')),
        launch_arguments=[('ign_args', ' -r -v 2 ' + os.path.join(share, 'worlds', 'pharmacy_world.sdf'))])

    bridge = Node(
        package='ros_ign_bridge',
        executable='parameter_bridge',
        parameters=[{'config_file': os.path.join(share, 'config', 'bridge.yaml')}],
        output='screen')

    return LaunchDescription([resource_path, gazebo, bridge])
