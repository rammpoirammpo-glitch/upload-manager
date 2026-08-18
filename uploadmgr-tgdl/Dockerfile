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
    libxcb-image0 libxcb-randr0 libxcb-xinput0 libxau6 libpcre2-16-0 \
    libxkbcommon0 libxkbcommon-x11-0 libfontconfig1 libfreetype6 \
    libxrandr2 libxrender1 libxshmfence1 libxi6 libxext6 libsm6 libice6 \
    libasound2 libpulse0 libdouble-conversion3 libicu70 libdatrie1 libgraphite2-3 \
    libthai0 libharfbuzz0b libx11-xcb1 libxcomposite1 libxcursor1 libxdamage1 \
    libxfixes3 libxinerama1 libxkbfile1 libxss1 libxtst6 \
    libcups2 libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 libsecret-1-0 \
    curl tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/app-src

COPY app/requirements.txt /opt/app-src/requirements.txt
RUN python3 -m pip install --no-cache-dir -r requirements.txt

COPY app/ /opt/app-src/

COPY start.sh /opt/app-src/start.sh
RUN chmod +x /opt/app-src/start.sh

EXPOSE 8080

ENV QT_QPA_PLATFORM=xcb \
    QT_X11_NO_MITSHM=1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/app-src/start.sh"]