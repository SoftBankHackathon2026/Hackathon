# Build context: migration/connection. Supply reviewed, digest-pinned images
# and an OpenSSL 3.5 source archive plus its independently verified checksum.
ARG RUST_IMAGE
ARG UBUNTU_IMAGE=ubuntu:24.04
FROM ${RUST_IMAGE} AS rust_toolchain

FROM ${UBUNTU_IMAGE} AS build
ARG OPENSSL_TARBALL
ARG OPENSSL_SHA256
ARG MIGRATION_RELEASE_MLDSA_PUBLIC_KEY
ARG MIGRATION_RELEASE_ED25519_PUBLIC_KEY
ARG MIGRATION_MIN_RELEASE_SEQUENCE
ENV DEBIAN_FRONTEND=noninteractive \
    RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:/opt/openssl/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    OPENSSL_DIR=/opt/openssl \
    OPENSSL_STATIC=1 \
    PKG_CONFIG_ALL_STATIC=1
COPY --from=rust_toolchain /usr/local/rustup /usr/local/rustup
COPY --from=rust_toolchain /usr/local/cargo /usr/local/cargo
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential perl pkg-config ca-certificates binutils python3 \
    && rm -rf /var/lib/apt/lists/*
COPY ${OPENSSL_TARBALL} /tmp/openssl.tar.gz
RUN test -n "$OPENSSL_SHA256" \
    && test "${#OPENSSL_SHA256}" -eq 64 \
    && printf '%s  %s\n' "$OPENSSL_SHA256" /tmp/openssl.tar.gz | sha256sum --check --status \
    && mkdir /tmp/openssl-src \
    && tar -xzf /tmp/openssl.tar.gz --strip-components=1 -C /tmp/openssl-src \
    && cd /tmp/openssl-src \
    && ./Configure --prefix=/opt/openssl --openssldir=/etc/ssl no-shared no-tests \
    && make -j2 \
    && make install_sw \
    && /opt/openssl/bin/openssl version | grep '^OpenSSL 3\.5\.' \
    && rm -rf /tmp/openssl-src /tmp/openssl.tar.gz
WORKDIR /src/agent
COPY agent/Cargo.toml agent/Cargo.lock ./
COPY agent/src ./src
COPY agent/tests ./tests
RUN test -n "$MIGRATION_RELEASE_MLDSA_PUBLIC_KEY" \
    && test -n "$MIGRATION_RELEASE_ED25519_PUBLIC_KEY" \
    && test -n "$MIGRATION_MIN_RELEASE_SEQUENCE" \
    && cargo build --locked --release \
    && cargo test --locked \
    && mkdir /output \
    && cp target/release/migration-connector target/release/migration-key-broker target/release/migration-net-helper /output/ \
    && for binary in /output/migration-*; do \
         ldd "$binary" > /tmp/linked-libraries || exit 1; \
         if grep -E 'libssl|libcrypto' /tmp/linked-libraries; then exit 1; fi; \
       done
WORKDIR /src
COPY inventory ./inventory
COPY releases/build_inventory.py ./releases/build_inventory.py
RUN python3 releases/build_inventory.py /output/inventory.pyz \
    && cd /output && sha384sum migration-* inventory.pyz > SHA384SUMS

FROM scratch AS artifacts
COPY --from=build /output/ /
