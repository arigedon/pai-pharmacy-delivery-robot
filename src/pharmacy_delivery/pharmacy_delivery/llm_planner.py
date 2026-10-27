"""LLM で日本語の配送指示を「タスク列(JSON)」に変換する.

LLM には「どこへ行き、何をするか」だけを決めさせる。
「どう走るか」（経路・速度）は決めさせない。経路は通路グラフの最短経路、
速度はオドメトリを使ったフィードバック制御が決める。

バックエンド（環境変数 LLM_BACKEND で切り替え）:
  - ollama（既定）: ローカルGPU上のLLM。指示文を外部に送らない
  - gemini       : Google Gemini API（GEMINI_API_KEY が必要）
"""
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import List, Optional

from .layout import PharmacyLayout

MAX_TASKS = 12
MAX_WAIT_SEC = 30.0


@dataclass
class Task:
    action: str                 # 'goto' | 'wait'
    location: Optional[str] = None
    seconds: float = 0.0
    note: str = ''


@dataclass
class Plan:
    understood: bool
    reply: str
    tasks: List[Task]
    raw: dict
    backend: str
    model: str
    latency_sec: float


class PlanError(Exception):
    pass


_UNKNOWN_RULE = """   まず、指示に出てくる場所のうち上の一覧に無いもの（例: 2階の倉庫、駐車場、事務室）を、
   指示の言葉のまま unknown_places に書き出す。一覧にある場所は書かない。無ければ空の配列にする。
   投薬カウンターは1番・2番・3番の3つだけ。それ以外の番号のカウンターは一覧に無い場所として扱う。
   unknown_places が空でなければ、似た場所に読み替えず、必ず understood を false にする"""


def unknown_check_enabled() -> bool:
    """「一覧にない場所」を先に書き出させる対策の有効/無効（評価で比較するため切り替え可能）."""
    return os.environ.get('PLANNER_UNKNOWN_CHECK', '1') != '0'


def build_system_prompt(layout: PharmacyLayout, unknown_check: bool = True) -> str:
    lines = []
    for n in layout.destinations:
        alias = '、'.join(n.aliases)
        lines.append(f'- {n.key}: {n.name}（別名: {alias}）')
    places = '\n'.join(lines)
    return f"""あなたは保険薬局の調剤室で働く配送ロボットのタスクプランナーです。
薬剤師の日本語の指示を、ロボットが実行するタスク列に変換してください。

# ロボットが行ける場所（location にはこのキーだけを使う）
{places}

# 使えるタスク
- goto: location へ移動する。note に目的（例:「インスリンを受け取る」）を書く
- wait: その場で seconds 秒待つ。荷物の積み込み・受け渡し・薬剤師の確認待ちに使う

# ルール
1. 荷物を「取りに行く」場所に着いたら wait 3秒（積み込み）を入れる
2. 荷物を「届ける」場所に着いたら wait 3秒（受け渡し）を入れる
3. 「鑑査してから」「確認してから」などの指示があれば、届ける前に audit へ行き wait 5秒を入れる
4. 「終わったら戻って」などの指示があれば最後に home へ goto する。戻る指示が無ければ home を付け足さない
5. 上の一覧にない場所（例: 外、2階、患者の自宅）が指示に含まれる、または行き先が特定できない場合は、
   understood を false にし、tasks を空にして、reply で何が分からないかを聞き返す
{_UNKNOWN_RULE if unknown_check else ''}
6. 患者への説明や服薬指導など、ロボットにできない・してはいけない業務を求められた場合も understood を false にし、
   reply で薬剤師が行う必要があることを伝える
7. reply には、実行する内容を薬剤師が確認できるよう、日本語で1〜2文で要約する
8. タスクは最大{MAX_TASKS}個まで

# 例
指示: 内服薬の棚から薬を取って2番カウンターへ
出力: {{{'"unknown_places": [], ' if unknown_check else ''}"understood": true, "reply": "薬品棚Aで薬を受け取り、投薬カウンター2番へ届けます。",
 "tasks": [{{"action": "goto", "location": "shelf_a", "note": "内服薬を受け取る"}},
           {{"action": "wait", "seconds": 3, "note": "積み込み"}},
           {{"action": "goto", "location": "counter_2", "note": "内服薬を届ける"}},
           {{"action": "wait", "seconds": 3, "note": "受け渡し"}}]}}
"""


def json_schema(layout: PharmacyLayout, unknown_check: bool = True) -> dict:
    """出力形式の JSON Schema。location は配送先キーの enum で制約する.

    enum で縛ると、一覧にない場所（例: 4番カウンター）も最も近い候補（counter_3）に
    強制的に読み替えられてしまう。そこで unknown_places を最初の項目に置き、
    行き先を選ぶ前に「一覧にない場所」を書き出させる。
    """
    keys = [n.key for n in layout.destinations]
    head = {'unknown_places': {'type': 'array', 'items': {'type': 'string'}}} if unknown_check else {}
    return {
        'type': 'object',
        'properties': {
            **head,
            'understood': {'type': 'boolean'},
            'reply': {'type': 'string'},
            'tasks': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'action': {'type': 'string', 'enum': ['goto', 'wait']},
                        'location': {'type': 'string', 'enum': keys},
                        'seconds': {'type': 'number'},
                        'note': {'type': 'string'},
                    },
                    'required': ['action'],
                },
            },
        },
        'required': [*head, 'understood', 'reply', 'tasks'],
    }


def _gemini_schema(schema: dict) -> dict:
    """JSON Schema を Gemini の responseSchema 形式（型名が大文字）に変換する."""
    out = {}
    for k, v in schema.items():
        if k == 'type' and isinstance(v, str):
            out[k] = v.upper()
        elif isinstance(v, dict):
            out[k] = _gemini_schema(v)
        else:
            out[k] = v
    return out


def validate(data: dict, layout: PharmacyLayout) -> List[Task]:
    """LLM の出力を検証する。スキーマで制約していても、実行前にもう一度確かめる."""
    if not isinstance(data.get('tasks'), list):
        raise PlanError('tasks が配列ではありません')
    if len(data['tasks']) > MAX_TASKS:
        raise PlanError(f'タスクが多すぎます（{len(data["tasks"])}個 > {MAX_TASKS}個）')
    allowed = {n.key for n in layout.destinations}
    tasks = []
    for i, t in enumerate(data['tasks']):
        action = t.get('action')
        note = str(t.get('note', ''))
        if action == 'goto':
            loc = t.get('location')
            if loc not in allowed:
                raise PlanError(f'タスク{i + 1}: 行き先 {loc!r} はレイアウトにありません')
            last_goto = next((p for p in reversed(tasks) if p.action == 'goto'), None)
            if last_goto and last_goto.location == loc:
                continue                    # すでにいる場所への移動は省く
            tasks.append(Task('goto', location=loc, note=note))
        elif action == 'wait':
            sec = float(t.get('seconds', 0))
            if not 0 < sec <= MAX_WAIT_SEC:
                raise PlanError(f'タスク{i + 1}: 待ち時間 {sec} 秒は範囲外です（0〜{MAX_WAIT_SEC}秒）')
            tasks.append(Task('wait', seconds=sec, note=note))
        else:
            raise PlanError(f'タスク{i + 1}: 未知の action {action!r}')
    return tasks


def _post_json(url: str, body: dict, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json', **headers}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', 'replace')[:300]
        raise PlanError(f'LLM API エラー {e.code}: {detail}') from e
    except urllib.error.URLError as e:
        raise PlanError(f'LLM API に接続できません（{url}）: {e.reason}') from e


class LLMPlanner:
    def __init__(self, layout: PharmacyLayout, backend: Optional[str] = None,
                 model: Optional[str] = None, timeout: float = 120.0):
        self.layout = layout
        self.backend = (backend or os.environ.get('LLM_BACKEND', 'ollama')).lower()
        self.timeout = timeout
        self.unknown_check = unknown_check_enabled()
        self.system_prompt = build_system_prompt(layout, self.unknown_check)
        self.schema = json_schema(layout, self.unknown_check)
        if self.backend == 'ollama':
            self.model = model or os.environ.get('OLLAMA_MODEL', 'qwen3:8b')
            self.url = os.environ.get('OLLAMA_URL', 'http://localhost:11434').rstrip('/') + '/api/chat'
        elif self.backend == 'gemini':
            self.model = model or os.environ.get('GEMINI_MODEL', 'gemini-3.8-flash')
            self.api_key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
            if not self.api_key:
                raise PlanError('環境変数 GEMINI_API_KEY が設定されていません')
            self.url = ('https://generativelanguage.googleapis.com/v1beta/models/'
                        f'{self.model}:generateContent')
        else:
            raise PlanError(f'未知の LLM_BACKEND: {self.backend}')

    def _call(self, instruction: str) -> str:
        if self.backend == 'ollama':
            body = {
                'model': self.model,
                'messages': [{'role': 'system', 'content': self.system_prompt},
                             {'role': 'user', 'content': instruction}],
                'format': self.schema,      # JSON Schema で出力を制約
                'think': False,
                'stream': False,
                'keep_alive': '60m',        # デモ中にモデルがGPUから外れないようにする
                'options': {'temperature': 0.1},
            }
            resp = _post_json(self.url, body, {}, self.timeout)
            try:
                return resp['message']['content']
            except KeyError as e:
                raise PlanError(f'Ollama の応答を解釈できません: {str(resp)[:300]}') from e
        body = {
            'systemInstruction': {'parts': [{'text': self.system_prompt}]},
            'contents': [{'role': 'user', 'parts': [{'text': instruction}]}],
            'generationConfig': {'temperature': 0.1, 'responseMimeType': 'application/json',
                                 'responseSchema': _gemini_schema(self.schema)},
        }
        resp = _post_json(self.url, body, {'x-goog-api-key': self.api_key}, self.timeout)
        try:
            return resp['candidates'][0]['content']['parts'][0]['text']
        except (KeyError, IndexError) as e:
            raise PlanError(f'Gemini の応答を解釈できません: {str(resp)[:300]}') from e

    def plan(self, instruction: str) -> Plan:
        t0 = time.monotonic()
        text = self._call(instruction)
        latency = time.monotonic() - t0
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise PlanError(f'LLM の出力が JSON ではありません: {text[:300]}') from e
        understood = bool(data.get('understood'))
        reply = str(data.get('reply', ''))
        unknown = [str(p) for p in data.get('unknown_places') or []] if self.unknown_check else []
        # LLM が「一覧にない」と判断した場所を、レイアウトの名前・別名と照合して検証する
        unknown = [p for p in unknown if not self.layout.is_known_place(p)]
        if unknown:
            # LLM が understood=true を返していても、一覧にない場所があれば実行しない
            understood = False
            reply = f'一覧にない場所が含まれています: {"、".join(unknown)}。行き先を確認してください。'
        tasks = validate(data, self.layout) if understood else []
        if understood and not tasks:
            raise PlanError('understood=true なのにタスクが空です')
        return Plan(understood, reply, tasks, data, self.backend, self.model, latency)
