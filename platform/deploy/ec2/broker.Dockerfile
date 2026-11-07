FROM rust:1.93-slim-trixie AS rust-toolchain

FROM ubuntu:24.04 AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential perl pkg-config curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=rust-toolchain /usr/local/cargo /usr/local/cargo
COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup
ENV PATH=/usr/local/cargo/bin:$PATH RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo
ARG OPENSSL_VERSION=3.5.9
ARG OPENSSL_SHA256=603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a
RUN curl -fSL "https://github.com/openssl/openssl/releases/download/openssl-${OPENSSL_VERSION}/openssl-${OPENSSL_VERSION}.tar.gz" -o /tmp/openssl.tar.gz \
    && echo "${OPENSSL_SHA256}  /tmp/openssl.tar.gz" | sha256sum -c - \
    && mkdir /tmp/openssl-src && tar -xzf /tmp/openssl.tar.gz --strip-components=1 -C /tmp/openssl-src \
    && cd /tmp/openssl-src && ./Configure --prefix=/opt/openssl --openssldir=/usr/lib/ssl no-shared no-tests \
    && make -j"$(nproc)" && make install_sw \
    && rm -rf /tmp/openssl-src /tmp/openssl.tar.gz
ENV OPENSSL_DIR=/opt/openssl OPENSSL_STATIC=1
WORKDIR /build/agent
COPY agent/Cargo.toml agent/Cargo.lock ./
COPY agent/src ./src
COPY agent/tests ./tests
COPY agent/examples ./examples
ARG MIGRATION_RELEASE_MLDSA_PUBLIC_KEY
ARG MIGRATION_RELEASE_ED25519_PUBLIC_KEY
ARG MIGRATION_MIN_RELEASE_SEQUENCE=1
ENV MIGRATION_RELEASE_MLDSA_PUBLIC_KEY=$MIGRATION_RELEASE_MLDSA_PUBLIC_KEY \
    MIGRATION_RELEASE_ED25519_PUBLIC_KEY=$MIGRATION_RELEASE_ED25519_PUBLIC_KEY \
    MIGRATION_MIN_RELEASE_SEQUENCE=$MIGRATION_MIN_RELEASE_SEQUENCE
RUN test -n "$MIGRATION_RELEASE_MLDSA_PUBLIC_KEY" && test -n "$MIGRATION_RELEASE_ED25519_PUBLIC_KEY" \
    && cargo build --locked --release \
    && mkdir /out \
    && cp target/release/migration-connector target/release/migration-key-broker target/release/migration-net-helper /out/ \
    && ! ldd /out/migration-key-broker | grep -E 'libssl|libcrypto' \
    && rm -rf target /usr/local/cargo/registry

FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends \
    iproute2 wireguard-tools nftables ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -m 0755 /etc/migration-connector /usr/local/libexec /opt/migration/bin
COPY --from=build /out/migration-key-broker /out/migration-net-helper /usr/local/libexec/
COPY --from=build /out/migration-connector /out/migration-net-helper /opt/migration/bin/
ENTRYPOINT ["/usr/local/libexec/migration-key-broker"]
CMD ["/etc/migration-connector/broker.json"]
