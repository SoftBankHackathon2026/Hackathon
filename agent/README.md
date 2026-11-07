# agent — 온프렘 AI 설정 수정 에이전트

마이그레이션이 OPA 에 막혔을 때 사유가 APP-01(대상 앱 DB 연결 실패)이면 Codex 가 설정값을 고치고, 검사를 통과한 값만
OpenBao 에 반영한 뒤 재검증하고 보고한다. **무엇을 고칠 수 있는지는 AI 가 아니라 `agent/scope.py` 가 정한다.**
온프렘 전용이다. 클라우드 간(AWS↔GCP) AI 수정은 다루지 않는다.

```
스모크 → OPA 판정 ─ allow ──────────────────────→ "고칠 것 없음"
                  ├ deny 이고 사유가 APP-01-* 뿐 ─┐
                  └ 그 밖(hold, error, 다른 사유) → 사람에게 넘김
  OpenBao 읽기(버전 기억) ← ──────────────────────┘
  → Codex 제안 → 정적 검사 → 수정 값 탐침(TCP + SELECT 1)
  → (auto_apply=false 면 여기서 승인 대기)
  → OpenBao cas 쓰기 → 재시작 → 새 파드 Ready 대기 → 스모크 + OPA 재판정
```

성공은 재판정 `outcome == allow` 로만 정한다. 적용했다는 사실만으로 성공 처리하지 않는다.

AI 가 건드리지 않는 것: OPA 정책, 승인자, 이전 도구 코드, 앱 소스, DB 데이터, 비밀값.

## 정적 검사

| 코드 | 거절 조건 |
| --- | --- |
| `S0-EMPTY` | 제안이 비어 있음 |
| `S1-KEY-NOT-ALLOWED` | `DB_HOST`, `DB_PORT` 밖의 키 |
| `S2-VALUE-NOT-IN-PLAN` | 값이 `plan.target_db` 와 다름 |
| `S3-SECRET-KEY` | 비밀값 키 (password, token, secret, dsn 등) |
| `S4-FORMAT` | 호스트·포트 형식 오류 |
| `S5-NO-CHANGE` | 현재 값과 같음. 설정 문제가 아닐 수 있다 |
| `S6-DUPLICATE-KEY` | 같은 키가 두 번 |
| `S7-FILE-MISMATCH` | 보고한 변경과 실제 파일 수정이 다름 |

거절된 제안의 값은 기록·보고하지 않는다. 허용 키 밖의 값은 비밀값일 수 있어서다.

## Codex 격리

Codex 는 `config/target.json`(`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`)만 보고, 환경변수도 허용 목록만 받는다.

| 실행 방식 | 어디서 | 격리 |
| --- | --- | --- |
| `local` | Mac·개발 | `--sandbox workspace-write` |
| `sidecar` | 파드 | 비밀값 없는 옆 컨테이너에서 `--sandbox danger-full-access` |
| `opensandbox` | OpenSandbox 서버가 있는 호스트 | 작업마다 gVisor 샌드박스를 새로 만들어 `--sandbox danger-full-access` 로 실행하고 지운다 |

파드에서 샌드박스를 끈 이유: `workspace-write` 는 Linux 에서 bubblewrap, 비특권 user namespace, /proc 마운트가 필요하다.
bwrap 을 설치해도 제한된 컨테이너에서는 시작하지 못하고, Codex 가 빈 제안을 낸다. 사이드카에는 `CODEX_API_KEY` 와 작업 폴더만
있고 PID 네임스페이스도 나뉜다. 다만 네트워크는 막혀 있지 않다. 에이전트는 사이드카가 쓴 파일을 신뢰하지 않고 검사한다.

### opensandbox 실행 방식

[OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) 서버에 작업마다 샌드박스를 하나 만든다.
`config/target.json` 과 출력 스키마만 넣고, Codex 를 샌드박스 안 일반 사용자(`sandbox_uid`)로 실행한 뒤
최종 메시지와 `config/target.json` 만 꺼내 온다. 꺼낸 파일은 다른 실행 방식과 똑같이 검사한다.

- 샌드박스에 넘기는 환경변수는 `CODEX_API_KEY` 와 `HOME` 둘뿐이다. OpenBao 토큰·DB 비밀번호·OpenSandbox API 키는 넘기지 않는다.
- 키는 샌드박스를 만들 때가 아니라 Codex 명령에만 준다. 컨테이너 설정에 남지 않는다.
- 샌드박스는 성공·실패와 상관없이 지운다.
- 샌드박스의 외부 통신 제한(OpenAI 도메인만)은 서버 호스트 쪽에서 건다. 에이전트 코드는 관여하지 않는다.

```yaml
codex:
  runner: opensandbox
  model: gpt-5.5
  sandbox_url: http://127.0.0.1:8090
  sandbox_api_key_env: OPENSANDBOX_API_KEY
  sandbox_image: oneport/codex-sandbox:latest
```

`pip install -e '.[opensandbox]'` 로 SDK 를 함께 설치한다.

## 돌려 보기

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ../migration -e '.[dev]'
pytest                       # Codex·OpenBao 는 fake

python -m agent.fakes        # fake 로 장애 → 복구 한 바퀴. 스모크·정책 클라이언트는 진짜 코드
python -m agent.fakes --no-auto-apply

# 실제 Codex + OPA(rego) + OpenBao(dev) + Postgres (docker). 앱과 재시작만 가짜다
CODEX_HOME_DIR=~/.codex dev/real_loop.sh     # 또는 CODEX_API_KEY

# 실제 rego 통합 테스트
docker run --rm -d --name opa -p 18181:8181 -v "$PWD/../policies:/p:ro" \
  openpolicyagent/opa:1.22.0-static run --server --addr :8181 /p/migration.rego
AGENT_TEST_OPA_URL=http://127.0.0.1:18181 pytest
```

설정 예시는 [agent.example.yaml](agent.example.yaml). 비밀값은 환경변수로만 받는다.

## 실행과 기록

```sh
python -m agent run [--no-auto-apply]
python -m agent approve <run_id>
python -m agent status [run_id]
```

- 끝 상태: `RESOLVED`, `NOTHING_TO_FIX`, `WAITING_APPROVAL`(종료 코드 0) / `HANDED_OFF`, `REJECTED`, `PROBE_FAILED`, `CAS_CONFLICT`, `RESTART_FAILED`, `UNRESOLVED`, `ERROR`(종료 코드 2)
- 실행 기록은 `state_dir/agent-runs.sqlite3`, 정책 판정은 `state_dir/policy.sqlite3`.
- 보고는 stdout 에 남기고, `SLACK_BOT_TOKEN` 과 `slack.channel` 이 있으면 `chat.postMessage` 로도 보낸다.
- 쓰기 뒤 재시작이나 재검증이 실패해도 자동으로 되돌리지 않는다. 상태와 저장소 버전을 보고하고 사람에게 넘긴다.

## 데모 장애 주입

공용 온프렘 sample-app 이 내려간다. 시연 시간을 팀과 맞춘 뒤 실행한다.

```sh
scripts/inject-fault.sh     # OpenBao DB_HOST 를 없는 호스트로 → 파드 전부 재생성
kubectl -n hackathon exec deploy/ai-agent -c agent -- python -m agent run
scripts/restore-fault.sh    # 에이전트가 복구하지 못했을 때
```

주입이 rollout restart 대신 파드를 한꺼번에 지우는 이유: restart 는 가용 파드를 남기며 한 개씩 바꾼다. 새 파드가 NotReady 면 멈추고,
옛 파드가 옛 URL 로 계속 응답해 장애가 드러나지 않는다.

## 현재 한계

- 스모크는 `--target-only` 다. 온프렘에 CDC 커넥터가 없어 대상 앱 DB 조회만 보며 복제·쓰기는 검증하지 않는다. 보고에 그렇게 적힌다.
- SEC-01·SEC-02 증거(`target_db_exposure`, `target_location`)는 수집기가 없어 설정에 선언한 값이다. OPA 입력의 `source: agent.declared-config` 로 드러난다.
- 재시작 대기는 Rollout API 를 가짜로 시험했다. 실제 클러스터의 restart·abort 동작은 시연 전에 확인한다.
- 사이드카 구성은 파드와 같은 제약의 로컬 컨테이너 2개와 실제 Codex(`gpt-5.5`)로 확인했다 (`dev/sidecar_check.sh`, 피크 304MiB). 실제 파드에서는 아직 돌리지 않았다.
- 사이드카는 `codex.model` 이 필수다. 지정하지 않으면 Codex 내장 기본 모델로 가는데, 그때 요청의 도구 목록이 비어 파일을 읽지 못하고 빈 제안만 낸다 (codex-cli 0.154, 흉내 API 로 확인).
