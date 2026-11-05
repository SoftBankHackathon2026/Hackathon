
## proxy-cache 와 노드 mirror (2026-11-05)

팀 결정으로 이미지 레지스트리를 Harbor 로 관리한다. 첫 단계는 **proxy-cache**: gitops 의 이미지 주소(`ghcr.io/...`, `docker.io/...`, `quay.io/...`)는 그대로 두고, k3s 노드가 Harbor 를 거쳐 받게 한다.

- Harbor 에 만든 것(admin API, 2026-11-05): 레지스트리 엔드포인트 `ghcr`(github-ghcr) · `dockerhub`(docker-hub) · `quay`(docker-registry 타입, quay 타입은 proxy 미지원) 와 같은 이름의 public proxy 프로젝트 3개. `/v2/<project>/<repo>/manifests/<tag>` 로 세 이미지(migration-tools, sample-app, openbao) 모두 200 확인.
- 노드 쪽: [`registries.yaml`](registries.yaml) 을 두 노드의 `/etc/rancher/k3s/registries.yaml` 로 복사하고 k3s 를 재시작한다(k3s 문서: 시작 때만 읽음). HTTP 엔드포인트라 `http://` 를 명시했다.
- 확인: 재시작 뒤 `crictl pull ghcr.io/softbankhackathon2026/migration-tools:<tag>` 가 Harbor `ghcr` 프로젝트의 repositories 에 나타나면 된다.
- 다음 단계(미정): CI 가 Harbor 프로젝트(예: `ghost`)로 직접 push 하고 gitops 주소를 바꾸는 방식. 그때도 이 mirror 설정은 그대로 둔다.
