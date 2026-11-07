import os
import sys
from datetime import timedelta

from opensandbox import SandboxSync
from opensandbox.config import ConnectionConfigSync

CHECKS = [
    ("gvisor 커널", "uname -r", lambda out: "gvisor" in out),
    ("SYS_ADMIN 없음", "grep CapEff /proc/self/status", lambda out: not int(out.split()[-1], 16) & (1 << 21)),
    ("OpenAI 허용", "curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://api.openai.com/v1/models", lambda out: out == "401"),
    ("허용 밖 도메인 거절", "curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://github.com || true", lambda out: out != "200"),
    ("직접 외부 통신 차단", "curl -s -o /dev/null --noproxy '*' --max-time 8 https://1.1.1.1 && echo open || echo blocked", lambda out: out.endswith("blocked")),
    ("호스트 API 차단", "curl -s -o /dev/null --noproxy '*' --max-time 5 http://172.31.250.1:8090/health && echo open || echo blocked", lambda out: out.endswith("blocked")),
    ("메타데이터 차단", "curl -s -o /dev/null --noproxy '*' --max-time 5 http://169.254.169.254/ && echo open || echo blocked", lambda out: out.endswith("blocked")),
    ("Codex 설치", "codex --version", lambda out: out.startswith("codex-cli")),
]


def main() -> int:
    config = ConnectionConfigSync(domain=os.environ.get("OPENSANDBOX_DOMAIN", "127.0.0.1:8090"), protocol="http",
                                  api_key=os.environ["OPENSANDBOX_SERVER_API_KEY"], request_timeout=timedelta(seconds=120))
    image = sys.argv[1] if len(sys.argv) > 1 else "oneport/codex-sandbox:latest"
    sandbox = SandboxSync.create(image, connection_config=config, timeout=timedelta(minutes=10),
                                 resource={"cpu": "1", "memory": "1Gi"})
    failed = 0
    try:
        for name, command, ok in CHECKS:
            result = sandbox.commands.run(command)
            out = "".join(m.text for m in result.logs.stdout).strip()
            passed = ok(out)
            failed += not passed
            print(f"{'PASS' if passed else 'FAIL'} {name}: {out[:80]}")
    finally:
        sandbox.kill()
        sandbox.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
