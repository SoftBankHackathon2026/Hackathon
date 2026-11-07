# 원격 AI 에이전트 (OpenSandbox 호스트에서 실행)

OpenSandbox 가 있는 호스트에서 에이전트를 돌려 온프렘 클러스터의 sample-app 설정을 고친다.
Codex 는 같은 호스트의 샌드박스에서 돌고, 클러스터에는 Tailscale + kube API 포트 포워드로만 닿는다.

```
[OpenSandbox 호스트]                                   [온프렘 k3s, hackathon 네임스페이스]
 oneport-agent (watch 60초)                             migration-policy (OPA)
   ├─ 127.0.0.1:18181 ─ kubectl port-forward ─────────▶ svc/migration-policy:8181
   ├─ 127.0.0.1:18200 ─ kubectl port-forward ─────────▶ svc/openbao:8200
   ├─ 127.0.0.1:18080 ─ kubectl port-forward ─────────▶ svc/sample-app-smoke:80
   ├─ 127.0.0.1:15432 ─ kubectl port-forward ─────────▶ svc/postgres:5432 (TLS 는 원래 호스트 이름으로 검증)
   ├─ kube API (Tailscale) ───────────────────────────▶ Rollout sample-app 재시작
   └─ 127.0.0.1:8090 OpenSandbox ─ gVisor 샌드박스 ─ Codex (OpenAI 도메인만 나감)
```

## 흐름

`watch` 가 60초마다 한 번 판정한다.

1. 대상 앱 스모크(`--target-only`) → OPA `pre_cutover` 판정
2. allow 면 끝. deny 이고 사유가 전부 APP-01 이면 OpenBao 값을 읽고 Codex(샌드박스)에 제안을 받는다
3. 정적 검사(S0~S7) → 수정 값으로 DB 탐침 → OpenBao cas 쓰기 → Rollout 재시작 → 다시 판정
4. 재판정 allow 일 때만 `RESOLVED`. 그 밖의 사유·hold 는 사람에게 넘긴다

승인 대기(`auto_apply: false`) 실행이 있으면 새로 판정하지 않는다.

## 설치

먼저 상위 폴더의 `install.sh` 로 OpenSandbox 호스트를 만든다. 호스트를 Tailscale 에 붙여 kube API 에 닿게 한다.

```sh
sudo tailscale up --accept-routes --accept-dns=false
sudo K8S_API_SERVER=https://<kube API 주소>:6443 agent/deploy/opensandbox/agent/install.sh
```

`install.sh` 는 kubectl(체크섬 확인), 에이전트 venv, 설정, systemd 유닛을 깐다. 비밀 파일이 다 있으면 서비스를 켜고, 없으면 빠진 파일을 알려 준다.

## 비밀 파일 (`/etc/oneport-agent`, root:opensandbox 0640)

| 파일 | 내용 | 얻는 곳 |
|---|---|---|
| `kubeconfig` | ServiceAccount `ai-agent` 토큰 kubeconfig | gitops `apps/ai-agent` 의 토큰 Secret |
| `kube-token`, `kube-ca.crt` | 같은 토큰과 클러스터 CA (Rollout 재시작용) | 같은 Secret |
| `db-ca.pem` | 대상 DB CA (verify-full) | Secret `sample-app-db-ca` |
| `agent.env` | `VAULT_TOKEN=`(에이전트 전용 OpenBao 토큰), `SAMPLE_APP_DATABASE_URL=`(탐침 비밀번호) | OpenBao, Secret `sample-app-db` |
| `codex.env` | `CODEX_API_KEY=` | 직접 넣는다 |

값은 저장소·로그에 남기지 않는다. `/etc/opensandbox/server.env` 의 OpenSandbox API 키는 서비스가 같이 읽는다.

## 확인

```sh
systemctl status oneport-agent 'oneport-forward@*'
journalctl -u oneport-agent -f
sudo -u oneport-agent /opt/oneport-agent/venv/bin/python -m agent --config /etc/oneport-agent/agent.yaml status
```

## 한계

- 포트 포워드가 끊기면 systemd 가 5초 뒤 다시 잇는다. 그동안의 판정은 `ERROR`/hold 로 끝나고 다음 주기에 다시 한다.
- 이 호스트가 클러스터에 하는 일은 ServiceAccount `ai-agent` 권한(Rollout 재시작, 파드 조회·포트 포워드) 안이다. OpenBao 쓰기는 에이전트 토큰 정책(한 경로)으로 막는다.

## 시연 (이전 전 설정이 남은 상황)

AWS → 온프렘으로 옮겼는데 앱 설정이 아직 AWS RDS 주소를 가리켜 대상 DB 에 붙지 못하는 상황을 만든다.
공용 sample-app 이 잠깐 내려가므로 팀과 시간을 맞춘다.

```sh
oneport-agent fault inject --host "$OLD_RDS_ENDPOINT"   # 주소는 저장소에 두지 않고 그때 넣는다
oneport-agent fault inject --set DB_NAME=migration_demo  # 이전 전 DB 이름이 남은 상황
journalctl -u oneport-agent -f                         # 다음 주기에 APP-01 → Codex(샌드박스) 제안
oneport-agent status                                   # 실행 기록
oneport-agent approve <run_id>                         # auto_apply: false 일 때 사람이 승인
oneport-agent fault restore                            # 에이전트가 못 고쳤을 때 계획서 값으로 되돌림
```

- 승인 시연: `/etc/oneport-agent/agent.yaml` 의 `auto_apply: false` 로 두면 제안·검사·탐침까지 하고 멈춘다. 승인하면 쓰기·재시작·재판정을 한다.
- Slack: `agent.env` 에 `SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...`(Incoming Webhook, 채널 하나에만 쓸 수 있어 권한이 가장 좁다)를 넣으면 시작·제안·반영·결과가 그 채널에 올라간다. 봇 토큰을 쓰려면 `SLACK_BOT_TOKEN=xoxb-...`(`chat:write`)와 `agent.yaml` 의 `slack.channel` 을 넣는다. 웹훅이 있으면 웹훅을 쓴다.
- `oneport-agent` 는 서비스와 같은 사용자·환경변수로 `python -m agent` 를 부른다. `fault` 는 `agent.demo_fault` 다.

## 고칠 수 있는 범위

`plan.target_db` 에 적은 항목만 고친다. 값은 계획서 값과 같아야 한다.

| 계획서 필드 | 키 | 형식 검사 |
|---|---|---|
| `host` | `DB_HOST` | 호스트 이름 |
| `port` | `DB_PORT` | 1~65535 |
| `name` | `DB_NAME` | 식별자 |
| `user` | `DB_USER` | 식별자 |
| `sslmode` | `DB_SSLMODE` | libpq sslmode 값 |

AI 에게 묻기 전에 지금 설정으로 DB 에 붙어 보고(진단), 실패 종류(DNS, 연결 거부, `SQLSTATE 3D000` DB 없음, `28P01` 인증 실패, TLS 등)를 근거로 넘긴다. 지금 설정으로 접속되면 설정 문제가 아니라고 보고 사람에게 넘긴다.


## 플랫폼 화면 연결

`platform.url` 과 `agent.env` 의 `ONEPORT_INGEST_TOKEN`(플랫폼 보고기와 같은 ingest 토큰)을 넣으면 watch 가 주기마다 플랫폼에 보고한다.
호스트는 밖에서 들어오는 연결을 받지 않으므로 모두 에이전트가 먼저 부른다.

- 상태: `POST /api/v1/ingest/agent/heartbeat`. 현재 설정(`config`)도 함께 보낸다. Slack 연결 방식(webhook/bot/none), 스레드, 고칠 수 있는 키와 계획값, 샌드박스 이미지·모델이 들어가고 토큰·webhook 주소 같은 비밀은 넣지 않는다.
- 실행 기록: 바뀐 실행만 `PUT /api/v1/ingest/agent/runs/<run_id>`
- 승인·거절: `GET /api/v1/ingest/agent/approvals` 로 가져와 처리하고 `POST .../approvals/<id>/ack` 로 결과를 알린다. 승인자는 실행 기록에 남는다.
- 명령: `GET /api/v1/ingest/agent/commands` 로 가져와 처리하고 `POST .../commands/<id>/ack` 에 결과 문장을 남긴다. 기다리는 동안 10초마다 확인한다.
  - `settings`: `auto_apply`(bool), `interval_seconds`(30~3600), `slack_enabled`(bool), `slack_thread_ts`(빈 값 또는 `1700000000.000100` 꼴)만 바꿀 수 있다. 다른 키나 범위 밖 값은 거절한다. 바꾼 값은 `state_dir/settings.json` 에 남아 재시작해도 유지된다(`agent.yaml` 보다 우선).
  - `rerun`: 남은 대기 시간을 건너뛰고 바로 다시 판정한다.
- Slack 승인 대기 메시지에 플랫폼 링크가 붙는다.
- `slack.buttons: true` 면 승인 대기 메시지에 승인, 거절, 플랫폼에서 보기 버튼이 붙는다. 버튼은 플랫폼 `/api/v1/slack/interactions` 가 받아 화면 승인과 같은 대기열에 넣는다. 플랫폼에 Slack 서명 비밀을 넣기 전에는 켜지 않는다(`platform/deploy/ec2/README.md`).
- `APP-01-UNVERIFIED` 보류(스모크 일시 실패)는 같은 내용이 3번 연속일 때만 Slack 으로 알린다.

플랫폼에 닿지 않아도 판정·수정은 그대로 하고 로그만 남긴다.
