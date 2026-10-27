#!/usr/bin/env python3
"""pharmacy_layout.yaml から Gazebo(Ignition Fortress) 用の薬局ワールドを生成する.

ロボット(demoRobot)の定義は nav_dev/models/world.sdf（Qiita記事のモデル）からそのまま取り込む。

使い方:
  python3 tools/gen_world.py  # pharmacy_delivery/ で実行
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

from pharmacy_delivery.layout import PharmacyLayout  # noqa: E402

LAYOUT = os.path.join(PKG, 'config', 'pharmacy_layout.yaml')
ROBOT_SRC = os.path.join(PKG, '..', 'nav_dev', 'models', 'world.sdf')
OUT = os.path.join(PKG, 'worlds', 'pharmacy_world.sdf')
WORLD = 'pharmacy'


CARGO = """
            <visual name="cargo_tray">
              <pose>0.07 0 0.2 0 0 0</pose>
              <geometry><box><size>0.2 0.17 0.1</size></box></geometry>
              <material><ambient>1.0 0.5 0.1 1</ambient><diffuse>1.0 0.5 0.1 1</diffuse></material>
            </visual>"""


def box_model(b, collide=True, z=None):
    rgba = ' '.join(str(c) for c in b.color)
    sz = b.sz if b.sz > 0 else 0.002
    zc = sz / 2 if z is None else z
    collision = f"""
        <collision name="collision">
          <geometry><box><size>{b.sx} {b.sy} {sz}</size></box></geometry>
        </collision>""" if collide else ''
    return f"""
    <model name="{b.name}">
      <static>true</static>
      <pose>{b.x} {b.y} {zc} 0 0 0</pose>
      <link name="link">{collision}
        <visual name="visual">
          <geometry><box><size>{b.sx} {b.sy} {sz}</size></box></geometry>
          <material><ambient>{rgba}</ambient><diffuse>{rgba}</diffuse><specular>0.1 0.1 0.1 1</specular></material>
        </visual>
      </link>
    </model>"""


def marker_model(n):
    """配送先の床マーカー（見た目だけ）."""
    return f"""
    <model name="mark_{n.key}">
      <static>true</static>
      <pose>{n.x} {n.y} 0.003 0 0 0</pose>
      <link name="link">
        <visual name="visual">
          <geometry><cylinder><radius>0.12</radius><length>0.002</length></cylinder></geometry>
          <material><ambient>0.1 0.6 0.3 1</ambient><diffuse>0.1 0.6 0.3 1</diffuse></material>
        </visual>
      </link>
    </model>"""


def main():
    layout = PharmacyLayout.load(LAYOUT)
    with open(ROBOT_SRC, encoding='utf-8') as f:
        src = f.read()
    m = re.search(r'<model name="demoRobot">.*?</model>\s*(?=</world>)', src, re.S)
    if not m:
        sys.exit('demoRobot モデルが見つかりません: ' + ROBOT_SRC)
    # 配送ロボットらしく、本体の上に荷台（見た目だけ）を載せる
    robot = m.group(0).replace('<link name="base_link">', '<link name="base_link">' + CARGO, 1)

    parts = [box_model(b) for b in layout.obstacles]
    parts += [box_model(z, collide=False, z=0.004) for z in layout.floor_zones]
    parts += [marker_model(n) for n in layout.destinations]

    sdf = f"""<?xml version="1.0" ?>
<!-- 自動生成ファイル: tools/gen_world.py で config/pharmacy_layout.yaml から生成。直接編集しない -->
<sdf version="1.8">
  <world name="{WORLD}">
    <physics name="1ms" type="ignored">
      <max_step_size>0.001</max_step_size>
      <real_time_factor>1.0</real_time_factor>
    </physics>
    <plugin filename="libignition-gazebo-physics-system.so" name="ignition::gazebo::systems::Physics"/>
    <plugin filename="libignition-gazebo-user-commands-system.so" name="ignition::gazebo::systems::UserCommands"/>
    <plugin filename="libignition-gazebo-scene-broadcaster-system.so" name="ignition::gazebo::systems::SceneBroadcaster"/>

    <gui fullscreen="0">
      <plugin filename="GzScene3D" name="3D View">
        <ignition-gui>
          <title>3D View</title>
          <property type="bool" key="showTitleBar">false</property>
          <property type="string" key="state">docked</property>
        </ignition-gui>
        <engine>ogre2</engine>
        <scene>scene</scene>
        <ambient_light>0.5 0.5 0.5</ambient_light>
        <background_color>0.75 0.8 0.85</background_color>
        <camera_pose>2.5 -5.3 6.4 0 0.92 1.5708</camera_pose>
      </plugin>
      <plugin filename="WorldControl" name="World control">
        <ignition-gui>
          <title>World control</title>
          <property type="bool" key="showTitleBar">false</property>
          <property type="bool" key="resizable">false</property>
          <property type="double" key="height">72</property>
          <property type="double" key="width">121</property>
          <property type="double" key="z">1</property>
          <property type="string" key="state">floating</property>
          <anchors target="3D View">
            <line own="left" target="left"/>
            <line own="bottom" target="bottom"/>
          </anchors>
        </ignition-gui>
        <play_pause>true</play_pause>
        <step>true</step>
        <start_paused>false</start_paused>
      </plugin>
      <plugin filename="WorldStats" name="World stats">
        <ignition-gui>
          <title>World stats</title>
          <property type="bool" key="showTitleBar">false</property>
          <property type="bool" key="resizable">false</property>
          <property type="double" key="height">110</property>
          <property type="double" key="width">290</property>
          <property type="double" key="z">1</property>
          <property type="string" key="state">floating</property>
          <anchors target="3D View">
            <line own="right" target="right"/>
            <line own="bottom" target="bottom"/>
          </anchors>
        </ignition-gui>
        <sim_time>true</sim_time>
        <real_time>true</real_time>
        <real_time_factor>true</real_time_factor>
      </plugin>
    </gui>

    <light type="directional" name="sun">
      <cast_shadows>false</cast_shadows>
      <pose>0 0 10 0 0 0</pose>
      <diffuse>0.9 0.9 0.9 1</diffuse>
      <specular>0.2 0.2 0.2 1</specular>
      <direction>-0.3 0.2 -0.9</direction>
    </light>

    <model name="ground_plane">
      <static>true</static>
      <link name="link">
        <collision name="collision">
          <geometry><plane><normal>0 0 1</normal></plane></geometry>
        </collision>
        <visual name="visual">
          <geometry><plane><normal>0 0 1</normal><size>40 40</size></plane></geometry>
          <material><ambient>0.6 0.6 0.6 1</ambient><diffuse>0.6 0.6 0.6 1</diffuse></material>
        </visual>
      </link>
    </model>
{''.join(parts)}

    {robot}
  </world>
</sdf>
"""
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8', newline='\n') as f:
        f.write(sdf)
    print('生成しました:', OUT)


if __name__ == '__main__':
    main()
