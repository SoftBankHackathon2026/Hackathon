# migration-connect

온프레미스 PostgreSQL 16 → AWS PostgreSQL 16 CDC 마이그레이션에 쓰는 Debezium Kafka Connect 이미지.
이미지 이름: `ghcr.io/softbankhackathon2026/migration-connect`

## 목적

- **Source**: Debezium PostgreSQL 커넥터 (`io.debezium.connector.postgresql.PostgresConnector`, `plugin.name=pgoutput`)
- **Sink**: Debezium JDBC 커넥터 (`io.debezium.connector.jdbc.JdbcSinkConnector`) → 대상 PostgreSQL 16
- Kafka: 기존 클러스터 Kafka 3.9.1 KRaft, `kafka:9092` PLAINTEXT, 브로커 2개

이미지 자체는 공식 `quay.io/debezium/connect` 위에 **빌드 시점 검증과 메타데이터만 추가**한다.
커넥터 JSON 템플릿(source/sink 설정)은 `migration/connectors/`에 별도로 둔다. 이 디렉터리는 워커 이미지만 담당한다.

## 버전 고정 근거

| 항목 | 값 | 근거 |
|---|---|---|
| 베이스 이미지 | `quay.io/debezium/connect:3.7.0.Final` | Debezium 릴리스 페이지에서 2026-09-29 기준 최신 안정(Final) 시리즈. https://debezium.io/releases/ |
| 베이스 digest | `sha256:ef2bf87d8d1d16427f0a5b4b13e3b78dd132696a75cf8c6d95203111e11386b7` | `docker buildx imagetools inspect quay.io/debezium/connect:3.7.0.Final` 결과 (OCI index, amd64+arm64) |
| 내장 Kafka | 4.3.1 | https://github.com/debezium/container-images/blob/main/kafka/3.7/Dockerfile (`KAFKA_VERSION=4.3.1`). Kafka 클라이언트는 구버전 브로커(3.9.1)와 호환된다. |
| 실행 사용자 | `kafka` (uid 1001) | https://github.com/debezium/container-images/blob/main/kafka/3.7/Dockerfile (`useradd -u 1001`) |

### JDBC sink 포함 여부

**포함됨. 별도 다운로드 불필요.**

공식 `connect/3.7/Dockerfile`이 `docker-maven-download debezium postgres` 와 `docker-maven-download debezium jdbc` 를 호출해
두 플러그인을 `/kafka/connect/` 아래에 설치한다.
https://github.com/debezium/container-images/blob/main/connect/3.7/Dockerfile

JDBC 플러그인 tar.gz(`io.debezium:debezium-connector-jdbc:3.7.0.Final:plugin`) 안에 `postgresql-42.7.13.jar`가 동봉되어 있어
PostgreSQL JDBC 드라이버도 추가로 넣을 필요가 없다 (Maven Central 아티팩트를 받아 `tar tzf`로 직접 확인).
https://repo1.maven.org/maven2/io/debezium/debezium-connector-jdbc/3.7.0.Final/

그래서 `Dockerfile`은 다운로드 대신 **빌드 시 존재 검증**(`test -d`, 드라이버 jar glob)만 수행하고,
베이스 이미지 구성이 바뀌어 누락되면 빌드가 실패하도록 했다.

## 로컬 빌드와 검증

```bash
cd migration/connect
docker build -t migration-connect:local .

# 두 플러그인 디렉터리가 보여야 한다.
docker run --rm --entrypoint ls migration-connect:local /kafka/connect/
#   ... debezium-connector-jdbc ... debezium-connector-postgres ...

# PostgreSQL JDBC 드라이버 확인
docker run --rm --entrypoint sh migration-connect:local -c 'ls /kafka/connect/debezium-connector-jdbc/postgresql-*.jar'

# 비루트 확인 (1001 이어야 한다)
docker run --rm --entrypoint id migration-connect:local -u
```

버전을 올릴 때:

```bash
docker buildx imagetools inspect quay.io/debezium/connect:<NEW>.Final   # Digest 줄을 복사
docker build --build-arg DEBEZIUM_VERSION=<NEW>.Final --build-arg BASE_DIGEST=@sha256:<digest> .
```

## Connect 워커 권장 설정

이미지의 `docker-entrypoint.sh`는 아래 단축 환경변수와 `CONNECT_*` 환경변수(점 → 밑줄, 대문자)를 워커 속성으로 변환한다.
https://github.com/debezium/container-images/blob/main/connect-base/3.7/docker-entrypoint.sh

| 환경변수 | 권장값 | 설명 |
|---|---|---|
| `BOOTSTRAP_SERVERS` | `kafka:9092` | PLAINTEXT, 무인증 |
| `GROUP_ID` | `migration-connect` | 워커 그룹. 다른 Connect 클러스터와 겹치면 안 된다. |
| `CONFIG_STORAGE_TOPIC` | `migration-connect-configs` | 내부 토픽. 파티션 1, compact |
| `OFFSET_STORAGE_TOPIC` | `migration-connect-offsets` | 내부 토픽. compact |
| `STATUS_STORAGE_TOPIC` | `migration-connect-status` | 내부 토픽. compact |
| `KEY_CONVERTER` | `org.apache.kafka.connect.json.JsonConverter` | 기본값과 동일 |
| `VALUE_CONVERTER` | `org.apache.kafka.connect.json.JsonConverter` | 기본값과 동일 |
| `CONNECT_KEY_CONVERTER_SCHEMAS_ENABLE` | `true` | JDBC sink는 스키마가 필요하다 |
| `CONNECT_VALUE_CONVERTER_SCHEMAS_ENABLE` | `true` | JDBC sink는 스키마가 필요하다 |
| `CONNECT_CONFIG_PROVIDERS` | `env` | config provider 이름 |
| `CONNECT_CONFIG_PROVIDERS_ENV_CLASS` | `org.apache.kafka.common.config.provider.EnvVarConfigProvider` | 커넥터 설정에서 `${env:VAR}` 로 환경변수 참조 |
| `CONNECT_CONFIG_PROVIDERS_ENV_PARAM_ALLOWLIST_PATTERN` | `^(SOURCE|TARGET)_DB_.*$` | 노출 가능한 환경변수 범위 제한 (권장) |
| `CONNECT_OFFSET_STORAGE_REPLICATION_FACTOR` 등 `*_REPLICATION_FACTOR` | `2` | 브로커 2개이므로 기본값 3은 실패한다 |
| `CONNECT_CONFIG_STORAGE_REPLICATION_FACTOR` | `2` | 위와 동일 |
| `CONNECT_STATUS_STORAGE_REPLICATION_FACTOR` | `2` | 위와 동일 |

- DB 비밀번호 등 자격증명은 이미지나 커넥터 JSON에 직접 쓰지 않고, 파드 환경변수(Secret)로 주입한 뒤
  커넥터 설정에서 `"database.password": "${env:SOURCE_DB_PASSWORD}"` 형태로 참조한다.
- 워커 수를 1로 두면 `CONNECT_*_REPLICATION_FACTOR` 외에는 기본값으로 충분하다.
- REST API는 8083 포트. 커넥터 등록은 `migration/connectors/`의 JSON을 `PUT /connectors/<name>/config` 로 적용한다.

## GHCR 패키지 가시성

`.github/workflows/migration-images.yml`이 `main` push 시 `ghcr.io/softbankhackathon2026/migration-connect:<full-sha>` 와 `:main` 태그를 올린다.
GHCR 패키지가 **private**이면 k3s 노드가 이미지를 당길 수 없으므로 `imagePullSecret`(`ghcr.io`, read:packages 권한 PAT)을
네임스페이스에 만들어 파드 스펙에 연결해야 한다. public으로 바꾸면 불필요하다.
