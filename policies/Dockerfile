# 정책 서비스 이미지: ghcr.io/softbankhackathon2026/migration-policy
# OPA 서버에 migration.rego·remediation.rego 를 넣는다 (테스트·예시는 제외). 정책 버전 = 이미지 digest.
# 평가 경로: POST /v1/data/ghost/migration/decision    (이전 단계 허용, migration/migration/policy_client.py)
#           POST /v1/data/ghost/remediation/decision  (AI 수정안 적용 허용, agent/agent/remediation.py)
FROM openpolicyagent/opa:1.22.0-static

COPY migration.rego /policies/migration.rego
COPY remediation.rego /policies/remediation.rego

# -static 이미지는 UID 1000(nonroot) 로 돈다. 쓰기 가능한 파일시스템이 필요 없다.
USER 1000:1000
EXPOSE 8181

ENTRYPOINT ["/opa"]
CMD ["run", "--server", "--addr=:8181", "--log-level=info", "--log-format=json", \
     "--disable-telemetry", \
     "--set=decision_logs.console=true", \
     "/policies"]
