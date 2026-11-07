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
| `src/crosscloud/audit_copy/` | 감사 기록 이관 최소판. 원본 바이트 복사기, bundle·manifest, CloudTrail 오프라인 검증기 |
| `terraform/audit/` | 감사 기록 보존 버킷 2개(GCS 보존 정책 미잠금, S3 Object Lock 활성) |
| `tests/` | 단위 시험 |

`Inventory` 스키마는 비밀로 의심되거나 분류가 불명인 env 값을 받지 않는다
(`attestation.suspected_values_retained` 는 0 만, `plain_nonsecret` 이 아닌 env
항목의 `value` 는 `null` 만 받는다).

## 감사 기록 이관

감사 로그를 원본 바이트 그대로 다른 클라우드의 보존 버킷으로 옮기고, 옮긴
뒤 대상 버킷만 읽어 검증한다. 로그 내용은 해석하지 않는다.

*   **AWS→GCP:** CloudTrail 로그와 다이제스트를 GCS 로 옮기고
    `ccx-<run>/bundle.json`(경로 대응표, 객체별 SHA-256, 다이제스트 서명,
    공개키)을 남긴다. 검증기는 다이제스트 체인 서명(SHA256withRSA)과 로그
    해시를 본다. 변조는 `invalid`, 삭제는 `missing` 이다. backfill 체인과
    검증이 꺼졌던 구간은 `evidence: none` 으로 표시한다.
*   **GCP→AWS:** `gcloud logging copy` 결과 파일을 S3 로 옮기고
    `ccx-<run>/manifest.json` 을 남긴다. 공급자 서명이 없으므로 결과는
    언제나 `evidence: none` 이고, 해시는 이관 중 불변만 증명한다.

명령은 `python -m crosscloud.audit_copy.cli --help` 로 본다. 버킷은
`terraform/audit/` 가 만들고, 잠그지 않는다.

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
