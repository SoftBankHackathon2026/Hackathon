"""데모용 장애 주입·원복. 공용 sample-app 을 내리는 명령이다. 시연 시간을 맞춘 뒤 실행한다.

  python -m agent.demo_fault inject [--host 주소]  # OpenBao DB_HOST 를 바꿈(기본: 없는 호스트) → 파드 전부 재생성 → 전부 NotReady
  python -m agent.demo_fault restore   # 계획서 값으로 되돌림 → 재시작 → Ready 대기 (에이전트가 복구 못 했을 때)

rollout restart 는 가용 파드를 남기며 한 개씩 바꾼다. 새 파드가 NotReady 면 멈추고 옛 파드가 옛 URL 로 계속 응답해
장애가 드러나지 않는다. 그래서 주입은 파드를 한꺼번에 지운다.
"""

from __future__ import annotations

import argparse
import sys
import urllib.parse

from .config import AgentConfig, load_config
from .kv import KvError, OpenBaoKv
from .restart import InClusterKubeApi, RestartError, RolloutRestarter

WRONG_HOST = "postgres-wrong.hackathon.svc.cluster.local"


def _kv(cfg: AgentConfig) -> OpenBaoKv:
    return OpenBaoKv(cfg.openbao.addr, cfg.openbao.mount, cfg.openbao.path, cfg.openbao.token, timeout=cfg.openbao.timeout)


def inject(cfg: AgentConfig, kv, api, host: str = WRONG_HOST) -> None:
    snapshot = kv.read()
    version = kv.write_changes({"DB_HOST": host}, expected_version=snapshot.version)
    print(f"OpenBao DB_HOST={host} (v{snapshot.version} → v{version})")
    pods_path = f"/api/v1/namespaces/{urllib.parse.quote(cfg.restart.namespace)}/pods"
    selector = urllib.parse.quote(cfg.restart.selector)
    for pod in api.get(f"{pods_path}?labelSelector={selector}").get("items", []):
        name = pod["metadata"]["name"]
        api.delete(f"{pods_path}/{urllib.parse.quote(name)}")
        print(f"파드 삭제: {name}")
    print("주입 완료. 새 파드는 /readyz 실패로 NotReady 가 된다. 다음: python -m agent run")


def restore(cfg: AgentConfig, kv, restarter) -> None:
    db = cfg.target_db
    snapshot = kv.read()
    version = kv.write_changes({"DB_HOST": str(db["host"]), "DB_PORT": str(db["port"])}, expected_version=snapshot.version)
    print(f"OpenBao 를 계획서 값으로 되돌림 (v{snapshot.version} → v{version}). 재시작한다")
    restarter.restart_and_wait()
    print("원복 완료")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent.demo_fault", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config")
    parser.add_argument("action", choices=["inject", "restore"])
    parser.add_argument("--host", default=WRONG_HOST)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    api = InClusterKubeApi(server=cfg.restart.api_server, token_file=cfg.restart.token_file, ca_file=cfg.restart.ca_file)
    try:
        if args.action == "inject":
            inject(cfg, _kv(cfg), api, host=args.host)
        else:
            restore(cfg, _kv(cfg), RolloutRestarter(cfg.restart, api))
    except (KvError, RestartError) as exc:
        print(f"실패: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
