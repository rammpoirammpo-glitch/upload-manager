FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/home/app \
    DISPLAY=:1 \
    RESOLUTION=1280x800

# System packages: X virtual framebuffer, VNC + noVNC web transport,
# a lightweight window manager, and every Qt/X11 library PySide6 needs.
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc websockify novnc fluxbox x11-utils xauth xdg-utils \
    fontconfig fonts-dejavu-core fonts-ubuntu \
    python3 python3-pip python3-venv \
    libgl1 libgl1-mesa-glx libegl1 libgles2 libglib2.0-0 libdbus-1-3 \
    libxcb1 libxcb-xinerama0 libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 \
    libxcb-render-util0 libxcb-shape0 libxcb-xfixes0 libxcb-xkb1 \
    libxkbcommon0 libxkbcommon-x11-0 libfontconfig1 libfreetype6 \
    libxrandr2 libxrender1 libxshmfence1 libxi6 libxext6 libsm6 libice6 \
    libasound2 libpulse0 libdouble-conversion3 libicu70 libdatrie1 libgraphite2-3 \
    libthai0 libharfbuzz0b libx11-xcb1 libxcomposite1 libxcursor1 libxdamage1 \
    libxfixes3 libxinerama1 libxkbfile1 libxss1 libxtst6 \
    curl tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/app-src

COPY app/requirements.txt /opt/app-src/requirements.txt
RUN python3 -m pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

COPY app/ /opt/app-src/

# Provide a working .env for the app (Telethon reads API_ID / API_HASH).
RUN if [ -f /app/.env.local ]; then cp /app/.env.local /app/.env; fi

COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

EXPOSE 8080

ENV QT_QPA_PLATFORM=xcb \
    QT_X11_NO_MITSHM=1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/start.sh"]