import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

CUTOVER_DB = "cutover.sqlite3"
POLICY_DB = "policy.sqlite3"
VERIFY_LABELS = {"pre": ("post-stop", "pre-stop"), "post": ("final",)}


def read_rows(path: Path, query: str, params: tuple) -> list[dict]:
    if not path.exists():
        return []
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(query, params)]


def read_cutover(state_dir: Path, job_id: str) -> dict | None:
    rows = read_rows(
        state_dir / CUTOVER_DB,
        "SELECT job_id, stage, checkpoint, updated_at FROM cutover_jobs WHERE job_id = ?",
        (job_id,),
    )
    if not rows:
        return None
    row = rows[0]
    return {**row, "checkpoint": json.loads(row["checkpoint"])}


def read_decisions(state_dir: Path, job_id: str) -> list[dict]:
    rows = read_rows(
        state_dir / POLICY_DB,
        "SELECT decision_id, job_id, stage, outcome, reason_codes, policy_version, evaluated_at "
        "FROM policy_decisions WHERE job_id = ? ORDER BY evaluated_at",
        (job_id,),
    )
    return [{**row, "reason_codes": json.loads(row["reason_codes"])} for row in rows]


def read_verify(report_dir: Path, job_id: str) -> list[dict]:
    reports = []
    for mode, labels in VERIFY_LABELS.items():
        for label in labels:
            path = report_dir / f"verify-{job_id}-{label}.json"
            if path.exists():
                reports.append({"mode": mode, "report": json.loads(path.read_text())})
                break
    return reports


def post(url: str, token: str, body: dict) -> int:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status


def report_once(args: argparse.Namespace, token: str) -> None:
    state_dir = Path(args.state_dir)
    url = f"{args.platform_url.rstrip('/')}/api/v1/ingest/migrations/{args.job_id}"
    body = {
        "cutover": read_cutover(state_dir, args.job_id),
        "policy_decisions": read_decisions(state_dir, args.job_id),
    }
    post(url, token, body)
    for verify in read_verify(Path(args.report_dir or state_dir / "reports"), args.job_id):
        post(url, token, {"verify": verify})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="cutover 상태·정책 판정·verify 보고서를 플랫폼으로 보낸다"
    )
    parser.add_argument("--job-id", required=True)
    parser.add_argument(
        "--state-dir", default=os.environ.get("WORKER_STATE_DIR", "/var/lib/worker")
    )
    parser.add_argument("--report-dir")
    parser.add_argument("--platform-url", default=os.environ.get("ONEPORT_PLATFORM_URL"))
    parser.add_argument("--interval", type=int, default=0)
    args = parser.parse_args(argv)
    token = os.environ.get("ONEPORT_INGEST_TOKEN")
    if not args.platform_url or not token:
        parser.error("ONEPORT_PLATFORM_URL과 ONEPORT_INGEST_TOKEN이 필요합니다.")
    while True:
        report_once(args, token)
        if args.interval <= 0:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
