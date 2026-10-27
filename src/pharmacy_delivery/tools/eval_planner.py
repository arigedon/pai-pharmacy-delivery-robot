#!/usr/bin/env python3
"""LLM プランナーの解釈精度を評価する（ロボットは動かさない）.

各指示について「訪れる場所の順序」と「受理/拒否」が期待どおりかを判定する。
wait の秒数や note の文言は採点しない。

使い方（pharmacy_delivery/ で実行）:
  python3 tools/eval_planner.py            # LLM_BACKEND / OLLAMA_MODEL などの環境変数に従う
  python3 tools/eval_planner.py --repeat 3 # 同じ指示を3回ずつ（出力の安定性を見る）
"""
import argparse
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from pharmacy_delivery.layout import PharmacyLayout  # noqa: E402
from pharmacy_delivery.llm_planner import LLMPlanner, PlanError  # noqa: E402

REJECT = None
# (指示, 期待する訪問先の順序 / REJECT は拒否されるべき指示)
CASES = [
    ('冷蔵庫からインスリンを取って、3番カウンターに届けて。終わったら調剤室に戻ってね',
     ['fridge', 'counter_3', 'home']),
    ('湿布を外用薬の棚から取って、鑑査してから1番窓口へお願い', ['shelf_b', 'audit', 'counter_1']),
    ('内服薬の棚で錠剤を受け取って、2番カウンターに急ぎで', ['shelf_a', 'counter_2']),
    ('ワクチンを冷所から出して2番窓口へ', ['fridge', 'counter_2']),
    ('点眼薬を取ってきて3番に。そのあと待機場所へ戻って', ['shelf_b', 'counter_3', 'home']),
    ('カプセルの棚から薬を持って、薬剤師の確認を受けてから1番カウンターへ', ['shelf_a', 'audit', 'counter_1']),
    ('鑑査台に行って', ['audit']),
    ('調剤室に戻って', ['home']),
    ('1番カウンターに置いてある薬を鑑査台まで運んで', ['counter_1', 'audit']),
    ('軟膏を棚から取って、2番窓口に届けたら、次に冷蔵庫に寄ってから戻ってきて',
     ['shelf_b', 'counter_2', 'fridge', 'home']),
    ('インスリンを冷蔵庫から鑑査台へ', ['fridge', 'audit']),
    ('3番カウンターの次に1番カウンターにも寄って', ['counter_3', 'counter_1']),
    ('錠剤の棚とシップの棚の両方から薬を取って2番へ', ['shelf_a', 'shelf_b', 'counter_2']),
    ('Bring the insulin from the fridge to counter 3', ['fridge', 'counter_3']),
    ('患者さんの家まで薬を届けて', REJECT),
    ('2番の患者さんに飲み方を説明してきて', REJECT),
    ('2階の倉庫から在庫を持ってきて', REJECT),
    ('お店の外の駐車場まで行って', REJECT),
    ('どこかに行って', REJECT),
    ('4番カウンターに届けて', REJECT),
]

# プロンプト調整に一度も使っていない指示（最後に1回だけ評価する）
HOLDOUT = [
    ('冷蔵庫の中の目薬を1番の窓口までお願いします', ['fridge', 'counter_1']),
    ('外用薬の棚からテープ剤を取ってきて。鑑査を通してから3番カウンターへ', ['shelf_b', 'audit', 'counter_3']),
    ('2番カウンターから戻ってきた薬を調剤室に持って帰って', ['counter_2', 'home']),
    ('内服薬の棚に寄ってから、冷蔵庫にも寄って、最後に鑑査台へ', ['shelf_a', 'fridge', 'audit']),
    ('鑑査が終わった薬を2番へ届けて、そのあと元の場所で待機', ['audit', 'counter_2', 'home']),
    ('錠剤の棚から1番カウンターへ。急ぎです', ['shelf_a', 'counter_1']),
    ('冷蔵庫から3番へ持っていったら、ついでに外用薬の棚も見てきて', ['fridge', 'counter_3', 'shelf_b']),
    ('Please take the ointment from the shelf to counter 2', ['shelf_b', 'counter_2']),
    ('5番カウンターに薬を届けて', REJECT),
    ('隣の病院まで処方箋を取りに行って', REJECT),
    ('待合室の患者さんに声をかけてきて', REJECT),
    ('適当に動いて', REJECT),
]


def visited(plan):
    return [t.location for t in plan.tasks if t.action == 'goto']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--repeat', type=int, default=1)
    ap.add_argument('--out', default=None, help='結果を書き出す JSON ファイル')
    ap.add_argument('--set', choices=['dev', 'holdout'], default='dev',
                    help='dev=調整に使った20件 / holdout=調整に使っていない12件')
    args = ap.parse_args()
    cases = CASES if args.set == 'dev' else HOLDOUT

    layout = PharmacyLayout.load(os.path.join(os.path.dirname(HERE), 'config', 'pharmacy_layout.yaml'))
    planner = LLMPlanner(layout)
    planner.plan('調剤室で待機して')  # ウォームアップ（モデル読み込み時間を計測から除く）

    rows, latencies = [], []
    for instruction, expected in cases:
        for r in range(args.repeat):
            try:
                plan = planner.plan(instruction)
                latencies.append(plan.latency_sec)
                got = visited(plan) if plan.understood else REJECT
                ok = got == expected
                detail = plan.reply
            except PlanError as e:
                got, ok, detail = 'ERROR', False, str(e)
            rows.append({'instruction': instruction, 'expected': expected, 'got': got,
                         'ok': ok, 'reply': detail})
            mark = '○' if ok else '×'
            print(f'{mark} {instruction}\n    期待: {expected}\n    結果: {got}  {detail}', flush=True)

    n_ok = sum(r['ok'] for r in rows)
    accept = [r for r in rows if r['expected'] is not REJECT]
    reject = [r for r in rows if r['expected'] is REJECT]
    summary = {
        'backend': planner.backend, 'model': planner.model, 'n': len(rows),
        'accuracy': round(n_ok / len(rows), 3),
        'accept_accuracy': round(sum(r['ok'] for r in accept) / len(accept), 3),
        'reject_accuracy': round(sum(r['ok'] for r in reject) / len(reject), 3),
        'latency_median_sec': round(statistics.median(latencies), 2),
        'latency_max_sec': round(max(latencies), 2),
    }
    print('\n=== 集計 ===')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            json.dump({'summary': summary, 'rows': rows}, f, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
