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

RUN apk add --no-cache ca-certificates tzdata \
    && addgroup -S app && adduser -S -G app app

COPY --from=builder /out/tdl-web /usr/local/bin/tdl-web

USER app
WORKDIR /data

# Listening port served through the Umbrel app_proxy.
EXPOSE 8080

# tdl-web serves the API + embedded static UI. The app_proxy in
# docker-compose maps the app to the Umbrel UI on port 8080.
ENTRYPOINT ["/usr/local/bin/tdl-web"]
CMD ["-http", ":8080", "-data", "/data"]