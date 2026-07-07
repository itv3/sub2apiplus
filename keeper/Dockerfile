FROM golang:1.24-bookworm AS builder

WORKDIR /src
COPY go.mod go.sum* ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/sub2apiplus-keeper .

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    HOME=/root \
    CODEX_NON_INTERACTIVE=1 \
    CODEX_INSTALL_DIR=/usr/local/bin \
    CODEX_HOME=/opt/codex-home

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      bash \
      bubblewrap \
      ca-certificates \
      coreutils \
      curl \
      findutils \
      git \
      jq \
      openssh-client \
      procps \
      ripgrep \
      tar \
      gzip \
      tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/codex-home /root/.local/bin /root/.claude \
    && curl -fsSL https://chatgpt.com/codex/install.sh | sh \
    && curl -fsSL https://claude.ai/install.sh | bash \
    && ln -sf /root/.local/bin/claude /usr/local/bin/claude \
    && codex --version >/tmp/codex-version.txt \
    && claude --version >/tmp/claude-version.txt

WORKDIR /app
COPY --from=builder /out/sub2apiplus-keeper /app/sub2apiplus-keeper

EXPOSE 38090
ENTRYPOINT ["/app/sub2apiplus-keeper"]
