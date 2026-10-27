"""薬局レイアウト（配送先・通路グラフ・障害物）の読み込みと経路探索."""
import heapq
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import yaml


@dataclass
class Node:
    key: str
    name: str
    x: float
    y: float
    yaw: Optional[float]
    expose: bool
    aliases: List[str] = field(default_factory=list)


@dataclass
class Box:
    name: str
    x: float
    y: float
    sx: float
    sy: float
    sz: float
    color: List[float]


class PharmacyLayout:
    def __init__(self, data: dict):
        self.nodes: Dict[str, Node] = {}
        for key, n in data['nodes'].items():
            self.nodes[key] = Node(
                key=key, name=n['name'], x=float(n['x']), y=float(n['y']),
                yaw=None if n.get('yaw') is None else float(n['yaw']),
                expose=bool(n.get('expose', False)),
                aliases=list(n.get('aliases', [])))
        self.adj: Dict[str, List[str]] = {k: [] for k in self.nodes}
        for a, b in data['edges']:
            if a not in self.nodes or b not in self.nodes:
                raise ValueError(f'edge [{a}, {b}] に未定義のノードがあります')
            self.adj[a].append(b)
            self.adj[b].append(a)
        self.obstacles = [Box(**o) for o in data.get('obstacles', [])]
        self.floor_zones = [Box(sz=0.0, **z) for z in data.get('floor_zones', [])]

    @classmethod
    def load(cls, path: str) -> 'PharmacyLayout':
        with open(path, encoding='utf-8') as f:
            return cls(yaml.safe_load(f))

    @property
    def destinations(self) -> List[Node]:
        """LLM に見せる配送先."""
        return [n for n in self.nodes.values() if n.expose]

    def dist(self, a: str, b: str) -> float:
        na, nb = self.nodes[a], self.nodes[b]
        return math.hypot(na.x - nb.x, na.y - nb.y)

    def shortest_path(self, start: str, goal: str) -> List[str]:
        """ダイクストラ法で通路グラフ上の最短経路を返す（start, goal を含む）."""
        if start not in self.nodes or goal not in self.nodes:
            raise KeyError(f'未知のノード: {start} -> {goal}')
        best = {start: 0.0}
        prev: Dict[str, str] = {}
        queue: List[Tuple[float, str]] = [(0.0, start)]
        while queue:
            d, u = heapq.heappop(queue)
            if u == goal:
                break
            if d > best.get(u, math.inf):
                continue
            for v in self.adj[u]:
                nd = d + self.dist(u, v)
                if nd < best.get(v, math.inf):
                    best[v] = nd
                    prev[v] = u
                    heapq.heappush(queue, (nd, v))
        if goal not in best:
            raise ValueError(f'{start} から {goal} への通路がありません')
        path = [goal]
        while path[-1] != start:
            path.append(prev[path[-1]])
        return path[::-1]

    def is_known_place(self, text: str) -> bool:
        """配送先の名前・別名・キーのいずれかと（空白・「の」を除いて）一致するか."""
        def norm(s: str) -> str:
            return ''.join(s.split()).replace('の', '').replace('（', '(').replace('）', ')').lower()
        t = norm(text)
        for n in self.destinations:
            if t in {norm(n.key), norm(n.name), *(norm(a) for a in n.aliases)}:
                return True
        return False

    def nearest_node(self, x: float, y: float) -> str:
        return min(self.nodes, key=lambda k: math.hypot(self.nodes[k].x - x, self.nodes[k].y - y))

    def edge_clearances(self) -> List[Tuple[str, str, str, float]]:
        """各通路（線分）と各障害物（矩形）の最短距離を返す: (a, b, obstacle, distance)."""
        result = []
        seen = set()
        for a, nbrs in self.adj.items():
            for b in nbrs:
                if (b, a) in seen:
                    continue
                seen.add((a, b))
                p, q = self.nodes[a], self.nodes[b]
                for box in self.obstacles:
                    d = _segment_box_distance(p.x, p.y, q.x, q.y, box)
                    result.append((a, b, box.name, d))
        return result


def _segment_box_distance(x1, y1, x2, y2, box: Box, samples: int = 200) -> float:
    """線分と軸平行矩形の最短距離（サンプリング近似。線分が矩形を貫けば 0）."""
    hx, hy = box.sx / 2, box.sy / 2
    best = math.inf
    for i in range(samples + 1):
        t = i / samples
        px, py = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
        dx = max(abs(px - box.x) - hx, 0.0)
        dy = max(abs(py - box.y) - hy, 0.0)
        best = min(best, math.hypot(dx, dy))
    return best
