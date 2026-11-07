
## proxy-cache 와 노드 mirror (2026-11-05)

팀 결정으로 이미지 레지스트리를 Harbor 로 관리한다. 첫 단계는 **proxy-cache**: gitops 의 이미지 주소(`ghcr.io/...`, `docker.io/...`, `quay.io/...`)는 그대로 두고, k3s 노드가 Harbor 를 거쳐 받게 한다.

- Harbor 에 만든 것(admin API, 2026-11-05): 레지스트리 엔드포인트 `ghcr`(github-ghcr) · `dockerhub`(docker-hub) · `quay`(docker-registry 타입, quay 타입은 proxy 미지원) 와 같은 이름의 public proxy 프로젝트 3개. `/v2/<project>/<repo>/manifests/<tag>` 로 세 이미지(migration-tools, sample-app, openbao) 모두 200 확인.
- 노드 쪽: [`registries.yaml`](registries.yaml) 을 두 노드의 `/etc/rancher/k3s/registries.yaml` 로 복사하고 k3s 를 재시작한다(k3s 문서: 시작 때만 읽음). HTTP 엔드포인트라 `http://` 를 명시했다.
- 확인: 재시작 뒤 `crictl pull ghcr.io/softbankhackathon2026/migration-tools:<tag>` 가 Harbor `ghcr` 프로젝트의 repositories 에 나타나면 된다.
- 다음 단계(미정): CI 가 Harbor 프로젝트(예: `ghost`)로 직접 push 하고 gitops 주소를 바꾸는 방식. 그때도 이 mirror 설정은 그대로 둔다.

## DB 권한과 fsGroupChangePolicy (2026-11-07)

- 증상: k3s 재시작 뒤 `harbor-database-0` 이 `pgdata/pg18 has invalid permissions` 로 CrashLoop 에 빠지고, proxy-cache 가 500 을 낸다.
- 원인: 차트가 DB 파드에 `fsGroup: 999` 를 고정해 둬서 kubelet 이 볼륨을 다시 붙일 때마다 안쪽 파일까지 그룹 권한(2770)을 다시 입힌다. Postgres 는 데이터 폴더를 0700·0750 만 받는다. 차트의 `data-permissions-ensurer` init 컨테이너가 700 으로 되돌리지만 파드를 새로 만들 때만 돈다.
- 해결: `apply.sh` 가 Harbor 를 설치할 때 [`post-render.sh`](post-render.sh) 로 DB StatefulSet 에 `fsGroupChangePolicy: OnRootMismatch` 를 넣는다. 볼륨 최상위 폴더의 그룹이 이미 맞으면 kubelet 이 안쪽 권한을 건드리지 않는다. 차트 values 로는 바꿀 수 없어서(템플릿에 고정) post-renderer 를 쓴다. 같은 차트의 registry 는 이미 이 설정을 쓴다.
- 적용하면 DB 파드가 한 번 다시 만들어진다(Harbor 가 잠깐 끊긴다). post-renderer 는 Helm 3 방식이고 `kubectl kustomize` 를 쓴다.
- 이미 망가진 상태라면 `kubectl -n harbor delete pod harbor-database-0` 로 파드를 다시 만들면 init 컨테이너가 권한을 되돌린다.
- 확인: `kubectl -n harbor get sts harbor-database -o jsonpath='{.spec.template.spec.securityContext}'` 에 `fsGroupChangePolicy` 가 보이면 된다.
