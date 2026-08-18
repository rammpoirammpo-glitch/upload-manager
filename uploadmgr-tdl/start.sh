#!/bin/sh
# tdl is a headless CLI tool. To give it a valid Umbrel app port we serve a
# lightweight status/help page with busybox httpd while tdl stays available
# via `docker exec`.
set -e

mkdir -p /data/web
if [ ! -f /data/web/index.html ] && [ -f /web/index.html ]; then
  cp /web/index.html /data/web/index.html
fi

echo "[tdl] serving status page on :8080"
exec busybox httpd -p 8080 -h /data/web -f
