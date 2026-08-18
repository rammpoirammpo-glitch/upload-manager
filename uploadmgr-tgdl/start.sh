#!/bin/bash
set -e

export DISPLAY=:1
export QT_QPA_PLATFORM=xcb
export XDG_RUNTIME_DIR=/tmp/xdg
mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"

echo "[start] launching Xvfb on :1..."
Xvfb :1 -screen 0 ${RESOLUTION}x24 -nolisten tcp &
XVFB_PID=$!
sleep 2

echo "[start] launching window manager (fluxbox)..."
fluxbox &
sleep 1

echo "[start] launching VNC server (x11vnc) on 5900..."
x11vnc -display :1 -rfbport 5900 -shared -forever -nopw -quiet -bg
sleep 1

echo "[start] launching noVNC web transport on 8080 -> 5900..."
websockify --web=/usr/share/novnc 8080 localhost:5900 &
sleep 2

echo "[start] starting Telegram Media Downloader GUI..."
# First run: seed the persistent /data volume with the app source + config
if [ ! -f /data/src/gui.py ]; then
  echo "[start] seeding /data with app source..."
  cp -r /opt/app-src/. /data/
fi
# Provide a working .env (Telethon reads API_ID / API_HASH)
if [ -f /data/.env.local ]; then cp /data/.env.local /data/.env 2>/dev/null || true; fi
cd /data
python3 -u src/gui.py

echo "[start] app exited, stopping services..."
kill $XVFB_PID 2>/dev/null || true
exit 0