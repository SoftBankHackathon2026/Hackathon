# agent — 이전 보류 원인을 고치는 AI 에이전트 (ghost-agent)

설계 원본은 `업무-아젠다/08-AI-에이전트와-샌드박스.md` § 샌드박스·에이전트 배치 기획. 이 README 는 실행 방법만 적는다.

## 무엇을 하나

1. 설정 저장소(온프렘 Vault KV v2 / AWS Secrets Manager / 개발용 파일)에서 대상 앱의 DB 접속 설정을 읽는다.
2. 그 값으로 대상 DB 에 실제 연결·조회를 시도하고(탐침), 결과를 `migration-policy`(OPA) 의 `pre_cutover` 단계로 판정한다.
3. 정책이 `APP-01-*`(대상 DB 연결 실패)로 거절하면 **Codex CLI** 에 현재 설정(비밀값 제외)·거절 사유·탐침 오류·허용 범위를 주고 수정안을 받는다.
4. 수정안을 정적 검사(S0~S4)하고, 수정 값으로 다시 탐침한 뒤 `remediation` 정책(`policies/remediation.rego`)으로 적용 허용을 판정한다.
5. allow 면 저장소의 허용 키만 고쳐 쓴다(버전 충돌 시 중단). `plan.remediation.auto_apply=false` 면 Slack 에서 `/migrate approve` 를 기다린다.
6. 저장소에서 다시 읽어 재탐침·재판정하고 결과를 Slack 에 보고한다.

AI 가 바꿀 수 있는 것은 `scope.allowed_keys`(기본 `DB_HOST`, `DB_PORT`) 뿐이고, 값은 `plan.target_db` 에 등록된 것만 허용된다.
정책·승인·검사 결과·비밀값은 바꾸지 못한다. 적용했다는 사실만으로 이전이 재개되지 않는다 — 재판정이 allow 여야 한다.

```
agent/agent/
  config.py       agent.yaml + 환경변수
  stores.py       FileStore · VaultKvStore · AwsSecretsManagerStore (read / write(cas))
  probe.py        PostgresProber → target_app_db / remediation_probe check
  scope.py        Proposal · Change · 정적 검사 S0~S4
  proposer.py     CodexProposer (codex exec, --output-schema schema/proposal.schema.json)
  remediation.py  Remediator.run() / approve(), RemediationStore(sqlite)
  notify.py       SlackNotifier(chat.postMessage) · LogNotifier
  slack_bot.py    /migrate start|status|approve (Socket Mode)
```

## 로컬에서 돌리기

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ../migration -e '.[dev]'      # aws/slack 은 선택: '.[dev,aws,slack]'
brew install opa                                # policy_dir 모드(로컬 opa eval)
pytest

# 개발용 파일 저장소로 한 바퀴 (Codex 가 설치돼 있고 CODEX_API_KEY 가 있어야 수정안 단계가 돈다)
cd dev && python -m agent run --config agent.dev.yaml --no-slack
python -m agent probe --config dev/agent.dev.yaml      # 탐침만
python -m agent status --config dev/agent.dev.yaml
```

`dev/target.json` 의 `DB_HOST` 가 일부러 틀려 있다. `plan.target_db.host` 를 실제로 붙는 Postgres 로 바꿔야 수정 뒤 재검사가 통과한다.

## 환경변수 (값은 어디에도 적지 않는다)

| 이름 | 용도 |
|---|---|
| `CODEX_API_KEY` | Codex CLI 인증 (`codex exec` 가 환경변수를 우선 사용) |
| `VAULT_TOKEN` (`VAULT_NAMESPACE`) | `store.kind: vault` |
| `AWS_*` 또는 IRSA | `store.kind: aws-secrets-manager` |
| `SLACK_BOT_TOKEN` (`xoxb-`), `SLACK_APP_TOKEN` (`xapp-`) | 알림·슬래시 명령. Socket Mode 라 Ingress 불필요 |

Codex 하위 프로세스에는 `VAULT_*`, `SLACK_*`, `AWS_*` 를 넘기지 않는다 (`proposer.py`).

## Slack 앱 설정 (한 번)

1. api.slack.com 에서 앱 생성 → Socket Mode 켜기 → App-Level Token(`connections:write`).
2. Bot Token Scopes: `chat:write`, `commands`. 채널에 봇 초대.
3. Slash Command `/migrate` 등록 (Socket Mode 라 Request URL 없음).
4. 토큰 두 개를 Secret `ai-agent-credentials` 에 넣는다 (`gitops/apps/ai-agent/secret.example.yaml`).

## 상태·기록

`state_dir/remediation.sqlite3` 에 실행 기록, `policy.sqlite3` 에 정책 판정(migration 패키지의 DecisionStore)이 남는다.
기록 형식은 `agent/schema/remediation_record.schema.json`.

## 아직 아닌 것

- 대상 앱 자체의 검사 endpoint 연결(7·18번 태스크). 지금 `target_app_db` 증거는 에이전트 파드의 탐침이다 (`source: agent.probe`).
- Kubernetes Job 샌드박스(앱 컨테이너 리허설). 지금 검증은 정적 검사 + 수정 값 탐침이다.
- 온프렘 OpenBao와 AWS Secrets Manager의 실제 운영 자격증명·앱 반영까지의 통합 검증. 저장소 어댑터가 있다는 것과 운영 연결 검증은 구분한다.
- DB cutover 자동 호출 연결. 이 모듈은 별도 실행되는 수정·재판정 도구이며, main에 포함하는 것만으로 DB 전환 경로가 OPA/AI를 호출하지는 않는다.
