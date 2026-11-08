# 반복 시연: 온프렘 → AWS

`demo-cycle.py`는 시연용 DB·접수 원장·트래픽 봇을 함께 초기화하고 새 이전 작업을 만든다.
공용 클러스터나 PVC를 삭제하지 않는다. 실제 사용 전 아래 일회성 배포가 필요하다.
**현재 변경은 로컬 개발·검증 단계이며 운영 배포 및 가연의 전체 시연은 미완료다.**

## 일회성 준비

1. Hackathon과 sample-app 변경을 리뷰·머지하고 새 이미지를 게시한다.
2. GitOps의 `*-repeat-demo` 이미지 placeholder를 확인된 게시 태그로 바꾼다.
3. AWS 접수용 `service-migration-auth` 공급과 전용 HTTPS 주소를 준비한다.
   상세 계약은 gitops `apps/service-migration-aws/README.md`에 있다.
4. GitOps app-of-apps에서 AWS `argocd-repeat-demo-aws`,
   온프렘 `argocd-repeat-demo-onprem` 그래프를 선택한다.
   루트 Application도 Git의 선언을 수정한다. 수동 apply/scale/delete는 하지 않는다.
5. source·destination·bot의 supervisor와 러너가 정상인지 확인한다.
   두 kubeconfig와 `kubectl exec` 권한, 플랫폼 operator 권한이 필요하다.

일회성 적용은 기존 접수/봇/러너 프로세스를 재시작한다. 진행 중 이전 명령을 먼저 끝낸다.
EKS 앱은 기존 full-aws 핀을 사용하고, 접수·봇만 새 supervisor 이미지가 필요하다.
러너의 `destination_app_version` 검증은 실제 EKS 앱 핀과 일치해야 한다.

## 시연 명령

작업 공간 루트에서 실행한다. 시연마다 새 `cycle`을 쓴다.

```sh
python3 Hackathon/migration/scripts/demo-cycle.py reset \
  --cycle demo_01 --confirm-reset demo_01
python3 Hackathon/migration/scripts/demo-cycle.py start --cycle demo_01
python3 Hackathon/migration/scripts/demo-cycle.py approve --cycle demo_01
python3 Hackathon/migration/scripts/demo-cycle.py status --cycle demo_01
```

- `reset`: 봇과 양쪽 접수를 정지하고 이전 DB·원장을 보관한다.
  온프렘 800건·AWS 0건, 온프렘만 쓰기 허용, 봇 생성 정지 상태로 준비한다.
- `start`: `sample-app-onprem` 하나를 선택한 새 플랫폼 작업을 만든다.
  DB는 기존 RDS에 매핑한다. 원본 800건 복제와 전환 전 100건 이후 READY를 기다린다.
- `approve`: READY에서 승인한다. AWS DB·앱·접수 원장을 전환하고
  전환 후 100건을 더해 총 1,000건을 확인하는 기존 러너를 실행한다.
- `status`: 로컬 cycle과 플랫폼 작업 상태를 조회한다. 다른 명령이 대기 중이어도 조회된다.

다음 시연은 `demo_02`로 같은 명령을 실행한다. `--wait` 기본값은 900초다.
시간이 지나도 서버 실행은 취소하지 않으며 `status`로 확인한다.
현재 공개 도메인까지 옮기는 기능은 포함하지 않는다. 온프렘 접수는 AWS로 전달하므로
이전 완료 후에도 온프렘 클러스터 전체를 종료하면 안 된다.

## 실패와 복구

초기화 중에는 러너의 영속 gate가 새 플랫폼 명령 실행을 막는다.
이때 제출된 명령은 `DEMO_RESET_IN_PROGRESS`로 명시적으로 실패 처리해
실행 대기열에 남아 초기화 재시도를 막지 않게 한다. 초기화 뒤 다시 제출한다.
초기화 실패 시 gate를 유지하며, 같은 cycle로 `reset`을 다시 실행한다.
중간 오류를 무시하고 `start`나 다른 cycle로 넘기지 않는다.
`start`는 제출 전에 로컬 상태를 기록하므로 응답 유실 때 자동 중복 실행하지 않는다.
`status`와 플랫폼 명령 로그로 서버 접수 여부를 먼저 확인한다.

다른 CDC connector·활성 replication slot·실행 중 cutover가 있으면 초기화를 거절한다.
이 명령은 다른 작업의 connector를 강제로 지우지 않는다.
이미 끝난 cycle의 재사용도 거절한다. 이전 데이터는 각 DB의
`demo_archive.<cycle>`, Redis `demo:archive:<cycle>:*`,
봇 PVC의 이전 원장 디렉터리에 남는다. 이 아카이브의 자동 삭제 정책은 없다.

두 DB의 원자적 분산 트랜잭션은 아니다. 양쪽 쓰기를 먼저 닫고
DB별 archive·truncate·800건 seed를 하나의 트랜잭션으로 실행한다.
대상만 초기화한 뒤 실패하면 같은 cycle 재실행으로 원본 초기화를 이어간다.
다른 테이블·DB·기존 cutover 체크포인트는 보존한다.

`DEMO_RESET_ENABLED=true`인 전용 overlay만 초기화 기능을 제공한다.
관리 HTTP는 각 Pod의 `127.0.0.1:9093`에만 열리며 기존 control token을 요구한다.
토큰은 명령 인수·로컬 상태 파일·로그에 출력하지 않는다.

## 로컬 검증과 한계

격리 PostgreSQL 두 DB의 연속 초기화·아카이브·권한 복구와
격리 Redis의 원장 보관·프로세스 재시작·cycle 재사용 거절을 시험한다.
플랫폼 명령 gate 및 실패 시 재개 테스트도 포함한다.
이 결과는 실제 Kafka 복제·AWS TLS·정책 판정·EKS 전환 성공을 뜻하지 않는다.
사용자가 DB와 데모앱 전체 흐름을 직접 시험하기 전에는 통합 검증 완료로 기록하지 않는다.
