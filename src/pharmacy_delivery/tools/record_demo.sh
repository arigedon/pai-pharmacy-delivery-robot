#!/bin/bash
# デモを自動実行し、デスクトップ(:0)を録画する（コンテナ内で ubuntu ユーザーとして実行）
#   bash tools/record_demo.sh "1|4番カウンターに届けて|5" /home/ubuntu/pai_final_ws/videos/demo_raw.mp4

DEMO="${1:-1|2|4番カウンターに届けて|5}"
OUT="${2:-$HOME/pai_final_ws/videos/demo_raw.mp4}"
export DISPLAY=:0
mkdir -p "$(dirname "$OUT")"
source /opt/ros/humble/setup.bash
source "$HOME/pai_final_ws/install/setup.bash"

pkill -f "[i]gn gazebo"; pkill -f "[p]arameter_bridge"; sleep 1
ros2 launch pharmacy_delivery pharmacy.launch.py > /tmp/pharmacy.log 2>&1 &
LAUNCH=$!
sleep 25   # Gazebo の起動待ち

ffmpeg -loglevel error -y -f x11grab -video_size 1920x1080 -framerate 20 -i :0.0 \
  -c:v libx264 -preset veryfast -crf 22 -pix_fmt yuv420p "$OUT" &
FFMPEG=$!

# xterm は UTF-8 のロケールで起動しないと日本語が化け、一部のバイトが制御コードとして解釈される
# 英数字(-fa)と全角文字(-fd)のフォントを分けないと、英数字まで全角幅になり間延びする
LANG=ja_JP.UTF-8 LC_ALL=ja_JP.UTF-8 xterm -u8 -fa 'DejaVu Sans Mono' -fd 'Noto Sans CJK JP' -fs 11 \
  -geometry 76x55+1250+32 \
  -bg '#1b1d23' -fg '#e6e6e6' -T 'pharmacy_delivery commander' \
  -e bash -c "source /opt/ros/humble/setup.bash; source $HOME/pai_final_ws/install/setup.bash; \
    export LANG=C.UTF-8 PYTHONUNBUFFERED=1 OLLAMA_URL=${OLLAMA_URL:-http://host.docker.internal:11434} \
      DEMO_SCRIPT='$DEMO' DEMO_START_DELAY=4 DEMO_END_HOLD=6; \
    ros2 run pharmacy_delivery commander"

kill -INT $FFMPEG; wait $FFMPEG
kill -INT $LAUNCH; sleep 3; pkill -f "[i]gn gazebo"
echo "録画しました: $OUT"
