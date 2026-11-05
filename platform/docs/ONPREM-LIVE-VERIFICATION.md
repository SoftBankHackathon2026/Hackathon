# 플랫폼 UI → 온프렘 실배포 검증

2026-10-03 KST. 로컬 플랫폼 화면의 **배포 시작** 버튼으로 요청했고, API·플랫폼 PostgreSQL·온프렘 Worker·Kubernetes API·Argo Rollouts 전체 경로를 사용했다. Kubernetes 리소스를 수동으로 배포한 결과를 UI에 성공으로 기록한 시험이 아니다.

## 검증 환경

- 앱 / 환경: `onprem-smoke` / `onprem-live-smoke`
- namespace: `oneport-apps`, context: `oneport-onprem`
- Rollout: `op-onprem-smoke-d49e6d96`
- 공개 주소: `https://onprem-smoke-1003.yubin.website`
- 앱: 저장소의 `platform/examples/release-demo`, 0.5 vCPU / 512Mi / 1 replica
- 이미지: private ECR `oneport/sdk-smoke-1003`의 Linux amd64 이미지
- digest: `sha256:4aa7beeb9060fd1f8ed144b06304b30a152fc19d934f0211f0719e84608efe17`
- 내부 검증: 선택형 preview Job 모드, 성공 샘플 2회. 공개 HTTPS는 외부 Worker에서 별도 확인.

```text
React 화면 → FastAPI → PostgreSQL 릴리즈 큐
                           ↑
                  로컬 On-prem Worker
                    ├─ Kubernetes API → Argo Rollout → 앱 Pod
                    ├─ Kubernetes Job → 내부 preview HTTP + 릴리즈 헤더
                    └─ 공개 HTTPS + 릴리즈 헤더
```

## 실제 결과

| 시나리오 | 릴리즈 | 결과 |
|---|---|---|
| 최초 v1 | `1a904e48b94bcd248414f38e` | 01:52:37 완료. Pod 실행·preview 검사·승격·공개 HTTPS 검증 성공 |
| v2 재배포 | `d05015d4c28796b6af854194` | 01:59:00 완료. 기존/후보 Pod를 따로 실행하고 preview 통과 후 전환 |
| 실패 후보 | `e0c67ffeac7d07d3ca33a999` | `VERSION=broken`으로 health 503 유도. `failed / release_failed_restored`로 종료, 기존 v2 복구 검증 완료 |

v2 공개 `/healthz`에 별도로 HTTPS 요청을 보내 `200`, `version=v2`, `healthy=true`, `X-Launchpad-Release=d05015d4c28796b6af854194`를 확인했다. v1/v2는 동일 이미지에 `VERSION` 설정을 변경한 릴리즈다. 다른 이미지 빌드를 비교한 시험은 아니다.

실패 후보는 readiness를 통과하지 못해 공개 트래픽을 받지 않았다. 대기 중 수행한 10회 HTTP 샘플은 모두 `200 / v2 / d05015d4…`였다. 이는 관측한 샘플의 결과이며 연속 무중단 부하 테스트를 의미하지 않는다. Argo의 실패 판정 후 플랫폼은 이전 요청을 복원하고, 별도의 내부 probe Job 두 개와 공개 HTTPS 검사를 다시 통과한 뒤 `이전 버전 복구 확인` 이벤트를 기록했다. 실패 후보 Pod도 정리됐으며 정상 앱 Pod만 Running 상태로 남았다. 수동으로 리소스나 플랫폼 상태를 고쳐 성공 처리하지 않았다.

실패 판정의 `progressDeadlineSeconds=360`은 최근 진행 상황 기준이다. 이전 ReplicaSet 정리 등의 상태 변화로 시작 시각부터 정확히 6분 후에 판정되는 것은 아니다. 이번 실패 요청은 01:59:21에 접수되어 약 11분 뒤 복구 검증을 마쳤다. 이 시간은 데모 안내에서 고려해야 한다.

코드 검증은 GitHub Platform CI에서 **195 tests passed**, Ruff 및 React build 통과로 확인했다. 기본 HTTP 모드 기존 fingerprint 유지, Job 재조회·대기 상태, 헤더 불일치·redirect·503 거부, 모드별 권한 검사도 테스트에 포함한다.

## 비밀정보와 잔여 조건

- kubeconfig·플랫폼 DB DSN·운영자 토큰·레지스트리 인증정보는 Git 제외 경로에만 보관한다.
- private ECR pull을 위해 테스트 앱 전용 immutable imagePullSecret을 사용했다. ECR 인증 토큰은 12시간 후 만료하므로 새 노드/캐시 없는 노드에서 이후 pull을 계속하려면 인증 갱신이 필요하다. 자동 갱신 구현은 이번 시험에 포함하지 않는다.
- 기존 온프렘 클러스터에서만 실행했다. 새 AWS Fargate task, ALB, DB는 만들지 않았다. ECR 이미지 저장 비용은 별도다.
- DB·Redis 자동 생성, 인증된 데이터 연결, 마이그레이션 및 완전 삭제는 이번 성공 범위에 포함하지 않는다. [별도 구현 계약과 누락 권한](ONPREM-DATA-SERVICES.md)을 참고한다.
- 현재 Mac의 내부 Service DNS 접근 문제는 Job 모드로 해결했다. 기본 HTTP 모드의 Worker 배치 조건은 [접속 문서](ONPREM-CONNECTION-HANDOFF.md)에 남겼다.

## 사용자 재현

온프렘 배포 요청에는 이미지 파일이나 로컬 Docker 태그가 아니라 **레지스트리의 전체 이미지 주소 + digest**를 넣는다. 이번 시험에서는 이미지를 먼저 빌드해 private ECR에 push한 뒤 UI에 주소를 입력했다. 온프렘 UI의 Git 주소 입력만으로 빌드까지 완료되는 기능을 검증한 것은 아니다.

플랫폼은 이미지 주소·포트·health 경로·레플리카·환경변수 및 Secret 참조를 Rollout의 Pod template로 변환해 Kubernetes API에 전달한다. 실제 이미지 다운로드는 온프렘 노드의 컨테이너 런타임이 수행한다. 따라서 플랫폼과 온프렘이 Docker daemon을 공유할 필요가 없다. ECR은 이미지 저장소일 뿐 배포 대상을 AWS로 제한하지 않는다. Private 이미지이면 온프렘의 imagePullSecret이 해당 레지스트리 pull 권한을 제공해야 한다.

1. 실행 중인 로컬 화면 `http://127.0.0.1:4320/#releases`에 접속한다.
2. 운영자 접속 후 `onprem-smoke` 환경을 선택한다.
3. 등록된 이미지 digest·포트 8080·health 경로 `/healthz`를 유지하고 `env.VERSION`을 새 값으로 지정한다.
4. **배포 시작**을 누르고 단계별 이벤트와 최종 결과를 확인한다.
5. 공개 주소의 `version`과 `release`가 요청한 릴리즈인지 확인한다.

로컬 비밀 실행 설정은 `platform/.data/aws-live/platform.json`(공통 API)과 `platform/.data/onprem-live/runtime.json`(온프렘 Worker)이다. 다른 팀원에게 이 파일을 그대로 복사하지 않고 각 실행 환경의 인증·DB 경로로 구성한다.
