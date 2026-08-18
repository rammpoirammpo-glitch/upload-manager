FROM golang:1.23-alpine AS builder

RUN apk add --no-cache git ca-certificates build-base

WORKDIR /build

# Build tdl from the official upstream source (AGPL-3.0).
ARG TDL_VERSION=v0.20.3
RUN git clone --depth 1 --branch ${TDL_VERSION} https://github.com/iyear/tdl.git . \
    && go mod download \
    && CGO_ENABLED=0 go build -trimpath -o /out/tdl .

FROM alpine:3.20

RUN apk add --no-cache ca-certificates tzdata \
    && addgroup -S tdl && adduser -S -G tdl tdl

COPY --from=builder /out/tdl /usr/local/bin/tdl

USER tdl
WORKDIR /data

ENTRYPOINT ["/bin/sh", "-c"]
CMD ["while true; do sleep 3600; done"]
