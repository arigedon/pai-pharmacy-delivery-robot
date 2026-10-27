# 日本語で指示できる薬局内配送ロボット（ROS 2 × Ignition Gazebo × ローカルLLM）

Physical AI講座2026 応用編1 最終課題。

薬剤師が「冷蔵庫からインスリンを取って、3番カウンターに届けて。終わったら調剤室に戻ってね」と日本語で指示すると、
差動二輪ロボットが薬局内を自律走行して配送する。

```
日本語の指示 ─▶ ローカルLLM（qwen3:8b） ─▶ タスク列(JSON) ─▶ 検証 ─▶ 薬剤師の確認 ─▶ 経路探索＋走行制御 ─▶ /cmd_vel
                  「どこへ行き何をするか」         スキーマ制約     ルール照合                 通路グラフの最短経路
                   だけを決める                    (enum)                                    オドメトリでフィードバック
```

## 工夫した点

1. **LLMに「走り方」を決めさせない**
   LLMが出すのは「行き先と待機」の列だけ。経路は、家具から0.45m以上離れていることを自動検証した通路グラフ上の最短経路（ダイクストラ法）。
   速度はオドメトリを使ったフィードバック制御が決める。LLMが誤っても、棚に突っ込む指令は出ない。
2. **3段の安全策**
   - 出力をJSONスキーマで制約し、行き先は登録済みの8か所（enum）からしか選べない
   - 計画をプログラムで再検証（未登録の場所・待ち時間の範囲・タスク数・連続する同じ移動）
   - 実行前に薬剤師が計画を確認する（human-in-the-loop）
3. **スキーマ制約の落とし穴を評価で見つけて対策した**
   enumで縛ると「4番カウンター」のような存在しない場所が、最も近い `counter_3` に**強制的に読み替えられる**ことが評価で分かった。
   出力の先頭に `unknown_places`（一覧にない場所）を書き出させ、さらにその内容を登録名・別名とルールで照合することで解消した。
4. **ローカルLLM**：RTX 3060上のOllamaで動かす。患者情報を含みうる指示文を外部APIに送らない。
5. **レイアウトを1つのYAMLで管理**：同じ `pharmacy_layout.yaml` から、Gazeboのワールド生成・LLMへの場所一覧・経路グラフ・安全性テストをすべて作る。

## 評価

指示の解釈（ロボットは動かさない）。各指示を3回ずつ実行し、「訪れる場所の順序」と「受理／拒否」が期待どおりかを判定。
評価スクリプト: [`tools/eval_planner.py`](src/pharmacy_delivery/tools/eval_planner.py)、結果: [`eval/`](src/pharmacy_delivery/eval/)

| 条件 | 指示 | 正解率 | 受理すべき指示 | 拒否すべき指示 |
|---|---|---|---|---|
| 対策前（enum制約のみ） | 調整用20件×3 | 95.0% | 100% | 83.3%（「4番カウンター」を3番に読み替え） |
| 対策後 | 調整用20件×3 | 100% | 100% | 100% |
| 対策後 | **調整に使っていない12件×3** | **91.7%** | 87.5% | 100% |

- 調整用の20件はプロンプトを直すときに見ていたため、過大評価の可能性がある。最後に一度だけ、未使用の12件で評価した。
- 未使用の12件での誤りは、英語の "the shelf"（棚はAとBの2つある）を「一覧にない場所」として確認を求めた1件（3回とも）。
  誤った場所へ運ぶのではなく、止まって聞き返す側の失敗。
- LLMの計画作成時間：中央値 約4.5秒、最大 約6.4秒（qwen3:8b、RTX 3060、Ollama）。

走行（シミュレーション）：デモの配送はすべて完了し、目的地への到着誤差は5cm以内（許容値5cm）。

単体テスト：17件（通路と障害物の距離、全配送先への到達可能性、計画の検証ルール）
`python3 -m pytest src/pharmacy_delivery/test`

## 構成

```
src/
├── pharmacy_delivery/            ← 本課題で作成
│   ├── config/pharmacy_layout.yaml   配送先・通路グラフ・障害物（単一の情報源）
│   ├── config/bridge.yaml            ROS 2 ⇔ Gazebo（/cmd_vel, /odom, /clock）
│   ├── pharmacy_delivery/
│   │   ├── layout.py                 レイアウト読込・ダイクストラ・通路の安全距離計算
│   │   ├── llm_planner.py            LLM（Ollama / Gemini）・スキーマ・計画の検証
│   │   └── commander.py              司令ノード（指示→計画→確認→走行→ログ）
│   ├── tools/gen_world.py            YAML から Gazebo のワールドを生成
│   ├── tools/eval_planner.py         指示解釈の評価
│   ├── tools/record_demo.sh          デモの自動実行と録画
│   ├── worlds/pharmacy_world.sdf     生成された薬局ワールド
│   └── test/                         単体テスト
└── nav_dev/                      ← 参考記事の再現（差動二輪ロボットのモデルとキー操作）
```

## 動かし方

### 1. 環境
- Docker Desktop（Windows）と、講座のイメージ `airobotbook/ros2-desktop-ai-robot-book-humble`
  （ROS 2 Humble・Ignition Gazebo Fortress・ros_ign_bridge 入り）
- [Ollama](https://ollama.com/) をホストで起動し、`ollama pull qwen3:8b`

### 2. ワークスペースの準備
```bash
git clone <このリポジトリ> pai_final_ws
cd pai_final_ws/src
# 参考記事と同じく、キー入力パッケージを取得（nav_dev の teleop が使う）
git clone https://github.com/nek009/key_event.git /tmp/key_event
cp -r /tmp/key_event/key_event_msgs /tmp/key_event/key_event_nodes .
```

### 3. コンテナを起動してビルド
```powershell
docker run -d --name pai_final -p 6080:80 --shm-size=1g `
  -v "<pai_final_ws/src の絶対パス>:/home/ubuntu/pai_final_ws/src" `
  airobotbook/ros2-desktop-ai-robot-book-humble:latest
```
ブラウザで http://localhost:6080 を開き、端末で:
```bash
cd ~/pai_final_ws && source /opt/ros/humble/setup.bash
colcon build --symlink-install && source install/setup.bash
ros2 launch pharmacy_delivery pharmacy.launch.py          # Gazebo とブリッジ
```

### 4. 司令ノード（別の端末）
```bash
export OLLAMA_URL=http://host.docker.internal:11434      # ホストの Ollama
ros2 run pharmacy_delivery commander
```
日本語で指示を入力する（番号 1〜5 でプリセット指示）。トピックからも指示できる:
```bash
ros2 topic pub --once /delivery_request std_msgs/msg/String "{data: 内服薬の棚から2番カウンターへ}"
```

Gemini API を使う場合は `LLM_BACKEND=gemini GEMINI_API_KEY=...`（既定モデル `gemini-3.8-flash`）。

### レイアウトを変えるとき
`config/pharmacy_layout.yaml` を編集 → `python3 tools/gen_world.py` → `colcon build` → `pytest` で通路の安全距離を確認。

## 参考・利用したもの

- 差動二輪ロボットのモデル・teleop：[OsawaKousei/2DRoboPrac_ws](https://github.com/OsawaKousei/2DRoboPrac_ws)（Apache-2.0）、
  解説記事 https://qiita.com/N622/items/9d89f77d85d9da0af29e
- キー入力：[nek009/key_event](https://github.com/nek009/key_event)（本リポジトリには含めていない）
- LLM：Qwen3 8B（Ollama）

## ライセンス

Apache License 2.0（[LICENSE](LICENSE)、[NOTICE](NOTICE)）
