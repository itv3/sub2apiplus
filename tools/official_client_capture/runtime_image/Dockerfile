# ARM64 官方客户端抓包运行时。正式 Campaign 必须绑定构建后镜像的 RepoDigest，
# 不能把本文件中的浮动基础镜像标签当作运行身份。
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        bubblewrap \
        ca-certificates \
        curl \
        git \
        iproute2 \
        jq \
        less \
        mitmproxy \
        openssl \
        procps \
        python3 \
        python3-zstandard \
        ripgrep \
        tcpdump \
        tini \
        tshark \
        util-linux \
    && python3 -c 'import zstandard; print(zstandard.__version__)' \
    && rm -rf /var/lib/apt/lists/*

COPY capture-entrypoint /usr/local/bin/capture-entrypoint
RUN chmod 0755 /usr/local/bin/capture-entrypoint

LABEL org.opencontainers.image.title="Sub2APIPlus 官方客户端抓包运行时"

ENTRYPOINT ["/usr/local/bin/capture-entrypoint"]
CMD ["bash", "-lc", "umask 077; exec sleep infinity"]
