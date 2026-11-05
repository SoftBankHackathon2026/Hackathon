# 온프렘 연결 준비 — 전달받은 접속 계약

| 항목 | 전달된 값 |
|---|---|
| Kubernetes API | `https://kube-api.yubin.website` |
| Context | `oneport-onprem` |
| namespace | `oneport-apps` |
| 플랫폼 계정 | `oneport-deployer` |
| 현재 전달 토큰 만료 | 2026-10-08 00:12:04 KST — 이후 재발급 필요 |
| Secret 파일 | `oneport-platform.kubeconfig`, 권한 0600, Git 제외 |

2026-10-03 사용자가 전달한 kubeconfig로 실제 접속과 권한 검사를 완료했다. 프로젝트 루트와 Downloads의 파일 권한을 0600으로 제한했고, 프로젝트 파일이 Git 제외 대상임을 확인했다. 토큰은 기록하지 않았다. 이후 **플랫폼 UI에서 DB 없는 앱의 최초 배포와 v2 블루그린 전환, 내부 preview 및 공개 HTTPS 검증에 성공**했다. DB/Redis 검증은 아직 하지 않았다. [실행 기록](ONPREM-LIVE-VERIFICATION.md)을 참고한다.

## 실제 점검 결과

| 항목 | 결과 |
|---|---|
| HTTPS API·전용 context 접속 | 성공, 인증서 검증 유지 |
| `oneport-apps` Pod·Rollout·Service·Ingress 목록 | 모두 조회 성공, 당시 0개 |
| 앱 Rollout·Service·Ingress 작업 | 필요한 권한 허용 |
| `oneport-runtime` 조회 | 성공 |
| StatefulSet get/create/delete | 초기 거부 → 2026-10-03 기반 Role 확대로 허용 확인 |
| PVC get/create/delete | 초기 거부 → 2026-10-03 기반 Role 확대로 허용 확인 |
| Job get/create | 허용 |
| Job list/delete | 초기 거부 → 2026-10-03 기반 Role 확대로 허용 확인 |
| 현재 Mac의 클러스터 내부 DNS | 해석 실패 |
| 현재 Mac → 플랫폼 PostgreSQL `SELECT 1` | 성공. 클러스터 내부 Worker에서의 연결을 입증하지는 않음 |

2026-10-03 인프라 관리자가 기존 `oneport-app-manager` Role을 확장했다. StatefulSet·PVC·Job·CronJob·HPA·PodDisruptionBudget·NetworkPolicy 관리, Pod 삭제와 메트릭 조회가 가능하다. Node·Namespace·RBAC·PV·StorageClass·CRD 관리, ServiceAccount 생성·TokenRequest API 직접 호출, Pod 직접 생성·exec는 계속 허용하지 않는다. 전체 권한은 [인프라 접속 계약](../../infra/onprem/data-access.md#접속과-권한)에 정리했다.

기반 설치에는 `oneport-apps`의 장기 토큰용 Secret(`kubernetes.io/service-account-token`) 생성·수정을 차단하는 admission 정책도 포함한다. 앱 설정·이미지 인증 Secret은 허용하며 서버 dry-run으로 양쪽을 검사한다. 이 정책과 자동 마운트 비활성화가 workload의 Pod template을 통한 모든 토큰 사용까지 차단하는 것은 아니다.

관리자 impersonation으로 허용·거부 권한 95개와 기반 상태를 검사했다. 기존 플랫폼 kubeconfig로 위 7종 리소스의 생성 server dry-run을 통과했고 `kube-system` StatefulSet 생성은 거부됐다. 테스트 리소스는 남기지 않았으며 kubeconfig·토큰을 변경하거나 재발급하지 않았다. 실제 PostgreSQL·Redis를 생성한 검증은 아니다. [추가 Role 예제](../examples/onprem/data-rbac-addon.yaml)는 이전 기반 Role을 사용하는 클러스터의 호환용으로만 남긴다.

실제 실행할 Worker 환경에서 다음 읽기 전용 점검을 반복할 수 있다. DB 설정 파일은 `database_url` 키가 있는 비밀 JSON이며 명령행에 DSN을 직접 넣지 않는다.

```bash
uv run --project platform/backend --extra aws python -m controlplane.execution.onprem.readiness \
  --kubeconfig ./oneport-platform.kubeconfig \
  --database-config /safe/path/platform-database.json
```

명령은 권한 심사·목록 조회·DNS 해석·DB `SELECT 1`만 수행한다. 누락된 실행 조건이 있으면 종료 코드 1을 반환한다. API 접속, 데이터 서비스 권한, Worker 네트워크 결과를 분리하며 실제 앱 검증을 성공으로 표시하지 않는다. DNS 해석에 성공해도 특정 preview Service의 HTTP 응답은 배포 후 별도로 확인해야 한다.

## 플랫폼 측 준비

`platform/examples/onprem/runtime.json`에 서버와 context를 반영했다. 실제 운영 설정은 Git 밖에서 만들고 kubeconfig, 플랫폼 PostgreSQL DSN, 앱 전용 hostname, 허용된 이미지 digest를 채운다. 컨테이너/DB/Redis 접속 비밀번호를 프런트엔드로 전달하지 않는다.

현재 구현은 namespaced Argo Rollout과 stable/preview Service를 사용한다. API endpoint와 TLS 검증을 고정하며 cluster-admin 또는 시스템 Secret 조회를 요구하지 않는다. 사전 검사는 SelfSubjectAccessReview로 실제 동작별 권한을 검사하고 namespaced Rollout API를 조회한다. `No resources found`는 해당 list 요청의 연결 성공이지, 모든 배포 권한이나 앱 실행 성공의 증거는 아니다.

## worker의 네트워크 조건

```text
브라우저 → 플랫폼 API → 플랫폼 PostgreSQL
                             ↑
                    온프렘 배포 worker
                      ├─ Kubernetes API HTTPS
                      ├─ <app>-preview.oneport-apps.svc.cluster.local
                      └─ 공개 앱 도메인 HTTPS
```

기본 `preview_probe_mode: "http"`에서는 worker가 private preview Service를 직접 HTTP로 확인하고 `X-Launchpad-Release`를 대조한 뒤 승격한다. 따라서 API에 접속할 수 있다는 이유만으로 Mac에서 이 모드의 worker를 실행하면 preview DNS/경로에서 실패할 수 있다. Tailscale 설치만으로 cluster Service CIDR과 DNS가 자동 연결된다고 가정하지 않는다.

배치 권장안은 클러스터 내부 worker다. 이때 worker가 접근 가능한 플랫폼 PostgreSQL 연결이 있어야 한다. 지금 Mac의 PostgreSQL은 `127.0.0.1:55433`이므로 이 주소를 그대로 클러스터 안에 넘기면 안 된다. 인프라 담당자와 worker 위치 및 DB 연결 경로를 먼저 맞춘다. DB를 인증 없이 공개하거나 health 검증을 생략해서 해결하지 않는다.

### 외부 Worker에서 내부 검증을 실행하는 선택 모드

`preview_probe_mode: "job"`은 내부 preview HTTP 검사만 짧은 Kubernetes Job으로 실행한다. Worker는 기존처럼 API와 플랫폼 DB에 접근하고, Job은 클러스터 내부 DNS로 preview를 검사한다. 공개 HTTPS 검사는 Worker에서 그대로 수행한다. 상주 Agent나 공개 preview 경로를 추가하지 않는다.

운영자가 runtime 설정에 다음 두 항목을 지정한다. 이미지에는 `python` 실행 파일과 표준 라이브러리가 있어야 하며, 신뢰하는 이미지 digest를 고정한다. 앱 사용자의 배포 요청으로 probe 이미지나 명령을 지정할 수 없다.

```json
{
  "preview_probe_mode": "job",
  "preview_probe_image": "<trusted-registry>/python-probe@sha256:<64-hex-digest>"
}
```

- Job은 runtime ServiceAccount를 사용하고 API 토큰 자동 마운트를 끈다. kubeconfig, 플랫폼 DB 인증정보, 앱 환경변수나 Secret을 전달하지 않는다.
- 앱의 고정된 내부 preview URL만 호출한다. redirect를 따르지 않으며 HTTP 200과 정확한 릴리즈 헤더를 함께 요구한다. 응답 본문은 로그에 남기지 않는다.
- 연속 검증 샘플마다 별도 Job을 만든다. Worker 재시작 시 같은 샘플의 Job을 재사용하며, 대기 중인 Job을 HTTP 실패로 세지 않는다.
- Job 실행 제한은 60초, 완료 후 TTL은 300초다. 이 모드는 TTL controller로 정리하며 Job 삭제 권한을 요구하지 않는다. 현재 기반 Role의 Job 삭제 권한은 앱 전체 수명 관리를 위해 별도로 추가한 것이다. 전체 배포의 기존 deadline도 유지한다.
- 이 모드의 사전 점검에는 `jobs/get`, `jobs/create`가 필요하다. `doctor`는 로컬 Service DNS를 요구하지 않지만 실제 Job 실행은 배포 시에만 검증한다.
- 일반 `readiness` 명령은 runtime 모드를 입력받지 않는 인프라 조사 도구다. Job 모드에서도 Mac의 내부 DNS 불가를 출력할 수 있으므로 runtime을 반영한 판단에는 `doctor`를 사용한다.

```bash
uv run --project platform/backend --extra aws python -m controlplane.execution.onprem.doctor \
  --config /safe/path/runtime.json --request /safe/path/request.json
```

Job이 생성되어도 노드 이미지 pull, NetworkPolicy, DNS가 막히면 검증에 실패한다. 해당 모드가 데이터 서비스 권한이나 DB/Redis 생성 기능까지 제공하는 것은 아니다. 기본 HTTP 모드의 기존 릴리즈 fingerprint는 유지하며, 모드를 바꾸는 작업은 환경 정책 변경으로 취급한다.

동작 근거: [Kubernetes Job](https://kubernetes.io/docs/concepts/workloads/controllers/job/), [완료 Job TTL 정리](https://kubernetes.io/docs/concepts/workloads/controllers/ttlafterfinished/).

## 실제 연결 시 확인할 것

1. kubeconfig 파일 경로, context, API 주소/TLS 검증과 만료 시각.
2. `oneport-apps`에서 Rollout·Service·Ingress·ReplicaSet 및 사용할 ServiceAccount 관련 권한. Migration을 넣는 경우 Job 권한도 확인.
3. 노드에서 테스트 이미지 pull 가능 여부. private Registry라면 미리 준비한 imagePullSecret을 참조.
4. worker → private preview Service, 공개 앱 hostname, 플랫폼 DB 연결.
5. 최초 배포 → preview 검증 → 승격 → 공개 응답 → v2 재배포/복구를 확인하고 결과 기록.

기반 클러스터·RBAC·시스템 Secret은 플랫폼이 임의로 생성/변경하지 않는다. 앱 DB/Redis가 아직 준비되지 않았으면 첫 연결 시험은 DB 없는 앱으로 분리한다. 전달된 앱별 데이터 서비스 생성·삭제 요구사항과 구현 순서는 [온프렘 데이터 서비스 계약](ONPREM-DATA-SERVICES.md)에 정리했다.
