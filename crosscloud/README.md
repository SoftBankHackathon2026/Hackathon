# crosscloud

운영 중인 컨테이너 서비스를 AWS 와 GCP 사이로 옮기는 이전 도구다. 원본
클라우드의 구성을 파악하고, 대상 클라우드의 대응 서비스를 판정하고, 앱 코드가
인프라를 직접 다루는 부분(예: RDS IAM 인증)을 AI 로 고친 뒤 샌드박스에서
검증한다.

*   **상태:** 개발 중. 지금은 공통 자원 모델(`Inventory`)과 그 JSON 스키마만
    있다. 수집·판정·적응·검증은 이어지는 PR 에서 더한다.
*   **연락처:** 김태윤
*   **관련 디렉토리:** 전환 상태 머신과 데이터 이전은 `migration/` 이 맡는다.
    crosscloud 는 데모 인프라에 기대지 않는다.

## 디렉토리

| 경로 | 내용 |
|---|---|
| `src/crosscloud/model.py` | 공통 자원 모델. 바꿀 수 없는 dataclass 값 객체 |
| `src/crosscloud/inventory_json.py` | `Inventory` ↔ JSON 변환. 두 방향 모두 스키마로 검사 |
| `schema/inventory.schema.json` | `Inventory` JSON 형식의 정본 |
| `src/crosscloud/secret_transfer/` | Secret 값 이관(AWS↔GCP). 값은 메모리 bytes 로만 다루고 SHA-256 으로 대조 |
| `src/crosscloud/datapath.py` | 원가연 전환 도구용 DB 로그인(admin·app·cdc·sink·verify)과 대상 스키마 준비 |
| `src/crosscloud/db_readiness.py` | datapath 준비 결과를 카탈로그 질의로 점검(원가연 PRECHECK 전) |
| `src/crosscloud/pg_client.py` | PostgreSQL 접속 정보와 pg_dump·psql 호출. 비밀번호는 명령줄에 넣지 않는다 |
| `tests/` | 단위 시험 |

`Inventory` 스키마는 비밀로 의심되거나 분류가 불명인 env 값을 받지 않는다
(`attestation.suspected_values_retained` 는 0 만, `plain_nonsecret` 이 아닌 env
항목의 `value` 는 `null` 만 받는다).

## 사용법

Python 3.12 이상이 필요하다. 이 디렉토리에서 실행한다.

editable 설치(-e)만 지원한다 — 스키마를 저장소 경로에서 읽는다.

```shell
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'

ruff check .
ruff format --check .
python -m pytest --cov=crosscloud --cov-report=term-missing --cov-fail-under=80
```

`datapath` 통합 시험은 시험 전용 PostgreSQL 이 있어야 돈다(없으면 건너뛰고
커버리지가 80% 아래로 떨어진다). 운영 DB 를 가리키지 않는다.

```shell
docker run -d --name r4-pg16 --label com.crosscloud.bench=r4 \
    -e POSTGRES_PASSWORD=local-only -p 127.0.0.1:55432:55432 \
    postgres:16 -c port=55432
export CROSSCLOUD_TEST_PG_DSN="host=127.0.0.1 port=55432 user=postgres password=local-only dbname=postgres sslmode=disable"
# 컨테이너 안의 pg_dump·psql 을 쓴다. 도구 주 버전이 서버와 다르면
# datapath 가 거부한다(새 pg_dump 의 덤프는 옛 서버에서 실패한다).
export CROSSCLOUD_TEST_PG_TOOLS="docker exec -i -e PGPASSWORD -e PGSSLMODE -e PGCONNECT_TIMEOUT r4-pg16"
python -m pytest
docker rm -f r4-pg16
```

같은 검사를 CI(`.github/workflows/crosscloud.yml`)가 PR 마다 돌린다. CI 는
PostgreSQL 서비스 컨테이너를 띄우고, 그 안의 pg_dump·psql 로 통합 시험과
서버 로그 카나리 시험까지 돌린다.

## 스타일 적용 기준

정본은 [Google 스타일 가이드](https://github.com/google/styleguide)다.

*   **Python:** pyguide. 기계 검사는 ruff 로 한다(`pyproject.toml`). 줄 길이
    80, docstring 은 google 형식이다. 저장소의 다른 Python 코드가 이미 ruff 를
    써서 같은 도구를 쓴다.
*   **Markdown:** docguide.
*   **JSON Schema:** Google 가이드가 다루지 않는다. 이 디렉토리 안의 일관성을
    따른다 — 들여쓰기 2칸, 각 객체는 `additionalProperties: false` 와 전체 필드
    `required`, dataclass 하나에 대문자 이름의 `$defs` 하나. 필드·선택지가
    `model.py` 와 같은지는 `tests/test_schema_matches_model.py` 가 지킨다.
