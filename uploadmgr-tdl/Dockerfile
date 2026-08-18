# Build the tdl-web GUI (a Telegram downloader/uploader/forwarder web app).
FROM golang:1.25-alpine AS builder

ENV GOTOOLCHAIN=auto CGO_ENABLED=0

RUN apk add --no-cache ca-certificates tzdata

WORKDIR /build
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN go build -trimpath -ldflags="-s -w" -o /out/tdl-web .

# ---- runtime ----
FROM alpine:3.20

# Runs as root (the Umbrel default). Umbrel mounts ${APP_DATA_DIR}/data at
# /data owned by root, so a non-root container cannot open its bbolt state DB
# and dies on startup (no UI). Root guarantees the app always starts.
RUN apk add --no-cache ca-certificates tzdata

COPY --from=builder /out/tdl-web /usr/local/bin/tdl-web

WORKDIR /data

# Listening port served through the Umbrel app_proxy.
# Uses 8050 to avoid clashing with anything already on 8080.
EXPOSE 8050

# tdl-web serves the API + embedded static UI. The app_proxy in
# docker-compose maps the app to the Umbrel UI on port 8050.
# We mkdir -p /data first so the state DB always has a home.
ENTRYPOINT ["/bin/sh", "-c"]
CMD ["mkdir -p /data && exec /usr/local/bin/tdl-web -http :8050 -data /data"]