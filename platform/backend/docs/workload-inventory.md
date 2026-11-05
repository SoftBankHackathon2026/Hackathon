# 워크로드 의존성 분석 1차

기존 Notion 02(앱·DB 전환·증거 인터페이스) 및 07(검사 증거)에 연결할 순수 분석 모듈이다. 대상 환경에 배포하거나 무중단 가능 여부를 판정하지 않는다. 앱 분석과 DB 복제·전환을 별도로 연결해야 한다.

저장소 루트에서 승인된 kube context와 namespace를 명시하고, Secret을 포함하지 않는 JSON을 stdin으로 전달한다. 아래 조회 명령은 클러스터를 바꾸지 않는다. 출력 원본에는 인라인 설정이 있을 수 있으므로 파일이나 로그에 저장하지 않는다.

```sh
kubectl --context "$SOURCE_CONTEXT" -n "$APP_NAMESPACE" get deployments,statefulsets,daemonsets,jobs,cronjobs,services,ingresses,persistentvolumeclaims,serviceaccounts,networkpolicies -o json \
  | PYTHONPATH=platform/backend/src python3 -m controlplane.domain.workload_inventory --namespace "$APP_NAMESPACE"
```

Argo Rollouts를 쓰면 해당 클러스터에서 `get rollouts.argoproj.io -o json` 결과도 같은 분석기에 별도로 전달한다. CRD가 없는 클러스터에 해당 조회를 강제하지 않는다. `get all`은 전체 리소스가 아니므로 사용하지 않는다.

결과에는 리소스 식별자, 이미지, Secret/ConfigMap/ServiceAccount/PVC/Ingress backend 참조와 대상 검토 항목만 담는다. Secret data/stringData, ConfigMap data, 인라인 환경변수·명령·annotation·오류 원문은 출력하지 않는다. Secret을 수집할 필요는 없다. `present_in_export=false`는 내보내기에서 확인하지 못했다는 뜻이며 클러스터에 없다는 판정이 아니다. optional 참조도 나열하므로 이를 일괄 배포 차단으로 해석하지 않는다.

Deployment/Pod/StatefulSet/DaemonSet/Job/CronJob/ReplicaSet/Argo Rollout의 일반·init 컨테이너를 지원한다. PVC 데이터·스토리지 클래스, Argo 분석 템플릿, 노드 스케줄링, 인그레스·LB·외부 VPC·방화벽은 **검토 필요**로 보고한다. CRD 내부의 임의 의존성, DB URL/자격 값, 클라우드 IAM 의미, 동적 볼륨 연결을 추론하지 않는다. 지원하지 않는 리소스는 명시적으로 남긴다. 입력은 Kubernetes API가 검증한 객체를 전제로 한다.

`readiness=not_evaluated`: API 연결, 실제 양쪽 이미지 pull, 시크릿 주입, DB 접근, OPA 판정 및 GitOps 배포 연동은 후속 작업이다. Secret 값을 읽거나 복사해서 부족한 참조를 채우지 않는다.

```sh
PYTHONPATH=platform/backend/src python3 -m unittest discover -s platform/backend/tests -p test_workload_inventory.py -v
```

## 통합 검증 (2026-11-06)

GitOps `ca45f5b`의 sample-app onprem/AWS overlay를 실제 `kubectl kustomize`로 렌더링한 뒤 분석했다. 각각 리소스 3/4개, 의존성 4/3개, 이미지 1개를 확인했다. 온프렘 hackathon API에서 일반 리소스 57개·참조 54개·이미지 14개를 읽었고, 별도 Argo Rollout 조회로 sample-app 이미지와 참조도 확인했다. Secret 원문은 조회하지 않았다.

재현용 선택 통합 테스트는 다음 환경변수를 명시해야 실행된다. 값을 지정하지 않으면 skip하며, 클러스터 변경은 없다.

```sh
export INVENTORY_TEST_GITOPS_ROOT=/path/to/reviewed/gitops
export INVENTORY_TEST_KUBECONFIG=/path/to/local/kubeconfig
export INVENTORY_TEST_CONTEXT=hackathon
export INVENTORY_TEST_NAMESPACE=hackathon
uv run --frozen --extra aws pytest -q tests/test_workload_inventory.py tests/test_workload_inventory_integration.py
```

AWS overlay 렌더링은 AWS 실환경 연결 검증이 아니다. 실환경 앱·DB 전환 사전 확인에서 migration-policy 파드 2개가 GHCR 이미지 인증 401로 ImagePullBackOff이며 서비스 endpoint가 없었다. 정책 판정이 가능한 상태로 복구되기 전에는 앱·DB 왕복/롤백 통합 성공으로 표시할 수 없다. 이 PR은 분석기 통합 검증 범위다.
