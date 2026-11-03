# syntax=docker/dockerfile:1
#
# migration-connect: 온프레미스 PostgreSQL 16 -> AWS PostgreSQL 16 CDC 마이그레이션용
# Debezium Kafka Connect 이미지.
#
# 포함 커넥터 (공식 이미지에 이미 내장, 추가 다운로드 없음):
#   - Debezium PostgreSQL Source  : /kafka/connect/debezium-connector-postgres  (pgoutput 플러그인 사용)
#   - Debezium JDBC Sink          : /kafka/connect/debezium-connector-jdbc      (postgresql JDBC 드라이버 동봉)
#
# 근거: https://github.com/debezium/container-images/blob/main/connect/3.7/Dockerfile
#       (docker-maven-download debezium postgres / debezium jdbc 호출로 두 플러그인을 설치함)
#
# 버전 변경 시: --build-arg DEBEZIUM_VERSION=3.x.y.Final --build-arg BASE_DIGEST=""
# (digest는 태그와 1:1이므로 버전을 바꾸면 digest를 비우거나 새 digest로 교체해야 한다)

ARG DEBEZIUM_VERSION=3.7.0.Final
# quay.io/debezium/connect:3.7.0.Final 의 OCI index digest (linux/amd64 + linux/arm64).
# `docker buildx imagetools inspect quay.io/debezium/connect:3.7.0.Final` 로 확인한 값.
ARG BASE_DIGEST=@sha256:ef2bf87d8d1d16427f0a5b4b13e3b78dd132696a75cf8c6d95203111e11386b7

FROM quay.io/debezium/connect:${DEBEZIUM_VERSION}${BASE_DIGEST}

ARG DEBEZIUM_VERSION
LABEL org.opencontainers.image.title="migration-connect" \
      org.opencontainers.image.description="Debezium Kafka Connect (PostgreSQL source + JDBC sink) for on-prem to AWS migration" \
      org.opencontainers.image.base.name="quay.io/debezium/connect:${DEBEZIUM_VERSION}" \
      org.opencontainers.image.source="https://github.com/softbankhackathon2026"

# 빌드 시점에 두 플러그인 디렉터리와 PostgreSQL JDBC 드라이버가 실제로 존재하는지 검증한다.
# 베이스 이미지 구성이 바뀌어 누락되면 빌드가 실패하므로, 런타임에 가서야 알게 되는 상황을 막는다.
RUN set -eu; \
    test -d /kafka/connect/debezium-connector-postgres; \
    test -d /kafka/connect/debezium-connector-jdbc; \
    ls /kafka/connect/debezium-connector-jdbc/postgresql-*.jar >/dev/null; \
    echo "plugins ok:"; ls /kafka/connect/

# 베이스 이미지는 이미 kafka 사용자(uid 1001)로 실행된다.
# Kubernetes의 runAsNonRoot 검증은 숫자 UID를 요구하므로 명시적으로 숫자로 지정한다.
USER 1001

# 자격증명은 이미지에 넣지 않는다. Connect 워커 설정은 환경변수(CONNECT_*)로,
# 커넥터의 DB 비밀번호는 config provider(env)로 런타임에 주입한다. 자세한 값은 README.md 참고.
