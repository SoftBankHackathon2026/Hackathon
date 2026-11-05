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
