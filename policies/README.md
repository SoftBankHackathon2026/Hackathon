# policies — 이전 허용·보류·거절 정책 (OPA/Rego)

처음이면 [개념.md](개념.md) → [사용법.md](사용법.md) 순서로 읽는다. 이 README는 규칙 표와 요약이다.

온프렘→AWS DB 이전의 각 단계를 **어떤 조건에서 실행해도 되는지** 판정한다. 설계와 규칙 선정 근거는
`업무-아젠다/04-정책과-OPA.md`, 호출 쪽 코드는 [`migration/migration/policy_client.py`](../migration/migration/policy_client.py).

```
개념.md                   OPA가 뭐고 어디에 서 있는지, 하지 않는 것
사용법.md                 검증 4단계, 입력 만들기, 규칙 바꾸기, 배포, 자주 나는 문제
migration.rego            정책 본문 (package ghost.migration, 결과는 data.ghost.migration.decision)
migration_test.rego       opa test 단위 테스트
schema/input.schema.json  입력 계약 (schema_version 1)
examples/*.json           단계별 허용/거절/보류/invalid 입력 예시. expected.json 이 기대 결과
Dockerfile                OPA 서버 이미지 (ghcr.io/softbankhackathon2026/migration-policy)
```

## 1차 규칙 (5개)

| 규칙 | 검사 이름 | 적용 단계 | 막는 조건 | 효과 |
|---|---|---|---|---|
| SEC-01 DB 비공개 | `target_db_exposure` | pre_start, pre_cutover, final_verify | `detail.public == true` 또는 status fail | deny |
| SEC-02 승인 위치 | `target_location` | plan, pre_start, pre_cutover | `detail.location` 이 `plan.approved_locations` 에 없음 | deny |
| APP-01 대상 앱 DB 연결·주문 조회 | `target_app_db` | pre_cutover, final_verify | `db_connected` 또는 `order_query_ok` 가 true 가 아님 (C05 장애 시나리오) | deny |
| OPS-01 최종 동기화 | `final_sync` | final_verify | 원본 차단·기준점 반영·전체 비교 중 하나라도 true 가 아님 | hold |
| OPS-02 쓰기 대기 상한 | `write_wait` | final_verify | `elapsed_seconds > plan.limits.write_wait_max_seconds` | deny |

공통 (모든 단계): 필수 검사 누락 → `EVIDENCE-MISSING`, 형식 오류·문자열 "true" → `EVIDENCE-INVALID`,
`observed_at` 이 `plan.limits.evidence_ttl_seconds` 보다 오래됨 → `EVIDENCE-STALE`, status unknown/error → `<규칙>-UNVERIFIED`,
증거의 `plan_digest` 가 현재 계획과 다름 → `EVIDENCE-PLAN-MISMATCH`. 전부 **hold** 다. 요청 자체가 계약에 어긋나면 **invalid**.

수치(유효기간·대기 상한·승인 위치)는 정책에 없고 `input.plan` 에서 받는다. 팀이 확정하지 않은 값을 정책이 정하지 않기 위해서다.

## 결과 읽는 법

```json
{"outcome": "deny", "allowed": false, "reason_codes": ["APP-01-TARGET-DB-UNREACHABLE"], "violations": [...],
 "policy_version": "migration-policy-v1", "evaluated_rules": ["APP-01","SEC-01","SEC-02"], "required_checks": [...]}
```

`allowed` 는 `outcome == "allow"` 일 때만 true 다. HTTP 200 이나 `result` 존재만으로 허용하지 않는다 — 클라이언트가 형식을 다시 검증한다.

## 로컬에서 돌리기

```bash
brew install opa            # 1.22.0 으로 개발·CI 고정
opa fmt --fail -l . && opa check --strict --ignore examples --ignore schema . && opa test --ignore examples --ignore schema . -v   # examples/*.json 이 data 로 적재돼 충돌하므로 제외
opa eval -d migration.rego -i examples/pre_cutover_app_db_unreachable_deny.json 'data.ghost.migration.decision' --format pretty

# 서버로
docker build -t migration-policy . && docker run --rm -p 8181:8181 migration-policy
curl -s localhost:8181/v1/data/ghost/migration/decision -d @<(jq '{input: .}' examples/pre_cutover_allow.json) | jq .result.outcome
```

Python 쪽: `python -m migration.policy evaluate --input ../policies/examples/final_verify_allow.json --opa-dir ../policies`.

## 바꿀 때

1. 규칙을 바꾸면 `policy_version` 을 올리고 `migration_test.rego`·`examples/expected.json` 을 같이 고친다.
2. 입력 계약을 바꾸면 `schema_version` 을 올리고 `policy_client.py` 의 `SCHEMA_VERSION` 을 맞춘다. 옛 버전 입력은 `SCHEMA-UNSUPPORTED` 로 거절된다.
3. CI 는 `.github/workflows/policies.yml` 이다 (opa fmt/check/test → 예시 대조 → pytest → 이미지 빌드). 배포는 `gitops/apps/migration-policy` 의 이미지 태그 교체로만 한다. 클러스터에서 정책 파일을 손으로 바꾸지 않는다.

## 배포 후 HTTP 판정 확인

명시적인 프로젝트 kubeconfig로 포트 포워드를 연 뒤 합성 입력 15개를 보낸다.
이 검사는 데이터 조회/이전/전환을 수행하지 않는다. 입력의 과거 시각은 stale 판정까지
재현하기 위한 테스트 값이며, 실제 환경 검증이나 이전 승인 근거로 재사용하지 않는다.

```bash
kubectl --kubeconfig "$PROJECT_KUBECONFIG" -n hackathon port-forward svc/migration-policy 18181:8181
# 별도 터미널, Hackathon 저장소 루트에서
python3 policies/smoke.py --url http://127.0.0.1:18181
```

기대 결과는 `Synthetic policy checks: 15/15 passed`, 종료 코드 0이다.
미적재 정책(HTTP 200 + 빈 객체), 다른 정책 버전, 판정/사유 불일치, 연결 실패는 종료 코드 1이다.
이미지 pull 실패는 이 검사 전에 해결해야 하며, 공개 범위를 바꾸지 않고 Harbor GHCR upstream의
읽기 인증을 구성하는 방법을 우선한다. 토큰은 Git/명령행 인자/로그에 남기지 않는다.
