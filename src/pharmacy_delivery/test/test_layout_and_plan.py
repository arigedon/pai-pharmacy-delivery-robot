import os

import pytest

from pharmacy_delivery.layout import PharmacyLayout
from pharmacy_delivery.llm_planner import PlanError, json_schema, validate

LAYOUT = os.path.join(os.path.dirname(__file__), '..', 'config', 'pharmacy_layout.yaml')
ROBOT_RADIUS = 0.2   # ロボットの外接円の半径 [m]（概算）
MARGIN = 0.1         # 余裕 [m]


@pytest.fixture(scope='module')
def layout():
    return PharmacyLayout.load(LAYOUT)


def test_every_corridor_keeps_clearance(layout):
    """どの通路を直進しても、家具・壁から ROBOT_RADIUS + MARGIN 以上離れている."""
    too_close = [c for c in layout.edge_clearances() if c[3] < ROBOT_RADIUS + MARGIN]
    assert too_close == []


def test_every_destination_reachable_both_ways(layout):
    for n in layout.destinations:
        assert layout.shortest_path('home', n.key)[-1] == n.key
        assert layout.shortest_path(n.key, 'home')[-1] == 'home'


def test_counter_route_goes_through_door(layout):
    path = layout.shortest_path('fridge', 'counter_3')
    assert 'door' in path and path[0] == 'fridge' and path[-1] == 'counter_3'


def test_schema_only_allows_exposed_destinations(layout):
    enum = json_schema(layout)['properties']['tasks']['items']['properties']['location']['enum']
    assert 'hub_n' not in enum and 'door' not in enum
    assert set(enum) == {n.key for n in layout.destinations}


def test_validate_accepts_good_plan(layout):
    tasks = validate({'tasks': [{'action': 'goto', 'location': 'fridge'},
                                {'action': 'wait', 'seconds': 3}]}, layout)
    assert [t.action for t in tasks] == ['goto', 'wait']


@pytest.mark.parametrize('bad', [
    {'tasks': [{'action': 'goto', 'location': 'patient_home'}]},   # 存在しない場所
    {'tasks': [{'action': 'goto', 'location': 'door'}]},           # 中継点は指定不可
    {'tasks': [{'action': 'wait', 'seconds': 120}]},               # 待ち時間が長すぎる
    {'tasks': [{'action': 'fly'}]},                                # 未知の動作
    {'tasks': [{'action': 'wait', 'seconds': 1}] * 13},            # タスクが多すぎる
])
def test_validate_rejects_bad_plans(layout, bad):
    with pytest.raises(PlanError):
        validate(bad, layout)


def test_consecutive_duplicate_goto_is_merged(layout):
    tasks = validate({'tasks': [{'action': 'goto', 'location': 'audit'},
                                {'action': 'wait', 'seconds': 5},
                                {'action': 'goto', 'location': 'audit'}]}, layout)
    assert [(t.action, t.location) for t in tasks] == [('goto', 'audit'), ('wait', None)]


@pytest.mark.parametrize('text,known', [
    ('3番カウンター', True), ('薬品棚A（内服薬）', True), ('冷蔵庫', True),
    ('4番カウンター', False), ('2階の倉庫', False), ('駐車場', False),
])
def test_is_known_place(layout, text, known):
    assert layout.is_known_place(text) is known
