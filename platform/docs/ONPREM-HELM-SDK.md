# 온프렘 Helm 실행 코드 참고

플랫폼 팀에 전달할 문서는 [Oneport 온프렘 Helm 연동 규격 v1](../../infra/onprem/docs/helm-platform-handoff.md)입니다.
이 문서는 플랫폼 [PR #35](https://github.com/softbank-hackathon-2026-term1-azalea/oneport-platform/pull/35)에서 작성한 Python 실행 코드를 재사용할 때 읽는 개발 참고입니다.
이 코드는 Helm CLI와 Kubernetes API 호출을 감싼 래퍼이며, 인프라 차트 사용에 별도 SDK가 필요한 것은 아닙니다.

## 재사용하기 전에 확인할 것

- 인수할 앱 차트는 `0.1.2`이지만 현재 `ChartArtifact.version`은 `Literal["0.1.0"]`입니다. 클라이언트도 패키지 내부 버전을 대조하므로 경로·SHA만 바꿔서는 사용할 수 없습니다.
- `0.1.2` 패키지를 재사용 경로에 연결하려면 허용 버전과 관련 테스트를 갱신하고 검증해야 합니다. 아래 예제와 실검증 기록은 기존 `0.1.0` 기준이며 새 버전 지원 완료를 뜻하지 않습니다.
- 현재 입력 변환은 자동 Blue/Green만 허용하며 수동 승인·Canary·dedicated 데이터·Migration 요청은 거부합니다. 차트의 지원 범위와 다릅니다.
- UI/API/Worker는 기존 Argo Provider를 사용하며 이 래퍼는 아직 Worker registry에 등록되지 않았습니다. 두 경로가 같은 Rollout을 수정하지 않도록 합니다.

## 코드 위치

아래 경로는 `platform/backend/src/controlplane/` 기준입니다.

| 구현 | 위치 | 책임 |
|---|---|---|
| 설정·영수증 | [providers/onprem/helm/models.py](../backend/src/controlplane/providers/onprem/helm/models.py) | 운영자 환경, 검토한 차트 경로/SHA, Helm revision |
| 입력 변환 | [providers/onprem/helm/plan.py](../backend/src/controlplane/providers/onprem/helm/plan.py) | 공통 DeploymentRequest → 허용된 chart values, 앱/환경별 release 이름 |
| 실행 | [providers/onprem/helm/client.py](../backend/src/controlplane/providers/onprem/helm/client.py) | Helm version/SHA 확인, install/upgrade/test/rollback, 불확실한 쓰기 결과 분류 |
| 상태 | [providers/onprem/helm/observation.py](../backend/src/controlplane/providers/onprem/helm/observation.py) | Helm 소유권·Rollout UID·generation·릴리즈·이미지·Healthy 확인 |

SDK의 `HelmReceipt.application_verified`는 항상 false다. Helm이 성공했더라도 Argo 준비 상태, 내부 후보 테스트, 공개 URL의 HTTP 200 및 `X-Launchpad-Release` 검증을 마쳐야 플랫폼이 성공으로 처리한다.

## 기존 호출 형태 — 앱 차트 0.1.0 기준

비밀값은 환경변수 dict나 chart values에 넣지 않는다. 운영자가 등록한 Secret의 name/key/UID를 전달한다. kubeconfig는 제한된 `oneport-onprem` context의 파일로만 공급한다.

```python
from pathlib import Path
from controlplane.domain.models import DeploymentRequest
from controlplane.providers.contracts import ReleaseKey
from controlplane.providers.onprem.helm import (
    ChartArtifact, HelmClient, HelmEnvironment, app_plan,
)

# 기존 0.1.0 호출 형태. 인수 패키지 0.1.2는 허용 버전 갱신·검증 후 연결합니다.
artifact = ChartArtifact(
    path=Path('/opt/oneport/charts/oneport-app-0.1.0.tgz'),
    sha256='<검토한 SHA256SUMS 값>',
)
environment = HelmEnvironment(
    application='demo', environment_id='onprem-demo',
    hostname='demo.yubin.website', max_cpu=0.5, max_memory_mib=512,
)
request = DeploymentRequest(
    application='demo', environment_id='onprem-demo',
    source={'kind': 'image', 'image': '<registry/repository@sha256:...>'},
    cpu='0.5', memory_mib=512, health_path='/health',
)
plan = app_plan(request, environment, ReleaseKey(value='<24자리 릴리즈 ID>'), artifact)
client = HelmClient(
    binary=Path('/opt/oneport/bin/helm'),
    kubeconfig=Path('/run/secrets/oneport-platform.kubeconfig'),
    context='oneport-onprem', expected_server='https://kube-api.yubin.website',
    artifact=artifact,
)
# 아래 쓰기 전에 앱/환경 작업 잠금과 intent 저장을 완료해야 한다.
receipt = client.apply(plan, expected_revision=None)
# receipt를 저장하고 Argo가 해당 릴리즈로 Healthy인지 관찰한다.
# client.test(receipt) 후에만 published=True의 plan을 적용한다.
```

이 코드는 SDK 사용 예시이며 그대로 반복 실행하는 배포 루프가 아니다. 최초 배포는 `route.enabled=false`로만 허용한다. 이후에는 직전에 기록한 Helm revision을 expected_revision으로 보낸다. Helm revision이 바뀌었거나 pending/failed이면 중단한다. 테스트 Job도 쓰기이며 실행 의도를 기록한다.

`rollback(target_plan, target_receipt, expected_revision=현재revision)`은 이전에 저장한 앱 release의 values와 실제 Helm 이력이 일치하는지 확인한다. 새 Helm revision이 생기고 앱 readiness/공개 응답을 다시 검증해야 한다. DB schema를 되돌리는 동작은 없다.

## 호출 결과와 재시도 처리

- `apply(plan, expected_revision=...)`는 기록한 revision과 실제 릴리스를 대조합니다. 이 검사는 분산 잠금을 대신하지 않으므로 호출자가 앱·환경 작업을 잠급니다.
- 쓰기 실패/timeout은 일부 리소스가 반영됐을 수 있어 `SubmissionUncertain`입니다. 호출자가 Helm 이력과 실제 리소스를 대조한 뒤 후속 동작을 결정합니다.
- `test(receipt)`도 Job을 생성하는 쓰기입니다. 실행 의도와 결과를 기록하며 `--logs`를 사용하지 않습니다.
- 차트 SHA·패키지 메타데이터·Secret UID 검사를 유지합니다. 일반 env는 Helm 기록에 남으므로 비밀번호를 넣지 않습니다.

작업 순서·health·승인·데이터 보존·삭제 계약과 완료 기준은 [연동 규격 v1](../../infra/onprem/docs/helm-platform-handoff.md)을 따릅니다.

<details>
<summary><strong>기존 SDK 실검증 기록 — 2026-10-03, 앱 차트 0.1.0</strong></summary>

이 기록은 당시 패키지의 SDK 단독 검증입니다. 현재 차트와의 연동이나 UI→Worker 연결 완료를 뜻하지 않습니다.

검토한 차트: `oneport-app:0.1.0`, SHA256 `a42164ee77ee2bf9c3f624654c89079669e7cdb87cbea304e3d7fdc18a46157a`.
CI artifact source는 `f64bbad51cdd1520cee81fe7d2c957cefac55afb`이며 PR head `7763784403e8a472c043593effbfa80b66e98ffb`의 모든 templates/schema/defaults와 동일함을 비교했다. 공식 Helm 배포 파일도 SHA 검증 후 실행했다.

테스트 앱 `helm-sdk-smoke`, 환경 `onprem-helm-sdk`, Helm release `op-helm-sdk-smoke-3806c0ebbd-app`. 제한된 kubeconfig를 사용했으며 기존 `op-onprem-smoke-d49e6d96`는 변경하지 않았다.

| 검증 | 결과 |
|---|---|
| 비공개 최초 설치 | Helm revision 1, Argo Healthy·내부 helm test 성공 |
| 공개 경로 생성 | revision 2, `https://helm-sdk-smoke.yubin.website/health` 200 + 정확한 릴리즈 헤더 |
| 다른 Docker Hub digest로 재배포 | revision 3, 새 릴리즈 `f31003f31003f31003f31003`, 내부·공개 응답 확인 |
| 과거 릴리즈 복원 | revision 4, 기존 `e31003e31003e31003e31003` 복원, Rollout UID 유지, 내부·공개 응답 확인 |
| 실패 후보 | revision 5, `VERSION=broken`의 `/healthz` 실패. Argo Degraded까지 기존 공개 서비스의 200·기존 릴리즈 헤더 유지 |
| 실패 후 복구 | revision 6, 저장된 정상 revision 4로 rollback, 내부 helm test·공개 응답·UID 확인 |

검증 후 임시 Helm release·테스트 Job·Pod를 정리했고 잔여 Pod 0개를 확인했다. 기존 온프렘 시연 Rollout은 Healthy 상태로 유지했다. 위 임시 URL은 정리 후 더 이상 제공하지 않는다.

검증 도구는 호출마다 비밀값 없는 intent/receipt를 Git 제외된 로컬 파일에 기록하고 단일 프로세스 잠금을 사용했다. 이는 SDK 실환경 검증용이며 플랫폼 DB Worker의 중복 실행/재시작 검증을 대신하지 않는다.

</details>

플랫폼 연결 작업은 [연동 규격 v1의 작업 순서](../../infra/onprem/docs/helm-platform-handoff.md#5-플랫폼-팀-작업-순서와-완료-기준)에서 관리합니다.
