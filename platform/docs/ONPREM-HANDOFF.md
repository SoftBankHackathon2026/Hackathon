# 온프렘 연결 체크리스트

기반 설치와 접속 규약은 [`infra/onprem`](../../infra/onprem/README.md)을 참고합니다.

## 인프라 담당자가 준비할 것

| 항목 | 연결 계약 |
|---|---|
| 클러스터 | K3s, Argo Rollouts v1.10.0, amd64 노드 |
| Kubernetes API | `https://kube-api.yubin.website`, 명시적 context와 만료되는 토큰을 포함한 플랫폼 전용 kubeconfig. 기본 HTTPS 신뢰 저장소 사용 |
| 계정 | `oneport-system/oneport-deployer` 최소 권한, 앱 NS `oneport-apps` |
| 앱 계정 | `oneport-runtime`, API 토큰 자동 마운트 없음 |
| ingress | Traefik websecure → active Service, Cloudflare Tunnel 운영 HTTPS |
| hostname | 운영자가 `<app-slug>-<4-char-id>.yubin.website`를 중복 없이 할당. 자동 할당은 후속 |
| preview | 공개하지 않음. Worker에서 `*-preview.oneport-apps.svc.cluster.local` HTTP 접근 |
| registry | 외부 registry의 digest 이미지. 노드 DNS/HTTPS/인증, 필요 시 같은 NS의 imagePullSecret |
| DB·Redis | 기존 리소스 연결용 immutable Secret. 논리 binding→Secret name/key/UID 등록 |
| Worker 실행 위치 | 클러스터 DNS/Service, API, 운영 HTTPS, 플랫폼 PostgreSQL 모두 접근 가능 |

Tailscale로 API IP에 접근하는 것만으로 Service DNS 경로가 준비되지는 않습니다. Worker를 해당 네트워크에서 실행하거나 인프라가 DNS/라우팅을 별도로 제공해야 합니다. SDK는 명시적 kubeconfig를 요구하며 기본 컨텍스트를 임의로 선택하지 않습니다.

현재 기반 Role은 `oneport-apps`의 StatefulSet·PVC·Job·CronJob·HPA·PodDisruptionBudget·NetworkPolicy 관리와 Pod 삭제를 포함합니다. `oneport-runtime` 조회는 허용하지만 ServiceAccount·RBAC 관리와 Pod 직접 생성·exec는 허용하지 않습니다. 전체 권한은 [인프라 접속 계약](../../infra/onprem/data-access.md#접속과-권한)을 참고하세요. 이전 구성을 사용하는 경우 인프라 담당자가 기반 Role을 갱신합니다. 플랫폼 코드가 자기 권한을 높이지 않으며, 배포 경로의 사전 권한 검사는 `providers/onprem/preflight.py`에서 확인합니다. 현재 인프라 계정은 앱 네임스페이스 전체의 Secret 조회가 가능하므로 등록된 참조와 소유권을 확인합니다. 권한 제공만으로 DB·Redis 자동 생성 기능이 구현되는 것은 아닙니다.

## 앱이 지켜야 할 조건

- digest로 고정한 amd64 이미지, 지정 포트에서 비루트 사용자로 실행 가능. 현재 보안 설정은 `providers/onprem/pods.py` 참조.
- `/healthz` 등 지정 경로에서 HTTP 200과 **`X-Launchpad-Release: <LAUNCHPAD_RELEASE_ID 환경변수 값>`** 응답. 이 헤더는 현재 실제 리비전 확인 계약입니다. 단순 200만 반환하는 범용 이미지로는 성공하지 않습니다. 참고 앱: `examples/release-demo`.
- DB/Redis 접속 정보는 논리 Secret binding으로 연결. DB 읽기/쓰기 확인이 필요하면 앱의 health 동작에 포함해야 하며 SDK가 임의 SQL을 실행하지 않습니다.
- 마이그레이션은 같은 이미지의 argv 명령으로 Kubernetes Job 실행. 구버전과 호환되는 변경만 허용하며 schema 자동 rollback은 없습니다.
- replicas는 고정 수입니다. SDK 상한 5와 등록 환경 상한을 모두 적용합니다. Blue/Green 중 구·신 버전 및 Migration Job이 공존하므로 양쪽 리소스를 수용하도록 용량을 계산합니다.

기존 `launchpad.io/*` label과 `LAUNCHPAD_RELEASE_ID`/`X-Launchpad-Release`는 실험 코드와의 계약을 보존했습니다. 이름 변경 시 Worker·앱·테스트를 함께 변경해야 합니다.

## 실환경 검증 순서

1. 예제 환경/요청을 실제 등록 값으로 교체하고 매니페스트를 확인합니다. 아래 render는 클러스터에 쓰지 않습니다.

```sh
uv run --project platform/backend python -m controlplane.providers.onprem.tool render \
  --environment /safe/path/environment.json --request /safe/path/request.json
```

2. 플랫폼 계정으로 read-only preflight를 실행합니다. 권한 심사와 리소스 조회이며 배포하지 않습니다.

```sh
uv run --project platform/backend python -m controlplane.providers.onprem.tool preflight \
  --environment /safe/path/environment.json --request /safe/path/request.json \
  --kubeconfig /safe/path/onprem-kubeconfig --context oneport-onprem \
  --server https://kube-api.yubin.website
```

`ready_for_sdk=true`는 실행 권한/참조 확인이며 앱 성공이 아닙니다. 결과의 Secret UID를 runtime binding에 반영합니다. controller reconciliation, 노드 image pull, preview DNS, 공개 HTTPS, DB 동작은 따로 확인해야 합니다.

3. API와 Worker에 같은 runtime 설정/DB를 제공하고 UI에서 첫 배포. 후보 검증 전 공개되지 않는지, 완료 후 URL과 Release ID가 일치하는지 확인합니다.
4. 다른 digest로 두 번째 배포. 기존 URL 유지, preview 새 버전 검증, 승격, 이력 기록을 확인합니다.
5. 실패하는 후보·실패 Migration·Worker 재시작을 시험하고 기존 버전 유지/복구, 중복 Migration 방지, uncertain 처리 및 이력을 확인합니다.
6. private registry/DB/Redis를 사용하는 앱으로 추가 검증합니다. 일반 health 성공과 DB 읽기/쓰기 성공을 구분합니다.

연결 진단 보완 후 현재 저장소의 53개 테스트는 실제 PostgreSQL과 Kubernetes 테스트 더블로 검증했습니다. 팀 kubeconfig, Worker 네트워크, 실이미지와 추가 RBAC를 연결한 위 전체 실검증은 아직 하지 않았습니다. 자동 정리와 과거 Release 선택 롤백 UI도 미완료입니다. 리소스 삭제는 다른 앱을 건드리지 않도록 별도 운영 절차로 처리합니다.

클러스터 구성이 끝난 뒤의 플랫폼 작업과 DB 용도 구분은 [첫 연결 가이드](FIRST-ONPREM-CONNECTION.md)를 참고하세요.

## 인프라 Helm 차트 연결

실제 차트 수신 이후의 SDK 구현·실환경 검증과 Worker 후속 작업은 [Helm SDK](ONPREM-HELM-SDK.md)를 참고합니다. 기존 Argo Provider와 Helm 실행 경로는 아직 별개입니다.
