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
| `src/crosscloud/cli.py` | `crosscloud` 명령 |
| `schema/inventory.schema.json` | `Inventory` JSON 형식의 정본 |
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

같은 검사를 CI(`.github/workflows/crosscloud.yml`)가 PR 마다 돌린다.

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
