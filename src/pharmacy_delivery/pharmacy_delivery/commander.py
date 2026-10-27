"""薬局配送ロボットの司令ノード.

日本語の指示 → LLM がタスク列を作る → 薬剤師が確認 → 通路グラフの最短経路を
オドメトリのフィードバック制御で走る。

入力:
  - 端末への入力（番号を入れるとプリセット指示を使う）
  - トピック /delivery_request (std_msgs/String)
出力:
  - /cmd_vel (geometry_msgs/Twist)
  - /delivery_status (std_msgs/String)
  - 実行ログ（JSON Lines）: ~/pai_final_ws/run_logs/
"""
import json
import math
import os
import queue
import sys
import threading
import time
from datetime import datetime

import rclpy
from ament_index_python.packages import get_package_share_directory
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from rclpy.node import Node
from std_msgs.msg import String

from .layout import PharmacyLayout
from .llm_planner import LLMPlanner, Plan, PlanError

PRESETS = [
    '冷蔵庫からインスリンを取って、3番カウンターに届けて。終わったら調剤室に戻ってね',
    '湿布を外用薬の棚から取って、鑑査してから1番窓口へお願い',
    '内服薬の棚で錠剤を受け取って、2番カウンターに急ぎで',
    '患者さんの家まで薬を届けて',
    '2番の患者さんに飲み方を説明してきて',
]

# 端末表示用の色
C_RESET, C_BOLD = '\033[0m', '\033[1m'
C_CYAN, C_GREEN, C_YELLOW, C_RED, C_GRAY = '\033[36m', '\033[32m', '\033[33m', '\033[31m', '\033[90m'


def wrap(a: float) -> float:
    return math.atan2(math.sin(a), math.cos(a))


class Aborted(Exception):
    pass


class PharmacyCommander(Node):
    def __init__(self):
        super().__init__('pharmacy_commander')
        share = get_package_share_directory('pharmacy_delivery')
        self.declare_parameter('layout_file', os.path.join(share, 'config', 'pharmacy_layout.yaml'))
        self.declare_parameter('log_dir', os.path.expanduser('~/pai_final_ws/run_logs'))
        self.declare_parameter('require_confirm', True)
        self.declare_parameter('max_linear', 0.35)    # [m/s]
        self.declare_parameter('max_angular', 1.2)    # [rad/s]

        self.layout = PharmacyLayout.load(self.get_parameter('layout_file').value)
        self.planner = LLMPlanner(self.layout)
        self.vmax = float(self.get_parameter('max_linear').value)
        self.wmax = float(self.get_parameter('max_angular').value)
        self.require_confirm = bool(self.get_parameter('require_confirm').value)
        self.log_dir = self.get_parameter('log_dir').value
        os.makedirs(self.log_dir, exist_ok=True)
        self.log_path = os.path.join(self.log_dir, datetime.now().strftime('run_%Y%m%d.jsonl'))

        self.pose = None          # (x, y, yaw)
        self.pose_stamp = 0.0
        self.requests: 'queue.Queue[tuple]' = queue.Queue()
        self.cmd_pub = self.create_publisher(Twist, '/cmd_vel', 10)
        self.status_pub = self.create_publisher(String, '/delivery_status', 10)
        self.create_subscription(Odometry, '/odom', self._on_odom, 10)
        self.create_subscription(String, '/delivery_request',
                                 lambda m: self.requests.put(('topic', m.data)), 10)

    # ---------- センサ ----------
    def _on_odom(self, msg: Odometry):
        q = msg.pose.pose.orientation
        yaw = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))
        self.pose = (msg.pose.pose.position.x, msg.pose.pose.position.y, yaw)
        self.pose_stamp = time.monotonic()

    def _current_pose(self):
        if self.pose is None or time.monotonic() - self.pose_stamp > 0.5:
            self.stop()
            raise Aborted('オドメトリが途絶えました（0.5秒以上更新なし）')
        return self.pose

    # ---------- 出力 ----------
    def say(self, text: str, color: str = ''):
        print(f'{color}{text}{C_RESET}', flush=True)
        self.status_pub.publish(String(data=text))

    def stop(self):
        self.cmd_pub.publish(Twist())

    def _send(self, v: float, w: float):
        t = Twist()
        t.linear.x = max(-self.vmax, min(self.vmax, v))
        t.angular.z = max(-self.wmax, min(self.wmax, w))
        self.cmd_pub.publish(t)

    # ---------- 制御 ----------
    def drive_to(self, x: float, y: float, tol: float, slow_down: bool):
        """目標点まで「その場旋回 → 向きを補正しながら前進」で走る."""
        x0, y0, _ = self._current_pose()
        timeout = time.monotonic() + math.hypot(x - x0, y - y0) / 0.1 + 15.0
        while True:
            if time.monotonic() > timeout:
                self.stop()
                raise Aborted(f'({x:.2f}, {y:.2f}) に時間内に到達できませんでした')
            px, py, yaw = self._current_pose()
            dist = math.hypot(x - px, y - py)
            if dist < tol:
                return
            err = wrap(math.atan2(y - py, x - px) - yaw)
            if abs(err) > 0.4:
                self._send(0.0, 2.5 * err)                 # まず向きを合わせる
            else:
                v = min(self.vmax, 1.0 * dist) if slow_down else self.vmax
                self._send(max(v, 0.08) * math.cos(err), 2.5 * err)
            time.sleep(0.05)

    def turn_to(self, target_yaw: float, tol: float = 0.05):
        timeout = time.monotonic() + 10.0
        while time.monotonic() < timeout:
            err = wrap(target_yaw - self._current_pose()[2])
            if abs(err) < tol:
                break
            self._send(0.0, max(0.3, min(self.wmax, 2.0 * abs(err))) * math.copysign(1, err))
            time.sleep(0.05)
        self.stop()

    def goto(self, key: str):
        px, py, _ = self._current_pose()
        start = self.layout.nearest_node(px, py)
        path = self.layout.shortest_path(start, key)
        names = ' → '.join(self.layout.nodes[k].name for k in path)
        self.say(f'    経路: {names}', C_GRAY)
        for i, k in enumerate(path):
            n = self.layout.nodes[k]
            last = i == len(path) - 1
            if not last and math.hypot(n.x - px, n.y - py) < 0.15:
                continue                                   # 今いる場所は飛ばす
            self.drive_to(n.x, n.y, tol=0.05 if last else 0.12, slow_down=last)
        self.stop()
        target = self.layout.nodes[key]
        if target.yaw is not None:
            self.turn_to(target.yaw)

    # ---------- 1件の指示を処理 ----------
    def process_request(self, source: str, instruction: str):
        record = {'time': datetime.now().isoformat(timespec='seconds'), 'source': source,
                  'instruction': instruction}
        self.say(f'\n{C_BOLD}【指示】{instruction}', C_CYAN)
        self.say(f'【LLM】{self.planner.backend}:{self.planner.model} で計画を作成中...', C_GRAY)
        try:
            plan: Plan = self.planner.plan(instruction)
        except PlanError as e:
            self.say(f'【エラー】計画を作れませんでした: {e}', C_RED)
            record.update(result='plan_error', error=str(e))
            return self._log(record)
        record.update(backend=plan.backend, model=plan.model,
                      llm_latency_sec=round(plan.latency_sec, 2), plan=plan.raw)
        self.say(f'【LLMの返答】{plan.reply}（{plan.latency_sec:.1f}秒）', C_YELLOW)
        if not plan.understood:
            self.say('【中止】実行しません（ロボットにはできない、または行き先が特定できない指示）', C_RED)
            record['result'] = 'rejected_by_llm'
            return self._log(record)

        self.say('【計画】')
        for i, t in enumerate(plan.tasks, 1):
            if t.action == 'goto':
                self.say(f'  {i}. 移動 → {self.layout.nodes[t.location].name}  {C_GRAY}{t.note}')
            else:
                self.say(f'  {i}. 待機 {t.seconds:.0f}秒  {C_GRAY}{t.note}')

        if self.require_confirm and source in ('terminal', 'demo'):
            question = f'{C_BOLD}薬剤師の確認: この計画で実行しますか？ [Y/n] {C_RESET}'
            if source == 'demo':
                print(question, end='', flush=True)
                ans = type_out('y')
            else:
                ans = input(question)
            if ans.strip().lower() not in ('', 'y', 'yes', 'はい'):
                self.say('【取消】実行しませんでした', C_GRAY)
                record['result'] = 'cancelled_by_pharmacist'
                return self._log(record)

        t0 = time.monotonic()
        try:
            for i, t in enumerate(plan.tasks, 1):
                if t.action == 'goto':
                    self.say(f'【実行 {i}/{len(plan.tasks)}】{self.layout.nodes[t.location].name} へ移動', C_GREEN)
                    self.goto(t.location)
                else:
                    self.say(f'【実行 {i}/{len(plan.tasks)}】{t.seconds:.0f}秒待機（{t.note}）', C_GREEN)
                    end = time.monotonic() + t.seconds
                    while time.monotonic() < end:
                        self.stop()
                        time.sleep(0.1)
            px, py, _ = self._current_pose()
            record.update(result='success', exec_sec=round(time.monotonic() - t0, 1),
                          final_pose=[round(px, 3), round(py, 3)])
            self.say(f'【完了】配送が終わりました（{time.monotonic() - t0:.0f}秒）', C_GREEN + C_BOLD)
        except Aborted as e:
            self.stop()
            record.update(result='aborted', error=str(e))
            self.say(f'【中断】{e}', C_RED)
        self._log(record)

    def _log(self, record: dict):
        with open(self.log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')


def type_out(text: str, delay: float = 0.07) -> str:
    """デモモード用: 人が入力しているように1文字ずつ表示する."""
    for ch in text:
        print(ch, end='', flush=True)
        time.sleep(delay)
    print(flush=True)
    return text


def _read_terminal(node: PharmacyCommander):
    while rclpy.ok():
        try:
            line = input().strip()
        except EOFError:
            return
        if not line:
            continue
        if line.isdigit() and 1 <= int(line) <= len(PRESETS):
            line = PRESETS[int(line) - 1]
        node.requests.put(('terminal', line))
        node.input_done.wait()
        node.input_done.clear()


def main(args=None):
    rclpy.init(args=args)
    node = PharmacyCommander()
    spin = threading.Thread(target=rclpy.spin, args=(node,), daemon=True)
    spin.start()

    print(f'{C_BOLD}=== 薬局配送ロボット（LLM: {node.planner.backend}:{node.planner.model}）==={C_RESET}')
    print('配送先: ' + '、'.join(n.name for n in node.layout.destinations))
    print('日本語で指示を入力してください。番号を入れるとプリセット指示を使います:')
    for i, p in enumerate(PRESETS, 1):
        print(f'  {i}. {p}')
    print(C_GRAY + 'LLMを準備中...' + C_RESET, flush=True)
    try:
        node.planner.plan('調剤室で待機して')    # モデルをGPUに読み込んでおく
    except PlanError as e:
        print(f'{C_RED}LLMに接続できません: {e}{C_RESET}')
    print(C_GREEN + '準備完了。指示をどうぞ。' + C_RESET, flush=True)

    # デモモード: 環境変数 DEMO_SCRIPT にプリセット番号か指示文を「|」区切りで渡すと、
    # 入力を自動化して順に実行する（動画撮影用）
    demo = [s.strip() for s in os.environ.get('DEMO_SCRIPT', '').split('|') if s.strip()]
    if demo:
        print(C_GRAY + '（デモモード: 指示の入力と確認の Y は自動入力しています）' + C_RESET, flush=True)
        try:
            time.sleep(float(os.environ.get('DEMO_START_DELAY', '3')))
            for item in demo:
                text = PRESETS[int(item) - 1] if item.isdigit() else item
                print(f'{C_BOLD}> {C_RESET}', end='', flush=True)
                type_out(text)
                node.process_request('demo', text)
                time.sleep(2.0)
            print(C_GRAY + '\nデモ終了' + C_RESET, flush=True)
            time.sleep(float(os.environ.get('DEMO_END_HOLD', '5')))
        finally:
            node.stop()
            time.sleep(0.2)
            node.destroy_node()
            rclpy.try_shutdown()
        return

    node.input_done = threading.Event()
    threading.Thread(target=_read_terminal, args=(node,), daemon=True).start()
    try:
        while rclpy.ok():
            try:
                source, text = node.requests.get(timeout=0.2)
            except queue.Empty:
                continue
            node.process_request(source, text)
            if source == 'terminal':
                node.input_done.set()
            print(C_GRAY + '\n次の指示をどうぞ（番号でプリセット）:' + C_RESET, flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        node.stop()
        time.sleep(0.2)
        node.destroy_node()
        rclpy.try_shutdown()
        sys.exit(0)


if __name__ == '__main__':
    main()
