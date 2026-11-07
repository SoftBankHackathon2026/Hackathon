"""ghost-agent: 이전이 정책에 막혔을 때 대상 설정을 고치는 AI 에이전트.

설계 원본: 업무-아젠다/08-AI-에이전트와-샌드박스.md § 샌드박스·에이전트 배치 기획.

모듈
  config       YAML + 환경변수 → AgentConfig (비밀값은 환경변수·설정 저장소에서만)
  stores       설정 저장소: Vault KV v2(온프렘) / AWS Secrets Manager(AWS) / 파일(개발)
  probe        대상 DB 연결·조회 탐침 → 정책 입력용 check
  scope        AI 가 바꿀 수 있는 키·값 범위와 정적 검사(S1~S4)
  proposer     Codex CLI 로 수정안 생성
  notify       Slack 알림(chat.postMessage) / 로그
  remediation  전체 흐름: 검사 → 보류 → 제안 → 검증 → 정책 → 적용 → 재검사 → 보고
  slack_bot    Slack 슬래시 명령(/migrate start|status|approve), Socket Mode
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
