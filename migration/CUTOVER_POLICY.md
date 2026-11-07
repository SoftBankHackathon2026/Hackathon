# 실제 데모 cutover의 OPA 실행 차단

이 변경은 `migration.cutover`의 기존 데모 상태 머신에 적용된다. `migration.cloud`의 일반화 실행기와 플랫폼 연결은 별도이며, 그 경로까지 정책이 강제된다고 보지 않는다.

## 실행 경계

- `COPYING`: 원본 복제 준비·커넥터 등록 전에 `pre_start` 재판정
- `FENCING`: 원본 쓰기 정지 전에 `pre_cutover` 재판정
- `SWITCHING`: 목적지 쓰기 개방 전에 `pre_cutover` 재판정. 원본 쓰기 차단·커넥터 종료를 확인하고 전체 비교를 다시 수행한 뒤 `final_verify`로 최종 동기화·대기 상한도 판정
- 직접 단계 메서드 호출도 같은 검사를 거친다. 판정은 매번 다시 실행하고 allow를 캐시하지 않는다.

## 배포 전 필수 준비

**기존 이미지와 동작이 달라진다. 설정이 없으면 새 코드의 변경 단계는 차단된다. 이 이미지로 운영 cutover를 교체하기 전에 증거 공급과 OPA 접속을 준비해야 한다.** 이번 코드 PR만으로 GitOps 배포나 실환경 정책 통합이 완료된 것은 아니다.

환경변수:

- `POLICY_OPA_URL`: 운영 OPA 주소
- `CUTOVER_POLICY_EVIDENCE`: 비밀값이 없는 정책 계획·증거 JSON 파일 경로

파일 구조:

```text
job_id: 실제 작업 ID
plan:
  source / target: 실제 설정의 환경 이름
  execution_digest: execution_digest(config) 결과
  approved_locations: 승인된 위치 목록
  limits: evidence_ttl_seconds / write_wait_max_seconds
  digest: digest_of(plan에서 digest 제외한 객체)
checks:
  target_db_exposure: 정책 계약에 맞는 실제 DB 외부 노출 검사
  target_location: 실제 목적지 위치 검사
  target_app_db: 실제 대상 앱 연결·조회 검사
```

`migration.cutover_policy.execution_digest(load_config(config_path))`는 실제 실행 설정 전체의 해시를 만든다. DB 비밀번호는 읽지 않는다. 해시가 달라지면 이전 증거를 사용하지 못한다. `policy_client.digest_of()`로 plan digest를 만든다. 파일은 신뢰하는 수집기/운영자가 제공하며, 원자적 교체로 갱신한다. 소스·목적지나 승인 제한을 바꾸려면 기존 작업을 무작정 재개하지 말고 새 계획/작업 절차를 사용한다.

각 check의 `observed_at`, `source`, `evidence_id`, `status`, `detail`은 실제 관측값이다. 예제의 pass를 복사해서 운영 증거로 쓰지 않는다. `final_sync`와 `write_wait`는 전환 직전 실행기가 직접 생성하여 외부 값보다 우선한다. 미래 시각·만료·누락·OPA 장애는 허용되지 않는다. 시크릿처럼 보이는 입력은 판정 DB에 저장하기 전에 거절한다.

## 상태·복구

정책 결과는 `policy.sqlite3`, 마지막 판정과 실행/계획 식별은 기존 cutover 체크포인트에 남긴다. 증거 파일 로딩·설정 검증 실패는 실행 오류 코드로 남고 OPA 결정 ID는 없다. 정책 거절은 결정 ID를 남긴다.

이미 저장된 작업에 정책 식별이 있으면 재시작 후에도 다른 계획/설정으로 이어서 실행할 수 없다. 목적지 쓰기를 열기 전 실패는 기존 상태 머신 규칙으로 처리한다. SWITCHING에 진입한 뒤 실패하면 보수적으로 NEEDS_RECOVERY로 남으므로, 실제 쓰기를 열지 않았더라도 자동 원복하지 않는다. 목적지 쓰기가 이미 열린 재시작은 원본 동결 데이터와 새 목적지를 동일하게 비교하지 않는다. 원본이 다시 쓰기 가능하면 진행을 차단한다.

원본·목적지 쓰기 차단 범위는 기존 데모의 INSERT 권한 계약이다. 이 변경이 일반 앱의 모든 UPDATE/DELETE/TRUNCATE를 자동 보호하는 것은 아니다. 전체 비교 한 번이 추가되므로 전환 시간은 다시 측정해야 한다.

## 검증 범위

실제 OPA CLI로 allow/deny/만료/미래/누락 및 재조회 동작을 검사하고, 가짜 DB·워커·Connect 포트로 변경이 실행되지 않음을 검사한다. 운영 앱/클라우드 증거 수집, 실제 OPA 서버·GitOps 배포, 무중단 왕복 통합은 후속 검증이다.

## AWS RDS 읽기전용 증거 수집기

`python -m migration.cutover_evidence`는 AWS CLI의 DescribeDBInstances와 대상
앱 `/api/info`를 읽어 위 파일을 원자적으로 교체한다. 인스턴스 ID·실행 설정의
DB endpoint/port·ARN 리전이 일치해야 RDS 사실을 채택한다. PubliclyAccessible은
실제 boolean을 OPA에 전달하므로 공개 DB는 정책에서 deny 된다. 조회 오류나
누락을 pass로 바꾸지 않는다. 앱은 기대 environment·DB명·실제 테이블 count 조회를
확인한다. API의 environment는 앱 실행 위치 라벨이며 DB 위치의 증명은 아니다.
따라서 RDS에 연결된 온프렘 standby 앱은 local일 수 있다. 앱의 DB 호스트 자체는
이 API가 노출하지 않으므로 연결 Secret/배포 참조 확인을 별도 수행해야 한다.

필수 인수: `--config`, `--output`, `--rds-identifier`, `--region`,
`--app-url`, `--app-environment`, `--approved-location`(반복 가능),
`--evidence-ttl-seconds`, `--write-wait-max-seconds`. AWS profile은 선택 인수다.
승인 위치와 시간 제한의 운영값은 기본값을 임의로 넣지 않는다.

읽기 권한이 있는 AWS 실행 환경에서 돌리고, 전환 동안 TTL보다 짧은 간격으로
증거를 갱신해야 한다. AWS 자격증명은 출력·파일에 포함하지 않는다. 수집기 성공은
정책 허용과 다르다(공개 DB·미승인 위치도 정확히 수집될 수 있다). 실행 허용은
항상 gate의 OPA 판정으로 결정한다. 실패 시 error 증거 또는 무효 표식을 쓰며,
출력 파일 자체 쓰기가 실패하면 이전 증거는 TTL까지 남을 수 있으므로 수집기
비정상 종료를 무시하고 전환을 실행하면 안 된다.

현재 AWS RDS 대상만 지원한다. 온프렘 귀환의 네트워크 노출 증거 수집은 별도다.
보안그룹·라우팅 전체를 PubliclyAccessible 한 필드로 감사했다고 주장하지 않는다.
GitOps runner 배포·증거 공급 자동화와 실제 DB 전환 시험은 이 수집기 PR만으로
완료되지 않는다. Mac에서 실제 AWS API·온프렘 standby 앱·운영 OPA에 읽기전용
검사를 수행해 올바른 라벨 allow/다른 기대 라벨 deny를 확인했다. 시험용 TTL60초와
대기300초는 운영 제한 확정이 아니며 DB 전환을 실행하지 않았다.
