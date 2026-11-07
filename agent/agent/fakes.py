"""로컬 한 바퀴와 테스트용 가짜 세계. 운영 경로에서 쓰지 않는다.

진짜 코드(smoke.py 의 HTTP 호출, policy_client, 정적 검사, 실행 기록)는 그대로 지나가고
바깥만 가짜다: 원본·대상 앱(HTTP), OpenBao, Codex, DB 탐침, 재시작. 앱은 재시작 때만 저장소 값을 읽는다.

  python -m agent.fakes              # 장애 주입 → 복구 한 바퀴
  python -m agent.fakes --no-auto-apply
"""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping, Sequence

from migration.policy_client import DecisionStore
from migration.smoke import run_smoke

from .kv import CasConflict, KvSnapshot
from .notify import StdoutNotifier
from .probe import ProbeResult
from .remediation import Deps, Remediator
from .runs import RunStore
from .scope import Change, Proposal

GOOD_HOST = "postgres.hackathon.svc.cluster.local"
GOOD_PORT = "5432"
JOB_ID = "fake_001"

PLAN: dict[str, Any] = {
    "source": "aws", "target": "onprem", "approved_locations": ["busan-local"],
    "limits": {"evidence_ttl_seconds": 300, "write_wait_max_seconds": 30},
    "target_db": {"host": GOOD_HOST, "port": int(GOOD_PORT)},
}
DECLARED: dict[str, dict[str, Any]] = {
    "target_db_exposure": {"public": False, "target_id": "fake"},
    "target_location": {"location": "busan-local", "target_id": "fake"},
}


class FakeKv:
    def __init__(self, data: Mapping[str, Any]) -> None:
        self.data, self.version = dict(data), 1
        self.writes: list[dict[str, str]] = []

    def read(self) -> KvSnapshot:
        return KvSnapshot(dict(self.data), self.version)

    def write_changes(self, changes: Mapping[str, str], *, expected_version: int) -> int:
        if expected_version != self.version:
            raise CasConflict(f"cas 충돌: 읽은 버전 {expected_version}, 현재 버전 {self.version}")
        self.data.update(changes)
        self.version += 1
        self.writes.append(dict(changes))
        return self.version


class FakeApps:
    """원본·대상 sample-app 요청 API. 대상은 live 설정이 계획과 맞을 때만 DB 에 닿고, 원본 행이 복제돼 보인다."""

    def __init__(self, kv: Any, *, good_host: str = GOOD_HOST, good_port: str = GOOD_PORT) -> None:
        self.kv = kv
        self.good_host, self.good_port = good_host, good_port
        self.live = dict(kv.read().data)  # 재시작해야만 바뀐다
        self.rows: dict[str, Any] = {}
        self.replicating = True
        self._servers = [self._serve("aws", self._source), self._serve("local", self._target)]

    @property
    def source_url(self) -> str:
        return f"http://127.0.0.1:{self._servers[0].server_address[1]}"

    @property
    def target_url(self) -> str:
        return f"http://127.0.0.1:{self._servers[1].server_address[1]}"

    def reload(self) -> None:
        self.live = dict(self.kv.read().data)

    def close(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()

    def _source(self, method: str, rid: str | None, body: Any) -> tuple[int, dict[str, Any]]:
        if method == "POST":
            created = body["request_id"] not in self.rows
            self.rows.setdefault(body["request_id"], body)
            return (201 if created else 200), {"created": created, "request": self.rows[body["request_id"]]}
        return self._read(rid)

    def _target(self, method: str, rid: str | None, body: Any) -> tuple[int, dict[str, Any]]:
        if self.live.get("DB_HOST") != self.good_host or str(self.live.get("DB_PORT")) != self.good_port:
            return 503, {"error": {"code": "DB_UNAVAILABLE", "message": "Database unavailable"}}
        if not self.replicating:
            return 404, {"error": {"code": "REQUEST_NOT_FOUND", "message": "Request was not found"}}
        return self._read(rid)

    def _read(self, rid: str | None) -> tuple[int, dict[str, Any]]:
        if rid in self.rows:
            return 200, {"request": self.rows[rid]}
        return 404, {"error": {"code": "REQUEST_NOT_FOUND", "message": "Request was not found"}}

    @staticmethod
    def _serve(environment: str, route: Callable[[str, str | None, Any], tuple[int, dict[str, Any]]]) -> ThreadingHTTPServer:
        class Handler(BaseHTTPRequestHandler):
            def _respond(self, method: str, rid: str | None, body: Any) -> None:
                code, doc = route(method, rid, body)
                raw = json.dumps({**doc, "environment": environment}).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                self._respond("GET", self.path.rsplit("/", 1)[-1], None)

            def do_POST(self) -> None:  # noqa: N802
                self._respond("POST", None, json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

            def log_message(self, *args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server


class FakeRestarter:
    def __init__(self, apps: FakeApps) -> None:
        self.apps, self.count = apps, 0

    def restart_and_wait(self) -> None:
        self.count += 1
        self.apps.reload()


class FakeProber:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.seen: list[dict[str, Any]] = []
        self.diagnosed: list[dict[str, Any]] = []

    def _result(self, config: Mapping[str, Any]) -> ProbeResult:
        if self.fail or config.get("DB_HOST") != GOOD_HOST:
            return ProbeResult(False, "tcp", "gaierror", "DNS: 호스트 이름을 찾지 못함")
        return ProbeResult(True, "done")

    def diagnose(self, config: Mapping[str, Any]) -> ProbeResult:
        self.diagnosed.append(dict(config))
        return self._result(config)

    def probe(self, config: Mapping[str, Any]) -> ProbeResult:
        self.seen.append(dict(config))
        return self._result(config)


class FakeEvaluator:
    """Rego 의 APP-01·공통 증거 규칙만 흉내 낸다. 실제 정책과의 일치는 통합 테스트(test_opa_integration)가 본다."""

    name = "fake-opa"

    def __init__(self, *, extra_reason: str | None = None, force_outcome: str | None = None) -> None:
        self.extra_reason, self.force_outcome = extra_reason, force_outcome

    def evaluate(self, policy_input: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
        check = policy_input["evidence"]["checks"]["target_app_db"]
        violations: list[dict[str, str]] = []
        if check["status"] != "pass":
            violations.append({"code": "APP-01-UNVERIFIED", "rule": "APP-01", "effect": "hold", "message": "수행 못함"})
        else:
            if check["detail"]["db_connected"] is False:
                violations.append({"code": "APP-01-TARGET-DB-UNREACHABLE", "rule": "APP-01", "effect": "deny", "message": "DB 연결 실패"})
            if check["detail"]["order_query_ok"] is False:
                violations.append({"code": "APP-01-ORDER-CHECK-FAILED", "rule": "APP-01", "effect": "deny", "message": "조회 실패"})
        if self.extra_reason:
            violations.append({"code": self.extra_reason, "rule": "SEC-01", "effect": "deny", "message": "다른 위반"})
        effects = {v["effect"] for v in violations}
        outcome = self.force_outcome or ("deny" if "deny" in effects else "hold" if effects else "allow")
        return {
            "policy_version": "fake-policy", "job_id": policy_input["job_id"], "stage": policy_input["stage"],
            "outcome": outcome, "allowed": outcome == "allow",
            "reason_codes": sorted(v["code"] for v in violations), "violations": violations,
        }, None


class FixHostProposer:
    """Codex 대신 계획서 값으로 고친다. 실제 호출 인자는 test_proposer 가 가짜 codex 실행 파일로 본다."""

    def __init__(self) -> None:
        self.calls: list[Mapping[str, Any]] = []

    def propose(
        self, *, current: Mapping[str, Any], reasons: Sequence[Mapping[str, Any]], target_db: Mapping[str, Any]
    ) -> tuple[Proposal, dict[str, str]]:
        self.calls.append({"current": dict(current), "reasons": list(reasons)})
        proposal = Proposal(
            (Change("DB_HOST", str(target_db["host"])),),
            "DB_HOST 가 계획서와 다르다", "앱 재시작 뒤 대상 DB 로 연결된다", "스모크와 OPA 재판정",
        )
        return proposal, proposal.values()


def faulty_kv() -> FakeKv:
    return FakeKv({
        "DB_HOST": "postgres-wrong.hackathon.svc.cluster.local", "DB_PORT": GOOD_PORT,
        "DB_NAME": "migration_demo", "DB_USER": "migration_app",
    })


def smoke_against(apps: FakeApps, *, replication_timeout: float = 1.0, target_only: bool = False) -> Callable[[], Mapping[str, Any]]:
    def smoke() -> Mapping[str, Any]:
        return run_smoke("pre_cutover", job_id=JOB_ID, source_url=None if target_only else apps.source_url,
                         target_url=apps.target_url, target_only=target_only, http_timeout=2.0,
                         replication_timeout=replication_timeout, poll_interval=0.05).to_check()
    return smoke


@dataclass
class World:
    kv: FakeKv
    apps: FakeApps
    restarter: FakeRestarter
    prober: FakeProber
    evaluator: FakeEvaluator
    proposer: Any
    runs: RunStore
    decisions: DecisionStore
    remediator: Remediator
    notes: list[str]

    def close(self) -> None:
        self.apps.close()
        self.runs.close()
        self.decisions.close()


class _ListNotifier:
    def __init__(self, sink: list[str], *, echo: bool) -> None:
        self.sink, self.echo = sink, echo

    def send(self, text: str) -> None:
        self.sink.append(text)
        if self.echo:
            StdoutNotifier().send(text)


def build_world(
    state_dir: str, *, kv: FakeKv | None = None, proposer: Any = None, evaluator: FakeEvaluator | None = None,
    prober: FakeProber | None = None, auto_apply: bool = True, echo: bool = False, target_only: bool = False,
) -> World:
    kv = kv or faulty_kv()
    apps = FakeApps(kv)
    restarter = FakeRestarter(apps)
    prober = prober or FakeProber()
    evaluator = evaluator or FakeEvaluator()
    proposer = proposer or FixHostProposer()
    runs, decisions = RunStore(state_dir), DecisionStore(state_dir)
    notes: list[str] = []
    remediator = Remediator(
        Deps(smoke=smoke_against(apps, target_only=target_only), evaluator=evaluator, decisions=decisions, kv=kv, proposer=proposer,
             prober=prober, restarter=restarter, notifier=_ListNotifier(notes, echo=echo), runs=runs),
        job_id=JOB_ID, plan=PLAN, declared_evidence=DECLARED, auto_apply=auto_apply,
    )
    return World(kv, apps, restarter, prober, evaluator, proposer, runs, decisions, remediator, notes)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-auto-apply", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="agent-fake-") as tmp:
        world = build_world(tmp, auto_apply=not args.no_auto_apply, echo=True)
        try:
            run = world.remediator.run()
            if run.state == "WAITING_APPROVAL":
                print("--- 승인 명령 실행 ---")
                run = world.remediator.approve(run.run_id)
            print(json.dumps({"state": run.state, "pre": run.detail.get("pre_decision"), "post": run.detail.get("post_decision"),
                              "kv_version": world.kv.version, "restarts": world.restarter.count}, ensure_ascii=False, indent=2))
            return 0 if run.state == "RESOLVED" else 2
        finally:
            world.close()


if __name__ == "__main__":
    raise SystemExit(main())
