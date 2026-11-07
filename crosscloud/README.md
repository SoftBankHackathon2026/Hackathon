# crosscloud

운영 중인 컨테이너 서비스를 AWS 와 GCP 사이로 옮기는 이전 도구다. 원본
클라우드의 구성을 파악하고 대상 클라우드의 대응 서비스를 판정한다. 앱 코드가
인프라를 직접 다루는 부분(예: RDS IAM 인증)은 찾아서 사람 결정으로 올린다.

*   **상태:** 개발 중. 공통 자원 모델(`Inventory`)·JSON 스키마와 파악·판정
    (`crosscloud discover`)이 있다. 앱 코드 자동 수정은 본선 범위에 없다 —
    결합이 있는 앱은 사람 결정으로 멈춘다.
*   **연락처:** 김태윤
*   **관련 디렉토리:** 전환 상태 머신과 데이터 이전은 `migration/` 이 맡는다.
    crosscloud 는 데모 인프라에 기대지 않는다.

## 디렉토리

| 경로 | 내용 |
|---|---|
| `src/crosscloud/model.py` | 공통 자원 모델. 바꿀 수 없는 dataclass 값 객체 |
| `src/crosscloud/inventory_json.py` | `Inventory` ↔ JSON 변환. 두 방향 모두 스키마로 검사 |
| `src/crosscloud/discover/` | 원본 구성 읽기(AWS·GCP), env 분류, 결합 탐지 |
| `src/crosscloud/mapping/` | 서비스 대응표, 판정, 원가연 지원 제한 검사 |
| `src/crosscloud/handoff.py` | 원가연 `config.cloud.yaml`·접수 큐 `ingress.env`·대상 앱 DB TLS env 렌더링 |
| `src/crosscloud/control_adapter.py` | 원가연 앱 제어 API 6개 → 접수 큐(durable-ingress) 제어 |
| `src/crosscloud/ingress_control.py` | 접수 큐 제어 API 클라이언트 |
| `src/crosscloud/health_probe.py` | 앱 `/readyz`·`/api/info` 점검 |
| `src/crosscloud/cli.py` | `crosscloud` 명령(`discover` · `ingress-start` · `serve-adapter`) |
| `deploy/operator-pc/` | 운영자 PC compose 예시(접수 큐·Redis·Connect)와 순서 |
| `schema/inventory.schema.json` | `Inventory` JSON 형식의 정본 |
| `src/crosscloud/secret_transfer/` | Secret 값 이관(AWS↔GCP). 값은 메모리 bytes 로만 다루고 SHA-256 으로 대조 |
| `src/crosscloud/datapath.py` | 원가연 전환 도구용 DB 로그인(admin·app·cdc·sink·verify)과 대상 스키마 준비 |
| `src/crosscloud/db_readiness.py` | datapath 준비 결과를 카탈로그 질의로 점검(원가연 PRECHECK 전) |
| `src/crosscloud/pg_client.py` | PostgreSQL 접속 정보와 pg_dump·psql 호출. 비밀번호는 명령줄에 넣지 않는다 |
| `src/crosscloud/audit_copy/` | 감사 기록 이관 최소판. 원본 바이트 복사기, bundle·manifest, CloudTrail 오프라인 검증기 |
| `terraform/audit/` | 감사 기록 보존 버킷 2개(GCS 보존 정책 미잠금, S3 Object Lock 활성) |
| `terraform/aws/` | AWS 쪽 이전 자원 한 벌(VPC·RDS·Secret 그릇·ECR·ECS+ALB). 설명은 `terraform/aws/README.md` |
| `tests/` | 단위 시험 |

`Inventory` 스키마는 비밀로 의심되거나 분류가 불명인 env 값을 받지 않는다
(`attestation.suspected_values_retained` 는 0 만, `plain_nonsecret` 이 아닌 env
항목의 `value` 는 `null` 만 받는다).

## 감사 기록 이관

감사 로그를 원본 바이트 그대로 다른 클라우드의 보존 버킷으로 옮기고, 옮긴
뒤 대상 버킷만 읽어 검증한다. 로그 내용은 해석하지 않는다.

*   **AWS→GCP:** CloudTrail 로그와 다이제스트를 GCS 로 옮기고
    `ccx-<run>/bundle.json`(경로 대응표, 객체별 SHA-256, 다이제스트 서명)을
    남긴다. 검증기는 다이제스트 체인 서명(SHA256withRSA)과 로그 해시를 본다.
    변조는 `invalid`, 삭제는 `missing` 이다. backfill 체인과 검증이 꺼졌던
    구간은 `evidence: none` 으로 표시한다.
*   **GCP→AWS:** `gcloud logging copy` 결과 파일을 S3 로 옮기고
    `ccx-<run>/manifest.json` 을 남긴다. 공급자 서명이 없으므로 검증 결과는
    언제나 `no_evidence` 이고, 해시는 이관 중 불변만 증명한다.

**신뢰 기준은 대상 버킷 밖에 둔다.** 이관 명령은 이관 기록(`--receipt`,
bundle·manifest 의 SHA-256·객체 수·체인 시작/끝 다이제스트)을 운영자 PC 에
쓴다. 검증은 이 기록과 맞는 bundle·manifest 만 믿고, 공개키는 운영자가
보관한 `aws cloudtrail list-public-keys` 출력 파일(리전마다 하나)만 쓴다.

결과는 넷 중 하나다. 종료 코드를 괄호에 적는다.

*   `valid`(0): 모든 객체를 서명으로 확인했다.
*   `invalid`(1): 변조·누락·이관 기록 불일치가 있다.
*   `no_evidence`(3): 어긋난 것은 없지만 서명으로 확인한 객체가 없다.
*   `partial`(4): 일부만 서명으로 확인했다. 다이제스트가 덮지 않는 객체(아직
    다이제스트가 오지 않은 최신 로그, backfill 만 덮는 로그 등)나 검증 꺼짐
    구간이 있다. 개수는 `counts` 에 있다.

입력 파일·`--run` 형식 오류는 한 줄 메시지와 종료 코드 2 다.

명령은 `python -m crosscloud.audit_copy.cli --help` 로 본다. 버킷은
`terraform/audit/` 가 만들고, 잠그지 않는다.

## Secret 값 이관

`secret_transfer` 는 원본 Secret 의 현재 값을 메모리로만 읽어 대상 Secret 에
새 버전으로 쓰고 SHA-256 으로 대조한다. 하나라도 실패하면 이 실행이 쓴 버전을
되돌린다.

*   **재시도:** 응답을 잃었거나 중간에 끊겼으면(되돌리기 전) 같은 `run_id` 로
    다시 돌린다. 이미 쓴 버전을 다시 만들지 않는다. 실패해서 되돌린 뒤에는 새
    `run_id` 로 돌린다 — AWS 는 같은 토큰으로 되돌린 버전을 `token_reused` 로
    거부한다.
*   **GCP 권한:** 현재 값을 버전 목록에서 고르므로, crosscloud 를 실행하는 GCP
    신원에 `secretmanager.versions.list` 가 필요하다(secretAccessor 에는 없다).

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

원본을 파악하고 판정한다. 읽기 전용이며 비밀값을 조회하지 않는다. AWS 는
기본 자격 증명 체인, GCP 는 ADC(`gcloud auth application-default login`)를
쓴다.

```shell
crosscloud discover --direction aws_to_gcp --region ap-northeast-2 \
    --repo request-api=../sample-app --schema-sql schema.sql \
    --output discover.json
crosscloud discover --direction gcp_to_aws --project PROJECT \
    --region asia-northeast3
```

결과는 `{"inventory": ..., "report": ...}` JSON 이다. 종료 코드는 다음과 같다.

*   `0`: 사람 결정이 없다.
*   `3`: 결과는 다 냈지만 `report.decisions` 가 남았다. 사람이 먼저 고른다.
    `--repo` 를 주지 않은 워크로드, 결합이 있는 워크로드, 수집하지 못한 것
    (Omission)도 여기에 들어간다.

    워크로드 신원의 권한(작업 역할·서비스 계정으로 닿는 대응표 밖 의존)은
    수집하지 않고 Omission 으로 남긴다. 그래서 작업 역할이 있는 ECS
    워크로드는 종료 `3` 이다. `gcp_to_aws` 는 Cloud Run 이 늘 서비스 계정을
    가지므로 워크로드가 하나라도 있으면 종료 `0` 이 될 수 없다.
*   `1`: 실행 실패(자격 증명·API·파일 쓰기 오류). 한 줄로 알린다.
*   `2`: 인자 오류.

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
