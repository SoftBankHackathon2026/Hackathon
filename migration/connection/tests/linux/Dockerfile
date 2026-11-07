# Isolated test runtime only. Product release images have a separate build.
FROM debian:trixie-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       iproute2 wireguard-tools nftables libssl3t64 python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
CMD ["sleep", "infinity"]
