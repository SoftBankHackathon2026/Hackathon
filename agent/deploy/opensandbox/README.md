# OpenSandbox 호스트 (Codex 실행용)

`codex.runner: opensandbox` 가 쓰는 OpenSandbox 서버를 Ubuntu 호스트 한 대에 올린다.
Codex 는 작업마다 새 gVisor 샌드박스에서 돌고, 밖으로는 OpenAI·ChatGPT 도메인으로만 나간다.

## 구성

| 구성 요소 | 내용 |
|---|---|
| Docker + gVisor | `runsc` 런타임. KVM 없이 도는 기본 방식(systrap)이라 일반 VM 에서도 된다 |
| OpenSandbox 서버 | `opensandbox-server` (기본 1.2.0), systemd `opensandbox.service`, `127.0.0.1:8090` 에만 열고 API 키 필수 |
| 샌드박스 네트워크 | Docker 네트워크 `sandbox-net` (172.31.250.0/24, 브리지 `br-sandbox`, 컨테이너끼리 통신 끔) |
| 외부 통신 차단 | `DOCKER-USER` 에서 `sandbox-net` 의 새 연결을 모두 버린다 (`sandbox-egress.service`) |
| 허용 프록시 | tinyproxy `172.31.250.1:8888`. `tinyproxy.filter` 의 도메인만 CONNECT 443 을 허용한다 |
| Codex 이미지 | `oneport/codex-sandbox:<codex 버전>` (`Dockerfile.codex`, uid 10001 `agent`) |

샌드박스 포트는 `publish_host = 127.0.0.1` 로 게시한다. Docker 가 게시한 포트는 ufw 를 거치지 않아서,
이 값이 없으면 공인 IP 호스트에서 execd 포트가 밖으로 열린다.

## 설치

```sh
sudo agent/deploy/opensandbox/install.sh            # 다시 돌려도 된다
OPENSANDBOX_VERSION=1.2.0 CODEX_VERSION=0.167.0 sudo -E agent/deploy/opensandbox/install.sh
```

- API 키는 처음 한 번 만들어 `/etc/opensandbox/server.env` (`OPENSANDBOX_SERVER_API_KEY`, root:opensandbox 0640) 에 둔다.
- Codex 키는 직접 넣는다: `/etc/oneport-agent/codex.env` 에 `CODEX_API_KEY=...` (root:opensandbox 0640). 저장소·로그에 남기지 않는다.
- ufw 가 켜져 있으면 `br-sandbox` 에서 프록시 포트로 오는 연결만 허용하는 규칙 하나를 더한다. 외부로 여는 포트는 없다.

## 확인

```sh
set -a; eval "$(sudo cat /etc/opensandbox/server.env)"; set +a
python agent/deploy/opensandbox/verify.py           # opensandbox SDK 가 있는 venv 에서
```

gVisor 커널, SYS_ADMIN 없음, OpenAI 도달, 허용 밖 도메인 거절, 직접 외부 통신·호스트 API·메타데이터 차단, Codex 설치를 본다.
하나라도 틀리면 종료 코드 1 이다.

## 에이전트 설정

```yaml
codex:
  runner: opensandbox
  model: gpt-5.5
  sandbox_url: http://127.0.0.1:8090
  sandbox_api_key_env: OPENSANDBOX_SERVER_API_KEY
  sandbox_image: oneport/codex-sandbox:latest
```

에이전트 프로세스에 `/etc/opensandbox/server.env` 와 `/etc/oneport-agent/codex.env` 를 환경변수로 준다.

## 한계

- OpenSandbox 의 `networkPolicy`(egress 사이드카)는 gVisor 와 같이 쓸 수 없다. 서버가 `networkPolicy is not compatible with runtime 'gvisor'` 로 거절한다.
  그래서 외부 통신 제한을 호스트에서 건다. Kata 로 바꾸면 쓸 수 있지만 `/dev/kvm` 이 필요하다.
- Codex 자체 샌드박스(bwrap)는 gVisor 안에서 시작하지 못한다 (`bwrap: loopback: Failed RTM_NEWADDR`).
  샌드박스 안에서는 `danger-full-access` 로 돌고 경계는 gVisor 와 외부 통신 차단이다.
- 허용 도메인 안에서의 유출(예: OpenAI API 로 다른 계정에 보내기)은 막지 못한다. 샌드박스에는 Codex 키와 비밀값 없는 설정 파일만 있다.
- 서버는 한 프로세스(SQLite)다. 여러 대로 늘리지 않는다.
